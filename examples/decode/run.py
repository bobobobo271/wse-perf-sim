"""Run parametrized decode, numerical validation and actual simulator timelines."""
import argparse
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import sys

import numpy as np

from ssnpu_model import simulate
from ssnpu_model.decode import DecodeSpec, make_decode_capture
from ssnpu_model.decode_numeric import make_inputs, execute_capture, dense_reference, compare, validate_inputs
from ssnpu_model.decode_plot import export_pipeline, export_resource_trace
from ssnpu_model.perfetto import to_pftrace, to_perfetto
from ssnpu_model.trace import TraceAdapter
from ssnpu_model.communication.protocol import platform_spec


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
    parser.add_argument('--inputs', type=Path, help='NPZ containing FP16 Q[B,Hq,D], K/V[B,Hkv,N,D]')
    parser.add_argument('--output-dir', type=Path, default=Path('workspace/decode'))
    parser.add_argument('--no-plots', action='store_true')
    args = parser.parse_args()
    spec = DecodeSpec(**{key: getattr(args,key) for key in asdict(DecodeSpec())}).validate()
    output = args.output_dir
    output.mkdir(parents=True, exist_ok=True)
    if args.inputs:
        with np.load(args.inputs, allow_pickle=False) as data:
            arrays = {name: data[name] for name in ('Q','K','V')}
    else:
        arrays = make_inputs(spec)
    validate_inputs(spec, arrays)
    np.savez(output/'inputs.npz', **arrays)
    config, plan, layout = make_decode_capture(output/'capture', spec)
    report = simulate(output/'capture', config, communication_plan=plan)
    trace = TraceAdapter(output/'capture', config, plan)
    export_communication(trace, config, output)
    actual = execute_capture(trace, spec, arrays)
    reference_spec = replace(spec, sequence_splits=1)
    sc, sp, _ = make_decode_capture(output/'single_core_capture', reference_spec, single_core=True)
    single_report = simulate(output/'single_core_capture', sc, communication_plan=sp)
    single = execute_capture(TraceAdapter(output/'single_core_capture', sc, sp), reference_spec, arrays)
    dense = dense_reference(spec, arrays)
    validation = dict(multicore_vs_single=compare(actual,single), multicore_vs_dense=compare(actual,dense),
                      single_vs_dense=compare(single,dense),
                      conditions=dict(input_dtype='float16', matmul_accumulation='float32',
                          probability_cast='float16 before PV', state_dtype='float32', dense_reference='float64',
                          seed=spec.seed, same_inputs=True, mask='All supplied KV tokens visible to this decode query',
                          single_core='All batch/head groups serialized on core 0, same PE count, tiles and HBM bandwidth'))
    validation['passed'] = all(validation[key]['passed'] for key in ('multicore_vs_single','multicore_vs_dense','single_vs_dense'))
    np.savez(output/'outputs.npz', multicore=actual,single_core=single,dense_reference=dense)
    cycle_ns = 1e9/config['wse']['core_clock_hz']['value']
    (output/'pipeline.pftrace').write_bytes(to_pftrace(report['instruction_events'], cycle_ns=cycle_ns))
    (output/'pipeline.trace.json').write_text(json.dumps(to_perfetto(report['instruction_events'],cycle_ns=cycle_ns))+'\n')
    (output/'instruction_events.jsonl').write_text(''.join(json.dumps(e)+'\n' for e in report['instruction_events']))
    (output/'hbm_events.jsonl').write_text(''.join(json.dumps(e)+'\n' for e in report['hbm_events']))
    (output/'noc_events.jsonl').write_text(''.join(json.dumps(e)+'\n' for e in report.get('noc',{}).get('events',[])))
    export_resource_trace(report, output)
    summary = dict(spec=asdict(spec), total_cycles=report['total_cycles'], total_time_us=report['total_time_ns']/1000,
                   single_core_time_us=single_report['total_time_ns']/1000,
                   instruction_count=report['instruction_count'], validation=validation, layout=layout,
                   hbm_bytes=report['resources']['chip/hbm']['bytes'],
                   software=dict(python=sys.version, numpy=np.__version__),
                   input_sha256=hashlib.sha256((output/'inputs.npz').read_bytes()).hexdigest(),
                   limits=['Synthetic uncalibrated cycle model; fixed equal PE compute partitions.',
                           'Cold KV cache in HBM each invocation; Q copies read separately by workers.',
                           'Automatic Calendar packets/groups conservatively serialized.',
                           'All workers merge; rank 0 writes each head group output.',
                           'No projections, RoPE, KV append, paging or cross-step cache model.'])
    for name, value in (('report.json',report),('single_core_report.json',single_report),
                        ('validation.json',validation),('summary.json',summary),('config.json',config)):
        (output/name).write_text(json.dumps(value,indent=2,allow_nan=False)+'\n')
    if not args.no_plots:
        export_pipeline(report,output)
    print(json.dumps({key:summary[key] for key in ('total_cycles','total_time_us','single_core_time_us','hbm_bytes','validation')},indent=2))
    if not validation['passed']:
        raise SystemExit('Numerical validation failed; see validation.json')


if __name__ == '__main__':
    main()
