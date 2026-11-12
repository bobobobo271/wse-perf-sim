# 设计合同与建模边界

## 输入适配

适配器读取 manifest、objects、tasks、线程 JSONL 和 events；拒绝不完整 capture、未知 schema、身份/顺序不一致、越界 PE、缺失依赖/生产者、环依赖、共享参与集合不匹配、无效 retention 和 barrier 不匹配。objects 元数据加载并记录哈希；版本 shape/dtype/storage 以指令上的快照为准。当前支持扁平 tile workload，不模拟嵌套任务启动、主机执行或跨 Core TileReg 通信。

依据 `pypto/python/pypto/workload/ssnpu_runtime.py` 的 finalize 合同，order_dependencies 是相邻线程指令链，用于年龄和生命周期关系；不转成执行完成依赖。dependencies（包含 capture 注入的内存重叠依赖）和 sync_dependencies 是完成依赖。每条输入还验证并链接真实存储版本生产者以及 view_producer。未知 order_dependencies 结构明确拒绝。

barrier 以 task/block/round 分组，核对完整参与集合、site 和 completion_dependencies；每轮依赖所有先前操作及上一轮 barrier，延迟后放行下一 phase。空阶段和尾部轮次保留。线程到达 barrier 由其前序操作完成隐式满足，不额外模拟线程解释器开销。task dependencies 连接任务完成节点，跳过的任务视为零工作完成节点。

## 每周期流程

1. 处理最小堆中本周期完成事件，发布 ready、释放存储。
2. 为已发射指令启动后续内部阶段，按进入等待队列的顺序竞争资源。
3. 解析零 cost 的 view / 控制节点；PE 间轮转，PE 内按线程年龄选最老的当前可发射指令，每轮每 PE 至多一条，重复直到额度耗尽或无进展。
4. 记录尚未发射指令的阻塞原因集合，推进一个周期。

仲裁起点每周期轮转，Core 间资源独立。窗口/ROB 无限，可绕过依赖、资源、容量阻塞的旧指令。输出容量在 tile 发射时分配；内部阶段不消耗 tile 发射额度。已发射阶段优先于新 tile 竞争资源。完成事件在周期开始处理，因此允许同周期消费者发射。

统一公式为 `service=max(1,ceil(work/throughput))`、`resource_available=start+service`、`complete=start+service+latency`；服务区间为半开区间。固定延迟可与下一条指令服务重叠。依次读取各共享输入，再执行计算/搬运，再写共享输出，内部阶段顺序串行是明确的初版近似。

支持 matmul、matmul_acc、full、load、store、ssnpu.load_into、exp、cast、row_sum/max/min、常见算术及 row_expand 算术，具体名单见 CostModel.VECTOR。matmul 按操作数逻辑 shape 的 M×N×K MAC；归约按有效行数×(有效宽度−1)；逐元素按输出有效元素数。内存搬运按 memory_accesses 中的范围×dtype 字节数，拒绝无法解释的范围；不是按整个 tensor 大小计费。每个 shared 输入按该消费者有效 tile 字节独立计费，不做广播合并。transpose_view / reshape(_view) / view 为零发射额度别名，不消耗带宽。

## 存储版本

私有版本以 storage_id/version 标识，共享版本以 shared_version 标识；view 解析到源版本，不重复分配。容量使用实际 tile shape，访问工作量可用 valid_shape。预扫描全部生产者与消费者，普通版本在生产完成且全部消费者完成后释放，无消费者输出在生产完成后释放。view 本身及后续消费者均保留源版本。

共享 use_id/operand 分组必须覆盖 participants；last-use 组必须唯一，其他 reuse 需在线程顺序或 barrier 阶段中先于终结组。释放同时等待 last-use 参与者和全部其他消费者。只有 reuse、没有 last-use 的版本作为 live-out 保留，不能静默回收。报告会保留 live bytes。

同物理 storage_id 的后续写入（shared handle 仅表示逻辑身份，不强制复用物理位置）等待前一版本生产者和所有旧读者完成；原位操作可自身读取旧版本，分配时转移同一位置的容量，防止双重计费。旧位置在原位操作发射时转移到新版本，执行期间由新版本继续占用。对同一固定位置跨不同线程、同 phase 的无序写入，若无显式依赖则报错，不推断硬件次序。超容量或保留版本阻塞且没有可推进事件时，报错列出 op、未完成依赖、输出版本和具体容量/固定位置。

## 验证与后续校准

可手算回归覆盖服务吞吐与结果 latency、同周期 ready、乱序绕过、PE/Core 额度、确定性、四 PE Cube/Vector 并行、共享搬运串行、last-use/reuse、别名、多代固定位置、原位覆盖、容量等待恢复/死锁、空阶段/尾部 barrier、任务依赖、异常输入、dtype/path override。真实 capture 的独立审计核对所有指令完成、每条完成依赖、发射上限、资源服务区间互斥、存储事件容量上下界。

默认值全部为合成逻辑测试值，没有借用 SuperScalarModel 的内部微操作吞吐作为 tile 参数。后续校准应记录对应 SuperScalarModel 参数路径/版本、单位换算、tile→内部操作映射和实测依据，再形成独立的硬件配置。当前不模拟多核互连、广播合并、访存缓存驻留、bank 冲突、有限 ROB、计算/搬运内部流水重叠或数值正确性。

## 源码组织

功能模块的职责表见 README。`api.simulate()` 负责组装组件；`trace.TraceAdapter` 生成 `records.Instruction/Version`，`cost.CostModel` 生成 `records.Stage`。`scheduler.Scheduler` 推进周期并协调 `events.EventQueue` 与 `resources.ResourceState`；`report.build_report()` 只读取状态并生成报告。`cli.main()` 处理命令行和文件写入。旧的 `model` 模块作为兼容入口保留。

本次拆分保留现有调度规则、cost 公式、容量与生命周期逻辑、配置格式及报告字段。归档在 examples 下的输入和结果不修改；验证产生的文件写入 workspace。
