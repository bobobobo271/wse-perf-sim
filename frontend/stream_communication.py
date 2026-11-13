"""Bind the v1 streaming copy interface to captured producer/consumer versions."""
from dataclasses import asdict

from errors import ModelError
from models.noc.protocol import (ReceiveDesc, ReceiveLayout, SendDesc, SendLayout,
    StreamRequest, TileBinding, streaming_platform)
from models.noc.stream_plan import compile_stream_round, install_stream_rounds
from records import Instruction
from tiles import nbytes, version_key


def bind(obj, owner):
    return TileBinding(obj['object_id'], obj['version'], obj['memory_space'],
                       (owner.core,) if obj['memory_space'] == 'SharedTileReg' else (owner.core, owner.pe))


def attach_stream_plan(trace, plan):
    """One send and one receive at most, using explicit routes and 2D layouts."""
    phases = plan['phases']
    if (not phases or any(not isinstance(p['name'], str) or not p['name'] for p in phases)
            or len({p['name'] for p in phases}) != len(phases)):
        raise ModelError('Provide unique streaming phase names')
    platform = streaming_platform(trace.config, **plan['buffers'])
    original = list(trace.instructions.values())
    objects = {version_key(o): o for i in original for o in i.raw.get('outputs', []) if o['kind'] == 'tile'}
    for instruction in original:
        instruction.age *= 3 * len(phases) + 1
    rounds, requests = [], []
    previous = set()
    for number, phase in enumerate(phases):
        name, opcode, epoch = (phase[k] for k in ('name', 'opcode', 'epoch'))
        members = tuple(sorted(e['core'] for e in phase['nodes']))
        if len(members) != len(set(members)):
            raise ModelError('One streaming binding per node is required')
        layouts, new_ids, targets = {}, set(), {}
        for entry in phase['nodes']:
            node = entry['core']
            owner = trace.instructions[entry['owner']]
            if owner not in original or owner.control or owner.core != node:
                raise ModelError('Streaming owner must be a capture operation on its node')
            ids = {kind: f'comm:{name}:{node}:{kind}' for kind in ('submit', 'wait')}
            source = target = send = receive = None
            if 'send' in entry:
                spec = dict(entry['send'])
                source = objects.get(tuple(spec.pop('source_version')))
                if source is None:
                    raise ModelError('Streaming source version has no preceding producer')
                if source['memory_space'] not in ('SharedTileReg', 'LocalTileReg', 'TileReg'):
                    raise ModelError('Streaming source must be a tile register object')
                send = SendDesc(bind(source, owner), SendLayout(**spec))
            if 'receive' in entry:
                spec = dict(entry['receive'])
                target = dict(spec.pop('target'), producer=ids['wait'])
                if target.get('kind') != 'tile' or target.get('memory_space') != 'SharedTileReg':
                    raise ModelError('Streaming target must be a SharedTileReg tile')
                key = version_key(target)
                if key in objects or key in targets:
                    raise ModelError('Duplicate streaming target version')
                if source and source.get('storage_id', source['object_id']) == target.get('storage_id', target['object_id']):
                    raise ModelError('Streaming source and destination need distinct storage')
                spec.setdefault('capacity', nbytes(target))
                spec.setdefault('valid_ranges', ((0, nbytes(target, target['shape'])),))
                receive = ReceiveDesc(bind(target, owner), ReceiveLayout(**spec))
                targets[key] = target
                for instruction in original:
                    for operand in instruction.raw.get('inputs', []):
                        if operand['kind'] == 'tile' and version_key(operand) == key:
                            operand['producer'] = ids['wait']
            layouts[node] = (send.layout if send else None, receive.layout if receive else None)
            request = StreamRequest(f'{name}/{node}', (owner.block, owner.raw['thread_id'], owner.id),
                                    opcode, epoch, members, '', send, receive)
            for offset, kind in enumerate(('submit', 'wait'), 1):
                raw = dict(owner.raw, ir_op=f'comm.{kind}', op_id=ids[kind],
                           inputs=[source] if kind == 'submit' and source else [],
                           outputs=[target] if kind == 'wait' and target else [],
                           memory_accesses=[], parameters={}, ssnpu=dict(owner.raw['ssnpu'], shared_inputs=[]),
                           communication=dict(phase=name, token=request.request_id, reserve_for=ids['wait'],
                                              request=asdict(request)))
                requests.append(raw['communication']['request'])
                deps = previous | set(entry.get('after', [])) | ({ids['submit']} if kind == 'wait' else {owner.id})
                trace.add(Instruction(ids[kind], node, owner.pe, owner.task, owner.block,
                                      owner.age + 3 * number + offset, raw, deps=deps, sync=set(owner.sync)))
                new_ids.add(ids[kind])
                for barrier in original:
                    if (barrier.raw['ir_op'] == 'barrier' and barrier.task == owner.task and
                            barrier.block == owner.block and
                            owner.raw['ssnpu']['phase'] <= int(barrier.id.rsplit(':', 1)[1])):
                        barrier.deps.add(ids[kind])
        rounds.append(compile_stream_round(opcode, epoch, layouts, platform))
        previous = new_ids
        objects.update(targets)
    trace.calendar_phases = phases
    trace.comm_platform = platform
    trace.comm_installation = install_stream_rounds(rounds, platform)
    for request in requests:
        request['version'] = trace.comm_installation['version']
