# 目录重构验证

本文记录历史目录重构验证。下方命令对应当时的代码版本。当前的 PTO capture 回放命令见 [Decode 示例](../examples/decode/README.md)。

## 后续调整：去掉包目录和旧入口

在 `0666364` 的基础上，将 `models/`、`runtime/`、`frontend/`、`workloads/`、`analysis/` 和入口文件移到仓库根目录，删除 `ssnpu_model/` 兼容目录及对应测试。当前入口为 `from api import simulate`、`python -m cli`，安装后的 `wse-perf-sim` 命令保持不变。

使用同一 Python 3.11.16、NumPy 2.4.6、Matplotlib 3.11.2 环境，完整测试 112 项通过；加 `-S` 后 108 项通过、4 项 NumPy 数值测试跳过。测试数量减少的一项是旧公共入口兼容测试。

使用下文相同的归档 capture、默认逻辑验证配置，以及每核 256 B、40 核 SharedTileReg 源的 AllGather，完整报告与重构前基线相等，周期分别为 matmul 7042、FlashAttention 2183、乱序示例 23、AllGather 8275。安装包中也没有上述两个包目录。从源码根目录外执行命令入口、模块入口、两种 Perfetto 入口和独立导入，共 5 项检查通过；matmul 完整报告与基线一致，两种时间线入口的输出逐字节一致。

原始记录位于 `workspace/flatten-tests.txt`、`workspace/flatten-stdlib-tests.txt` 和 `workspace/flatten/validation.json`。以下保留初版重构的测试条件和结果，其中旧入口兼容测试仅对应初版。

## 初版重构记录

2026-11-13，以提交 `3e4915bda4d22a84a8258c6efcb738dd9cc6188d` 为重构前基线，在同一台机器、同一 Python 环境下比较迁移前后的行为。新的目录说明见 [项目结构](STRUCTURE.md)。

## 条件与控制变量

- Python 3.11.16，NumPy 2.4.6，Matplotlib 3.11.2。基础功能另用同一 Python 加 `-S` 运行，关闭第三方包加载。
- 归档 capture、配置 JSON、硬件参数、时序公式和通信计划生成规则保持一致。比较完整 JSON 报告，不只比较总周期。
- Decode 在重构前生成一次输入，重构后通过 `--inputs` 读取同一份 `inputs.npz`。固定 seed=7，逐数组比较输入与输出。
- 生成物都放在仓内 `workspace/structure/`。外部 SuperScalarModel benchmark 工具本轮验证脚本入口，数值回归使用本仓 capture 和模型。

系统默认 `python3` 指向 Python 3.7.8，首次测试启动在类型注解导入处失败。改用项目支持的 Python 3.11.16 后完成以下全部验证；该失败没有进入前后对照结果。

## 测试

| 测试 | 条件 | 结果 |
|---|---|---|
| 重构前原有测试 | Python 3.11.16，有 NumPy | 109 项通过 |
| 重构后完整测试 | 相同环境，增加 4 项架构边界检查 | 113 项通过 |
| 重构后标准库测试 | 相同 Python，加 `-S` | 109 项通过，4 项 NumPy 数值测试跳过 |
| 安装验证 | 无网络构建 wheel，安装至独立目录，从源码根目录外运行 | 5 个入口/隔离检查通过 |

新增的四项检查分别验证模型与运行基础设施的导入边界、独立 NoC 导入不会加载核与 capture、公共 API 不加载 NumPy/Matplotlib，以及旧公共入口引用同一实现。前两项防止目录拆分后重新出现反向依赖。

安装验证使用 setuptools 84.0.0、wheel 0.46.3，执行新命令入口、旧 `python -m ssnpu_model`、两种 Perfetto 入口和独立 NoC 导入。安装后的 matmul 完整报告与重构前报告一致；命令入口和模块入口生成的 Perfetto 文件逐字节一致。

## 负载回放

以下每项的重构前后完整报告均相等。

| 负载 | 条件 | 重构前/后周期 |
|---|---|---:|
| 归档 matmul | `examples/matmul/capture`，默认逻辑验证配置，168 条指令 | 7042 / 7042 |
| 归档 FlashAttention | `examples/fa/capture`，默认逻辑验证配置，320 条指令 | 2183 / 2183 |
| 归档乱序执行示例 | `examples/ooo/capture`，3 条指令 | 23 / 23 |
| 40 核 AllGather | 每核 256 B，SharedTileReg 源，行列两阶段，WSE 默认配置 | 8275 / 8275 |
| 40 核 AllGather，含 SRAM 读 | 同上，先从 SRAM 加载源数据 | 8411 / 8411 |
| 独立 NoC 字节驱动 | 节点 0、1、9，每节点 64 B，2 轮，错开请求到达 | 162 / 162 |
| 40 核 Decode | 下述固定规格，8376 条指令 | 33920 / 33920 |
| 同输入单核 Decode | 所有组在 core 0 串行，5672 条指令 | 95089 / 95089 |

独立 NoC 驱动还逐字节校验 AllGather 输出，并确认网络及消息已收尾。AllGather 使用当前基线的核准入与通信协议，因此应与本表基线比较。

Decode 条件为 batch=1、查询头 32、KV 头 8、head_dim=128、序列长 1280、每组 5 个序列分片，共 40 个核，每核 4 个 PE（处理单元）；计算 Tile 为 128 tokens，HBM 搬运块为 256 tokens，HBM 带宽 256 GB/s、启动开销 0。输入 FP16、矩阵累加 FP32、状态 FP32，PV 前概率转 FP16；dense 参考使用 FP64。数值容差为 atol=5e-4、rtol=5e-3。

多核耗时保持 24.228571 µs，单核保持 67.920714 µs。多核相对单核最大绝对误差为 1.2315344e-5，相对 dense 参考为 1.2733850e-5，三组数值比较均通过；重构前后输出数组完全相同。

比较共覆盖 22 项输出，包括完整报告和配置、输入/输出数组、原生 Perfetto、资源时间线、指令/HBM/NoC 事件、绘图区间、通信请求、安装配置和平台参数。事件与通信文件逐字节一致。比较摘要及哈希保存在 [structure-comparison.json](validation/structure-comparison.json)。

## 复现

在仓库根目录执行，`python3` 须为 3.11 或更新版本。完整测试与 Decode 示例需要先安装 `examples/decode/requirements.txt` 中的依赖。

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python3 -m unittest discover -s tests -v
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python3 -S -m unittest discover -s tests -v

PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python3 tests/replay_captures.py \
  examples/matmul/capture examples/fa/capture examples/ooo/capture \
  --output-dir workspace/structure/replay

PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python3 examples/wse_allgather.py \
  --output-dir workspace/structure/allgather
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python3 examples/wse_allgather.py \
  --sram --output-dir workspace/structure/allgather-sram
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python3 examples/communication/driver.py \
  --output workspace/structure/standalone.json

PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python3 examples/decode/run.py \
  --query-heads 32 --kv-heads 8 --head-dim 128 --sequence-length 1280 \
  --sequence-splits 5 --tile-tokens 128 --hbm-chunk-tokens 256 \
  --output-dir workspace/structure/decode
```

本轮原始证据位于 `workspace/structure-baseline-tests.txt`、`workspace/structure-final-tests.txt`、`workspace/structure-stdlib-tests.txt`、`workspace/structure/installation-checks.json`，前后负载结果分别位于 `workspace/structure/before/` 和 `after/`。
