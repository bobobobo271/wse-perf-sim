"""Explicit capture bindings and WSE40 Calendar trees (5 rows x 8 columns).

The tree/row-column construction is migrated from tilesim_wse 3445e99;
this module and the runtime depend only on Python and ssnpu_model.
"""
import copy
import hashlib
import json
import math
from collections import deque
from pathlib import Path

from .errors import ModelError
from .records import Instruction
from .tiles import nbytes, version_key


def integer(value, name, minimum=0):
    if type(value) is not int or value < minimum:
        raise ModelError(f'Invalid {name}: {value!r}')
    return value


def node_id(value):
    if integer(value, 'node') >= 40:
        raise ModelError('WSE40 node must be in 0..39')
    return value


def neighbors(node):
    row, col = divmod(node, 8)
    return tuple(n for n in (node - 8 if row else -1, node + 8 if row < 4 else -1,
                            node - 1 if col else -1, node + 1 if col < 7 else -1) if n >= 0)


def route_bitmap(passing, landing):
    passing, landing = set(passing), set(landing)
    if not landing <= passing:
        raise ModelError('Landing nodes must be passing nodes')
    return sum((3 if n in landing else 2) << (2 * node_id(n)) for n in passing)


def route_tree(source, bits):
    node_id(source)
    if integer(bits, 'route_bits') >= 1 << 80:
        raise ModelError('Route bitmap exceeds 80 bits')
    pairs = [(bits >> (2 * n)) & 3 for n in range(40)]
    active = {n for n, p in enumerate(pairs) if p & 2}
    landing = {n for n, p in enumerate(pairs) if p & 1}
    if 1 in pairs or source not in active or not landing:
        raise ModelError('Illegal route: 01, absent source, or no landing node')
    children, distance, seen = {}, {source: 0}, {source}
    queue = deque([(source, None)])
    while queue:
        node, parent = queue.popleft()
        children[node] = tuple(n for n in neighbors(node) if n in active and n != parent)
        for child in children[node]:
            if child in seen:
                raise ModelError('Route induced subgraph contains a cycle')
            seen.add(child)
            distance[child] = distance[node] + 1
            queue.append((child, node))
    if seen != active:
        raise ModelError('Disconnected route')
    return children, landing, distance


def multicast_routes(source, destinations):
    """Cover destinations once, merging shortest paths into induced trees."""
    node_id(source)
    if not destinations or len(set(destinations)) != len(destinations):
        raise ModelError('Provide unique nonempty destinations')
    for destination in destinations:
        node_id(destination)
    parents, queue = {source: None}, deque([source])
    while queue:
        node = queue.popleft()
        for peer in sorted(neighbors(node)):
            if peer not in parents:
                parents[peer] = node
                queue.append(peer)
    trees = []
    for destination in sorted(destinations):
        path, node = set(), destination
        while node is not None:
            path.add(node)
            node = parents[node]
        for passing, landing in trees:
            union = passing | path
            edges = sum(peer in union for node in union for peer in neighbors(node)) // 2
            # The union of source-rooted paths is connected; n-1 edges proves a tree.
            if edges == len(union) - 1:
                passing.update(path)
                landing.add(destination)
                break
        else:
            trees.append((path, {destination}))
    return [route_bitmap(passing, landing) for passing, landing in trees]


def transmissions(entry, parameters):
    """Normalize explicit single-window and automatically planned multi-tree sends."""
    if 'routes' in entry:
        return entry['routes']
    start = entry['window'] * parameters['slot_cycles']
    return [dict(route_bits=entry['route_bits'], start_cycle=start,
                 end_cycle=start + parameters['slot_cycles'])]


def compile_phase(phase, config):
    """Plan copy traffic in conservative, nonoverlapping whole-packet windows.

    Uses existing NoC and Shared-write service parameters. A packet drains its
    tree and destination writes before the following tree starts. No new hardware
    register format or payload storage is required by this behavioral model.
    """
    if phase.get('routing') != 'auto':
        if 'routing' in phase:
            raise ModelError('Unknown routing mode')
        return
    parameters = {key: value['value'] for key, value in config['wse'].items()}
    payload = parameters['link_bytes_per_cycle'] - parameters['header_bytes']
    participants = {node_id(entry['core']) for entry in phase['nodes']}
    write = config['engines']['shared_write']
    cursor = 0
    for entry in sorted(phase['nodes'], key=lambda item: item['core']):
        if any(field in entry for field in ('routes', 'route_bits', 'window')):
            raise ModelError('Automatic routing takes destinations, not precompiled routes/windows')
        destinations = entry.get('destinations', sorted(participants))
        if not set(destinations) <= participants:
            raise ModelError('Route lands outside registered participants')
        size = integer(entry['nbytes'], 'nbytes', 1)
        flits = math.ceil(size / payload)
        # Two core cycles cover edge rounding and a margin at the visibility boundary.
        service = math.ceil(size / write['throughput']) + write['latency'] + 2
        write_cycles = math.ceil(service * parameters['noc_clock_hz'] / parameters['core_clock_hz'])
        entry['routes'] = []
        for bits in multicast_routes(entry['core'], destinations):
            _, _, distance = route_tree(entry['core'], bits)
            duration = (parameters['tx_cdc_cycles'] + flits + max(distance.values()) * parameters['hop_cycles']
                        + parameters['rx_cdc_cycles'] + write_cycles + parameters['guard_cycles'])
            entry['routes'].append(dict(route_bits=bits, start_cycle=cursor, end_cycle=cursor + duration))
            cursor += duration


def allgather_group(sources, targets, owners, shard_bytes, *, name='allgather', epoch=0):
    """Build an automatically routed AllGather for any group on the WSE40 mesh."""
    integer(shard_bytes, 'shard_bytes', 1)
    members = sorted(sources)
    if not members or set(targets) != set(members) or set(owners) != set(members):
        raise ModelError('AllGather source, target and owner bindings must cover the same group')
    nodes = [dict(core=core, owner=owners[core], source_version=list(version_key(sources[core])),
                  target=copy.deepcopy(targets[core]), source_offset=0,
                  destination_offset=rank * shard_bytes, nbytes=shard_bytes)
             for rank, core in enumerate(members)]
    return dict(schema='wse-communication/1', non_speculative=True,
                phases=[dict(name=name, opcode=1, epoch=epoch, routing='auto', nodes=nodes)])


def operation_id(phase, core, kind):
    return f'calendar:{phase}:{core}:{kind}'


def attach_plan(trace, plan):
    """Insert three semantic commands per node: post, send/drain, await/publish.

    Alignment is performed by the NoC once all descriptors/data are staged and
    all receive ports admitted. Post completes independently from await.
    """
    if isinstance(plan, (str, Path)):
        data = Path(plan).read_bytes()
        plan = json.loads(data)
    else:
        data = json.dumps(plan, sort_keys=True, allow_nan=False).encode()
        plan = copy.deepcopy(plan)
    if 'wse' not in trace.config:
        raise ModelError('Communication requires a WSE configuration')
    if plan.get('schema') != 'wse-communication/1' or plan.get('non_speculative') is not True:
        raise ModelError('Expected explicit wse-communication/1 non_speculative=true plan')
    phases = plan['phases']
    if not phases or len({p['name'] for p in phases}) != len(phases):
        raise ModelError('Provide unique nonempty communication phases')
    trace.source_hashes['communication_plan'] = hashlib.sha256(data).hexdigest()
    trace.communication_plan = copy.deepcopy(plan)
    objects = {version_key(o): o for i in trace.instructions.values() for o in i.raw.get('outputs', [])
               if o['kind'] == 'tile'}
    original = list(trace.instructions.values())
    for i in original:
        i.age *= 3 * len(phases) + 1
    previous, epochs = set(), {}
    for number, phase in enumerate(phases):
        name, entries = phase['name'], phase['nodes']
        if not isinstance(name, str) or not name:
            raise ModelError('Invalid phase name')
        if phase['opcode'] != 1:
            raise ModelError('First version supports AllGather/copy opcode 1, not reduction')
        epoch = integer(phase['epoch'], 'epoch')
        modulo = 1 << trace.config['wse']['epoch_bits']['value']
        if epoch >= modulo or (1 in epochs and epoch != (epochs[1] + 1) % modulo):
            raise ModelError('Invalid or nonsequential Calendar epoch')
        epochs[1] = epoch
        if any(i.core not in range(40) for i in original):
            raise ModelError('Capture core mapping lies outside WSE40')
        if not entries or len({e['core'] for e in entries}) != len(entries):
            raise ModelError('One binding per participating node is required')
        participants = {node_id(e['core']) for e in entries}
        compile_phase(phase, trace.config)
        parameters = {k: v['value'] for k, v in trace.config['wse'].items()}
        expected = {core: [] for core in participants}
        for e in entries:
            owner = trace.instructions[e['owner']]
            if owner not in original or owner.control or owner.core != e['core']:
                raise ModelError('Communication owner must reference a capture operation on this core')
            if phase.get('routing') != 'auto':
                if 'routes' in e:
                    raise ModelError('Multi-tree routes are generated by routing=auto')
                integer(e['window'], 'window')
                if e['window'] >= trace.config['wse']['slots_per_period']['value']:
                    raise ModelError('Window outside Calendar period')
            size = integer(e['nbytes'], 'nbytes', 1)
            integer(e.get('source_offset', 0), 'source_offset')
            off = integer(e['destination_offset'], 'destination_offset')
            landing = set()
            for transmission in transmissions(e, parameters):
                _, targets, _ = route_tree(e['core'], transmission['route_bits'])
                if landing & targets:
                    raise ModelError('Duplicate landing across route rounds')
                landing.update(targets)
            if not landing <= participants:
                raise ModelError('Route lands outside registered participants')
            for node in landing:
                expected[node].append(dict(source=e['core'], offset=off, nbytes=size))
        new_ids = set()
        targets = {}
        for e in entries:
            target = copy.deepcopy(e['target'])
            if target.get('kind') != 'tile' or target.get('memory_space') != 'SharedTileReg':
                raise ModelError('NoC targets must be SharedTileReg objects')
            key = version_key(target)
            if key in objects or key in targets:
                raise ModelError(f'Duplicate target version producer: {key}')
            intervals = sorted((w['offset'], w['offset'] + w['nbytes']) for w in expected[e['core']])
            cursor = 0
            for start, end in intervals:
                if start != cursor:
                    raise ModelError('Receive ranges overlap or have a hole')
                cursor = end
            if cursor != nbytes(target, target['shape']) or nbytes(target) != cursor:
                raise ModelError('Receive coverage must exactly fill the target valid/allocation shape')
            targets[key] = target
        # A source can only be a capture output or a preceding phase's output.
        for e in entries:
            source = copy.deepcopy(objects[tuple(e['source_version'])])
            target_object = e['target']
            if source.get('storage_id', source['object_id']) == target_object.get('storage_id', target_object['object_id']):
                raise ModelError('First-version Calendar requires distinct source and destination storage slots')
            if source['memory_space'] != 'SharedTileReg':
                raise ModelError('NoC sources must be SharedTileReg objects')
            if e.get('source_offset', 0) + e['nbytes'] > nbytes(source):
                raise ModelError('Source range exceeds valid Tile bytes')
            owner = trace.instructions[e['owner']]
            ids = {kind: operation_id(name, e['core'], kind) for kind in ('post', 'send', 'receive')}
            target = targets[version_key(e['target'])]
            target['producer'] = ids['receive']
            e['target'] = target
            e['expected'] = expected[e['core']]
            for offset, kind in enumerate(('post', 'send', 'receive'), 1):
                raw = dict(owner.raw, ir_op='calendar.' + kind, op_id=ids[kind],
                           inputs=[source] if kind == 'send' else [],
                           outputs=[target] if kind == 'receive' else [],
                           memory_accesses=[], parameters={},
                           ssnpu=dict(owner.raw['ssnpu'], shared_inputs=[]),
                           calendar=dict(phase=name, core=e['core'], reserve_for=ids['receive'], nbytes=e['nbytes']))
                deps = previous | set(e.get('after', []))
                if kind != 'post':
                    deps = deps | {ids['post']}
                trace.add(Instruction(ids[kind], owner.core, owner.pe, owner.task, owner.block,
                                      owner.age + number * 3 + offset, raw, deps=deps, sync=set(owner.sync)))
                new_ids.add(ids[kind])
                for barrier in original:
                    if (barrier.raw['ir_op'] == 'barrier' and barrier.task == owner.task and
                            barrier.block == owner.block and
                            owner.raw['ssnpu']['phase'] <= int(barrier.id.rsplit(':', 1)[1])):
                        barrier.deps.add(ids[kind])
            # Exact version matching supplies the receive producer, not a guessed dependency.
            for i in original:
                for obj in i.raw.get('inputs', []):
                    if obj['kind'] == 'tile' and version_key(obj) == version_key(target):
                        obj['producer'] = ids['receive']
        objects.update(targets)
        previous = new_ids
    trace.calendar_phases = phases


def allgather_40(sources, row_targets, column_targets, owners, shard_bytes):
    """Two phases over all 40 nodes; object identities come from the caller."""
    integer(shard_bytes, 'shard_bytes', 1)
    if any(set(m) != set(range(40)) for m in (sources, row_targets, column_targets, owners)):
        raise ModelError('AllGather bindings must cover exactly cores 0..39')
    phases = []
    for name, source_objects, targets, size, epoch in (
            ('row', sources, row_targets, shard_bytes, 0),
            ('column', row_targets, column_targets, 8 * shard_bytes, 1)):
        nodes = []
        for core in range(40):
            row, column = divmod(core, 8)
            group = list(range(row * 8, row * 8 + 8)) if name == 'row' else list(range(column, 40, 8))
            slot = column if name == 'row' else row
            nodes.append(dict(core=core, owner=owners[core], source_version=list(version_key(source_objects[core])),
                              target=copy.deepcopy(targets[core]), route_bits=route_bitmap(group, group),
                              window=slot, source_offset=0, destination_offset=slot * size, nbytes=size))
        phases.append(dict(name=name, opcode=1, epoch=epoch, nodes=nodes))
    return dict(schema='wse-communication/1', non_speculative=True, phases=phases)
