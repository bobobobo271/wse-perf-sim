# 核与 NoC 接口

本版本按 [issue #4 的最新接口](https://github.com/hmljy2020/wse-perf-sim/issues/4#issuecomment-6848590938) 和 [issue #9](https://github.com/hmljy2020/wse-perf-sim/issues/9) 区分两种请求：Tile 通信任务、按地址访问 SRAM。核与 NoC（片上网络）通过公共消息交互。路由和 Calendar 时隙在执行前确定，通信提交路径只验证并执行已安装的配置。

## Tile 通信接口

| 参与角色 | `comm.submit` 请求字段 |
|---|---|
| 发送 | `opcode`、`route_bits`、`send_bytes` |
| 接收 | `opcode`、`expected_receive_bytes` |
| 同时发送与接收 | 共用一个 `opcode`，同时填写发送和接收字段 |

节点编号、消息端点和关联令牌由仿真消息信封提供，操作数里没有 epoch、Tile 地址或偏移。源和目标 Tile 版本由核内指令绑定，偏移和二维布局由编译计划提供。接收字节数统计远端有效数据。本地自拷贝也要写完，计入目标覆盖范围。

## 编译与装载

`compile_plan(source_plan, config)` 按路由和数据量生成 `CalReg[opcode]`。每个 flit（流控单元）在线路上占 64 B，最多携带 52 B 有效数据，另外 12 B 用于包头。模型只记录线路总字节和有效字节。每个源的连续数据流按 52 B 切分，128 B 拆成 52、52、24 B 三个 flit。一个 flit 可以跨越多个本地存储访问或带填充的行。

编译表同时保存成员、合法提交字段、每段本地读写范围、相对注入、链路、发送结束和写入时刻。配置由 `opcode` 选择，当前同一时刻只执行一轮。上轮排空后可以复用同一 opcode。不同布局或流量需要不同的编译配置。

```python
import json
from pathlib import Path
from config import wse_config
from frontend.communication import compile_plan
from api import simulate

config = wse_config()
compiled = compile_plan(source_plan, config)
Path('workspace/compiled.json').write_text(json.dumps(compiled, indent=2))
report = simulate(capture_dir, config, communication_plan='workspace/compiled.json')
```

源计划沿用 `wse-communication/stream-v1`。连续发送区可只写 `source_version`、`nbytes`、`route_bits`，接收侧写 `target` 和 `exp_val`。多行、有间距或多个源落到同一目标时，可以在编译计划写具体布局。现行编译产物格式是 `wse-communication/compiled-v2`，安装镜像是 `wse-communit/3`。旧的已编译产物需从源计划重编，传入源计划时前端会在创建运行组件前编译。

## 运行与完成

1. 核内先确认源版本及目标空间，等待已占用的端口服务结束。实际发出 `CommSubmit` 的同一拍，`CommPort` 就进入 `comm.wait` 状态。
2. `CommUnit` 把提交本身作为就绪信号。收齐本轮所有成员的请求就对齐，然后给表里的相对时刻加上共同起点。数据读写仍通过 TileReg，准备状态在核内处理。
3. 纯发送核在最后一个 flit 离开发送链路后收到完成通知。纯接收核在最后一段写入可见、目标覆盖完整后收到完成通知。兼具发送和接收的核同时满足两项条件才结束等待。核的后续数据依赖由 `comm.wait` 完成解锁。

端点缓冲上限以有效字节计，需容纳至少一个有效 flit。默认存储访问粒度可小于 52 B，编译器把一个 flit 映射到若干本地访问并计入服务时间。固定服务超时会报错。成功完成后核内释放源引用与端口保留。

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

前两例：节点 0–3，源为 16×16 FP16（512 B），访问粒度 32 B。广播逐字节检查三个目标；汇集逐行检查横向拼接的 1536 B。第三例用 128 B 假 SRAM，在偏移 16 写入 32 B，等待写确认后读回并比较全部字节。历史验证结果见 [接口验证记录](INTERFACE_VALIDATION.md)。
