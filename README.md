# SSNPU Python tile 模型

Python 3.11+、仅标准库。读取 workload capture v2 / SSNPU v1，采用独立组件、消息队列和周期驱动。每 Core 建立 Scheduler、ROB、TileReg、TLSU、Sync，每 PE 建立 Cube、Vector。Core 间资源独立，PE 数默认 4。参数用于逻辑验证，尚未校准硬件周期精度。

从 ssnpu-model 目录执行，所有新产物写入上级 workspace：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python3 -m ssnpu_model \
  examples/matmul/capture --config config/logic-validation.json \
  --output ../workspace/ssnpu-components/matmul.json

PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python3 -m unittest discover -s tests -v

PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python3 tests/replay_captures.py \
  examples/matmul/capture examples/fa/capture examples/ooo/capture \
  --output-dir ../workspace/ssnpu-components/replay
```

公开 API 保持 `from ssnpu_model import simulate`；`simulate(capture_dir, config=None)` 返回 JSON 可序列化报告，config 可为字典或 JSON 文件路径，异常类型为 ModelError。设置顶层 `message_log: true` 开启带发送/接收周期的消息日志。需要观察中间状态时，可直接构建 `Simulator(TraceAdapter(path, config), config)` 并调用 snapshot；测试可通过 tick_order 指定组件调用顺序。

指令打点始终包含在报告的 `instruction_events` 中，也可导出为按周期排列的 JSONL，用于绘制流水图：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python3 -m ssnpu_model \
  examples/matmul/capture --output ../workspace/ssnpu-components/pipeline/matmul.json \
  --event-log ../workspace/ssnpu-components/pipeline/matmul.events.jsonl
```

每行包含 cycle、event、op_id、ir_op、core、pe、task、block、thread、control、component 和 execution_component。按 op_id 配对 issue / rob_complete，即得到指令从发射到 ROB 完成的区间；按 execution_component 分执行单元泳道。control=true 表示 barrier/task 控制节点，可按需过滤。API 直接读取 `report['instruction_events']` 或 `report['timeline']`。

打点包含 allocation_request、allocated、issue、received、operands_ready、compute_start/end、writeback_start/end、complete、rob_complete、retire。complete 是执行单元完成并发送通知；rob_complete 是下一周期 ROB 接收完成通知；retire 是按线程顺序退休，可能晚于 rob_complete。消息日志的 message_log 开关与指令打点独立。

Simulator 将指令派给 Cube、Vector、TLSU，各单元内部实现自己的工作量与延迟公式，没有独立 CostModel 或预生成的执行阶段。TileReg 直接管理容量、版本和访问端口。

每组件每周期运行一次 tick，本周期消息只在下一周期交付。PE 间轮转、PE 内最老就绪发射；窗口、消息队列和 ROB 无限。输出在派发前原子预留，tile 容量与发射额度继续生效。执行单元可同时持有多条在途命令，共享 Vector 类别竞争同一 PE 资源；共享读、共享写、memory 各自竞争 Core 服务资源。统一计时为 `service=max(1,ceil(work/throughput))`、`service_end=start+service`、`result=service_end+latency`，组件消息延迟单独计入。

| 文件 | 职责 |
|---|---|
| api.py / simulator.py | API、组件构建、时钟推进、停滞诊断和汇总 |
| clock.py / components/protocol.py | 下一周期交付、确定性消息顺序、dataclass 协议 |
| components/base.py / service.py | 队列接口、局部观察、服务时钟 |
| components/scheduler.py | 依赖唤醒、输出预留、轮转发射 |
| components/rob.py / sync.py | 无限 ROB、按线程退休、barrier 与 task |
| components/tile.py | local/shared 版本、容量、ready、访问端口与释放 |
| components/cube.py / vector.py / tlsu.py / execution.py | 操作数握手、计算或搬运、独立服务与写回 |
| trace.py / records.py / tiles.py | 输入校验、不可变使用的指令/版本元数据、大小计算 |
| config.py | 参数及配置校验；指令工作量和延迟在对应执行单元内计算 |
| report.py | 只读汇总组件状态、时间线和统计 |
| cli.py / __main__.py | CLI |

报告包含总周期、最后指令完成周期、Core/PE 完成时间、发射/访问/计算/写回/完成/退休时间线、组件状态、服务统计、存储峰值及分配释放事件、等待原因、配置与输入 SHA256。总周期包含控制节点、退休、释放和消息收尾。完成广播和 tile 释放不等待退休。停滞异常包含各组件的队列、依赖、请求、占用版本和等待条件。

路径表默认将 DDR/L1/L2 映射到 memory；路径项可设为 `{"engine":"memory","throughput":512,"latency":4}`。各计算类别支持 dtype_overrides。配置记录数值、单位、作用域、含义和来源。

[DESIGN.md](DESIGN.md) 说明组件协议、存储生命周期、可手算周期和 load→matmul→store 的代码阅读顺序。[examples/](examples/README.md) 的 capture、源码和历史报告保留原样，旧报告周期仅供比较。
