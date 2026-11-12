import argparse
import json
from pathlib import Path
from .model import ModelError, simulate

parser = argparse.ArgumentParser(description='SSNPU tile performance model (uncalibrated)')
parser.add_argument('capture_dir')
parser.add_argument('--config')
parser.add_argument('--output', required=True, help='JSON report destination (use workspace/)')
args = parser.parse_args()
try:
    report = simulate(args.capture_dir, args.config)
except ModelError as exc:
    parser.exit(2, f'{exc}\n')
Path(args.output).parent.mkdir(parents=True, exist_ok=True)
Path(args.output).write_text(json.dumps(report, indent=2, allow_nan=False) + '\n')
print(f'{report["instruction_count"]} instructions completed in {report["total_cycles"]} cycles')
