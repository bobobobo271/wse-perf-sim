# SSNPU 模型初版验证记录

日期：2026-11-12。Python 3 标准库运行；配置：config/logic-validation.json。

## 结果

- 20 项 unittest 回归通过，完整输出见 regression.log。
- 命令行示例 examples/ooo 回放 3 条指令，6 周期，见 ../ooo/results/report.json。
- 两份真实 capture 均完成，逐指令依赖、发射额度、资源服务区间、存储生命周期和容量审计通过。

| workload | tile 指令数 | 周期 | 每 PE Local 峰值（byte） | 每 Core Shared 峰值（byte） |
|---|---:|---:|---:|---:|
| matmul | 168 | 7001 | 49152 | 32768 |
| fa | 320 | 1910 | 53760 | 32768 |

两个 workload 均为 2 个独立 Core、每 Core 4 PE；对应两个 Core 的完成周期相同。

## 输入与复现

原始输入来自 workspace/ssnpu-workload-design/output/{matmul,fa}，归档位于 examples/{matmul,fa}/capture。真实 capture 包含 19 种不同操作（两个 workload 的并集）。每份 JSON 报告保存读取文件的 SHA256、完整模拟配置、建模假设、全部控制/指令时间线和存储事件。输入副本及原始结果已归档在 examples；重跑产物写入 workspace。

完整重跑命令见 ../../README.md；summary.json 为机器可读汇总，../matmul/results/report.json / ../fa/results/report.json 为详细报告。

接口核对确认：order_dependencies 是线程年龄链，dependencies/sync_dependencies 才约束完成；shared_storage_policy=independent_versions，同 handle 不表示同物理位置。固定位置覆盖按 storage_id 判断。reuse-only 的共享版本保留为 live-out；last-use 等待全部必要读者完成。

## 限制

当前参数全部为合成逻辑验证值，未与 SuperScalarModel 校准。阶段内部顺序串行，未模拟广播合并、有限 ROB、缓存驻留与多 Core 互连。真实 capture 的数值计算结果由负载侧负责，本模型验证调度和时序约束。
