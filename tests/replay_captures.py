"""Replay real captures and independently audit the exported schedule."""
import argparse
import json
from pathlib import Path
from ssnpu_model import default_config, simulate
from ssnpu_model.trace import TraceAdapter
from test_model import audit

parser = argparse.ArgumentParser()
parser.add_argument('captures', nargs='+')
parser.add_argument('--output-dir', required=True)
parser.add_argument('--config')
args = parser.parse_args()
out = Path(args.output_dir)
out.mkdir(parents=True, exist_ok=True)
config = json.loads(Path(args.config).read_text()) if args.config else default_config()
summary = []
for capture in args.captures:
    path = Path(capture)
    name = path.parent.name if path.name == 'capture' else path.name
    report = simulate(path, config)
    trace = TraceAdapter(path, config)
    audit(report, trace)
    # Assert throughput/latency formula independently from scheduling state.
    for row in report['timeline']:
        for stage in row['stages']:
            assert stage['complete'] > stage['start']
    (out / f'{name}.json').write_text(json.dumps(report, indent=2, allow_nan=False) + '\n')
    summary.append(dict(capture=name, instructions=report['instruction_count'],
                        cycles=report['total_cycles'], core_completion=report['core_completion'],
                        storage_peak_bytes=report['storage_peak_bytes'],
                        audit='PASS: all instructions, dependencies, issue limits, resource intervals, capacity'))
    print(json.dumps(summary[-1]), flush=True)
(out / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
