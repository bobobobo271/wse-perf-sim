# 核与 NoC 接口

本版本按 [issue #4 的最新接口](https://github.com/hmljy2020/wse-perf-sim/issues/4#issuecomment-6848590938) 和 [issue #9](https://github.com/hmljy2020/wse-perf-sim/issues/9) 区分两种请求：Tile 通信任务、按地址访问 SRAM。核与 NoC（片上网络）通过公共消息交互。路由和 Calendar 时隙在执行前确定，通信提交路径只验证并执行已安装的配置。

## 编译、装载、执行

| 阶段 | 输入与输出 | 代码 |
|---|---|---|
| 编译／负载构造 | 确定参与者、路由位图 routeBits、二维布局、容量与预期字节数；生成相对读、发送、写时刻 | `frontend.communication.compile_plan`、`models/noc/compiler.py` |
| 装载 | 校验平台与安装镜像版本，安装 `CalReg[opcode]` | `CommUnit(installation, platform)` |
| 核运行 | 提交编译字段、本轮 epoch、源与目标的实际 Tile 版本 | `CommSubmit(node, endpoint, CommRequest(...))` |
| 通信执行 | 准备资源、等待全组 Prepared、确定共同时间原点、按表读写和转发、发布与完成 | 唯一的 `models/noc/unit.py::CommUnit` |

`opcode` 选择配置，`epoch` 区分通信轮次。CalReg 中没有 epoch。不同轮可以选择同一配置并绑定不同 Tile 版本；布局、路由或数据量改变时，当前严格匹配方式要求选择另一份编译配置。相同 opcode 下安装两份不同配置会在编译阶段报错。

这些 opcode 是模型配置编号，未定义硬件指令位宽。当前每个仿真实例同时执行一轮；下一轮请求可重试。同一 `(opcode, epoch)` 在一次 launch（程序启动实例）中只用一次，已完成身份不回绕复用。新 launch 使用新实例。

### 可保存、单独装载的编译结果

```python
import json
from pathlib import Path
from config import wse_config
from frontend.communication import compile_plan
from api import simulate

config = wse_config()
compiled = compile_plan(source_plan, config)  # 在核开始运行前调用
Path('workspace/compiled.json').write_text(json.dumps(compiled, indent=2))
report = simulate(capture_dir, config, communication_plan='workspace/compiled.json')
```

源计划格式为 `wse-communication/stream-v1`，编译结果为 `wse-communication/compiled-v1`，其中包含请求的静态字段和 `wse-communit/2` 安装镜像。把源计划直接交给 simulate 时，前端会先编译再创建运行组件；传入 compiled 文件则直接装载，绕过编译器。运行中的核、CommPort 和 CommUnit 均不调用路由或时隙生成函数。

安装镜像中的每个 `calreg[opcode]` 包含：

- `entries`：每个节点允许的发送／接收布局，包含 routeBits、capacity、expVal。
- `segments`：源、落点、源／目标偏移、字节数，以及 `read_cycle / inject_cycle / write_cycle / check_cycle`。
- 每段的 `links`：有向链路、flit 编号、相对 NoC 周期、有效字节和包头／填充开销。
- `stride`：该编译器采用的流水阶段间隔，单位为核周期。

它是行为模型的寄存器镜像表达。各事件绝对时刻等于本轮 align 的共同原点加表中的相对时刻。运行时晚到只影响共同原点；服务超过约定界限时报错，不重新排时隙。安装版本摘要同时约束平台、布局、路由和时隙。

## Tile 通信公共合同

公共定义在 `runtime/communication.py`。只保留一个 CommRequest 和一个 CommUnit，原整包执行器、DataRange/DataCommand 协议和双实现选择分支已移除。

```text
CommRequest(request_id, origin, opcode, epoch, members, version, send=None, receive=None)
CommSubmit(node, endpoint, request)
SendDesc(binding, SendLayout(...))
ReceiveDesc(binding, ReceiveLayout(...))
TileBinding(object_id, version, memory_space, owner)
```

每个参与节点最多一条发送描述符、一个接收区域；只收或只发的节点也提交。`request_id` 是本节点的请求身份，内部通过 `(node, request_id)` 区分节点间同名请求。`origin` 保留 block、thread、instruction 的诊断身份，节点编号由提交端点提供。

SendLayout 中所有偏移均为字节：

```text
src_offset, row_count, row_bytes, src_row_stride,
recv_sym_base, self_off, recv_row_stride, route_bits, self_copy

第 r 行：读取 src_offset + r * src_row_stride 起的 row_bytes 字节
         写入 self_off + r * recv_row_stride 起的 row_bytes 字节
```

ReceiveLayout 包含 `recv_sym_base, capacity, exp_val, valid_ranges`。逻辑接收符号相同的节点可以各自绑定不同 Tile。`exp_val` 统计成功写入且可见的远端有效字节，本地 self_copy 走实际读写但不计入它。`valid_ranges` 是必须覆盖的半开字节区间；padding 不自动算入有效数据。当前一个接收登记绑定一个 Shared Tile 对象。

| 交互 | 含义 |
|---|---|
| Accepted / Rejected | 接管成功，或拒绝并明确是否可重试 |
| DataAccess(prepare) → DataReply(prepared) | 核侧保留源、预留目标与有界端口服务，返回源／目标句柄 |
| read → read_done | 分段读取，经过存储端口服务 |
| write → write_done | 分段写入，写入可见后返回 |
| publish → published | TileReg 独立检查范围覆盖后设置目标 ready |
| Completion | 本地发送责任结束且本地接收目标已发布，或明确的故障 |

DataAccess/DataReply 携带 `node、token、access_id`，防止跨节点身份混淆和重复响应。TileReg 管理分配、版本、权限、发布与回收；CommUnit 通过句柄访问。源引用保守保留到本地成功完成。故障时未完成请求返回 `safe_to_release=False`，目标保持未发布，资源隔离，当前版本不做故障恢复。

TX/RX 分别指发送／接收缓冲。源计划显式给出 `access_bytes、tx_bytes、rx_bytes`。编译器根据有限缓冲安排分段读、网络和写流水；当前保守安排每个网络时隙一段，允许读／网络／写阶段重叠。每段单独计网络包头开销。核侧在通信期间保留相关读写端口服务，普通访问可能等待。本地完成与全网排空分别记录，发送节点无需等待所有接收节点发布。

### AllGather 的迁移

`allgather_group` 和 `allgather_40` 保留高层构造入口，编译成“向根节点汇集完整结果，再广播结果”的复制轮次。多棵诱导树拆成多次广播，每次请求仍只有一份 routeBits。它们与手写广播／汇集使用同一协议和执行器。

这是一个明确的保守算法选择，通信量、根节点负载和时间会与旧版同时多源 AllGather 不同，旧性能数值不再作为这条路径的回归基准。旧 `wse-communication/1` 原始计划会明确报错，需迁移或重新生成。历史 examples 中的归档输入和结果保持原样。

## SRAM 访存合同

公共定义在 `runtime/memory.py`，对应 issue #9。SRAM 是每核按地址访问的静态随机存取存储器，与 Shared Tile 绑定分开管理。

```text
MemoryRequest(request_id, src_core, dst_core, op, offset_bytes, nbytes, data=None)
MemorySubmit(endpoint, request)
MemoryAccepted / MemoryRejected(request_id, reason, retryable)
MemoryDone(request_id, status, data=None, error='', stages=())
MemoryRetired(request_id)
```

request_id 使用 `(源核, 单调序号)`。TLSU（Tile 访存单元）内部保存请求与指令、访问阶段的关联。负载的 `memory_accesses[].target_core` 显式指定目标，缺省为本核；`offset_bytes` 相对目标 SRAM 起点。目标核无需运行配对通信指令，没有指令的节点也有 SRAM 服务端点。

本地访问直接送目标 SRAM 服务。远端读经历请求传输、目标 SRAM 读取、数据返回；远端写经历数据传输、目标写入、确认返回。MemoryAccepted 只表示接管。MemoryDone 成功后，load 继续核内 Tile 写回再发布 ready；store 才结束。需写后读可见性时，后续读依赖写完成。

每核一个 `models/memory/sram.py::SRAM`，本地、远端和 HBM 搬运的 SRAM 阶段共享同一端口。占用时间为 `ceil(nbytes / throughput)`，启动开销单独作为可流水的结果延迟。这个口径遵循 issue #9 的建议，替换了旧版把 SRAM 启动开销全部串行占用端口的计时。

`models/noc/memory.py::MemoryNetwork` 是可替换的聚合访存网络后端：请求与返回共享一个带宽资源，控制消息和数据头部都计费。它与 Calendar 网络使用独立的资源模型，当前不模拟二者混合竞争，也不实现 SSM 的 H/V 端口或真实路由。若以后接 Calendar 后端，需离线提供请求、返回方向的路由和时隙，保持核侧消息合同不变。

容量计满时返回可重试拒绝，发送方保留请求。SRAM 与访存网络的在途项一直保留到接收方消费完成并返回 MemoryRetired，防止完成交付前提前归还容量。运行队列按下一拍可靠交付消息，未另建有损或有限反馈队列。重复身份、重复终态和越界访问明确报错。

`memory_transport` 显式配置带宽（B/核周期）、固定延迟（核周期）、控制与头部字节、在途容量和 SRAM 队列深度。默认参数是合成功能验证值。实际字节仅在假核调试中携带，性能核传元数据。

## 独立运行

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python3.11 -S examples/communication/streaming.py broadcast --buffer-bytes 64
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python3.11 -S examples/communication/streaming.py gather --buffer-bytes 64
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python3.11 -S examples/communication/memory.py
```

前两例：节点 0–3，源为 16×16 FP16（512 B），访问粒度 32 B。广播逐字节检查三个目标；汇集逐行检查横向拼接的 1536 B。第三例用 128 B 假 SRAM，在偏移 16 写入 32 B，等待写确认后读回并比较全部字节。详细条件和结果见 [接口验证记录](INTERFACE_VALIDATION.md)。
