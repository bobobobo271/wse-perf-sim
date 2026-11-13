"""Sole owner of storage, readiness, consumers and shared access ports."""
import math
from collections import Counter, defaultdict

from .base import Component
from .protocol import (Access, AccessDone, Allocate, Allocated, Complete, Consumed,
                       Dispatch, core_name, unit_name)
from .service import Service
from ..errors import ModelError
from .protocol import (ReceivePrepare, PortFence, WriteReservations, RouterWrite,
                       PublishReceive, TransferReply)


class TileReg(Component):
    def __init__(self, name, instructions, versions, config):
        super().__init__(name)
        self.nodes, self.versions, self.config = instructions, versions, config
        self.active = set()
        self.usage, self.peak = Counter(), Counter()
        self.storage_events = []
        self.service = Service()
        self.ready, self.done = set(), set()
        self.allocations, self.accesses, self.running = [], [], []
        self.blocked = {}
        self.receives, self.reservations = {}, {}
        self.port_fences, self.router_running = set(), []
        self.waits = defaultdict(Counter)

    def finish_instruction(self, key):
        i = self.nodes[key]
        self.done.add(key)
        for vk in sorted(i.inputs | i.outputs, key=str):
            v = self.versions[vk]
            if not v.retained and v.producer in self.done and v.consumers <= self.done:
                self.release(vk)
                self.ready.discard(vk)

    def release(self, key, superseded_by=None):
        if key not in self.active:
            return
        self.active.remove(key)
        v = self.versions[key]
        self.usage[v.pool] -= v.size
        event = dict(cycle=self.cycle, action='free', version=list(key), pool=v.pool, bytes=v.size)
        if superseded_by is not None:
            event['superseded_by'] = superseded_by
        self.storage_events.append(event)

    def allocate(self, instruction):
        if instruction.raw['ir_op'] == 'comm.submit':
            instruction = self.nodes[instruction.raw['communication']['reserve_for']]
        # Submit reserves the wait producer's output; wait must not allocate it twice.
        if instruction.raw['ir_op'] == 'comm.wait' and instruction.outputs <= self.active:
            return None
        delta, replace = Counter(), set()
        for key in sorted(instruction.outputs, key=str):
            v = self.versions[key]
            for old in sorted(self.active, key=str):
                ov = self.versions[old]
                if ov.slot == v.slot and old != key:
                    if ov.retained or ov.producer not in self.done or not (ov.consumers - {instruction.id}) <= self.done:
                        return f'fixed_position:{old}'
                    replace.add(old)
            delta[v.pool] += v.size
        for old in replace:
            delta[self.versions[old].pool] -= self.versions[old].size
        for pool, amount in delta.items():
            limit = self.config['parameters']['shared_capacity' if pool.endswith('/shared') else 'local_capacity']['value']
            if self.usage[pool] + amount > limit:
                return f'capacity:{pool}'
        for key in sorted(replace, key=str):
            self.release(key, superseded_by=instruction.id)
        for key in sorted(instruction.outputs, key=str):
            v = self.versions[key]
            self.active.add(key)
            self.usage[v.pool] += v.size
            self.peak[v.pool] = max(self.peak[v.pool], self.usage[v.pool])
            self.storage_events.append(dict(cycle=self.cycle, action='allocate', version=list(key), pool=v.pool, bytes=v.size))
        return None

    def start_access(self, request):
        engine = 'shared_write' if request.write else 'shared_read'
        e = self.config['engines'][engine]
        cost = e | e.get('dtype_overrides', {}).get(request.dtype, {})
        i = self.nodes[request.instruction]
        duration = max(1, math.ceil(request.work / cost['throughput']))
        if request.write and (self.port_fences or any(
                self.cycle < start + service and start < self.cycle + duration
                for start, service, _ in self.reservations.values())):
            return None
        return self.service.start(core_name(i.core, engine), engine, request.work,
                                  max(1, math.ceil(request.work / cost['throughput'])), cost['latency'], self.cycle)

    def respond(self, request):
        if request.write:
            self.ready.add(request.version)
        self.send(unit_name(self.nodes[request.instruction]),
                  AccessDone(request.instruction, request.request), 'access')

    def transfer_message(self, message):
        if not isinstance(message, (ReceivePrepare, PortFence, WriteReservations, RouterWrite, PublishReceive)):
            return False
        key = message.instruction
        i = self.nodes[key]
        version, = i.outputs
        if version not in self.active:
            raise ModelError(f'Calendar accesses an inactive generation: {key}')
        if isinstance(message, ReceivePrepare):
            if message.version != version or key in self.receives:
                raise ModelError('Invalid or duplicate receive registration')
            expected = {source: (offset, size) for source, offset, size in message.expected}
            cursor = 0
            for offset, size in sorted(expected.values()):
                if offset != cursor or size <= 0:
                    raise ModelError('Overlapping or incomplete receive authorization')
                cursor += size
            if cursor != self.versions[version].size or len(expected) != len(message.expected):
                raise ModelError('Invalid receive coverage')
            self.receives[key] = dict(version=version, expected=expected, pending=set(), committed=set(), published=False)
            self.send(core_name(i.core, 'comm'), TransferReply(key, 'posted'))
        elif isinstance(message, PortFence):
            if key not in self.receives:
                raise ModelError('Port fence before receive registration')
            self.port_fences.add(key)
            available = max(self.cycle, self.service.available[core_name(i.core, 'shared_write')])
            self.send(core_name(i.core, 'comm'), TransferReply(key, 'port_ready', available))
        elif isinstance(message, WriteReservations):
            if key not in self.port_fences:
                raise ModelError('Write reservation without admission fence')
            expected = self.receives[key]['expected']
            if {entry[0] for entry in message.entries} != set(expected):
                raise ModelError('Reservation coverage differs from receive coverage')
            for source, start, service, latency in message.entries:
                if (key, source) in self.reservations or start < self.cycle:
                    raise ModelError('Duplicate or late write reservation')
                for old_start, old_service, _ in self.reservations.values():
                    if start < old_start + old_service and old_start < start + service:
                        raise ModelError('Shared write reservations overlap')
                self.reservations[key, source] = start, service, latency
            self.port_fences.remove(key)
            self.send(core_name(i.core, 'comm'), TransferReply(key, 'reserved'))
        elif isinstance(message, RouterWrite):
            ctx = self.receives[key]
            if message.version != version or ctx['version'] != version:
                raise ModelError('Stale router write generation')
            if ctx['expected'].get(message.source) != (message.offset, message.work):
                raise ModelError('Router write is outside its authorized segment')
            if message.source in ctx['pending'] | ctx['committed']:
                return True  # Identical duplicates, including pending writes, never count twice.
            start, service, latency = self.reservations.pop((key, message.source))
            if self.cycle != start:
                raise ModelError('Router missed its reserved write window')
            event = self.service.start(core_name(i.core, 'shared_write'), 'shared_write',
                                       message.work, service, latency, self.cycle)
            if event is None:
                raise ModelError('Reserved Shared write port is busy')
            ctx['pending'].add(message.source)
            self.observe(key, stage=event)
            self.router_running.append((message, event['complete']))
        elif isinstance(message, PublishReceive):
            ctx = self.receives[key]
            if ctx['published'] or ctx['pending'] or ctx['committed'] != set(ctx['expected']):
                raise ModelError('Premature or duplicate receive publication')
            self.ready.add(version)
            ctx['published'] = True
            self.send(core_name(i.core, 'comm'), TransferReply(key, 'published'))
        return True

    def tick(self, cycle):
        for _, message in self.inputs(cycle):
            try:
                if self.transfer_message(message):
                    continue
            except ModelError as exc:
                self.send(core_name(self.nodes[message.instruction].core, 'comm'),
                          TransferReply(message.instruction, 'fault', str(exc)))
                continue
            if isinstance(message, Allocate):
                self.allocations.append(message)
            elif isinstance(message, Access):
                i = self.nodes[message.instruction]
                assert message.version in (i.outputs if message.write else i.inputs), message
                self.accesses.append(message)
            elif isinstance(message, Consumed):
                self.finish_instruction(message.instruction)
            elif isinstance(message, Dispatch):
                i = self.nodes[message.instruction]
                assert i.alias
                self.observe(i.id, complete=cycle, state='complete')
                self.send(core_name(i.core, 'rob'), Complete(i.id))
                self.finish_instruction(i.id)
            else:
                raise TypeError(message)
        for message, end in list(self.router_running):
            if end <= cycle:
                ctx = self.receives[message.instruction]
                ctx['pending'].remove(message.source)
                ctx['committed'].add(message.source)
                self.send(core_name(self.nodes[message.instruction].core, 'comm'), TransferReply(message.instruction, 'written', (message.source, end)))
                self.router_running.remove((message, end))
        for request, end in list(self.running):
            if end <= cycle:
                self.respond(request)
                self.running.remove((request, end))
        self.blocked = {}
        for request in list(self.allocations):
            i = self.nodes[request.instruction]
            reason = self.allocate(i)
            if reason:
                self.blocked[str(i.id)] = reason
                self.waits[str(i.id)][reason] += 1
                continue
            # An in-place writer may still read the old contents until its write.
            self.send(core_name(i.core, 'scheduler'), Allocated(i.id, request.request), 'allocation')
            self.allocations.remove(request)
        # Requests arrive in deterministic bus order. Each direction has its own port.
        for request in list(self.accesses):
            if request.write:
                assert request.version in self.active, request
            elif request.version not in self.ready:
                self.blocked[request.request] = f'tile_not_ready:{request.version}'
                continue
            shared = self.versions[request.version].pool.endswith('/shared')
            if shared:
                event = self.start_access(request)
                if event is None:
                    self.blocked[request.request] = 'shared_port_busy'
                    continue
                self.observe(request.instruction, stage=event)
                self.running.append((request, event['complete']))
            else:
                # Private access cost is included in compute; its message round trip remains.
                self.respond(request)
            self.accesses.remove(request)

    def is_idle(self):
        return super().is_idle() and not (self.allocations or self.accesses or self.running
                                               or self.router_running or self.port_fences or self.reservations)

    def next_event(self):
        return min((end for _, end in self.running + self.router_running), default=None)

    def snapshot(self):
        return dict(super().snapshot(), calendar_receives={str(k): dict(pending=sorted(v['pending']),
                    committed=sorted(v['committed']), published=v['published']) for k, v in self.receives.items()},
                    write_reservations={str(k): v for k, v in self.reservations.items()},
                    port_fences=sorted(self.port_fences), allocations=[r.request for r in self.allocations],
                    accesses=[r.request for r in self.accesses],
                    running=[(r.request, end) for r, end in self.running], blocked=self.blocked.copy(),
                    usage=dict(self.usage), active=[list(k) for k in sorted(self.active, key=str)],
                    ready=[list(k) for k in sorted(self.ready, key=str)],
                    consumers={str(k): sorted(map(str, self.versions[k].consumers - self.done))
                               for k in sorted(self.active, key=str)})
