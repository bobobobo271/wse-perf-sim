# 从 PTO capture 回放 FlashAttention

入口读取 PyPTO 导出的 workload capture v2（SSNPU v1）。在 PTO 侧完成张量形状、分块、指令和核映射，由 `TileInterpreter.run(..., mode="trace_only")` 记录实际指令流，再调用 `write_capture()` 写出目录。时序模型读取 capture 中的形状、数据类型、访存范围和依赖。

在仓库根目录运行。以下命令用已归档的 PyPTO dense FA 验证导入和 trace 导出，配置保留原核模型的访存路径，只加入 WSE 核时钟用于换算时间。

```bash
PYTHONPATH=. python3.11 -S - <<'PY'
import json
from pathlib import Path
from config import default_config, wse_config

config = default_config()
config['wse'] = wse_config()['wse']
path = Path('workspace/pto-fa-config.json')
path.parent.mkdir(exist_ok=True)
path.write_text(json.dumps(config, indent=2) + '\n')
PY

PYTHONPATH=. python3.11 -S examples/decode/run.py examples/fa/capture \
  --config workspace/pto-fa-config.json --no-plots \
  --output-dir workspace/pto-fa-replay
```

跑自己的 decode 时，将位置参数替换为外部工具导出的 capture 目录，将 `--config` 指向对应硬件配置。若存在 WSE 核间通信，传入 `--communication-plan`，使通信端点绑定到这份 capture 中的指令和 Tile 版本。HBM（高带宽内存）与 SRAM 间的显式搬运需要输入记录提供 `memory.copy` 及读写范围，硬件配置提供对应带宽。单核时序对照可通过 `--single-core-capture` 传入单独捕获的单核负载。

原有 8 核与 40 核结果对应历史手写 capture。重跑这两个映射需要相应的 PTO capture，以及覆盖 HBM 搬运和集合通信的输入记录与绑定。当前已归档的 `examples/fa/capture` 是两核 dense FA。

`--no-plots` 使用 Python 标准库生成报告和 Perfetto trace。需要静态图时，先安装 `examples/decode/requirements.txt` 中的 Matplotlib，再去掉 `-S` 和 `--no-plots`。

| 文件 | 内容 |
|---|---|
| `report.json` / `summary.json` | 时序、资源服务、输入文件哈希和结果摘要 |
| `single_core_report.json` | 提供单核 capture 时的时序报告 |
| `pipeline.pftrace` | 指令生命周期和通信等待 |
| `resources.trace.json` | 计算、存储和网络资源占用 |
| `pipeline_overview.png` / `pipeline.png` | 启用绘图时输出的资源流水图 |
| `communication/` | 提供通信计划时输出的核侧请求、网络安装配置和平台参数 |
