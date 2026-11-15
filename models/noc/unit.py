"""Execute the installed Calendar. A submission is the endpoint's ready signal."""
from collections import Counter, defaultdict
import copy
from dataclasses import dataclass, field
from fractions import Fraction
from math import ceil

from errors import ModelError
from runtime.communication import Completion, Rejected, DataAccess, DataReply, CommSubmit, digest
from runtime.component import Component


@dataclass
class Segment:
    spec: dict
    data: bytes | None = None
    readable: bool = False
    pending: set = field(default_factory=set)


class CommUnit(Component):
    """One active round. The same opcode can be submitted again after drain."""

    def __init__(self, installation, platform):
        super().__init__('communit')
        body = {k: v for k, v in installation.items() if k != 'version'}
        if (body.get('schema') != 'wse-communit/3' or digest(body) != installation.get('version')
                or body['platform_hash'] != digest(platform)):
            raise ModelError('Invalid Calendar installation or platform')
        self.version = installation['version']
        self.platform = copy.deepcopy(platform)
        self.installation = copy.deepcopy(installation)
        self.calreg = {int(op): copy.deepcopy(r) for op, r in body['calreg'].items()}
        self.network = self.platform['network']
        self.ratio = Fraction(self.network['noc_clock_hz'], self.network['core_clock_hz'])
        self.core_ns = Fraction(10**9, self.network['core_clock_hz'])
        self.noc_ns = Fraction(10**9, self.network['noc_clock_hz'])
        self.active, self.origin, self.failure = None, None, None
        self.participants, self.tokens, self.replies, self.segments = {}, set(), {}, {}
        self.completed = set()
        self.actions = defaultdict(list)
        self.tx_left, self.rx_left, self.received = Counter(), Counter(), Counter()
        self.tx_used, self.rx_used, self.tx_peak, self.rx_peak = (Counter() for _ in range(4))
        self.events = []

    def record(self, kind, **fields):
        timestamp = fields.get('noc_cycle')
        time = self.cycle * self.core_ns if timestamp is None else timestamp * self.noc_ns
        end = time if kind != 'link' else time + self.noc_ns
        self.events.append(dict(kind=kind, cycle=self.cycle, phase=self.active, time_ns=float(time),
                                time_ns_exact=str(time), end_ns=float(end), **fields))

    def access(self, node, kind, index, ranges, data=None):
        sub = self.participants[node]
        self.replies[node, sub.token, index, f'{kind}_done'] = node
        self.send(sub.endpoint, DataAccess(sub.token, kind, index, ranges, node, data))

    def submit(self, sub):
        request = sub.request
        try:
            if self.failure:
                raise ModelError('CommUnit quarantined after fault')
            if type(sub.node) is not int or type(request.get('opcode')) is not int:
                raise ModelError('Invalid node or opcode')
            if any(type(value) is not int for value in request.values()):
                raise ModelError('Invalid communication operand')
            if not sub.token or not sub.endpoint or (sub.node, sub.token) in self.tokens:
                raise ModelError('Missing or duplicate submission token')
            entries = self.calreg[request['opcode']]['entries']
            entry = entries.get(sub.node, entries.get(str(sub.node)))
            if request != entry:
                raise ModelError('Request operands differ from installed Calendar')
        except (KeyError, ModelError) as exc:
            self.send(sub.endpoint, Rejected(sub.token, str(exc), False))
            return
        if self.active is not None and (self.active != request['opcode'] or self.origin is not None):
            self.send(sub.endpoint, Rejected(sub.token, 'communication round busy', True))
            self.record('backpressure', node=sub.node, token=sub.token)
            return
        if sub.node in self.participants:
            self.send(sub.endpoint, Rejected(sub.token, 'Duplicate node submission', False))
            return
        self.active = request['opcode']
        self.tokens.add((sub.node, sub.token))
        self.participants[sub.node] = sub
        self.record('submit', node=sub.node, token=sub.token)

    def fail(self, reason):
        self.failure = str(reason)
        self.record('fault', error=self.failure)
        for node, sub in self.participants.items():
            if node not in self.completed:
                self.send(sub.endpoint, Completion(sub.token, 'fault', self.failure, False))

    def align(self):
        compiled = self.calreg[self.active]
        n = self.network
        lead = ceil((n['alignment_cycles'] + n['arm_cycles']) / self.ratio)
        self.origin = ceil((self.cycle + lead + 1) / self.ratio.denominator) * self.ratio.denominator
        for index, spec in enumerate(compiled['segments']):
            self.segments[index] = Segment(spec)
            self.tx_left[spec['source']] += 1
            self.rx_left.update(spec['targets'])
            for action, field in (('read', 'read_cycle'), ('inject', 'inject_cycle'),
                                  ('sent', 'send_cycle'), ('write', 'write_cycle'),
                                  ('check_write', 'check_cycle')):
                self.actions[self.origin + spec[field]].append((action, index))
        self.record('align', origin=self.origin, opcode=self.active)

    def buffer(self, used, peak, node, change, limit):
        used[node] += change
        if not 0 <= used[node] <= self.platform[limit]:
            raise ModelError(f'{limit} exceeded at node {node}')
        peak[node] = max(peak[node], used[node])

    def step(self, kind, index):
        seg = self.segments[index]
        spec = seg.spec
        source, size = spec['source'], spec['nbytes']
        if kind == 'check_write':
            if seg.pending:
                raise ModelError(f'Destination write missed Calendar service bound for flit {index}')
        elif kind == 'read':
            self.buffer(self.tx_used, self.tx_peak, source, size, 'tx_bytes')
            self.access(source, 'read', index, spec['src'])
            self.record('read', node=source, segment=index, nbytes=size)
        elif kind == 'inject':
            if not seg.readable:
                raise ModelError(f'Source read missed Calendar slot for flit {index}')
            for node in spec['targets']:
                self.buffer(self.rx_used, self.rx_peak, node, size, 'rx_bytes')
            for link in spec['links']:
                event = dict(link, noc_cycle=int(self.origin * self.ratio) + link['noc_cycle'])
                self.record('link', source=source, segment=index, **event)
            seg.pending.update(spec['targets'])
            self.record('inject', node=source, segment=index, nbytes=size)
        elif kind == 'sent':
            self.tx_left[source] -= 1
            self.buffer(self.tx_used, self.tx_peak, source, -size, 'tx_bytes')
            self.record('sent', node=source, segment=index, nbytes=size)
        else:
            for node in spec['targets']:
                self.access(node, 'write', index, spec['dst'], seg.data)
            self.record('write', source=source, segment=index, targets=spec['targets'])
            seg.data = None

    def receive_reply(self, reply):
        key = reply.node, reply.token, reply.access_id, reply.event
        if key not in self.replies:
            if reply.event == 'fault' and any(k[:3] == key[:3] for k in self.replies):
                raise ModelError(str(reply.detail))
            raise ModelError('Unexpected or duplicate data reply')
        node = self.replies.pop(key)
        seg = self.segments[reply.access_id]
        size = seg.spec['nbytes']
        if reply.event == 'read_done':
            if reply.detail is not None and (not isinstance(reply.detail, bytes) or len(reply.detail) != size):
                raise ModelError('Read returned a different byte count')
            seg.data, seg.readable = reply.detail, True
            self.record('read_done', node=node, segment=reply.access_id)
        else:
            seg.pending.remove(node)
            self.buffer(self.rx_used, self.rx_peak, node, -size, 'rx_bytes')
            self.rx_left[node] -= 1
            if node != seg.spec['source']:
                self.received[node] += size
            self.record('write_visible', node=node, segment=reply.access_id, nbytes=size)
            if not self.rx_left[node] and self.received[node] != self.participants[node].request.get('expected_receive_bytes'):
                raise ModelError('Visible receive bytes differ from expected byte count')

    def tick(self, cycle):
        for _, message in self.inputs(cycle):
            if isinstance(message, CommSubmit):
                self.submit(message)
            elif not self.failure:
                try:
                    if not isinstance(message, DataReply):
                        raise ModelError('Unknown CommUnit message')
                    self.receive_reply(message)
                except ModelError as exc:
                    self.fail(exc)
        if self.active is None or self.failure:
            return
        if self.origin is None:
            if len(self.participants) == len(self.calreg[self.active]['entries']):
                self.align()
            return
        try:
            for kind, index in self.actions.pop(cycle, ()):
                self.step(kind, index)
        except ModelError as exc:
            self.fail(exc)
            return
        for node, sub in sorted(self.participants.items()):
            if node not in self.completed and not self.tx_left[node] and not self.rx_left[node]:
                self.completed.add(node)
                self.send(sub.endpoint, Completion(sub.token, 'success'))
                self.record('completion', node=node, remote_received=self.received[node])
        if not self.actions and not self.replies and len(self.completed) == len(self.participants):
            self.active, self.origin = None, None
            self.participants.clear()
            self.completed.clear()
            self.received.clear()
            self.segments.clear()

    def drained(self):
        return (self.failure is None and self.active is None and not self.actions
                and not self.replies and super().is_idle())

    def is_idle(self):
        return self.drained()

    def next_event(self):
        return min(self.actions, default=None)

    def report(self):
        return dict(interface='CommUnit flit copy', version=self.version, drained=self.drained(),
                    events=sorted(self.events, key=lambda event: event['time_ns']),
                    tx_peak=dict(self.tx_peak), rx_peak=dict(self.rx_peak),
                    assumptions=['Each scheduled network segment carries at most one flit per directed link.'])

    def snapshot(self):
        missing = (sorted(set(map(int, self.calreg[self.active]['entries'])) - self.participants.keys())
                   if self.active is not None else [])
        return dict(super().snapshot(), active=self.active, missing=missing,
                    pending_accesses=len(self.replies), failure=self.failure)
