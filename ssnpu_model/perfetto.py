"""Convert instruction JSONL to hierarchical Perfetto protobuf or legacy JSON.

Run: python -m ssnpu_model.perfetto events.jsonl --output trace.pftrace
Native tracks nest Core -> PE -> execution unit -> in-flight commands.
"""
import argparse
import heapq
import json
import math
import struct
from collections import defaultdict
from pathlib import Path

from .errors import ModelError


PHASES = (
    ('operand access', 'received', 'operands_ready'),
    ('resource wait', 'operands_ready', 'compute_start'),
    ('compute (service + result latency)', 'compute_start', 'compute_end'),
    ('writeback', 'writeback_start', 'writeback_end'),
)
CALENDAR_PHASES = {
    'calendar.post': (('receive registration', 'received', 'complete'),),
    'calendar.send': (('source access', 'received', 'operands_ready'),
                      ('Calendar wait + TX drain', 'operands_ready', 'complete')),
    'calendar.receive': (('receive wait + publication', 'received', 'complete'),),
}
IDENTITY = ('op_id', 'ir_op', 'core', 'pe', 'execution_component')


def to_perfetto(events, cycle_ns=1.0):
    """Build a trace without changing events; require complete instruction logs.

    Each Core is a process. Execution tracks cover received -> complete.
    ROB notification/retirement and Scheduler milestones use separate tracks.
    Subtracks prevent overlapping instructions from becoming invalid nested
    synchronous slices. Raw milestone events are retained as thread instants.
    """
    if isinstance(cycle_ns, bool) or not isinstance(cycle_ns, (int, float)) or not math.isfinite(cycle_ns) or cycle_ns <= 0:
        raise ModelError('cycle_ns must be finite and positive')
    instructions = {}
    for index, event in enumerate(events, 1):
        if not isinstance(event, dict) or any(k not in event for k in (*IDENTITY, 'event', 'cycle')):
            raise ModelError(f'Incomplete milestone record {index}')
        key, cycle, name = event['op_id'], event['cycle'], event['event']
        if type(key) not in (int, str) or not isinstance(name, str) or not name:
            raise ModelError(f'Invalid instruction/event identity at record {index}')
        if type(cycle) is not int or cycle < 0 or any(type(event[k]) is not int or event[k] < 0 for k in ('core', 'pe')):
            raise ModelError(f'Invalid cycle/core/pe at record {index}')
        if not isinstance(event['execution_component'], str) or not event['execution_component']:
            raise ModelError(f'Invalid execution component at record {index}')
        instruction = instructions.setdefault(key, {'identity': event, 'times': {}, 'events': []})
        if any(event[k] != instruction['identity'][k] for k in IDENTITY):
            raise ModelError(f'Conflicting metadata for instruction {key}')
        if name in instruction['times']:
            raise ModelError(f'Duplicate {name} for instruction {key}')
        instruction['times'][name] = cycle
        instruction['events'].append(event)
    groups = defaultdict(list)
    for key, instruction in instructions.items():
        times, identity = instruction['times'], instruction['identity']
        for name in ('issue', 'complete', 'rob_complete', 'retire'):
            if name not in times:
                raise ModelError(f'Missing {name} for instruction {key}')
        optional = ('received', 'operands_ready', 'compute_start', 'compute_end', 'writeback_start', 'writeback_end')
        phase_group = optional
        if identity['ir_op'] in CALENDAR_PHASES:
            phase_group = ('received', 'operands_ready') if identity['ir_op'] == 'calendar.send' else ('received',)
            if not all(k in times for k in phase_group):
                raise ModelError(f'Incomplete Calendar timestamps for instruction {key}')
        for group in (phase_group, ('allocation_request', 'allocated')):
            if any(k in times for k in group) and not all(k in times for k in group):
                raise ModelError(f'Incomplete phase timestamps for instruction {key}')
        order = ('allocation_request', 'allocated', 'issue', *optional, 'complete', 'rob_complete', 'retire')
        ordered = [times[k] for k in order if k in times]
        if ordered != sorted(ordered):
            raise ModelError(f'Invalid lifecycle order for instruction {key}')
        instruction['tracks'] = {}
        groups[identity['core'], identity['execution_component'], 'execution'].append(instruction)
        groups[identity['core'], f'core:{identity["core"]}/rob', 'rob'].append(instruction)
    trace = []
    for core in sorted({core for core, _, _ in groups}):
        trace.append(dict(ph='M', name='process_name', pid=core+1, args={'name': f'Core {core}'}))
        trace.append(dict(ph='M', name='process_sort_index', pid=core+1, args={'sort_index': core}))
    next_tid = 1

    def new_track(core, name, path):
        nonlocal next_tid
        tid, next_tid = next_tid, next_tid + 1
        trace.append(dict(ph='M', name='thread_name', pid=core+1, tid=tid, args={'name': name, 'path': path}))
        trace.append(dict(ph='M', name='thread_sort_index', pid=core+1, tid=tid, args={'sort_index': tid}))
        return tid

    for (core, unit, role), commands in sorted(groups.items()):
        active, free, count = [], [], 0

        def interval(instruction):
            times = instruction['times']
            if role == 'execution':
                # Old control/view logs have no receive event. Preserve their
                # recorded issue timestamp and identify the fallback in args.
                return times.get('received', times['issue']), times['complete']
            return times['complete'], times['retire']

        for instruction in sorted(commands, key=lambda i: (interval(i)[0], str(i['identity']['op_id']))):
            identity, times = instruction['identity'], instruction['times']
            begin, finish = interval(instruction)
            while active and active[0][0] <= begin:
                _, tid = heapq.heappop(active)
                heapq.heappush(free, tid)
            if free:
                tid = heapq.heappop(free)
            else:
                label = 'active' if role == 'execution' else 'completion/retirement'
                components = unit.split('/')[1:]
                names = {'cube': 'Cube', 'vector': 'Vector', 'tlsu': 'TLSU', 'tile': 'TileReg', 'sync': 'Sync', 'rob': 'ROB'}
                path = [f'Core {core}'] + [f'PE {part[3:]}' if part.startswith('pe:') else names.get(part, part) for part in components]
                if role == 'execution':
                    path.append('In-flight commands')
                path.append(f'{label} {count}')
                tid = new_track(core, f'{unit} / {label} {count}', path)
                count += 1
            heapq.heappush(active, (finish, tid))
            instruction['tracks'][role] = tid
            args = {k: v for k, v in identity.items() if k not in ('cycle', 'event', 'component')}
            args.update({f'{k}_cycle': v for k, v in times.items()})
            instruction['args'] = args

            def span(name, start, end, category, extra=None):
                trace.append(dict(ph='X', name=name, cat=category, pid=core+1, tid=tid,
                                  ts=start * cycle_ns / 1000, dur=(end-start) * cycle_ns / 1000,
                                  args=args | {'start_cycle': start, 'end_cycle': end} | (extra or {})))

            label = f'#{identity["op_id"]} {identity["ir_op"]}'
            if role == 'execution':
                span(label, begin, finish, 'ssnpu.instruction',
                     {'interval_start_event': 'received' if 'received' in times else 'issue'})
                phases = CALENDAR_PHASES.get(identity['ir_op'], PHASES)
            else:
                span(label + ' / ROB', begin, finish, 'ssnpu.rob')
                phases = (('ROB notification', 'complete', 'rob_complete'),
                          ('retirement wait', 'rob_complete', 'retire'))
            for name, start, end in phases:
                if start in times and end in times and times[end] > times[start]:
                    span(name, times[start], times[end], 'ssnpu.phase')
    frontend = {}
    for _, instruction in sorted(instructions.items(), key=lambda pair: str(pair[0])):
        identity, args = instruction['identity'], instruction['args']
        core, tracks = identity['core'], instruction['tracks']
        for event in instruction['events']:
            if event['event'] in ('rob_complete', 'retire'):
                tid = tracks['rob']
            elif event['event'] in ('allocation_request', 'allocated', 'issue') and event.get('component') != identity['execution_component']:
                component = event.get('component') or f'core:{core}/scheduler'
                if (core, component) not in frontend:
                    frontend[core, component] = new_track(core, component + ' / milestones',
                                                          [f'Core {core}', 'Scheduler', 'Milestones'])
                tid = frontend[core, component]
            else:
                tid = tracks['execution']
            trace.append(dict(ph='i', s='t', name=event['event'], cat='ssnpu.milestone',
                              pid=core+1, tid=tid, ts=event['cycle'] * cycle_ns / 1000,
                              args=args | {'cycle': event['cycle'], 'component': event.get('component')}))
    # At equal starts, emit the containing X slice before its children/instants.
    trace.sort(key=lambda e: (e.get('ts', -1), e.get('pid', 0), e.get('tid', 0),
                             0 if e['ph'] == 'M' else 1 if e['ph'] == 'X' else 2, -e.get('dur', 0),
                             0 if e.get('cat') in ('ssnpu.instruction', 'ssnpu.rob') else 1, e['name']))
    return dict(traceEvents=trace, displayTimeUnit='ns', otherData={
        'source': 'SSNPU instruction milestone log', 'cycle_ns': cycle_ns,
        'clock': 'Synthetic cycle-to-time mapping; not hardware calibrated',
        'instruction_interval': 'received -> complete',
        'control_interval': 'issue -> complete for control/view logs without received',
        'rob_interval': 'complete -> retire; separate from execution units',
        'compute_interval': 'Includes service and result latency; not resource busy time',
    })


def _varint(value):
    result = bytearray()
    while value > 127:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def _uint(field, value):
    return _varint(field << 3) + _varint(value)


def _blob(field, value):
    if isinstance(value, str):
        value = value.encode('utf-8')
    return _varint((field << 3) | 2) + _varint(len(value)) + value


def _annotation(name, value):
    # DebugAnnotation fields: name=10, bool=2, uint=3, int=4,
    # double=5, string=6. No interning or external protobuf dependency.
    data = _blob(10, name)
    if isinstance(value, bool):
        data += _uint(2, int(value))
    elif isinstance(value, int):
        if not -(1 << 63) <= value < (1 << 64):
            raise ModelError(f'Annotation {name} exceeds protobuf integer range')
        data += _uint(3 if value >= 0 else 4, value & ((1 << 64)-1))
    elif isinstance(value, float):
        data += _varint((5 << 3) | 1) + struct.pack('<d', value)
    else:
        data += _blob(6, value if isinstance(value, str) else json.dumps(value))
    return _blob(4, data)


def to_pftrace(events, cycle_ns=1.0):
    """Encode a native .pftrace with collapsible custom parent/child tracks.

    Uses the stable TracePacket/TrackEvent API; field definitions:
    https://github.com/google/perfetto/tree/main/protos/perfetto/trace/track_event
    Only the wire fields used here are encoded. No process/thread descriptors
    are set because their special nesting semantics flatten custom parents.
    Timestamps are rounded to nanoseconds; original cycles remain in args.
    """
    trace = to_perfetto(events, cycle_ns)
    paths, track_ids, children, descriptors = {}, {}, defaultdict(int), []
    for event in trace['traceEvents']:
        if event['ph'] != 'M' or event['name'] != 'thread_name':
            continue
        path = tuple(event['args']['path'])
        for depth in range(1, len(path)+1):
            prefix, parent = path[:depth], paths.get(path[:depth-1], 0)
            if prefix not in paths:
                uuid = paths[prefix] = len(paths)+1
                descriptor = _uint(1, uuid) + _blob(2, prefix[-1]) + _uint(11, 3) + _uint(12, children[parent])
                children[parent] += 1
                if parent:
                    descriptor += _uint(5, parent)
                description = f'1 cycle = {cycle_ns} ns (synthetic mapping). ' + trace['otherData']['instruction_interval']
                descriptor += _blob(14, description)
                descriptors.append(_blob(1, _uint(10, 1) + _blob(60, descriptor)))
        track_ids[event['pid'], event['tid']] = paths[path]
    by_track = defaultdict(list)
    for event in trace['traceEvents']:
        if event['ph'] != 'M':
            by_track[track_ids[event['pid'], event['tid']]].append(event)
    records = []

    def emit(cycle, track, kind, event=None):
        payload = _uint(9, kind) + _uint(11, track)
        if event:
            payload += _blob(23, event['name']) + _blob(22, event['cat'])
            payload += b''.join(_annotation(k, v) for k, v in sorted(event['args'].items()))
        timestamp = round(cycle * cycle_ns)
        if not 0 <= timestamp < (1 << 64):
            raise ModelError('Trace timestamp exceeds uint64 nanosecond range')
        packet = _uint(8, timestamp) + _uint(10, 1) + _uint(13, 2) + _blob(11, payload)
        records.append((cycle, len(records), _blob(1, packet)))

    for track, track_events in sorted(by_track.items()):
        ends = []
        for event in track_events:
            cycle = event['args']['start_cycle'] if event['ph'] == 'X' else event['args']['cycle']
            while ends and ends[-1] <= cycle:
                emit(ends.pop(), track, 2)
            if event['ph'] == 'i':
                emit(cycle, track, 3, event)
                continue
            end = event['args']['end_cycle']
            if ends and end > ends[-1]:
                raise ModelError('Crossing slices on a native track')
            emit(cycle, track, 1, event)
            if end == cycle:
                emit(end, track, 2)
            else:
                ends.append(end)
        while ends:
            emit(ends.pop(), track, 2)
    # Trace.packet=1. Reset the sole packet sequence, then define all custom
    # tracks before time-ordered events. BEGIN/END order is preserved on ties.
    reset = _blob(1, _uint(10, 1) + _uint(13, 1))
    return reset + b''.join(descriptors) + b''.join(packet for _, _, packet in sorted(records))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('event_log', help='Instruction milestone JSONL from --event-log')
    parser.add_argument('--output', required=True, help='.pftrace or legacy .json destination (use workspace/)')
    parser.add_argument('--format', choices=('pftrace', 'json'), help='Default: .json suffix selects JSON; otherwise native pftrace')
    parser.add_argument('--cycle-ns', type=float, default=1.0, help='Nanoseconds per model cycle (default: synthetic 1 ns)')
    args = parser.parse_args(argv)
    try:
        with Path(args.event_log).open() as source:
            events = (json.loads(line) for line in source if line.strip())
            format_name = args.format or ('json' if Path(args.output).suffix == '.json' else 'pftrace')
            result = to_pftrace(events, args.cycle_ns) if format_name == 'pftrace' else to_perfetto(events, args.cycle_ns)
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        if format_name == 'pftrace':
            path.write_bytes(result)
        else:
            path.write_text(json.dumps(result, indent=2, allow_nan=False) + '\n')
    except (ModelError, OSError, json.JSONDecodeError) as exc:
        parser.exit(2, f'{exc}\n')
    print(f'Wrote {format_name} trace to {path}')


if __name__ == '__main__':
    main()
