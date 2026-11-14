"""One target SRAM port shared by local, remote and HBM staging accesses."""
from math import ceil

from errors import ModelError
from runtime.memory import (MemorySubmit, MemoryAccepted, MemoryRejected,
                            MemoryDone, MemoryRetired, valid_request)
from runtime.component import Component
from runtime.service import Service


class SRAM(Component):
    def __init__(self, node, *, capacity, throughput, read_latency, write_latency, queue_depth=16, storage=None):
        super().__init__(f'core:{node}/sram')
        self.node, self.capacity, self.throughput = node, capacity, throughput
        self.latencies = dict(read=read_latency, write=write_latency)
        self.queue_depth = queue_depth
        if capacity <= 0 or throughput <= 0 or queue_depth <= 0 or min(self.latencies.values()) < 0:
            raise ModelError('Invalid SRAM service configuration')
        if storage is not None and len(storage) != capacity:
            raise ModelError('Debug storage must match SRAM capacity')
        self.storage, self.service = storage, Service()
        self.active, self.seen, self.events = {}, set(), []

    def tick(self, cycle):
        for _, message in self.inputs(cycle):
            if isinstance(message, MemoryRetired):
                entry = self.active.get(message.request_id)
                if entry is None or entry['state'] != 'done':
                    raise ModelError('Invalid SRAM completion retirement')
                del self.active[message.request_id]
                continue
            if not isinstance(message, MemorySubmit):
                raise TypeError(message)
            request = message.request
            if (not valid_request(request) or request.dst_core != self.node
                    or request.offset_bytes + request.nbytes > self.capacity
                    or request.request_id in self.seen
                    or (self.storage is not None and request.op == 'write' and request.data is None)):
                self.send(message.endpoint, MemoryRejected(
                    request.request_id, 'Invalid SRAM range, identity or payload', False))
            elif len(self.active) >= self.queue_depth:
                self.send(message.endpoint, MemoryRejected(request.request_id, 'SRAM queue full', True))
            else:
                self.seen.add(request.request_id)
                self.active[request.request_id] = dict(submission=message, state='queued', queued=cycle)
                self.send(message.endpoint, MemoryAccepted(request.request_id))
        for key, entry in self.active.items():
            sub = entry['submission']
            request = sub.request
            if entry['state'] == 'queued':
                event = self.service.start(self.name, 'sram', request.nbytes,
                    max(1, ceil(request.nbytes / self.throughput)), self.latencies[request.op], cycle)
                if event is not None:
                    entry.update(state='running', event=event)
                    self.events.append(dict(event, request_id=key, op=request.op, queued=entry['queued']))
            if entry['state'] == 'running' and entry['event']['complete'] <= cycle:
                data = None
                if self.storage is not None:
                    start, end = request.offset_bytes, request.offset_bytes + request.nbytes
                    if request.op == 'write':
                        self.storage[start:end] = request.data
                    else:
                        data = bytes(self.storage[start:end])
                self.send(sub.endpoint, MemoryDone(key, 'success', data, stages=(entry['event'],)))
                entry['state'] = 'done'

    def next_event(self):
        return min((e['event']['complete'] for e in self.active.values()
                    if e['state'] == 'running'), default=None)

    def is_idle(self):
        return super().is_idle() and not self.active

    def snapshot(self):
        return dict(super().snapshot(), requests={str(k): e['state'] for k, e in self.active.items()})
