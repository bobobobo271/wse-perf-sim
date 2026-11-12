# fa workload

- `workload.py`: original workload generator; requires a pypto build with SSNPU workload support.
- `capture/`: captured workload and lowered IR, replayable with Python standard library only.
- `results/report.json`: original verified simulation result, 320 tile instructions, 1910 cycles.

Run from the repository root:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python3 -m ssnpu_model examples/fa/capture --config config/logic-validation.json --output workspace/fa/report.json
```

To regenerate a capture in an environment with pypto installed:

```bash
python3 examples/fa/workload.py --output-dir workspace/captures/fa
```

Both blocks map to independent Cores, each with four PEs. Rates are synthetic logic-validation values; cycle counts are not hardware predictions. Capture location fields preserve provenance from the original machine. Reports include input file hashes and the exact simulation configuration.
