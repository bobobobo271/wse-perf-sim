"""Bind the v1 streaming copy interface to captured producer/consumer versions."""
from config import platform_spec
from dataclasses import asdict
import copy
import hashlib
import json
from pathlib import Path

from errors import ModelError
from runtime.communication import (ReceiveDesc, ReceiveLayout, SendDesc, SendLayout,
    CommRequest, TileBinding)
from runtime.communication import digest
from models.noc.routing import integer, multicast_routes, route_bitmap, route_tree
from records import Instruction
from tiles import nbytes, version_key


def bind(obj, owner):
    return TileBinding(obj['object_id'], obj['version'], obj['memory_space'],
                       (owner.core,) if obj['memory_space'] == 'SharedTileReg' else (owner.core, owner.pe))


def _attach_compiled_plan(trace, plan):
    """One send and one receive at most, using explicit routes and 2D layouts."""
    phases = plan['phases']
    if (not phases or any(not isinstance(p['name'], str) or not p['name'] for p in phases)
            or len({p['name'] for p in phases}) != len(phases)):
        raise ModelError('Provide unique streaming phase names')
    platform = platform_spec(trace.config, **plan['buffers'])
    if digest(platform) != plan['installation']['platform_hash']:
        raise ModelError('Compiled communication platform mismatch; recompile before launch')
    original = list(trace.instructions.values())
    objects = {version_key(o): o for i in original for o in i.raw.get('outputs', []) if o['kind'] == 'tile'}
    for instruction in original:
        instruction.age *= 3 * len(phases) + 1
    requests = []
    previous = set()
    for number, phase in enumerate(phases):
        name, opcode, epoch = (phase[k] for k in ('name', 'opcode', 'epoch'))
        members = tuple(sorted(e['core'] for e in phase['nodes']))
        if len(members) != len(set(members)):
            raise ModelError('One streaming binding per node is required')
        new_ids, targets = set(), {}
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
            request = CommRequest(f'{name}/{node}', (owner.block, owner.raw['thread_id'], owner.id),
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
        previous = new_ids
        objects.update(targets)
    trace.calendar_phases = phases
    trace.comm_platform = platform
    trace.comm_installation = copy.deepcopy(plan['installation'])
    for request in requests:
        request['version'] = trace.comm_installation['version']


def compile_plan(plan, config):
    """Offline workload compilation. The returned JSON artifact is loadable as-is."""
    from models.noc.compiler import compile_calendar, make_installation
    plan = copy.deepcopy(plan)
    if plan.get('schema') != 'wse-communication/stream-v1' or plan.get('non_speculative') is not True:
        raise ModelError('Expected wse-communication/stream-v1 with non_speculative=true; migrate legacy plans')
    platform = platform_spec(config, **plan['buffers'])
    calendars, identities = [], set()
    for phase in plan['phases']:
        identity = phase['opcode'], integer(phase['epoch'], 'epoch')
        if identity[1] >= 1 << platform['network']['epoch_bits'] or identity in identities:
            raise ModelError('Invalid or repeated opcode/epoch within launch')
        identities.add(identity)
        layouts = {}
        for entry in phase['nodes']:
            if entry['core'] in layouts:
                raise ModelError('Duplicate node in communication plan')
            send = receive = None
            if 'send' in entry:
                spec = dict(entry['send'])
                spec.pop('source_version')
                send = SendLayout(**spec)
            if 'receive' in entry:
                spec = dict(entry['receive'])
                target = spec.pop('target')
                spec.setdefault('capacity', nbytes(target))
                spec.setdefault('valid_ranges', ((0, nbytes(target, target['shape'])),))
                receive = ReceiveLayout(**spec)
            layouts[entry['core']] = (send, receive)
        calendars.append(compile_calendar(phase['opcode'], layouts, platform))
    plan.update(schema='wse-communication/compiled-v1', installation=make_installation(calendars, platform))
    return plan


def attach_plan(trace, plan):
    """Load precompiled requests and CalReg before constructing runtime components.

    Source plans are a convenience: compile them once here, before simulation.
    Compiled artifacts bypass the compiler completely.
    """
    if isinstance(plan, (str, Path)):
        data = Path(plan).read_bytes()
        plan = json.loads(data)
    else:
        data = json.dumps(plan, sort_keys=True, allow_nan=False).encode()
        plan = copy.deepcopy(plan)
    if 'wse' not in trace.config:
        raise ModelError('Communication requires a WSE configuration')
    trace.source_hashes['communication_plan'] = hashlib.sha256(data).hexdigest()
    trace.communication_plan = copy.deepcopy(plan)
    if plan.get('schema') != 'wse-communication/compiled-v1':
        plan = compile_plan(plan, trace.config)
    if plan.get('non_speculative') is not True:
        raise ModelError('Communication requires non_speculative=true')
    _attach_compiled_plan(trace, plan)


def operation_id(phase, core, kind):
    return f'comm:{phase}:{core}:{kind}'


def allgather_group(sources, targets, owners, shard_bytes, *, name='allgather', epoch=0, opcode=1):
    """Lower AllGather to one gather and tree broadcasts using the sole v1 ABI.

    Each round has at most one send/receive per participant. No partially
    published output is exposed: gather at the root, then copy the full result.
    """
    integer(shard_bytes, 'shard_bytes', 1)
    members = sorted(sources)
    if not members or set(targets) != set(members) or set(owners) != set(members):
        raise ModelError('AllGather needs matching source/target/owner groups of at least one node')
    root = members[0]
    total = len(members) * shard_bytes
    def send(source, size, offset, bits, local=False):
        return dict(source_version=list(version_key(source)), src_offset=0, row_count=1,
                    row_bytes=size, src_row_stride=size, recv_sym_base='OUTPUT', self_off=offset,
                    recv_row_stride=size, route_bits=bits, self_copy=local)
    def receive(node, remote):
        return dict(target=copy.deepcopy(targets[node]), recv_sym_base='OUTPUT', capacity=total, exp_val=remote)
    nodes = []
    for rank, node in enumerate(members):
        bits = 0 if node == root else multicast_routes(node, [root])[0]
        entry = dict(core=node, owner=owners[node], send=send(sources[node], shard_bytes, rank*shard_bytes, bits, node==root))
        if node == root:
            entry['receive'] = receive(node, total-shard_bytes)
        nodes.append(entry)
    phases = [dict(name=name + ':gather', opcode=opcode, epoch=epoch, nodes=nodes)]
    for index, bits in enumerate(multicast_routes(root, members[1:]) if len(members)>1 else [], 1):
        _, landing, _ = route_tree(root, bits)
        nodes = [dict(core=root, owner=owners[root], send=send(targets[root], total, 0, bits))]
        nodes += [dict(core=node, owner=owners[node], receive=receive(node, total)) for node in sorted(landing)]
        phases.append(dict(name=f'{name}:broadcast{index}', opcode=opcode+index, epoch=epoch, nodes=nodes))
    return dict(schema='wse-communication/stream-v1', non_speculative=True,
                buffers=dict(access_bytes=256, tx_bytes=512, rx_bytes=512), phases=phases)


def allgather_40(sources, row_targets, column_targets, owners, shard_bytes):
    """Compile row and column AllGather groups into the same copy interface."""
    if any(set(m) != set(range(40)) for m in (sources, row_targets, column_targets, owners)):
        raise ModelError('AllGather bindings must cover exactly cores 0..39')
    phases, opcode = [], 1
    for label, source, target, groups, size in (
        ('row', sources, row_targets, [range(r*8, r*8+8) for r in range(5)], shard_bytes),
        ('column', row_targets, column_targets, [range(c, 40, 8) for c in range(8)], 8*shard_bytes)):
        for index, members in enumerate(groups):
            plan = allgather_group({n: source[n] for n in members}, {n: target[n] for n in members},
                                  {n: owners[n] for n in members}, size, name=f'{label}{index}', opcode=opcode)
            phases.extend(plan['phases'])
            opcode += len(plan['phases'])
    return dict(plan, phases=phases)
