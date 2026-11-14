"""Run both copy examples through the sole CommUnit and public v1 contract."""
import argparse
import json
from pathlib import Path

from examples.communication.streaming import run_demo


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('workspace/communit-driver/report.json'))
    args = parser.parse_args()
    report = {mode: run_demo(mode) for mode in ('broadcast', 'gather')}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({mode: {'cycle': r['cycle'], 'drained': r['drained']} for mode, r in report.items()}))
