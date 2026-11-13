"""Generate a synthetic capture v2 + explicit Calendar plan under workspace/.

40 active cores, PE 0 per core: produce/load D bytes, row+column AllGather,
then a local vector consumer of the 40D-byte result. This is not a PyPTO export;
it exercises the same capture importer. Archived PyPTO examples are unchanged.
"""
import argparse
import json
from pathlib import Path

from wse_perf_sim import simulate
from wse_perf_sim.config import wse_config
from wse_perf_sim.frontend.communication import allgather_40


def tile(name, size, shared=True, producer=None):
    obj = dict(object_id=name, storage_id=name, version=0, kind='tile', shape=[1, size],
               valid_shape=[1, size], dtype='uint8', producer=producer,
               memory_space='SharedTileReg' if shared else 'LocalTileReg')
    if shared:
        obj['shared_version'] = name + ':0'
    return obj


def make_capture(directory, shard_bytes=256, *, sram=False):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    sources = {c: tile(f'source:{c}', shard_bytes, producer=f'produce:{c}') for c in range(40)}
    rows = {c: tile(f'row:{c}', 8 * shard_bytes) for c in range(40)}
    columns = {c: tile(f'column:{c}', 40 * shard_bytes) for c in range(40)}
    owners = {c: f'produce:{c}' for c in range(40)}
    instances = []
    for core in range(40):
        identity = dict(task_id=0, block_id=core, subblock_id=0, thread_id=core)
        common = dict(identity, parameters={}, dependencies=[], sync_dependencies=[], memory_accesses=[],
                      ssnpu=dict(phase=0, participants=[core], shared_inputs=[]))
        first = dict(common, op_id=owners[core], ir_op='tile.full', inputs=[], outputs=[sources[core]],
                     instance_op_sequence=0, order_dependencies=[])
        if sram:
            memory = dict(object_id=f'memory:{core}', kind='tensor', shape=[1, shard_bytes],
                          dtype='uint8', memory_space='SRAM')
            first.update(ir_op='tile.load', inputs=[memory],
                         memory_accesses=[dict(object_id=memory['object_id'], shape=[1, shard_bytes], offset_bytes=0)])
        second = dict(common, op_id=f'consume:{core}', ir_op='tile.adds', inputs=[columns[core]],
                      outputs=[tile(f'result:{core}', 40 * shard_bytes, False, f'consume:{core}')],
                      instance_op_sequence=1, order_dependencies=[owners[core]])
        filename = f'core_{core}.jsonl'
        (directory / filename).write_text(''.join(json.dumps(r) + '\n' for r in (first, second)))
        instances.append(dict(identity, mapping=dict(core_id=core, pe_id=0), operation_count=2,
                              status='completed', trace_file=filename))
    (directory / 'events.jsonl').write_text('')
    (directory / 'tasks.json').write_text(json.dumps([dict(task_id=0, dependencies=[], status='completed')]))
    (directory / 'objects.json').write_text(json.dumps(list(sources.values()) + list(rows.values()) + list(columns.values())))
    (directory / 'manifest.json').write_text(json.dumps(dict(format_version=2, ssnpu_spec_version=1, complete=True,
        instances=instances, objects_file='objects.json', tasks_file='tasks.json', events_file='events.jsonl')))
    plan = allgather_40(sources, rows, columns, owners, shard_bytes)
    (directory / 'communication.json').write_text(json.dumps(plan, indent=2) + '\n')
    return plan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', default='workspace/wse40')
    parser.add_argument('--shard-bytes', type=int, default=256)
    parser.add_argument('--sram', action='store_true')
    args = parser.parse_args()
    output = Path(args.output_dir)
    plan = make_capture(output / 'capture', args.shard_bytes, sram=args.sram)
    report = simulate(output / 'capture', wse_config(), communication_plan=plan)
    (output / 'report.json').write_text(json.dumps(report, indent=2, allow_nan=False) + '\n')
    print(json.dumps(dict(total_cycles=report['total_cycles'], total_time_ns=report['total_time_ns'],
                         instructions=report['instruction_count'], phases=report['noc']['phases'])))


if __name__ == '__main__':
    main()
