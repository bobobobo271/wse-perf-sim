# WSE40 Calendar 集成

本版本以 ssnpu-model 为主体，用 Python 标准库实现全片 NoC（片上网络）。运行时不导入 TileSim。每个 NoC 节点连接一核的 SharedTileReg；SRAM 只通过该核 TLSU（Tile 访存单元）访问。

设计依据为 [WSE 白皮书 v0.2](https://github.com/PTO-ISA/wse-model/blob/e9ecb1aeb766372110bc4d4847f19d4a451c1108/docs/design/wse-system-architecture-whitepaper.md) §4.5–4.7、§9、§11。核接入和接收登记顺序采用新版白皮书；Calendar 文档中的旧核 MTE 接口不作为本版本接口。树路由、行列计划与链路计时迁自 tilesim_wse `3445e9942707a54f2ed16323e60260bd07d15c3f`，保留来源，取消 TileSim 运行框架。

## 运行

要求 Python 3.11+，仅标准库。以下命令在仓库根目录运行，产物写入 `workspace/`：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python3 -S examples/wse_allgather.py --output-dir workspace/wse40
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python3 -S examples/wse_allgather.py --sram --output-dir workspace/wse40-sram
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python3 -S -m ssnpu_model \
  workspace/wse40/capture --config config/wse40.json \
  --communication-plan workspace/wse40/capture/communication.json \
  --output workspace/wse40/cli-report.json --event-log workspace/wse40/instructions.jsonl
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python3 -S -m unittest discover -s tests -v
```

API 保持原用法，并新增可选 `communication_plan`：

```python
from ssnpu_model import simulate, wse_config
report = simulate(capture_dir, wse_config(), communication_plan=plan_path_or_dict)
```

传通信计划且省略配置时，使用 `wse_config()`；原调用不传计划时仍使用原逻辑验证配置。`examples/wse_allgather.py` 生成合成 capture v2 和通信计划，用来测试导入、调度和通信，并非 PyPTO 编译产物。原仓 matmul、FA、ooo 的 PyPTO capture 和归档结果保留原样。

## 参数及范围

| 参数 | 设置与来源 |
|---|---|
| 拓扑 | 40 节点，5×8，节点号 `row*8+column` |
| 核 / NoC 时钟 | 1.4 / 2 GHz，白皮书目标；配置项 |
| 有向链路 | 64 B/NoC 周期；每周期注入、弹出各一 flit，多播分支并行 |
| 包头 | 暂定每 flit 12 B，默认有效载荷上限 52 B |
| 跳延迟 | 暂定 1 NoC 周期，包含链路传输 |
| 发送 / 接收跨域延迟 | 各暂定 2 NoC 周期 |
| Calendar | 暂定 8 时隙/周期，每时隙 1024 NoC 周期；对齐 16、arm 8、窗口余量 1 NoC 周期 |
| SharedTileReg | 每核 1 MiB；读写各 256 B/核周期，各附加 2 核周期延迟；读写独立 |
| LocalTileReg | 每 PE 1 MiB；默认每核 4 PE |
| SRAM | 每核 384 MiB；读写共享 1 TB/s，读写头开销各 100 ns |
| 端点缓冲 | 整包发送/接收；按静态计划预留，报告每核 TX/RX 预约容量峰值 |

参数保存在 `config/wse40.json`，WSE 参数附单位、来源和状态。Shared 容量与端口、Local 容量、写预约策略、端点缓冲和 NoC 未定项保持 `pending`。Tile 容量是用户确认的功能验证配置，尚未对应白皮书整核 2.25 MiB 暂存预算。原核计算吞吐保持合成逻辑验证参数；例如原配置的 CUBE 4096 MAC/周期是每 PE，不能当作白皮书整核 4096 MAC/周期目标已经实现。

标量、地址计算、Batcher、分支推测和故障恢复不增加详细建模。计划必须声明 `non_speculative: true`，表示它描述确定的非投机执行路径。这是输入合同，不是模拟器对机器码非投机资格的证明。

## 执行与存储

一个 `CalendarNoC` 对象管理全片网络。现有 Scheduler、ROB、TileReg、TLSU 和执行单元继续工作。ROB 是按线程维护顺序退休的重排缓冲；执行完成仍可提前广播。

每个参与节点、每阶段插入三个模型操作：

1. `calendar.post`：为接收生产者预留目标 Tile，登记接收范围；登记成功后独立完成。
2. `calendar.send`：通过 TLSU 读取源 SharedTileReg，整包 staging 后释放本次源读引用，等待本地 TX 注入排空后完成。它合并发送与本地 drain 的模型等待。
3. `calendar.receive`：等待接收写入、范围覆盖和发布，完成后唤醒原计算消费者。

全体节点的源数据已 staging、目标已登记后，NoC 请求各 Shared owner 暂停启动普通写，取得已有写的服务结束时间，安装接收写预约并等待确认，然后建立共同 Calendar 时间原点。`align` 由这个集合状态转换实现。每节点只报到一次；跨阶段等待上一阶段所有收发操作完成。时隙和路由来自只读显式计划；模型不定义尚未定稿的 CalReg 内部编码，也不实现跨轮并发或自动跨时隙重放。

Shared 写端口预约使用核周期区间 `[start, service_end)`。普通写能在空档完整服务时才启动，跨入预约区间则等待；读端口独立。端口服务结束与写可见时间分开，结果延迟仍可流水重叠。准入暂停普通写只覆盖获取端口状态和安装预约的握手；通信期间普通写可以使用空档。

接收目标具有唯一生产者和代际。TileReg 分别记录预期分段、pending 分段和已提交分段。重复的 pending/已提交分段不重复占用端口或计数；身份相同但范围不同、旧代际和越界写报错。每个分段完成只确认其范围；全部范围完整且写入可见后，NoC 核对唯一远端有效字节数，TileReg 才发布整个版本。本地自拷贝占 Shared 写端口，但不计入远端 `expVal`。

源引用可在 staging 后释放，TX 责任继续到注入排空；本地段也使用已经保留的 staging 数据。模拟器不保存真实 tensor payload，验证的是身份、范围、依赖与时序。原 Shared retention/last-use 合同继续决定存储回收；默认 retained 的 Shared 输出可以保持 live-out。

第一版每阶段每参与节点提供一个非空源数据块（自动模式可分多棵树发送）、接收一个完整目标对象；源和目的使用不同存储槽。已有功能覆盖 AllGather/复制的 opcode 1，归约需要另定 merge 语义。显式计划的大包若超过固定窗口则失败；自动计划按包长和路径生成窗口。接收写端口违约、链路冲突和超窗均终止该次运行并给出错误。

## 时间口径

核组件使用整数核周期；NoC 事件使用 `Fraction` 精确纳秒。每个核时钟边界前处理此前到期的网络事件，网络消息在该接收边界交付。核内消息继续下一核周期交付，网络交付携带原始网络时间。组件调用顺序不改变结果。

对非空 Shared 请求，服务时间为 `ceil(bytes/256)` 核周期，结果在服务结束后再过 2 核周期可见。端口排队、消息交接和跨域延迟单独计入。普通共享写与网络写只使用 TileReg 的同一个 Shared 写服务。

SRAM 每次访问占用服务资源的时间为：

`ceil((startup_ns / 1e9 + bytes / bandwidth_Bps) * core_clock_Hz)` 核周期。

100 ns 头开销属于串行服务，不作为可流水重叠的结果延迟。读写使用同一个每核资源。WSE 配置只预置显式 `SRAM` 地址域；不把 capture 中的 DDR/L1/L2 名字默认为本核 SRAM。访问检查对象范围与 `offset_bytes`（缺省 0）；完整驻留内存分配和 bank 冲突未细化。

Shared→NoC→Shared 不访问 SRAM。`total_cycles` 包括核内退休、消息与网络收尾；`total_time_ns_exact` 保留精确时间。NoC 窗口余量用于准入检查，并非额外的核内指令延迟。

端点缓冲峰值是按计划为每阶段预留整包空间后的最大需求：TX 为本核源包，RX 为本核远端接收包之和。它不是实际逐周期占用实测，也不代表满足了某个已知硬件深度。源/分段等身份作为模拟器元数据保存；12 B 包头只是已列字段的暂定基线，线上身份/地址编码仍待定。

## 通信计划

`schema` 为 `wse-communication/1`，`non_speculative` 必须显式为 true。`phases` 中每项包含 `name`、`opcode`、`epoch`、`nodes`。每个节点绑定：

| 字段 | 含义 |
|---|---|
| `core` / `owner` | 节点号、用于确定 Thread/Block/任务身份和插入位置的原 capture 操作 ID |
| `source_version` | 原导入器的版本键，例如 `["shared", "source:0:0"]` |
| `target` | 完整 SharedTileReg 对象描述，含形状、类型、存储槽、版本身份 |
| `source_offset` / `nbytes` | 源有效范围；默认源偏移为 0 |
| `destination_offset` | 本源数据在各落核目标中的偏移 |
| `route_bits` / `window` | 同一计划产生的 80 bit 路由位图和固定发送窗口 |
| `after` | 可选的额外完成依赖，使用 capture 操作 ID |

`owner` 的数据输出不是隐式控制依赖；源版本自动建立数据依赖，额外顺序由 `after` 明确表达。插入操作继承 owner 的 barrier 阶段，已有 barrier 的完成集合也加入这些操作。各阶段依次收束；同 opcode 的 epoch 逐轮递增并按配置位宽回绕。

目标对象的消费者在 capture 输入中引用同一版本；导入器将其 producer 绑定到接收完成操作。计划不能覆盖已有 capture 输出的生产者。目标必须被本地和远端分段恰好覆盖，无空洞、重叠或越界。`routeBits` 的 P 节点诱导子图必须包含源、连通且无环。生成样例见 `workspace/wse40/capture/communication.json`。

## 自动规划

任意参与组可使用 `allgather_group`。`sources`、`targets`、`owners` 分别为以节点号为键的源对象、
目标对象和原 capture 操作 ID，`shard_bytes` 为每个源发送的字节数。

```python
from ssnpu_model import simulate, wse_config
from ssnpu_model.noc_plan import allgather_group

plan = allgather_group(sources, targets, owners, shard_bytes=256)
report = simulate(capture_dir, wse_config(), communication_plan=plan)
```

它生成 `routing: "auto"` 的阶段。导入计划时，模型自动选路并生成发送窗口。
手写计划也可设置该字段，节点提供可选 `destinations` 列表，默认向全部参与节点发送。
自动模式下节点无需填写 `route_bits` 和 `window`。

选路先取最短路径，再把能够保持诱导树的路径合并。完整 2×2 组等有环集合会分成多棵树发送，
每个目的节点只落核一次。发送窗口根据包长、跳数、跨域时延和 Shared 写入服务计算。
当前自动调度按整包、逐树串行分配全网窗口，窗口内各跳可流水传输、分支可并行转发。
这是便于验证的保守行为模型，通信耗时受这一调度选择影响。自动窗口按负载字节数生成，
用于行为级调度估算；它不作为白皮书中与数据几何无关的固定 CalReg 表编码实现。
自动模式与固定窗口基线分别报告。

报告的 `noc.schedule` 给出各源、各轮的位图和相对对齐原点的起止周期，单位为 NoC 周期。
`noc.events` 记录实际链路传输和接收写入，链路事件中的 `round` 区分同一源的不同树。
模型沿用现有字节范围、版本和依赖计量，自动规划直接进入现有 `simulate` 入口。

新增测试固定 5×8 网格、1.4/2 GHz 核/NoC 时钟、64 B 链路、12 B 包头和 1 周期跳延迟。
节点 `{0,1,8,9}` 各发送 256 B，验证 8 棵树、16 次唯一写入、每核远端收到 768 B，以及接收完成后唤醒消费者。
另分别改变参与节点位置、包长至 65536 B、链路宽度至 32 B，检查路径、窗口和接收计数。
测试入口为 `python3 -S -m unittest discover -s tests`，生成文件保存在 `workspace/calendar-integration/`。

## 结果与验证

报告包含原核时间线、配置、输入哈希，并增加 `communication_plan`、`noc.events`、链路 flit/有效载荷/线速字节统计、每阶段远端提交计数和端点预约容量。Perfetto 指令导出识别登记、源读取、Calendar 等待/TX 排空、接收等待/发布；网络逐 flit 事件保留在 JSON 报告中。

测试条件和结果见 [VALIDATION.md](VALIDATION.md)。原始运行文件留在 `workspace/validation/`；仓库提交精简结果、日志和源码哈希，便于复核。
