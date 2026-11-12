# SSNPU Python tile 模型

Python 3.11+、仅标准库。读取 workload capture v2 / SSNPU spec v1，提供四 PE、单 Core 调度；多个 block 根据 capture 的 core_id 复用模型，Core 间资源独立。

配置为**逻辑验证用、尚未硬件校准**。当前结果用于检查调度逻辑及比较参数影响，不代表硬件周期预测精度。

从本仓库根目录执行：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python3 -m ssnpu_model \
  examples/matmul/capture --config config/logic-validation.json \
  --output workspace/matmul/report.json

PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python3 -m unittest discover -s tests -v

PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python3 tests/replay_captures.py \
  examples/matmul/capture examples/fa/capture --output-dir workspace/replay
```

[examples/](examples/README.md) 分 workload 保存源码、capture、IR 和已验证的原始结果。每个负载目录提供单独复现命令。

Python API：`from ssnpu_model import simulate`；`simulate(capture_dir, config=None)` 返回可序列化字典。config 接受字典或 JSON 文件路径；省略时使用 `default_config()`。异常统一为 `ModelError`。归档示例位于 examples/；新运行产物和测试临时 capture 写入 workspace/，该目录不纳入版本控制。

源码按功能拆分到 `ssnpu_model/`，模块职责如下：

| 模块 | 职责 |
|---|---|
| `api.py` | `simulate()` 入口，组合配置、输入适配和调度 |
| `config.py` | 默认参数及配置校验 |
| `records.py`、`tiles.py` | 内部记录、tile 字节数与版本身份 |
| `trace.py` | capture 加载、依赖校验、barrier 与版本生命周期 |
| `cost.py` | 将 tile 操作转换为执行阶段和 cost |
| `events.py` | 完成事件最小堆 |
| `resources.py` | 资源状态、容量准入与分配释放 |
| `scheduler.py` | 每周期调度、发射仲裁、内部阶段推进 |
| `report.py` | 时间线、资源及等待统计报告 |
| `cli.py`、`__main__.py` | 命令行参数与报告文件输出 |
| `errors.py` | 统一异常类型 |
| `model.py` | 兼容原有导入路径，转发旧模块的公开符号 |

依赖方向为 API → trace / scheduler，scheduler → cost / events / resources / report；内部记录和配置作为底层模块，不反向依赖调度器。现有 `from ssnpu_model import simulate`、`from ssnpu_model.model import TraceAdapter` 和 CLI 调用保持可用。

配置文件集中记录数值、单位、作用域、含义、来源。计算类别可通过 `dtype_overrides` 覆盖 throughput / latency，exp、cast、reduce 与普通逐元素运算竞争同一 PE Vector。Cube 为每 PE 独立资源。共享读、共享写和内存搬运分别为每 Core 单独资源。

路径表默认将 DDR/L1/L2 都映射到统一 memory 通道；可以将路径项设为 `{"engine":"memory","throughput":512,"latency":4}`，改变该访问路径的 cost。路径不模拟 cache 命中、替换或一致性。配置中的吞吐单位分别为 MAC/cycle、element/cycle、reduction/cycle、byte/cycle，固定 latency 为 service 结束到 ready 的周期数。

输出包含总周期、Core/PE 完成时间、逐指令/阶段时间线、服务占用率、搬运字节、存储峰值/分配释放事件、等待原因集合、完整配置和输入文件 SHA256。`pe_completion` 包含该 PE 参与的尾部 barrier，`pe_tile_completion` 仅计算 tile 指令。task/barrier 作为可见的控制节点列入时间线，但不计入 instruction_count。等待原因集合按 instruction-cycle 计数，不可把重叠类别直接相加；内部阶段资源等待单列。

详细语义、支持范围和后续校准边界见 [DESIGN.md](DESIGN.md)。
