# matmul workload

- `workload.py`: original workload generator; requires a pypto build with SSNPU workload support.
- `capture/`: captured workload and lowered IR, replayable with Python standard library only.
- `results/report.json`: original verified simulation result, 168 tile instructions, 7001 cycles.

Run from the repository root:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python3 -m cli examples/matmul/capture --config config/logic-validation.json --output workspace/matmul/report.json
```

To regenerate a capture in an environment with pypto installed:

```bash
python3 examples/matmul/workload.py --output-dir workspace/captures/matmul
```

Both blocks map to independent Cores, each with four PEs. Rates are synthetic logic-validation values; cycle counts are not hardware predictions. Capture location fields preserve provenance from the original machine. Reports include input file hashes and the exact simulation configuration.
