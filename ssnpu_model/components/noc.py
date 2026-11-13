"""WSE40 Calendar network; exact network time, core-clock control messages.

One object owns the fabric. Sources are staged before alignment. Shared write
slots are installed under an admission fence, then ordinary writes use gaps.
Whole-packet adapter buffers are provisioned from the plan, not assumed SRAM.
"""
import heapq
import math
from bisect import bisect_left
from collections import Counter, defaultdict
from fractions import Fraction

from ..errors import ModelError
from ..noc_plan import operation_id, route_tree, transmissions
from ..tiles import version_key
from .base import Component
from .protocol import (CalendarCommand, CalendarComplete, CalendarReply, ReceivePrepare,
                       PortFence, WriteReservations, RouterWrite, PublishReceive, core_name)


class CalendarNoC(Component):
    def __init__(self, instructions, phases, config):
        super().__init__('noc')
        self.nodes, self.config = instructions, config
        self.p = {k: v['value'] for k, v in config['wse'].items()}
        self.core_ns = Fraction(10**9, self.p['core_clock_hz'])
        self.noc_ns = Fraction(10**9, self.p['noc_clock_hz'])
        self.payload = self.p['link_bytes_per_cycle'] - self.p['header_bytes']
        self.states, self.operations = {}, {}
        self.heap, self.intervals, self.events = [], defaultdict(list), []
        self.serial = self.processed = 0
        self.links = defaultdict(Counter)
        self.buffer_required = Counter()
        for phase in phases:
            name = phase['name']
            entries = {e['core']: e for e in phase['nodes']}
            state = dict(plan=phase, entries=entries, posted=set(), staged=set(), ports={},
                         reserved=set(), waiting=set(), published=set(), committed=set(), tx_done=set(),
                         committed_remote=Counter(), status='preparing', coverage=defaultdict(set))
            self.states[name] = state
            for core, e in entries.items():
                for kind in ('post', 'send', 'receive'):
                    self.operations[operation_id(name, core, kind)] = name, core, kind
                self.buffer_required[f'core:{core}/tx'] = max(self.buffer_required[f'core:{core}/tx'], e['nbytes'])
                remote = sum(w['nbytes'] for w in e['expected'] if w['source'] != core)
                self.buffer_required[f'core:{core}/rx'] = max(self.buffer_required[f'core:{core}/rx'], remote)

    def record(self, kind, time, *, end=None, **fields):
        self.events.append(dict(kind=kind, time_ns=float(time), time_ns_exact=str(time),
                                end_ns=float(time if end is None else end), **fields))

    def push(self, when, kind, name, source, node, flit=-1, round_id=0):
        self.serial += 1
        heapq.heappush(self.heap, (when, self.serial, kind, name, source, node, flit, round_id))

    def reserve(self, resource, start, end, name, source, **fields):
        intervals = self.intervals[resource]
        index = bisect_left(intervals, (start,))
        if (index and intervals[index - 1][1] > start) or (index < len(intervals) and intervals[index][0] < end):
            raise ModelError(f'Calendar resource collision: {resource}, {name}/{source}, {start}')
        intervals.insert(index, (start, end))
        self.record('link' if resource.startswith('link:') else 'port', start, end=end,
                    resource=resource, phase=name, source=source, **fields)

    def bound(self, state, source, end, round_id=0):
        entry = state['entries'][source]
        limit = state['origin'] + transmissions(entry, self.p)[round_id]['end_cycle'] * self.noc_ns
        if end + self.p['guard_cycles'] * self.noc_ns > limit:
            raise ModelError(f'Calendar window exceeded: {state["plan"]["name"]}/{source}')

    def schedule_writes(self, name):
        s = self.states[name]
        # Reservation request/ack: two core-message cycles, then an execution edge.
        earliest = max((self.cycle + 3) * self.core_ns,
                       max(s['ports'].values()) * self.core_ns)
        s['origin'] = math.ceil(earliest / self.noc_ns) * self.noc_ns + (
            self.p['alignment_cycles'] + self.p['arm_cycles']) * self.noc_ns
        s['writes'], s['routes'] = {}, {}
        requests = defaultdict(list)
        engine = self.config['engines']['shared_write']
        for source, entry in sorted(s['entries'].items()):
            count = math.ceil(entry['nbytes'] / self.payload)
            for round_id, transmission in enumerate(transmissions(entry, self.p)):
                children, landing, distance = route_tree(source, transmission['route_bits'])
                s['routes'][source, round_id] = children, landing
                start = s['origin'] + transmission['start_cycle'] * self.noc_ns
                injection = start + self.p['tx_cdc_cycles'] * self.noc_ns
                for target in landing:
                    ready = start if source == target else (injection + (count - 1 + distance[target] *
                        self.p['hop_cycles'] + self.p['rx_cdc_cycles']) * self.noc_ns)
                    requests[target].append((math.ceil(ready / self.core_ns), source, round_id))
        for target, entries in sorted(requests.items()):
            available = s['ports'][target]
            reservations = []
            for ready, source, round_id in sorted(entries):
                work = s['entries'][source]['nbytes']
                service = max(1, math.ceil(work / engine['throughput']))
                start = max(ready, available)
                latency = engine['latency']
                self.bound(s, source, (start + service + latency) * self.core_ns, round_id)
                reservations.append((source, start, service, latency))
                s['writes'][source, target] = start
                available = start + service
                self.record('write_reservation', start * self.core_ns,
                            end=available * self.core_ns, phase=name, source=source, node=target,
                            ready_cycle=ready, start_cycle=start, service_cycles=service,
                            visible_cycle=start + service + latency, bytes=work)
            self.send(core_name(target, 'tile'), WriteReservations(operation_id(name, target, 'receive'), tuple(reservations)))
        s['status'] = 'installing'

    def activate(self, name):
        s = self.states[name]
        if self.cycle * self.core_ns >= s['origin']:
            raise ModelError('Calendar admission missed the common origin')
        self.record('align', s['origin'], phase=name)
        for source, entry in sorted(s['entries'].items()):
            count = math.ceil(entry['nbytes'] / self.payload)
            drained = s['origin']
            for round_id, transmission in enumerate(transmissions(entry, self.p)):
                start = s['origin'] + transmission['start_cycle'] * self.noc_ns
                inject = start + self.p['tx_cdc_cycles'] * self.noc_ns
                children, landing = s['routes'][source, round_id]
                if source in landing:
                    self.push(s['writes'][source, source] * self.core_ns, 'write', name, source, source)
                if children[source]:
                    for flit in range(count):
                        self.push(inject + flit * self.noc_ns, 'inject', name, source, source, flit, round_id)
                    end = inject + count * self.noc_ns
                else:
                    end = start
                self.bound(s, source, end, round_id)
                drained = max(drained, end)
            self.push(drained, 'tx_done', name, source, source)
        s['status'] = 'running'

    def tick(self, cycle):
        for _, message in self.inputs(cycle):
            name, core, kind = self.operations[message.instruction]
            s, now = self.states[name], cycle * self.core_ns
            entry = s['entries'][core]
            receive = operation_id(name, core, 'receive')
            if isinstance(message, CalendarCommand):
                if kind == 'post':
                    self.send(core_name(core, 'tile'), ReceivePrepare(receive, version_key(entry['target']),
                              tuple((w['source'], w['offset'], w['nbytes']) for w in entry['expected'])))
                elif kind == 'send':
                    s['staged'].add(core)
                    self.record('source_released', now, phase=name, source=core, bytes=entry['nbytes'])
                elif kind == 'receive':
                    s['waiting'].add(core)
                    if core in s['published']:
                        self.send(core_name(core, 'tlsu'), CalendarComplete(receive))
            elif isinstance(message, CalendarReply):
                if message.event == 'posted':
                    s['posted'].add(core)
                    self.send(core_name(core, 'tlsu'), CalendarComplete(operation_id(name, core, 'post')))
                elif message.event == 'port_ready':
                    s['ports'][core] = message.detail
                elif message.event == 'reserved':
                    s['reserved'].add(core)
                elif message.event == 'written':
                    source, visible_cycle = message.detail
                    pair = source, core
                    if pair in s['committed']:
                        continue
                    s['committed'].add(pair)
                    size = s['entries'][source]['nbytes']
                    if source != core:
                        s['committed_remote'][core] += size
                    self.record('write_visible', visible_cycle * self.core_ns, phase=name,
                                source=source, node=core, bytes=size, local=source == core)
                    expected = {(w['source'], core) for w in entry['expected']}
                    if expected <= s['committed']:
                        expval = sum(w['nbytes'] for w in entry['expected'] if w['source'] != core)
                        if s['committed_remote'][core] != expval:
                            raise ModelError('Receive byte count differs from expVal')
                        self.send(core_name(core, 'tile'), PublishReceive(receive))
                elif message.event == 'published':
                    s['published'].add(core)
                    self.record('receive_published', now, phase=name, node=core)
                    if core in s['waiting']:
                        self.send(core_name(core, 'tlsu'), CalendarComplete(receive))
                else:
                    raise ModelError(f'Unknown Calendar reply: {message.event}')
            else:
                raise TypeError(message)
        for name, s in self.states.items():
            members = set(s['entries'])
            if s['status'] == 'preparing' and s['posted'] == s['staged'] == members:
                for core in sorted(members):
                    self.send(core_name(core, 'tile'), PortFence(operation_id(name, core, 'receive')))
                s['status'] = 'fencing'
            if s['status'] == 'fencing' and s['ports'].keys() == members:
                self.schedule_writes(name)
            if s['status'] == 'installing' and s['reserved'] == members:
                self.activate(name)
            if s['status'] == 'running' and s['published'] == s['tx_done'] == members:
                s['status'] = 'done'

    def advance(self, cycle):
        """Return core messages due on this edge, before any core component ticks."""
        due = []
        until = cycle * self.core_ns
        while self.heap and self.heap[0][0] <= until:
            now, _, kind, name, source, node, flit, round_id = heapq.heappop(self.heap)
            self.processed += 1
            self.progress += 1
            if self.processed > 2_000_000:
                raise ModelError('NoC simulator event limit exceeded (not a hardware limit)')
            s = self.states[name]
            entry = s['entries'][source]
            children, landing = s['routes'][source, round_id]
            if kind in ('inject', 'arrive'):
                if kind == 'inject':
                    self.reserve(f'inject:{node}', now, now + self.noc_ns, name, source, flit=flit)
                elif node in landing:
                    self.reserve(f'eject:{node}', now - self.noc_ns, now, name, source, flit=flit)
                    seen = s['coverage'][source, node]
                    if flit in seen:
                        raise ModelError('Duplicate flit on scheduled network')
                    seen.add(flit)
                    if len(seen) == math.ceil(entry['nbytes'] / self.payload):
                        write_cycle = s['writes'][source, node]
                        if now + self.p['rx_cdc_cycles'] * self.noc_ns > write_cycle * self.core_ns:
                            raise ModelError('Packet misses reserved Shared write')
                        self.push(write_cycle * self.core_ns, 'write', name, source, node)
                valid = min(self.payload, entry['nbytes'] - flit * self.payload)
                for child in children[node]:
                    resource = f'link:{node}->{child}'
                    arrival = now + self.p['hop_cycles'] * self.noc_ns
                    self.bound(s, source, arrival, round_id)
                    self.reserve(resource, now, now + self.noc_ns, name, source, flit=flit,
                                 round=round_id, payload_bytes=valid, header_bytes=self.p['header_bytes'],
                                 padding_bytes=self.payload - valid)
                    self.links[resource].update(flits=1, wire_bytes=self.p['link_bytes_per_cycle'], payload_bytes=valid)
                    self.push(arrival, 'arrive', name, source, child, flit, round_id)
            elif kind == 'write':
                receive = operation_id(name, node, 'receive')
                target = s['entries'][node]['target']
                due.append((core_name(node, 'tile'), RouterWrite(receive, version_key(target), source,
                            entry['destination_offset'], entry['nbytes']), now))
            elif kind == 'tx_done':
                s['tx_done'].add(source)
                self.record('tx_drained', now, phase=name, source=source)
                due.append((core_name(source, 'tlsu'), CalendarComplete(operation_id(name, source, 'send')), now))
        return due

    def is_idle(self):
        return super().is_idle() and not self.heap and all(s['status'] == 'done' for s in self.states.values())

    def next_event(self):
        return self.heap[0][0] if self.heap else None

    def snapshot(self):
        return dict(super().snapshot(), network_events=len(self.heap), phases={name: dict(
            status=s['status'], posted=sorted(s['posted']), staged=sorted(s['staged']),
            published=sorted(s['published']), tx_done=sorted(s['tx_done'])) for name, s in self.states.items()})

    def report(self):
        return dict(schedule={name: [dict(source=source, round=index, **transmission)
                                    for source, entry in sorted(s['entries'].items())
                                    for index, transmission in enumerate(transmissions(entry, self.p))]
                              for name, s in self.states.items()},
                    events=sorted(self.events, key=lambda e: (e['time_ns'], e['kind'], str(e))),
                    links={k: dict(v) for k, v in sorted(self.links.items())},
                    adapter_reserved_peak_bytes=dict(self.buffer_required),
                    phases={name: dict(status=s['status'], remote_committed_bytes={c: s['committed_remote'][c] for c in s['entries']})
                            for name, s in self.states.items()},
                    assumptions=['WSE40 mesh; one source packet per participant and phase; phases drain serially.',
                                 'One flit per NoC cycle per injection/ejection/directed link; parallel multicast.',
                                 'Automatic plans serialize whole-packet tree windows using declared link and Shared service.',
                                 'Whole-packet TX/RX buffers provisioned from the plan; hardware depth is pending.',
                                 'Shared writes reserve service windows; ordinary writes use gaps (pending).',
                                 'Simulator source/segment identities are metadata; on-wire encoding remains pending.',
                                 'Header size is a provisional baseline, not proof of complete address/identity encoding.',
                                 'Non-speculative captured path only; no branch rollback or hardware fault recovery.'])
