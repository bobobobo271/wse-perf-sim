"""Run shape-based decode timing and export simulator timelines."""
import argparse
from dataclasses import asdict, replace
import json
from pathlib import Path
import sys

from api import simulate
from workloads.decode import DecodeSpec, make_decode_capture
from analysis.pipeline import export_pipeline, export_resource_trace
from analysis.perfetto import to_pftrace, to_perfetto
from frontend.capture import TraceAdapter
from models.noc.protocol import platform_spec


def export_communication(trace, config, output):
    """Keep independently loadable core requests and WSE installation apart."""
    if not trace.calendar_phases:
        return
    directory = output / 'communication'
    directory.mkdir(parents=True, exist_ok=True)
    requests = [i.raw['communication']['request'] for i in trace.instructions.values()
                if i.raw['ir_op'] == 'comm.submit']
    for name, value in (('requests.json', requests), ('installation.json', trace.comm_installation),
                        ('platform.json', platform_spec(config))):
        (directory/name).write_text(json.dumps(value, indent=2, allow_nan=False)+'\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name, value in asdict(DecodeSpec()).items():
        parser.add_argument('--'+name.replace('_','-'), type=type(value), default=value)
    parser.add_argument('--output-dir', type=Path, default=Path('workspace/decode'))
    parser.add_argument('--no-plots', action='store_true')
    args = parser.parse_args()
    spec = DecodeSpec(**{key: getattr(args,key) for key in asdict(DecodeSpec())}).validate()
    output = args.output_dir
    output.mkdir(parents=True, exist_ok=True)
    config, plan, layout = make_decode_capture(output/'capture', spec)
    report = simulate(output/'capture', config, communication_plan=plan)
    trace = TraceAdapter(output/'capture', config, plan)
    export_communication(trace, config, output)
    single_spec = replace(spec, sequence_splits=1)
    sc, sp, _ = make_decode_capture(output/'single_core_capture', single_spec, single_core=True)
    single_report = simulate(output/'single_core_capture', sc, communication_plan=sp)
    cycle_ns = 1e9/config['wse']['core_clock_hz']['value']
    (output/'pipeline.pftrace').write_bytes(to_pftrace(report['instruction_events'], cycle_ns=cycle_ns))
    (output/'pipeline.trace.json').write_text(json.dumps(to_perfetto(report['instruction_events'],cycle_ns=cycle_ns))+'\n')
    (output/'instruction_events.jsonl').write_text(''.join(json.dumps(e)+'\n' for e in report['instruction_events']))
    (output/'hbm_events.jsonl').write_text(''.join(json.dumps(e)+'\n' for e in report['hbm_events']))
    (output/'noc_events.jsonl').write_text(''.join(json.dumps(e)+'\n' for e in report.get('noc',{}).get('events',[])))
    export_resource_trace(report, output)
    summary = dict(spec=asdict(spec), total_cycles=report['total_cycles'], total_time_us=report['total_time_ns']/1000,
                   single_core_time_us=single_report['total_time_ns']/1000,
                   instruction_count=report['instruction_count'], layout=layout,
                   hbm_bytes=report['resources']['chip/hbm']['bytes'],
                   software=dict(python=sys.version),
                   limits=['Synthetic uncalibrated cycle model; fixed equal PE compute partitions.',
                           'Cold KV cache in HBM each invocation; Q copies read separately by workers.',
                           'Automatic Calendar packets/groups conservatively serialized.',
                           'All workers merge; rank 0 writes each head group output.',
                           'No projections, RoPE, KV append, paging or cross-step cache model.'])
    for name, value in (('report.json',report),('single_core_report.json',single_report),
                        ('summary.json',summary),('config.json',config)):
        (output/name).write_text(json.dumps(value,indent=2,allow_nan=False)+'\n')
    if not args.no_plots:
        export_pipeline(report,output)
    print(json.dumps({key:summary[key] for key in ('total_cycles','total_time_us','single_core_time_us','hbm_bytes')},indent=2))


if __name__ == '__main__':
    main()
