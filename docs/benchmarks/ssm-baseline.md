# 首轮 SSM / Python 固定负载基线

目录迁移说明：下文保留原实验的目录和命令记录。当前仓库脚本位于 `benchmarks/ssm_baseline.py`、`benchmarks/calibrate_ssm.py`，仿真命令入口为 `python -m cli`；在仓库根目录设置 `PYTHONPATH=.` 后运行。见 [项目结构](../STRUCTURE.md)。

运行日期：2026-11-13。本轮固定参数、不拟合、不改 SSM 实现。全部是一个 Core、一个 PE、一个执行线程。用例包含准备 load、目标操作、输出 store。

后续取消时间限制后，7 个 SSM 用例均已完整跑完，两次结果一致。最新 SSM 结果见 [完整运行简报](ssm-native-results.md)；下文保留首轮 120 秒限制下的历史记录。

## 本轮结果

| 用例 | SSM 状态 | Python 状态 | SSM 原生周期（两次） | Python 原生周期（两次） | 配对说明 |
|---|---|---|---:|---:|---|
| matmul M16/N32/K32 | 成功 | 成功 | 137845 / 137845 | 72 / 72 | 4 条动态操作，负载核对通过；使用补跑 |
| matmul M32/N64/K64 | 两次超时 | 成功 | 无完整运行统计 | 160 / 160 | gfrun 两次通过；gfsim 均达到 120 秒 |
| add 16×16 | 成功 | 成功 | 14869 / 14869 | 59 / 59 | 4 条动态操作，负载核对通过 |
| exp 16×16 | 成功 | 成功 | 14876 / 14876 | 58 / 58 | 3 条动态操作，负载核对通过 |
| row_sum 16×16 | 成功 | 接口失败 | 14840 / 14840 | 不生成周期 | scratch 的 tile.create 未被性能模型支持 |
| load 16×16 | 成功 | 成功 | 13996 / 13996 | 41 / 41 | 2 条动态操作，负载核对通过 |
| store 16×16 | 成功 | 成功 | 13996 / 13996 | 41 / 41 | 与 load 用例执行相同任务 |

已建立 **5 个用例的任务等价原生周期基线**，包括至少一组 matmul 和 Vector；两侧成功运行的周期均重复稳定。尚未建立同一派发/完成边界下的周期比例。这些数值不是模型精度结论。

首轮完整运行保存在 `workspace/ssm-baseline/20261113-run1/`。小 matmul 初始两次均打印 137845 周期，但默认逐周期 counters 使时间线输出达到 120 秒超时，均按失败保留。随后仅将 `dfx.swimCounterSampleInterval` 从 1 固定改为 8，补跑保存在 `20261113-sampled/`，两次在约 93/97 秒内正常退出，周期仍为 137845。合并汇总在 `20261113-final/`，使用补跑的小 matmul 与原轮其余 6 例。采样差异是观测配置差异；其余模型参数不变。

row_sum 的解释器 capture 保留完整 load、scratch 声明、row_sum、store；两次 Python 性能回放均报 `Unknown operation tile.create at 1`。没有增加额外 tdup/full 来掩盖接口缺口，也没有修改性能模型。大 matmul 的计时超时日志未输出完整周期/泳道，不能据此判断 Cube 计算性能。

Python 的 tile 区间、退休和收尾另列如下（两次一致）：

| 用例 | first issue | last complete | issue→complete | last retire | total−last complete |
|---|---:|---:|---:|---:|---:|
| matmul M16/N32/K32 | 3 | 68 | 65 | 69 | 4 |
| matmul M32/N64/K64 | 3 | 156 | 153 | 157 | 4 |
| add | 3 | 55 | 52 | 56 | 4 |
| exp | 3 | 54 | 51 | 55 | 4 |
| load / store | 3 | 37 | 34 | 38 | 4 |

SSM 候选 TileOp 驻留区间分别为：小 matmul 201、add 113、exp 118、row_sum 93、load/store 81 周期，两次均一致。小 matmul 的 201 周期与 Python 的 65 周期起点语义不同，所有比例留空；大 matmul 没有可提取的完成区间。

## 周期口径

SSM 原生周期是 `gfsim` 报告的 `Total Cycles`：程序启动至模拟器终止，包含 ELF 的标量初始化、地址计算、分支、参考结果计算/检查、前端、缓存和 tile 操作。两组 matmul 的完整 ELF 还执行标量 `reference_matmul`。超时前已经打印的周期只作为诊断数据，不算成功运行。

`BENCHSTART/BENCHEND` 编译为 `B.HINT TRACE.begin/end`。检查当前 SSM 的 `isa/Block.cpp::HandleBHINT`、`BlockHint.h`、`BROB.cpp` 和 `SimSys::needTerminate`：TRACE 提示形成标量块，没有把原生总周期截成 benchmark 区间，也没有提供等价于“所有异步 store 已完成”的计时屏障。`warmupHint` 路径是另一项机制，本轮未开启。不能直接相减两条 TRACE 的前端时间作为完成区间。

Python 原生周期是 `total_cycles`，从模型周期 0 推进到退休、释放和消息收尾完成；另记 `min(issue)`、`max(complete)`、`max(retire)`、`total_cycles-max(complete)`。只对实际 tile 指令取区间，控制节点另保留。这里 complete 是执行单元发送完成消息，下一周期才是 ROB 接收完成。

SSM 提取完整 TileOp duration，排除 Vector 的 `uops=` 子阶段，保留 `ts`、`dur` 和运行时 descriptor。TLSU TLOAD 从 LIQ 分配到事务完成，TSTORE 从 SCB dispatch 到 L2 写响应；Vector 从接收到最后 CELL resolve。依据为当前 `modelSpec/tlsu/swimlane_v2_events.md`、`modelSpec/vector/vec_swimlane_modeling_spec.md` 及实现。首条 load 的 LIQ 分配晚于派发，与 Python issue 含义不同。因此 SSM 的候选驻留区间仅供观察，**所有可比区间比例均为 null（暂不可直接比较）**，不以完整 ELF 周期/Python 总周期判断模型精度。

## 负载核对

核对 SuperNPUBench 的 case 源码、`*_bench.hpp`、预编译 ELF 相邻反汇编，以及 `gfrun -t 1` 的实际动态块、维度指令、dtype、寄存器引用和地址/stride。实际执行序列优先于源码。Python 使用手写扩展 Tile IR，经当前 `TileInterpreter` 导出 capture，没有手工构造 trace。

| 用例 | 实际 tile 序列 | 有效工作量 | GM 读/写 byte |
|---|---|---:|---:|
| FP16 matmul M16/N32/K32 | load A、load B、matmul、store FP32 C | 16384 MAC | 3072 / 2048 |
| FP16 matmul M32/N64/K64 | 同上 | 131072 MAC | 12288 / 8192 |
| FP32 add 16×16 | load A、load B、add、store | 256 元素 | 2048 / 1024 |
| FP32 exp 16×16 | load、exp、store | 256 元素 | 1024 / 1024 |
| FP32 row_sum 16×16 | load、row_sum、store 16×1 | 240 次加法 | 1024 / 64 |
| FP32 load 16×16 | load、store | 256 元素搬运 | 1024 / 1024 |
| FP32 store 16×16 | load、store | 256 元素搬运 | 1024 / 1024 |

所有访问为完整连续矩形，Python offset 均为 `[0,0]`。matmul 的 A/B FP16 行 stride 分别为 64/64 或 128/128 byte，FP32 C 为 128 或 256 byte；Vector 和 Memory 完整 tile 的 stride 为 64 byte，row_sum 输出为 4 byte。动态 GM 基址见保存的 `B.IOR` 操作数和 TLSU descriptor。字节数是有效 GM payload，不包含缓存行放大、标量访问、内部 TileReg 访问或 scratch。

matmul 两条 load 无数据依赖，matmul 依赖两者，store 依赖 matmul；add 同构，exp/row_sum 为单输入链，两个 Memory 用例为 load→store。SSM 的 `T#1/M#1/U#1` binder 与 Python capture 的 producer/dependencies 对应；Python 的 order_dependencies 只表达程序年龄，不额外强制执行串行。

matmul ELF 用 ND2M16/ND2M32 和 ND2N8 load、M162ND/M322ND store；Python 用逻辑矩形和 LocalTileReg，不模拟 CELL 布局转换。row_sum 反汇编的 `Row=2, Col=16, ValidRow=16` 及 128B 分配不能解释为只处理两行：运行时 row-reduction descriptor 发布 `validRow=16, validCol=1`，store 为 16 行、stride 4。SSM rename 为归约中间值把目的 backing 扩到 1024B，保存的运行时 descriptor 也显示 size=1024；这两种内部容量都不同于有效 64B GM 输出。

两个 Memory 用例都调用 `bench_load`，实际负载相同。Vector/Memory 的预编译 ELF 没有独立数值验证路径，`gfrun R2=0` 表示功能执行结束；本轮不把它宣称为独立数值正确性验证。Python 使用 trace-only TensorSpec，同样不验证数值。

## 配置与版本

Python 从当前 `config/logic-validation.json` 复制，仅设 `pe_count=1`、`rob_depth=null`。其余固定：Core/PE issue width 为 4/2，local/shared 容量各 1 MiB，Cube 4096 MAC/cycle + 4 cycle，Vector 256 元素/cycle + 2，exp 64 + 4，reduce 128 reduction/cycle + 3，memory 256 byte/cycle + 8。每条组件消息下一周期交付。没有为 SSM 调参或校准。

SSM 显式覆盖 `core.threadCount=1, stdPeCount=1, streamCount=1, cube_core_num=1, cube_thread_num=1, vec_core_num=1`，并将参考/功能核心 `softcore.multiThreadNum=1`，避免其默认四线程重复工作。运行时打印的 block ROB=256、decode width=4、Cube/Vector ISQ=32、Vector ROB=64；Python ROB 无限。SSM 的缓存、标量供给、有限窗口、CELL 分配/转换和流水线资源不能用 Python 合成吞吐参数替代。

保存完整配置源码快照、显式覆盖、运行时实际打印的配置表及泳道 process 拓扑。SSM 没有打印全部组件的完整生效配置，因此未打印字段的证据是该构建的配置源码与二进制哈希，不能把源码默认值说成逐字段运行时回读。没有参数拟合。

| 仓库 | HEAD（工作区改动另存） |
|---|---|
| ssnpu-model | `c5e37d6bad5f622a4ce4046a2b38e86a96240e84` |
| pypto | `496f9dc39da6316993a649dbc9e810e7f303f914` |
| SuperScalarModel | `637bde0ec63955c4796828b297e16a057efad3cc` |
| SuperNPUBench | `5afe215169495bb8e8f03644be5000ffb5b51fe3` |

本轮使用当时的工作区，包括已有的未提交修改；HEAD 本身不足以复现。原始 status、tracked diff、staged diff 均已保留，现有改动没有被覆盖。

`gfsim` SHA256：`4e6ca7b47f0d0300c3ce92f23667994b3d76cf646525c3d675821343377f03db`；`gfrun`：`cd399eab4becff77045538432211106b3cb5caef05beaade3e2904736008df0e`。逐 ELF 哈希见 JSON 汇总。

## 复现和产物

从 hw-native-sys 根目录运行，使用含 torch 和当前 pypto 扩展的 Python 3.11：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=pypto/python:ssnpu-model \
  .venv-ir/bin/python ssnpu-model/ssm_baseline.py \
  --ssm-root /home/lc/superscalarNPU/SuperScalarModel \
  --bench-root /home/lc/superscalarNPU/SuperNPUBench \
  --output workspace/ssm-baseline/NEW-RUN-ID
```

输出目录必须不存在。默认运行固定 7 例，每侧两次，所有 gfrun/gfsim 子进程均有 120 秒超时；失败继续下一用例。`--case NAME` 可重复指定补跑用例。计数器采样默认 8，可设 `--counter-sample-interval 1` 重现初始观测配置；它只影响泳道计数器输出，仍逐操作记录完整 tile duration。

`--timeout 0` 取消宿主时间限制，`--repeat-workers 2` 可将两次独立 SSM 重复并行运行；它们不改变模拟器参数。

加 `--audit-only` 可重做现有日志的核对和汇总，不重跑模型。用多个 `--merge-run PATH` 合并已有 summary，后列目录覆盖同名 case，`source_run` 保留原始目录。`acceptance.json` 分别报告 7 例状态是否齐全、是否至少有一例 matmul 和一例 Vector 配对、是否存在可直接比较的区间。

每例保存源码/头文件/ELF/反汇编、IR、capture、Python report/事件 JSONL/Perfetto 时间线、两次 gfrun 原始日志和动态 descriptor、运行结束状态、两次 gfsim 日志/泳道/提取的 tile events/拓扑/已打印配置。完整 argv、cwd、退出码、超时与 wall time 单独存 JSON。根目录保存脚本快照、仓库状态、二进制哈希、配置以及 JSON/CSV 汇总。运行文件都在 `workspace/ssm-baseline/`；本文保留核心结论，临时产物清理后仍可理解本轮结果。
