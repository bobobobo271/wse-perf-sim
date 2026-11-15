# WSE40 Calendar 与访存集成

本项目采用 Python 3.11+ 标准库。拓扑为 5×8 个节点，节点号 `row*8+column`。核／NoC 时钟配置为 1.65／2 GHz，每条有向链路 64 B/NoC 周期；每个 flit 有 52 B 有效载荷和 12 B 包头。Shared/Local Tile 容量和端口参数仍是待校准配置。

当前接口与执行规则见 [COMMUNIT.md](COMMUNIT.md)，测试条件见 [INTERFACE_VALIDATION.md](INTERFACE_VALIDATION.md)。

```python
from api import simulate, wse_config
from frontend.communication import compile_plan

config = wse_config()
compiled = compile_plan(source_plan, config)
report = simulate(capture_dir, config, communication_plan=compiled)
```

编译结果包含核侧路由与布局，以及 WSE 侧 CalReg 相对时隙。运行时只在全部参与者资源就绪后确定共同时间原点，并按已安装配置执行。`CalReg[opcode]` 在上一轮排空后可复用。`slot_cycles/slots_per_period` 是旧配置文件保留字段，当前编译器按片段服务和路径确定 stride，不使用它们排时隙。

通信采用有限 TX/RX 缓冲和分段读写。仅在本地接收范围完整可见后由 TileReg 发布，本地发送责任结束后返回成功。源引用保守保留到本地请求成功。各通信轮当前串行准入。

每核 SRAM 容量配置为 384 MiB，读写共享 1 TB/s，读／写启动延迟各 100 ns。SRAM 服务时间按字节带宽计算，启动开销是可流水结果延迟。本地访问、远端访问、HBM 搬运的 SRAM 阶段竞争同一目标端口。Tile 通信自身通过 TileReg 存储服务，不额外计入 SRAM。

远端 SRAM 请求使用显式 `target_core` 和 `offset_bytes`。初版远端网络为独立聚合 cost model，计入请求、数据／确认、包头和带宽竞争；参数在 `memory_transport` 中记录，尚未与硬件校准。

## 自动规划

```python
from frontend.communication import allgather_group, compile_plan
plan = allgather_group(sources, targets, owners, shard_bytes=256)
compiled = compile_plan(plan, config)
```

sources、targets、owners 按节点编号索引。AllGather 在负载构造期转成 gather 与 broadcast 复制任务，多树广播分轮完成。当前算法会集中经过根节点，与旧版多源并发算法的流量和性能不同。全 40 核的 `allgather_40` 按行、列构造这些任务。

显式源计划使用 `wse-communication/stream-v1`，每节点最多一份 send、一份 receive。旧 `wse-communication/1` 计划需重新生成，已归档的示例输入和历史结果不覆盖。

输出包括指令时间线、实际 SRAM 服务、网络请求与返回、Calendar 分段事件和缓冲峰值。时间为模型估算；默认包头、跳延迟、端点服务和缓冲容量均未做真实 WSE 标定。
