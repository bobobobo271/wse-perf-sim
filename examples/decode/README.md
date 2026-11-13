# WSE decode FlashAttention

这个示例以 Q/K/V 为输入，执行 GQA decode 的 attention 主体，输出 O。Q 的形状为 `[batch, query_heads, head_dim]`，K/V 为 `[batch, kv_heads, sequence_length, head_dim]`。每个请求只有一个查询位置，提供的所有 KV token 都对它可见；调用方应先完成位置编码，并把当前 token 的 K/V 纳入输入。

输入使用 FP16；矩阵乘法以 FP32 累加，softmax 的最大值、指数和及输出累加值使用 FP32。与原 FA 示例一致，PV 矩阵乘法前把概率块转换为 FP16。输出 O 为 FP32。本示例按用户确认的范围省略前后投影。

通信接入采用 [CommUnit 解耦接口](../../docs/COMMUNIT.md)：核侧提交后可继续独立工作，`comm.wait` 成功才允许合并接收状态。[本次复跑结果](../../docs/COMMUNIT_VALIDATION.md)记录固定输入、控制变量和接口变化后的时间。

## 运行

从仓库根目录运行，使用 Python 3.11 或更新版本。时序模拟器仍只依赖标准库；数值验证和绘图另需 NumPy、Matplotlib。

```bash
python3.11 -m venv workspace/decode-venv
workspace/decode-venv/bin/python -m pip install -r examples/decode/requirements.txt

PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. \
MPLCONFIGDIR=workspace/matplotlib OPENBLAS_NUM_THREADS=1 \
workspace/decode-venv/bin/python examples/decode/run.py \
  --batch 1 --query-heads 8 --kv-heads 2 --head-dim 32 \
  --sequence-length 512 --sequence-splits 4 \
  --tile-tokens 64 --hbm-chunk-tokens 128 \
  --hbm-gbps 256 --hbm-startup-ns 0 --seed 7 \
  --output-dir workspace/decode-demo
```

`--hbm-gbps` 的单位为十进制 **GB/s，全片共享**。核数增加时，总 HBM 带宽保持不变。`--pe-count` 指每个核内的处理单元数，默认四个；`--sequence-splits` 指每个 KV head 任务的序列分片核数，支持 1–40。Q head 数必须是 KV head 数的整数倍。

输入可通过 `--inputs path/to/inputs.npz` 提供，文件须包含上述形状和 FP16 类型的 Q、K、V。未提供时，按 `--seed` 生成均值 0、标准差 0.5 的正态随机输入，并保存在输出目录。所有生成产物写入 `workspace/`。

前面讨论的 40 核映射可用以下参数运行：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. \
MPLCONFIGDIR=workspace/matplotlib OPENBLAS_NUM_THREADS=1 \
workspace/decode-venv/bin/python examples/decode/run.py \
  --query-heads 32 --kv-heads 8 --head-dim 128 \
  --sequence-length 1280 --sequence-splits 5 \
  --tile-tokens 128 --hbm-chunk-tokens 256 --hbm-gbps 256 \
  --output-dir workspace/decode-wse40
```

时序模型逐周期推进，大尺寸 capture 的运行时间和详细日志体积都会增长。`--no-plots` 可关闭静态绘图，保留数值验证、报告和 Perfetto 文件。

## 分工与数据流

每个 `(batch, KV head)` 构成一个任务，关联的 Q heads 共享该任务的 K/V。历史序列按整数边界 `N*rank//splits` 切分，尾块按实际大小计算；分片可以为空。

核按列优先分配：`0,8,16,24,32,1,9,…`。五个分片时，一列正好处理一个 KV head。任务总数超过核容量时分批执行；下一批等待前一批计算、通信和输出完成。`layout.json` 保存实际的核、序列范围和批次。

核内把关联的 Q heads 分给各 PE。K/V 计算块放在 Shared Tile，由本核的计算单元读取。每个 PE 保持自己的在线 softmax 状态：

```text
m = -inf, l = 0, U = 0
对本核每个 K/V 块：
    S = Q @ K.T / sqrt(head_dim)
    mn = max(m, row_max(S))
    alpha = exp(m - mn)
    P = exp(S - mn)
    l = alpha * l + row_sum(P)
    U = alpha * U + FP16(P) @ V
    m = mn
```

局部扫描结束后，核把每个 Q head 的 `(m,l,U)` 打包成 FP32 数据。组内 AllGather 收齐各分片后，以全局最大值重新缩放 `l` 和 `U`，最终输出 `U/l`。当前每个参与核均执行合并，只有 rank 0 把 O 写回 HBM。单分片时省去集合通信。自动 Calendar 计划采用现有的保守串行排程，通信路由、字节落点和接收覆盖检查均来自现有模型。

首版明确计入每个序列分片核各自从 HBM 读取 Q 的开销。Q 尚未采用核间广播。K/V 在各核间按序列分片，每次调用从 HBM 冷读一次，核内多个 Q heads 复用计算块。

## 存储与时序口径

HBM 采用一个全片 FIFO 带宽服务，不模拟 bank、行命中或缓存。每次服务占用为 `ceil(core_hz * (startup_ns/1e9 + bytes/BW))`，至少一个核周期。启动延迟也占用该串行服务，默认零。请求、服务和完成消息按现有组件协议推进，因此总时延还包含消息传递和等待。

`memory.copy` 显式表示 HBM 与 local SRAM 之间的搬运：

- 读入：HBM 服务完成 → 本核 SRAM 写入 → 目的数据可见。
- 写出：本核 SRAM 读取 → HBM 服务完成 → 写回完成。

本核 SRAM 的读写共用现有服务资源，包含原配置的每次访问 100 ns 启动开销和 1 TB/s 带宽。HBM 参数表示传输服务带宽，目标 SRAM 写入另计。以上参数是当前建模条件，不是实测结果。

每核为 K、V 各分配两个 `hbm_chunk_tokens × head_dim` 的 SRAM 缓冲区。缓冲区复用依赖上一使用者完成，当前缓冲区计算期间可以预取另一缓冲区。每个搬运块再按 `tile_tokens` 读入计算 Tile。另计 Q 和输出缓冲，`layout.json` 记录每核 SRAM 预留字节数；TileReg 负责核内 Tile 的实际峰值与生命周期。

WSE 的计算预算按整核设置：FP16 Cube 为 4096 MAC/cycle/核（MAC 为一次乘加），Vector/转换以 256 B/cycle/核为预算。当前固定等分给 `pe_count` 个 PE，空闲份额不借给其他 PE；exp/reduce 另受合成操作速率约束。真实小矩阵利用率、Tile 端口、Shared 容量等参数仍需校准。原来的 `default_config()` 合成逻辑验证配置保持每 PE 口径。

## 数值验证与测试条件

数值执行器读取时序模型实际使用的 capture 依赖图，逐条执行其中的 load、矩阵、向量和 store 操作。Calendar 接收按实际计划的来源、字节偏移和覆盖范围组装数据；执行器还显式等待同一集合的全部发送源就绪。它不另写一套独立的多核 attention 捷径。

每次运行进行三个比较：

1. 多核输出与单核输出：全部 batch/head 任务在 Core 0 上串行运行，PE 数、输入、计算块、搬运块及 HBM 总带宽保持不变，仅改变跨核划分。
2. 多核输出与独立 FP64 稠密 attention：后者直接计算完整分数矩阵和 softmax，不使用分块递推或集合计划。
3. 单核输出与相同的 FP64 参考。

通过条件为每个元素满足 `abs(actual-reference) <= 5e-4 + 5e-3*abs(reference)`。该容差考虑 PV 前的 FP16 概率转换；`validation.json` 同时记录最大绝对误差、均方根误差和容差占用比例，失败时命令退出码非零。

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. OPENBLAS_NUM_THREADS=1 \
workspace/decode-venv/bin/python -m unittest discover -s tests -v
```

新增测试使用固定输入与精度，覆盖两个随机种子、2/3/5 个分片、非整除序列、单 token 与空分片、大分数稳定性，以及超过 40 核后的分批复用。负向对照会破坏一份实际发送状态，要求数值比较失败。时序测试分别检查 HBM 总字节数、共享带宽仲裁、只改变 HBM 带宽的对照、SRAM 双缓冲复用、资源占用互斥、组件调用顺序不变性和流水图时间来源。未安装 NumPy 时仅跳过数值测试，完整验证应在上述虚拟环境中运行。

## 输出文件

| 文件 | 内容 |
|---|---|
| `communication/requests.json` | 核侧请求，含路由和绑定，不含时隙 |
| `communication/installation.json` / `platform.json` | 可独立装入 CommUnit 的时隙配置与端点/网络参数 |
| `summary.json` / `validation.json` | 参数、运行环境、输入哈希、时延、验证条件和误差 |
| `report.json` / `single_core_report.json` | 多核与单核完整时序、资源服务、存储峰值和配置 |
| `inputs.npz` / `outputs.npz` | 可复用输入，以及多核、单核、FP64 参考输出 |
| `capture/` / `single_core_capture/` | 两个实际执行计划，包括通信、映射和规格 |
| `pipeline_overview.png` | 核级计算、HBM/SRAM 与 NoC 服务区间概览；PE 区间合并显示 |
| `pipeline.png` / `pipeline.svg` | 保留各 PE 和各 NoC 链路的详细流水图 |
| `pipeline.pftrace` | 可在 Perfetto 中缩放的指令生命周期、请求接纳与完成令牌等待轨道 |
| `resources.trace.json` | 可在 Perfetto 中查看的实际计算、HBM/SRAM、Shared 和 NoC 链路服务轨道 |
| `instruction_events.jsonl` / `hbm_events.jsonl` / `noc_events.jsonl` | 原始事件，可追溯到具体指令、请求和链路 |

静态图的横轴由模型核频率换算，条带取自资源真实服务区间；NoC 使用自身时钟产生的事件时间。等待、延迟和消息开销会表现为空隙，完整生命周期可在 `pipeline.pftrace` 中查看。
