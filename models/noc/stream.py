"""Execute precompiled Calendar slots against independent TileReg endpoints."""
from collections import Counter, defaultdict
import copy
from dataclasses import asdict, dataclass, field
from fractions import Fraction
from math import ceil

from errors import ModelError
from models.noc.protocol import (Accepted, Completion, Rejected, StreamAccess,
                                 StreamReply, StreamSubmit, digest)
from runtime.component import Component


@dataclass
class Participant:
    submission: StreamSubmit
    prepared: bool = False
    source: tuple | None = None
    destination: tuple | None = None
    sent: int = 0
    received: int = 0
    published: bool = False
    complete: bool = False


@dataclass
class Segment:
    spec: dict
    data: bytes | None = None
    readable: bool = False
    pending: set = field(default_factory=set)


class StreamingCommUnit(Component):
    """One active round per launch context. CalReg supplies immutable slots."""

    def __init__(self, installation, platform):
        super().__init__('communit')
        body = {k: v for k, v in installation.items() if k != 'version'}
        if (body.get('schema') != 'wse-communit-stream/1' or digest(body) != installation.get('version')
                or body['platform_hash'] != digest(platform)):
            raise ModelError('Invalid streaming Calendar installation or platform')
        self.version = installation['version']
        # Installation is value data and can be transported as JSON.
        self.platform = copy.deepcopy(platform)
        self.rounds = {(r['opcode'], r['epoch']): copy.deepcopy(r) for r in body['calreg']}
        self.network = self.platform['network']
        self.ratio = Fraction(self.network['noc_clock_hz'], self.network['core_clock_hz'])
        self.core_ns = Fraction(10**9, self.network['core_clock_hz'])
        self.noc_ns = Fraction(10**9, self.network['noc_clock_hz'])
        self.payload = self.network['link_bytes_per_cycle'] - self.network['header_bytes']
        self.active, self.origin, self.failure = None, None, None
        self.participants, self.tokens, self.replies, self.segments = {}, set(), {}, {}
        self.actions = defaultdict(list)
        self.tx_left, self.rx_left = Counter(), Counter()
        self.tx_used, self.rx_used, self.tx_peak, self.rx_peak = (Counter() for _ in range(4))
        self.events = []

    def record(self, kind, **fields):
        timestamp = fields.get('noc_cycle')
        time = self.cycle * self.core_ns if timestamp is None else timestamp * self.noc_ns
        end = time if kind != 'link' else time + self.noc_ns
        self.events.append(dict(kind=kind, cycle=self.cycle, phase=self.active, time_ns=float(time),
                                time_ns_exact=str(time), end_ns=float(end), **fields))

    def access(self, node, kind, access_id, **fields):
        sub = self.participants[node].submission
        expected = {'prepare': 'prepared', 'read': 'read_done', 'write': 'write_done',
                    'publish': 'published'}[kind]
        self.replies[sub.request.request_id, access_id, expected] = node
        self.send(sub.endpoint, StreamAccess(sub.request.request_id, kind, access_id, **fields))

    def submit(self, sub):
        r = sub.request
        key = (r.opcode, r.epoch)
        try:
            if self.failure:
                raise ModelError('CommUnit quarantined after fault')
            if type(sub.node) is not int or type(r.opcode) is not int or type(r.epoch) is not int:
                raise ModelError('Invalid node or communication identity')
            if r.version != self.version:
                raise ModelError('Request/CalReg version mismatch')
            if not r.request_id or not sub.endpoint or r.request_id in self.tokens:
                raise ModelError('Missing or duplicate request identity')
            compiled = self.rounds[key]
            entries = compiled['entries']
            entry = entries.get(sub.node, entries.get(str(sub.node)))
            if entry is None or tuple(sorted(map(int, entries))) != r.members:
                raise ModelError('Request participant mismatch')
            for desc, field in ((r.send, 'send'), (r.receive, 'receive')):
                if digest(asdict(desc.layout) if desc else None) != digest(entry[field]):
                    raise ModelError('Request layout/route differs from installed Calendar')
            if r.receive and r.receive.binding.memory_space != 'SharedTileReg':
                raise ModelError('Receive binding must be in SharedTileReg')
            if self.active == key and sub.node in self.participants:
                raise ModelError('Duplicate node submission')
        except (KeyError, ModelError) as exc:
            self.send(sub.endpoint, Rejected(r.request_id, str(exc), False))
            return
        if self.active not in (None, key):
            self.send(sub.endpoint, Rejected(r.request_id, 'communication round busy', True))
            self.record('backpressure', node=sub.node, token=r.request_id)
            return
        self.active = key
        self.tokens.add(r.request_id)
        self.participants[sub.node] = Participant(sub)
        self.send(sub.endpoint, Accepted(r.request_id))
        self.record('accepted', node=sub.node, token=r.request_id)
        self.access(sub.node, 'prepare', -1,
                    detail=(r.send, r.receive, self.platform['read_service'], self.platform['write_service']))

    def fail(self, reason):
        self.failure = str(reason)
        self.record('fault', error=self.failure)
        for p in self.participants.values():
            if not p.complete:
                self.send(p.submission.endpoint, Completion(
                    p.submission.request.request_id, 'fault', self.failure, False))

    def align(self):
        compiled = self.rounds[self.active]
        n = self.network
        lead = ceil((n['alignment_cycles'] + n['arm_cycles']) / self.ratio)
        self.origin = ceil((self.cycle + lead + 1) / self.ratio.denominator) * self.ratio.denominator
        stride = compiled['stride']
        for index, spec in enumerate(compiled['segments']):
            self.segments[index] = Segment(spec)
            self.tx_left[spec['source']] += 1
            self.rx_left.update(spec['targets'])
            start = self.origin + spec['read_cycle']
            self.actions[start].append(('read', index))
            self.actions[start + stride].append(('inject', index))
            self.actions[start + 2 * stride].append(('write', index))
            self.actions[start + 3 * stride].append(('check_write', index))
        self.record('align', origin=self.origin, opcode=self.active[0], epoch=self.active[1])

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
                raise ModelError(f'Destination write missed Calendar service bound for segment {index}')
        elif kind == 'read':
            self.buffer(self.tx_used, self.tx_peak, source, size, 'tx_bytes')
            self.access(source, 'read', index, handle=self.participants[source].source,
                        offset=spec['src'], nbytes=size)
            self.record('read', node=source, segment=index, offset=spec['src'], nbytes=size)
        elif kind == 'inject':
            if not seg.readable:
                raise ModelError(f'Source read missed Calendar slot for segment {index}')
            for node in spec['targets']:
                self.buffer(self.rx_used, self.rx_peak, node, size, 'rx_bytes')
            routes = self.rounds[self.active]['routes']
            route = routes.get(source, routes.get(str(source)))
            distances = route['distance']
            start = int(self.cycle * self.ratio) + self.network['tx_cdc_cycles']
            for parent, children in route['children'].items():
                distance = distances.get(parent, distances.get(str(parent)))
                for child in children:
                    for offset in range(0, size, self.payload):
                        valid = min(self.payload, size - offset)
                        self.record('link', source=source, link=(int(parent), child), segment=index,
                                    resource=f'link:{parent}->{child}',
                                    header_bytes=self.network['header_bytes'], padding_bytes=self.payload - valid,
                                    noc_cycle=start + distance * self.network['hop_cycles'] + offset // self.payload,
                                    payload_bytes=valid, wire_bytes=self.network['link_bytes_per_cycle'])
            seg.pending.update(spec['targets'])
            self.record('inject', node=source, segment=index, nbytes=size)
        else:
            # The static segment stride covers the last flit and RX clock
            # crossing. Reserve space from injection through WriteDone.
            self.buffer(self.tx_used, self.tx_peak, source, -size, 'tx_bytes')
            self.tx_left[source] -= 1
            self.participants[source].sent += size
            for node in spec['targets']:
                self.access(node, 'write', index, handle=self.participants[node].destination,
                            offset=spec['dst'], nbytes=size, detail=(source, seg.data))
            self.record('write', source=source, segment=index, targets=spec['targets'])
            seg.data = None

    def receive_reply(self, reply):
        key = reply.token, reply.access_id, reply.event
        if key not in self.replies:
            if reply.event == 'fault' and any(k[:2] == key[:2] for k in self.replies):
                raise ModelError(str(reply.detail))
            raise ModelError('Unexpected or duplicate data reply')
        node = self.replies.pop(key)
        p = self.participants[node]
        if reply.event == 'prepared':
            p.source, p.destination = reply.detail
            r = p.submission.request
            if bool(p.source) != bool(r.send) or bool(p.destination) != bool(r.receive):
                raise ModelError('Prepared handles differ from requested access')
            p.prepared = True
        elif reply.event == 'published':
            p.published = True
            self.record('published', node=node)
        else:
            seg = self.segments[reply.access_id]
            size = seg.spec['nbytes']
            if reply.event == 'read_done':
                if reply.detail is not None and (not isinstance(reply.detail, bytes) or len(reply.detail) != size):
                    raise ModelError('Read returned a different byte range')
                seg.data = reply.detail
                seg.readable = True
                self.record('read_done', node=node, segment=reply.access_id)
            else:
                seg.pending.remove(node)
                self.buffer(self.rx_used, self.rx_peak, node, -size, 'rx_bytes')
                self.rx_left[node] -= 1
                if node != seg.spec['source']:
                    p.received += size
                self.record('write_visible', node=node, segment=reply.access_id, nbytes=size)
                if not self.rx_left[node]:
                    if p.received != p.submission.request.receive.layout.exp_val:
                        raise ModelError('Visible receive bytes differ from expVal')
                    self.access(node, 'publish', -2, handle=p.destination,
                                detail=p.submission.request.receive.layout.valid_ranges)

    def advance(self, cycle):
        return ()  # Streaming actions are already scheduled on core clock edges.

    def tick(self, cycle):
        for _, message in self.inputs(cycle):
            if isinstance(message, StreamSubmit):
                self.submit(message)
            elif not self.failure:
                try:
                    if not isinstance(message, StreamReply):
                        raise ModelError('Unknown streaming CommUnit message')
                    self.receive_reply(message)
                except ModelError as exc:
                    self.fail(exc)
        if self.active is None or self.failure:
            return
        if self.origin is None:
            if (len(self.participants) == len(self.rounds[self.active]['entries'])
                    and all(p.prepared for p in self.participants.values())):
                self.align()
            return
        try:
            for kind, index in self.actions.pop(cycle, ()):
                self.step(kind, index)
        except ModelError as exc:
            self.fail(exc)
            return
        for node, p in sorted(self.participants.items()):
            r = p.submission.request
            if not p.complete and not self.tx_left[node] and (r.receive is None or p.published):
                p.complete = True
                self.send(p.submission.endpoint, Completion(r.request_id, 'success'))
                self.record('completion', node=node, sent=p.sent, remote_received=p.received)
        if not self.actions and not self.replies and all(p.complete for p in self.participants.values()):
            del self.rounds[self.active]
            self.active, self.origin = None, None
            self.participants.clear()
            self.segments.clear()

    def drained(self):
        return (self.failure is None and self.active is None and not self.actions
                and not self.replies and super().is_idle())

    def is_idle(self):
        return self.drained() and not self.rounds

    def next_event(self):
        return min(self.actions, default=None)

    def report(self):
        return dict(interface='CommUnit streaming copy v1', version=self.version, drained=self.drained(),
                    events=sorted(self.events, key=lambda event: event['time_ns']), tx_peak=dict(self.tx_peak), rx_peak=dict(self.rx_peak),
                    assumptions=['One network segment train per slot; read, network and write stages overlap.',
                                 'TX and RX space is charged until the corresponding service stage finishes.',
                                 'Endpoint service and provisional wire format are configured, not calibrated.'])

    def snapshot(self):
        missing = (sorted(set(map(int, self.rounds[self.active]['entries'])) - self.participants.keys())
                   if self.active is not None else [])
        return dict(super().snapshot(), active=self.active, missing=missing,
                    pending_accesses=len(self.replies), failure=self.failure)
