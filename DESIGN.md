# WSE 性能仿真设计

WSE（Wafer-Scale Engine，晶圆级计算引擎）性能仿真器用 Python 回放 Tile（数据分块）指令，估算核内执行、片上通信、存储搬运和资源争用的时间。核内结构参考 SuperscalarNPU（超标量神经网络处理器）及 SuperScalarModel，系统架构参考 [WSE 白皮书 v0.3](https://github.com/PTO-ISA/wse-model/blob/1361e38550a7a208a1a5928a1af082c48c88bf6c/docs/design/wse-system-architecture-whitepaper.md)。本文中的 WSE40 参数是仿真配置，暂定值按各自的验证用途使用。

## 1. 前端

### 负载导入

正式算子负载由 PTO 工具链提供。PyPTO 在导出前确定张量形状、数据类型、分块、指令和核映射。仿真器读取静态 capture（指令记录目录），用形状、访存范围和依赖回放时序。数值正确性验证可以另跑。组件测试可以构造同格式的合成记录。[PTO 回放说明](examples/decode/README.md)

`frontend/capture.py` 的 `TraceAdapter` 读取 capture v2 / SSNPU（SuperscalarNPU）v1，校验线程身份、原始顺序、数据依赖、同步屏障及 Tile 版本的生命周期。`order_dependencies` 只确定同线程的指令顺序与退休次序，数据与同步依赖决定指令何时可发射。构建后的 `Instruction` / `Version` 元数据只读。`operands` 保留操作数原有顺序和解析别名后的版本。

联合回放入口为 `api.simulate(capture_dir, config=None, *, communication_plan=None)`，配置可用字典或 JSON 文件。核独立回放用 `api.simulate_core`，可注入通信或 SRAM（Static Random-Access Memory，静态随机存取存储器）后端。命令行入口为 `python -m cli`。

### 通信编译与装载

Calendar 是通信时隙表。设计允许在执行前手工或自动编排路由和时隙。现有前端可读取显式通信计划，把通信绑定到 capture 中的指令和 Tile 版本。源计划描述成员、源与目标布局、传输量和路由，编译器生成相对时隙。核侧保留指令绑定与请求字段，网络侧装载按 `opcode`（操作码）索引的 `CalReg`。两侧产物由同一次编译生成，装载时校验布局、版本和平台参数。正式的 PTO 通信导出仍待接入。

`frontend/communication.py` 的 `compile_plan` 将 `wse-communication/stream-v1` 源计划编成 `wse-communication/compiled-v2`，包含 `wse-communit/3` 安装镜像。传入源计划时，前端在创建运行组件前编译。已编译计划可保存为 JSON，再直接装载。旧版编译产物需要用源计划重生成。运行中的核与 CommUnit（通信单元）只校验并执行已经装载的配置。[计划格式与示例](docs/COMMUNIT.md#编译与装载)

## 2. 后端

后端按核、NoC（Network-on-Chip，片上网络）和存储划分。组件各自维护队列与动态状态，经公共消息交互。[工程结构](docs/STRUCTURE.md)

### 2.1 Superscalar 核

#### 组件与状态

每核装配 Scheduler（调度器）、ROB（Reorder Buffer，重排序缓冲区）、TileReg（Tile 寄存器）、TLSU（Tile Load/Store Unit，Tile 访存单元）、Sync（同步单元）和 CommPort（核侧通信端口）。每个 PE（Processing Element，处理单元）装配 Cube 矩阵计算单元和 Vector 向量计算单元。Cube、Vector 和 TLSU 根据各自收到的指令计算工作量和延迟。公共 `Service` 记录服务时钟与统计，`ExecutionUnit` 复用操作数访问、计算队列和写回流程。

| 组件 | 独占的动态状态 |
| --- | --- |
| Scheduler | 已完成的依赖、待发射窗口、输出分配请求、PE 轮转和发射额度 |
| ROB | 每线程的条目、准入游标、窗口占用、完成状态和退休头 |
| TileReg | Local/Shared 容量、版本就绪状态、消费者完成记录、分配队列和共享读写端口 |
| Cube / Vector | 在途命令、操作数请求、计算服务、结果延迟和写回等待 |
| TLSU | 访存请求与指令的关联、load 的 Tile 写回、store 完成 |
| Sync | 控制节点依赖、barrier（同步屏障）和 task（任务）放行 |
| CommPort | 本地通信绑定、请求令牌、等待、重试和完成状态 |

#### 时钟、消息与指令调度

`runtime/runner.py` 每拍交付上一拍的消息，对每个组件调用一次 `tick(cycle)`，再收集本拍消息供下一拍交付。`receive` 入队，`drain_outputs` 取走输出，`is_idle` 和 `snapshot` 查询状态。消息按源组件名称及源端先入先出顺序交付。消息信封记录源、目的、端口和发送/接收周期，修改组件 tick 顺序不改变仲裁结果。

| 消息 | 发送方 → 接收方 | 含义 |
|---|---|---|
| Admitted | ROB → Scheduler | 已占线程窗口槽位，下一周期允许检查依赖及预留输出 |
| Allocate / Allocated | Scheduler ↔ TileReg | 原子保留全部输出，等待期间不重复请求 |
| Dispatch | Scheduler → Cube / Vector / TLSU / TileReg | 已分配命令；view 交给 TileReg |
| Issued | Scheduler / Sync → ROB | 发射通知 |
| Access / AccessDone | 执行单元 ↔ TileReg | 按操作数顺序访问输入，或逐一写回输出 |
| Complete / Completed | 执行单元 / TileReg / Sync → ROB → 各 Scheduler / Sync | 最终完成与依赖广播 |
| Consumed | 执行单元 → TileReg | 指令已完成，输入不再使用，输出可检查释放 |

ROB 按原始线程顺序建立条目，每拍先处理完成、按序退休，再按序补入空槽。准入和退休宽度无限。`rob_depth` 限制每线程已准入且尚未退休的指令数，正整数表示有限深度，`null` 表示无限。完成但未退休的指令仍占槽，view（同一存储的别名视图）也占槽。控制节点由 Sync 管理，单独记录退休。零延迟控制节点同拍产生发射和完成消息，消息仍在下一拍交付。核内消息队列和执行单元队列使用无限深度。

Scheduler 收到 `Admitted` 后检查数据和同步依赖，再申请一次输出分配。TileReg 原子保留全部输出。已保留但尚未发射的输出继续占容量。Scheduler 在 PE 间轮转，在每个 PE 内选择最老的就绪指令，实际派发才消耗核与 PE 的发射额度。执行单元允许多条指令在途，依次访问每条指令的输入。等待输入或写回的旧指令可以被其他就绪指令绕过，就绪命令按 `(task, age, id)` 仲裁。完成广播唤醒数据与同步依赖，退休仍按线程顺序推进。

#### 服务时间与计算预算

WSE40 核时钟为 **1.65 GHz**。FP16（16 位浮点）Cube 预算为每核每周期 4096 MAC（乘加），Vector 与类型转换预算为每核每周期 256 B。当前等分到各 PE，暂不借用其他 PE 的空闲份额。Vector 按数据类型把字节吞吐换算为元素吞吐，指数与归约还受各自的合成速率限制。通用 `default_config()` 使用独立的每 PE 合成参数。

```text
service = max(1, ceil(work / throughput))
service_end = start + service
result = service_end + latency
```

服务区间为半开区间。服务结束后资源可以接下一条命令，前一条命令仍可等待结果延迟或写回。组件消息的周期单独计。Cube 的矩阵乘按 `M × N × K` 次乘加计，Vector 的逐元素运算按有效输出元素数计，行归约按 `有效行数 × (有效宽度 − 1)` 计，TLSU 按访问范围与数据类型计算字节数。通用配置支持 DDR、L1、L2 等逻辑访存路径及吞吐/延迟覆盖。缓存驻留、存储体冲突和单条指令内部的细粒度重叠超出当前建模粒度。

输入全部可用后才计算，结果产生后才申请写回，输出写回完成后广播指令完成。store 在内存写入完成后结束。view 不另占计算或访存资源，也不分配新存储，其依赖放行与完成仍走消息通路。

#### TileReg 与数据生命周期

SharedTileReg 为每核共享，也是 Calendar 通信的读写端点。LocalTileReg 为各 PE 私有。首版功能验证配置为每核 Shared 1 MiB、每 PE Local 1 MiB。Shared 读写各 256 B/核周期，结果延迟各 2 核周期，两个方向独立。NoC 接收写和核内普通写共享写口，首版通过通信期间的端口预留提供约定的服务；容量和端口参数均待硬件校准。私有操作数的额外服务包含在原有计算假设中，请求与响应各耗一拍。

TileReg 是修改版本就绪、容量和消费者状态的唯一组件。普通版本在生产完成且全部消费者结束后释放，无消费者的版本在生产完成消息到达时释放。shared last-use（最后一次使用）须覆盖参与者并等其他消费者完成。声明 reuse（复用）的共享版本可保留为 live-out（本段执行结束后仍可用的输出）。依赖唤醒与容量释放随完成消息推进。

固定位置后续写入等旧生产者和其他读者完成。原位指令可以读取被自己替代的旧版本，分配时将容量所有权交给新版本，写入前仍可通过旧版本标识读取。带 `superseded_by` 的释放事件表示容量转移。前端校验无序的同位置写入、非法 last-use/reuse、别名环、缺失生产者和依赖图环。

#### 从 load → matmul → store 阅读代码

先读 `api.py` 和 `simulator.py` 的组装与时钟循环，再按下列消息流读组件。完整目录见 [项目结构](docs/STRUCTURE.md)。以下使用通用逻辑验证配置。B 已就绪，load 产生 A，matmul 产生 C，store 写回 DDR。

```text
ROB --Admitted(load)--> Scheduler
Scheduler --Allocate(A)--> TileReg --Allocated--> Scheduler
Scheduler --Issued(load)--> ROB
Scheduler --Dispatch(load)--> TLSU
TLSU [memory service + latency] --Access(write A)--> TileReg
TileReg --AccessDone--> TLSU --Complete(load)--> ROB --Completed--> Scheduler/Sync
                          \--Consumed(load)--> TileReg
Scheduler [matmul 依赖就绪] --Allocate(C)--> TileReg --Allocated--> Scheduler
Scheduler --Dispatch(matmul)--> Cube
Cube --Access(read A)--> TileReg --AccessDone--> Cube
Cube --Access(read B)--> TileReg --AccessDone--> Cube
Cube [cube service + latency] --Access(write C)--> TileReg --AccessDone--> Cube
Cube --Complete(matmul)--> ROB --Completed--> Scheduler/Sync
     \--Consumed(matmul)--> TileReg [检查 A/B 释放]
Scheduler --Allocate(无 tile 输出)--> TileReg --Allocated--> Scheduler
Scheduler --Dispatch(store)--> TLSU --Access(read C)--> TileReg --AccessDone--> TLSU
TLSU [memory write service + latency] --Complete(store)--> ROB
                                    \--Consumed(store)--> TileReg [检查 C 释放]
```

每条箭头固定占一周期。依次阅读 `models/ssnpu/` 下的 `scheduler.py`、`tile.py`、`tlsu.py` / `execution.py`、`cube.py`、`rob.py`；再读 `sync.py` 理解尾部 barrier 与 task 完成。Vector 使用相同的小型命令结构，服务资源为共享 Vector。

可以用一条私有输出的 4 元素 `tile.full` 手算时序。设吞吐为 4 元素/周期，结果延迟为 2 周期。周期 0 准入，1 请求分配，2 分配，3 派发，4 接收并计算，5 服务结束，7 产生结果并请求写入，8 写入，9 响应并完成。第 10 周期，ROB 接收完成并退休，TileReg 处理消费。第 11 周期交付完成广播，任务完成节点随即完成，第 12 周期退休，第 13 周期交付最后一条广播，`total_cycles` 为 13。

### 2.2 Die 内 NoC

#### 2.2.1 NoC 设计

首版在一个 die（裸片）内按 WSE40 建模。拓扑为 5×8 的二维网格，节点号为 `row * 8 + column`。NoC 频率为 2 GHz，每条有向链路每 NoC 周期发送 64 B。跳延迟、跨时钟域开销、对齐和端点服务按配置计。当前默认跳延迟为 1 NoC 周期，收发跨时钟域各 2 NoC 周期，属暂定参数。

Calendar 的数据路径为源 SharedTileReg → TX（发送缓冲）→ NoC → RX（接收缓冲）→ 目标 SharedTileReg。多播在共享链路上传一次，到分支节点再复制。链路上的 flit（流控单元）固定占 64 B，包含 12 B 包头和最多 52 B 有效数据。模型计有效字节和线路字节，包头字段不展开。连续有效数据按 52 B 切分，128 B 对应 52、52、24 B 三个 flit，在一条链路上共占 192 B。

编译计划独立设置存储访问粒度和 TX/RX 容量。一个 flit 可跨多个存储访问范围，跨带填充的行时只打包有效字节。缓冲容量按有效字节计，至少能容纳本轮最大的有效 flit，报告记录端点占用峰值。按计划预留缓冲是建模口径，硬件缓冲深度待确认。

远端 SRAM 访存目前用独立的聚合传输后端，与 Calendar 使用两套资源。白皮书 v0.3 规定竞争同一方向链路时 Calendar 优先于 PGAS（Partitioned Global Address Space，分区全局地址空间）访存。共享链路仲裁、两种流量混合时的带宽竞争及具体硬件端口仍待建模。[WSE40 运行说明](docs/WSE.md)

#### 2.2.2 Calendar 算法

算法输入是参与节点、源与落点、布局、有效字节数和平台服务参数。先确定转发树，再分割数据流、核对接收范围，并按链路、端口、跨域时间与缓冲容量排出相对时隙。设计要求是资源互不冲突的传输可以重叠。当前编译器保守地给每个网络时隙安排一个 flit，读、网络和写阶段可重叠。运行时一次接纳一轮，跨独立通信轮的并发尚未实现。

当前支持广播与汇集两种复制模式。一轮中每核最多一份发送描述和一份接收登记。AllGather（全收集，每个成员最终得到全部分片）先将各分片汇集到根节点，再用一棵或多棵树广播完整结果。`allgather_40` 先做行内全收集，再做列内全收集。当前汇集与广播方案会把流量集中到根节点，其性能按这一算法计算。

##### 2.2.2.1 routeBits 设计

`routeBits` 是核侧提交的路由位图，字段名为 `route_bits`。WSE40 每节点占 2 位，节点 `n` 对应位 `[2n+1:2n]`，共 80 位。

| 两位值 | 节点作用 |
| --- | --- |
| `00` | 不参与这条路由 |
| `10` | 属于路由树，可发出或转发，不落地 |
| `11` | 属于路由树，并接收一份有效数据 |
| `01` | 非法编码 |

编译器取所有启用节点及其网格邻接边，校验它们构成一棵包含发送源、连通且无环的诱导树，再从源节点沿树确定转发方向。远端落点必须已登记接收。源节点如需留存一份数据，在编译计划中使用本地复制。比如节点 0 经节点 1 发给节点 2，三个节点的位对从节点 0 起分别为 `10`、`10`、`11`，位图为 `0x3a`。节点 1 仅转发，节点 2 落地。

自动路由先从源点用广度优先搜索得到最短路径树，再尝试合并各落点路径。只有合并后仍为诱导树才放进同一张位图，否则生成新的树和发送轮次。该方法可复用公共链路，未做全局流量均衡。[路由实现](models/noc/routing.py)

##### 2.2.2.2 CalReg 设计

`CalReg[opcode]` 是网络侧预先装载的模型时隙配置。每份配置记录参与成员、各节点允许提交的操作数、布局摘要、流水阶段间隔 `stride` 以及各 flit 的访问范围和事件。字段使用两种时钟单位。

| 时序内容 | 单位与含义 |
| --- | --- |
| `read_cycle` / `inject_cycle` | 相对核周期，读取源数据并在约定时隙注入 |
| `links[].noc_cycle` | 相对 NoC 周期，每条有向链路上的传输时刻，已计入发送跨域与逐跳延迟 |
| `send_cycle` | 相对核周期，发送侧该 flit 离开发送链路 |
| `write_cycle` / `check_cycle` | 相对核周期，向目标发写请求及检查写服务上界 |

编译器先按 52 B 分段，再按行宽、跨行间距与存储访问粒度拆出本地读写范围。它校验预期远端字节、落点、目标范围覆盖及写入重叠，按最慢的端点服务与网络路径决定流水阶段间隔，并为每段预留发送和接收缓冲。`slot_cycles` 和 `slots_per_period` 为旧配置兼容字段，现行算法用服务、路径和缓冲计算时隙。

运行时收齐本轮成员后，CommUnit 给对齐与启动留出配置的时间，再选择核与 NoC 可共同表示的时刻作为起点。各事件在其时钟域用“共同起点 + 相对周期”确定实际时刻。某个成员晚到会推迟共同起点。同一配置可在前一轮排空后复用。若不同路由、布局或流量需要不同配置，则分配不同 `opcode`。错过读写服务上界时报告故障。实际硬件寄存器格式和线上位布局待定。[时隙编译器](models/noc/compiler.py) · [执行器](models/noc/unit.py)

### 2.3 核与 NoC 接口

Tile 通信采用一套公共合同，定义在 `runtime/communication.py`。核准备源版本、目标空间和端口服务，CommUnit 按已装载的路由和时隙完成通信。核、NoC 可分别连接假后端或假核验证，联合仿真复用同一合同。[独立运行方式](docs/DECOUPLING.md)

| 角色 | `comm.submit` 操作数 | 本地完成条件 |
| --- | --- | --- |
| 只发送 | `opcode`、`route_bits`、`send_bytes` | 最后一个 flit 从发送端发出 |
| 只接收 | `opcode`、`expected_receive_bytes` | 收齐预期远端有效字节并写入可见，目标有效范围已覆盖 |
| 同时发送与接收 | 共用 `opcode`，同时提供发送字段与预期接收字节 | 本地发送与接收均完成 |

节点、端点和关联令牌在仿真消息信封中提供。源/目标 Tile 版本由核内指令绑定，偏移与二维布局由编译计划持有。操作数省去地址、偏移与 epoch（通信轮次标识）。本地复制写入计入目标范围覆盖，远端字节数只统计远端数据。

1. 核侧完成准备，发出 `comm.submit` 的同一拍进入 `comm.wait` 的等待状态。提交本身表示就绪。
2. CommUnit 校验提交与已装载配置。收齐本轮参与节点的请求后对齐并执行时隙表。通过内部 `DataAccess` / `DataReply` 逐段读写 TileReg。
3. CommUnit 按上表通知各核完成。收到成功反馈后，核释放通信引用和端口预留，完成 `comm.wait` 并放行依赖数据的指令。发送端本地完成与全网排空分别记录。

当前其他轮占用执行器时返回可重试的拒绝，本地核端口保留请求。错过时隙、写入失败等执行故障会终止相关未完成请求。跨轮并发、同一操作码的在途轮次匹配及故障恢复仍待设计。[接口与消息细节](docs/COMMUNIT.md)

## 3. 其他

### 3.1 SRAM 与 HBM

SRAM 是每核本地地址空间。WSE40 配置每核 384 MiB，读写共享每核 1 TB/s，读写各有 100 ns 启动开销，均可配置。占用端口的时间按数据量除以带宽并向上取整为核周期，启动开销作为可流水的结果延迟。本地访问、远端访问与 HBM（High Bandwidth Memory，高带宽内存）搬运在同一目标核的 SRAM 阶段共享端口。

远端 SRAM 使用 `runtime/memory.py` 的按地址访存合同，带目标核、字节偏移和长度，目标核无需提交配对通信指令。远端读将请求送到目标 SRAM，再返回数据，源核的 load 随后完成 Tile 写回。远端写等目标 SRAM 写入生效并收到确认后结束。使用 Calendar 的核间 Tile 通信由 SharedTileReg 收发。[SRAM 访存合同](docs/COMMUNIT.md#sram-访存合同)

HBM 通过搬运通路与各核 SRAM 交换数据。Decode（逐词元解码）的 KV Cache（键值缓存）先假设驻留在 HBM，行为模型计算搬运时间与带宽竞争。当前配置全片 10 TB/s，单核上限 128 B/核周期。实际在搬运的核按下式共享带宽，参与核变化时重新计算。

```text
每核带宽（B/核周期）= min(单核上限, 全片带宽（B/s）/ 核频率（Hz）/ 实际搬运核数)
```

1.65 GHz 下，40 核均达到单核上限时合计 8.448 TB/s。要验证全片限速，可保持单核上限与负载固定，调低全片带宽。[HBM 配置与示例](examples/decode/README.md)

### 3.2 算例、报告与验证

首个端到端目标是 Decode FlashAttention（FA，注意力算子）的注意力主体。以 Q/K/V（查询、键、值）为入口，前后投影暂缓。参数包括模型尺寸、序列切分核数和 HBM 带宽。用单核参考结果核对多核合并的数值，再按实际事件生成计算、搬运和通信流水图。目前归档的矩阵乘和两核稠密 FA capture 可验证核内回放。两份记录都没有跨核通信，FA 记录也没有显式 HBM 搬运。覆盖全链路需要相应 PTO capture 和通信配置。[算例归档](examples/README.md)

`total_cycles` 是从周期 0 起，指令、控制节点、退休、资源释放和在途消息全部处理完的最后周期编号。报告用 `last_instruction_complete_cycle` 记录最后一条非控制指令完成时刻，用 `last_node_complete_cycle` 记录所有节点（含控制节点）的完成时刻，并给出资源服务区间、端点缓冲和存储峰值。可用 `instruction_events` 或命令行 `--event-log` 导出打点，再由 `analysis/perfetto.py` 输出 Perfetto 时间线。指令从发射到 ROB 收到完成消息可用 `issue` / `rob_complete` 配对。准入、分配、计算和写回各有独立打点，退休可能晚于 `rob_complete`。

配置加上 `"message_log": true` 可查看消息源、目的、端口、发送与接收周期。资源利用率统计服务时间，等待原因可能重叠。没有进展、未来服务事件或在途消息时，Runner 抛出带周期和组件快照的停滞异常。快照中的 ROB 窗口列出退休头、下一条待准入指令。报告的 `rob_windows` 列出容量、当前及峰值占用，并统计每拍补入后窗口已满且仍有指令待准入的周期数。

验证报告保留 capture 来源、拓扑、核映射、时钟、负载尺寸、配置和比较中改动的变量。组件测试覆盖消息延迟、乱序完成与按序退休、分配原子性、消费后释放以及共享资源争用。接口与 HBM 的代表性用例如下。

| 验证项 | 测试条件与检查 |
| --- | --- |
| 核内回放 | 固定归档 capture 与合成时序配置，历史试验只更改 ROB 深度为无限、4 或 16。矩阵乘为 168 条指令/7042 周期，FA 为 320 条指令/2183 周期，三种深度的总周期在这两份 capture 上相同。按测试深度顺序，最大单线程窗口峰值分别为矩阵乘 24、4、16，FA 43、4、16。最终窗口占用均为 0。 |
| 路由与 Calendar | 在 5×8 网格上枚举节点 0、1、8、9 的非空落点集合，检查落点恰好覆盖，并拒绝成环、断开或含非法位对的路由。用节点 0–3 上的四节点广播和汇集检查配置匹配与链路时隙。每个源 512 B、存储访问粒度 32 B、收发缓冲各 64 B。广播只改源偏移，验证同一操作码拒绝不同布局。汇集检查同一有向链路上的 flit 时隙不重叠。 |
| 核间完成 | 在 WSE40 配置下从 0 号核经单条链路发 128 B 给 1 号核，存储访问粒度 32 B、收发缓冲各 64 B。核对三个 flit、请求字段及收发完成时刻。 |
| HBM 带宽 | 两核同时各将 256 B 从 HBM 搬到本地 SRAM，固定时钟、负载与单核上限，仅把全片带宽从 10 TB/s 改到 256 GB/s。两核并行，HBM 阶段由 2 核周期增至 4 核周期。 |

[核内组件测试](tests/test_components.py) · [ROB 窗口测试](tests/test_rob_window.py) · [路由与时隙测试](tests/test_noc_planner.py) · [128 B 通信测试](tests/test_streaming_communit.py) · [HBM 与 Decode 测试](tests/test_decode.py) · [接口历史验证](docs/INTERFACE_VALIDATION.md)

### 3.3 适用范围与待确认事项

- 本项目在现有 `wse-perf-sim` 上做行为级建模，运行入口使用 Python 3.11+ 标准库。标量与地址计算按 Tile 粒度简化，Batcher（数据分发与汇收单元）先以驻留数据替代。Decode 示例、模型与报告代码按职责分开放置。[工程结构](docs/STRUCTURE.md)
- WSE 白皮书 v0.3 给出了 4 个裸片、每个 34 个计算核和 2 个 Batcher、每核本地 SRAM 约 100 MB 的架构方案。WSE40 是本项目当前仿真基线。物理核到网格的映射、封装、片间互连与时钟关系需单独确认。
- 白皮书描述预填充完成后的 Decode 流程。预填充在哪运行、注意力与前馈网络的物理部署、实际 KV Cache 布局和程序启动通路仍待明确。核侧 TLSU 到硬件路由器的实际端口、可访问存储、服务时序和缓冲容量仍需核对原始设计与模型源码。
- 示例与历史归档放在 `examples/`，新运行产物写入 `workspace/`。需要发布时按项目约定推送到用户的 fork，并记录改动文件、测试条件和结果。
