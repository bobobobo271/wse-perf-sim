# SSM 基础 benchmark 完整运行结果

目录迁移说明：下文保留原实验的目录和命令记录。当前仓库脚本位于 `benchmarks/ssm_baseline.py`、`benchmarks/calibrate_ssm.py`，仿真包名为 `wse_perf_sim`；在仓库根目录设置 `PYTHONPATH=.` 后运行。见 [项目结构](../STRUCTURE.md)。

2026-11-13：此前选定的 7 个 SSM 预编译微基准已全部完整运行。每例执行两次 gfrun 和两次 gfsim，全部正常结束，功能执行 R2=0，原生周期和 tile 驻留区间两次均一致。运行没有宿主时间限制（timeout=null）。

| 用例 | 动态 tile 指令数 | 完整 ELF 周期 | tile 驻留周期 |
|---|---:|---:|---:|
| FP16 matmul M16/N32/K32 | 4 | 137845 | 201 |
| FP16 matmul M32/N64/K64 | 4 | 1005830 | 470 |
| FP32 add 16×16 | 4 | 14869 | 113 |
| FP32 exp 16×16 | 3 | 14876 | 118 |
| FP32 row_sum 16×16 | 3 | 14840 | 93 |
| FP32 load 16×16 | 2 | 13996 | 81 |
| FP32 store 16×16 | 2 | 13996 | 81 |

此前超时的 M32/N64/K64 matmul 现已完整结束，耗时 671.85 / 675.17 秒（宿主运行含时间线输出，两次重复并行）。先前的 120 秒限制截断了仍在推进的程序；tile 输出 store 在第 172771 周期已完成，完整 ELF 此后还执行标量 reference_matmul 和结果检查。

完整 ELF 周期取 gfsim 的 Total Cycles；tile 驻留周期取首个 TLOAD 生命周期起点至最后 TSTORE 完成。后者包含准备 load 和输出 store，并非单条计算指令延迟。load/store 两个命名用例实际都执行 load→store。

配置固定：1 Core、1 PE、1 线程，seed=1，泳道 counters 每 8 周期采样；tile 操作事件完整保留。gfrun 也显式设置单线程。SSM HEAD 为 637bde0ec63955c4796828b297e16a057efad3cc，二进制与上一轮一致，未改 SSM 实现。Vector/Memory ELF 的 R2=0 表示功能执行结束，不代表额外启用了数值检查。

复现（hw-native-sys 根目录，输出目录须不存在）：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=pypto/python:ssnpu-model \
  .venv-ir/bin/python ssnpu-model/ssm_baseline.py \
  --ssm-root /home/lc/superscalarNPU/SuperScalarModel \
  --bench-root /home/lc/superscalarNPU/SuperNPUBench \
  --timeout 0 --repeat-workers 2 \
  --output workspace/ssm-baseline/NEW-RUN-ID
```

完整命令、退出码、ELF/二进制哈希、配置、工作区状态及原始日志保存在 workspace/ssm-baseline/20261113-unlimited/；JSON/CSV 原生结果为 native-results.json/csv，逐操作时间线为各用例的 gfsim-*.tile-events.json，完整泳道为 gfsim-*.swimlane.json。

本页只汇报 SSM 完整运行结果；Python 性能模型的 row_sum scratch 接口缺口仍在，不影响上述 SSM 执行。
