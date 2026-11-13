# SSM 参数校准：第一轮

目录迁移说明：下文保留原实验的目录和命令记录。当前仓库脚本位于 `benchmarks/ssm_baseline.py`、`benchmarks/calibrate_ssm.py`，仿真包名为 `wse_perf_sim`；在仓库根目录设置 `PYTHONPATH=.` 后运行。见 [项目结构](../STRUCTURE.md)。

2026-11-13。基于 [SSM 完整运行结果](ssm-native-results.md) 做经验校准，只修改新配置中的 dtype 吞吐/延迟参数，不改模型实现、消息协议或原始 `logic-validation.json`。

当前数据的总跨度确实可以拟合接近。两份可选配置如下：

- [`ssm-span-calibrated.json`](../../config/ssm-span-calibrated.json)：优先复现当前负载的总跨度。
- [`ssm-stage-calibrated.json`](../../config/ssm-stage-calibrated.json)：同时约束总跨度、输入就绪、计算窗口和 store 窗口，用作分阶段核对。

## 结果

下表均为 load→计算→store 的时间跨度；Python 使用 first issue→last complete，SSM 使用首个 TLOAD 的 LIQ 起点→最后 store 响应。起止差异被折入有效参数，本轮是对这个观测约定的拟合，不是严格同边界的硬件精度验证。

| 用例 | SSM | 校准前 | 总跨度拟合 | 分阶段约束拟合 |
|---|---:|---:|---:|---:|
| matmul M16/N32/K32 | 201 | 65 | 201 | 206 |
| matmul M32/N64/K64 | 470 | 153 | 470 | 471 |
| FP32 add 16×16 | 113 | 52 | 113 | 98 |
| FP32 exp 16×16 | 118 | 51 | 118 | 109 |
| FP32 load 16×16 | 81 | 34 | 80 | 66 |
| FP32 store 16×16 | 81 | 34 | 80 | 66 |
| FP32 row_sum 16×16 | 93 | 接口失败 | 接口失败 | 接口失败 |

| 指标 | 校准前 | 总跨度拟合 | 分阶段约束拟合 |
|---|---:|---:|---:|
| 总跨度平均绝对百分比误差 | 60.78% | **0.25%** | **8.42%** |
| 总跨度最大绝对百分比误差 | 67.66% | 1.23% | 18.52% |
| 分阶段平均绝对百分比误差 | 59.01% | 28.93% | 17.93% |
| 分阶段最大绝对百分比误差 | 82.14% | 58.33% | 44.64% |

统计对 load/store 相同负载去重，只计 5 种独立负载；没有独立留出集，以上都是训练数据上的拟合误差。row_sum 的 scratch 声明 `tile.create` 仍不被性能模型支持，未参与拟合，reduce 参数保持原值。

## 有效参数

每格为 `throughput / latency`；吞吐单位见第一列，延迟单位为 cycle。只通过 dtype_overrides 应用到所列 dtype，其他 dtype 的基础成本保持原值。

| 单元 / dtype / 吞吐单位 | 校准前 | 总跨度拟合 | 分阶段约束拟合 |
|---|---:|---:|---:|
| memory / FP32 / byte/cycle | 256 / 8 | 256 / 31 | 143.720 / 20 |
| memory / FP16 / byte/cycle | 256 / 8 | 143.720 / 51 | 110.703 / 46 |
| Cube / FP16 / MAC/cycle | 4096 / 4 | 627.139 / 40 | 809.087 / 54 |
| Vector / FP32 / element/cycle | 256 / 2 | 256 / 17 | 256 / 12 |
| exp / FP32 / element/cycle | 64 / 4 | 64 / 25 | 64 / 30 |

保持 1 PE、无限 ROB、issue width、容量及每条消息下一周期交付不变。Vector/exp 只有一种 shape，吞吐和固定延迟无法分别辨识，因此固定其吞吐，仅拟合延迟。小数吞吐是 `ceil(work/throughput)` 服务周期的等价代表，不表示带宽测量具有这些小数位的精度。

## 拟合方法与限制

使用 8 个参数、确定性多起点坐标搜索。总跨度目标为各独立用例的相对误差平方和，平局时选择分阶段误差较小的解。分阶段版本同时加入输入就绪、计算窗口、store 窗口，相对误差平方权重为总跨度 2、每阶段 1。搜索边界、seed、评价次数、输入哈希及完整配置均保留在产物中；不宣称找到了全局最优解。

固定指令链可精确简化为吞吐服务周期、固定延迟和消息握手成本；先用原始参数和 4 组确定性随机参数，对 5 种负载做 25 次真实 Simulator 回放，验证简化公式。最终两份配置及原配置均对全部 7 例各回放两次，成功项完整报告一致，失败项稳定复现同一错误。

主要剩余差异是 **FP32 load/store 不对称**：SSM 的 1 KiB copy 中，load=56、store=24 周期；现有 Python 对同 dtype 的读写共用 memory 成本，两阶段必然相同。总跨度拟合得到 38+4+38=80，虽然接近 81，但内部读写误差抵消。其参数也会在 Cube、输入准备和 store 之间分配误差：例如大 matmul 的总跨度为 470，但计算窗口为 256，SSM 为 224。

因此，当前配置适合复现这批单 PE、小 tile 的观测结果。若要把校准用于其他尺寸、长循环或流水重叠，下一步应加入独立 shape/链长验证，并将 load/store 的成本分开；先不要用总跨度的 0.25% 拟合误差宣称内部流水或其他负载也有相同精度。

## 使用与复现

从 hw-native-sys 根目录回放大 matmul：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=ssnpu-model .venv-ir/bin/python -m ssnpu_model \
  workspace/ssm-baseline/20261113-unlimited/tmatmul_fp16_32x64x64/capture \
  --config ssnpu-model/config/ssm-span-calibrated.json \
  --output workspace/ssm-calibration/check-large.json
```

这份配置的大 matmul tile 跨度为 470，总运行周期为 477；后者额外包含首条 issue 前的时间和退休/消息收尾。

重跑拟合，不需要重跑 SSM 或安装优化库，输出目录须不存在：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=ssnpu-model .venv-ir/bin/python ssnpu-model/calibrate_ssm.py \
  --baseline workspace/ssm-baseline/20261113-unlimited \
  --output workspace/ssm-calibration/NEW-RUN-ID
```

本轮产物为 `workspace/ssm-calibration/20261113-calibrated/`：`results.json/csv`、配置、输入哈希、原始工作区 diff、逐例双次报告、事件 JSONL 和 Perfetto 时间线。原始 capture 未改写，SSM 完整 ELF 周期未作为拟合目标。
