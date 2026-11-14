"""Aggregate remote-memory backend, with no routing or Calendar compilation.

Both traffic directions share one configured bandwidth resource. This backend
is independent of the Calendar fabric; it is not an SSM H/V implementation.
"""
from math import ceil

from errors import ModelError
from runtime.memory import (MemorySubmit, MemoryAccepted, MemoryRejected,
                            MemoryDone, MemoryRetired, valid_request)
from runtime.component import Component
from runtime.service import Service


class MemoryNetwork(Component):
    def __init__(self, nodes, *, throughput, latency, control_bytes, header_bytes, inflight):
        super().__init__('memory-network')
        self.nodes = set(nodes)
        self.throughput, self.latency = throughput, latency
        self.control_bytes, self.header_bytes, self.inflight = control_bytes, header_bytes, inflight
        if min(throughput, control_bytes, inflight) <= 0 or min(latency, header_bytes) < 0:
            raise ModelError('Invalid remote memory transport configuration')
        self.service = Service()
        self.active, self.seen, self.events = {}, set(), []

    def tick(self, cycle):
        for _, message in self.inputs(cycle):
            if isinstance(message, MemorySubmit):
                request = message.request
                if (not valid_request(request) or request.request_id in self.seen
                        or request.src_core not in self.nodes or request.dst_core not in self.nodes
                        or request.src_core == request.dst_core):
                    self.send(message.endpoint, MemoryRejected(
                        request.request_id, 'Invalid remote memory request', False))
                elif len(self.active) >= self.inflight:
                    self.send(message.endpoint, MemoryRejected(
                        request.request_id, 'Remote memory credit full', True))
                else:
                    self.seen.add(request.request_id)
                    self.active[request.request_id] = dict(
                        submission=message, state='request', stages=[], queued=cycle)
                    self.send(message.endpoint, MemoryAccepted(request.request_id))
                continue
            entry = self.active.get(message.request_id)
            if entry is None:
                raise ModelError('Unknown or duplicate memory response')
            request = entry['submission'].request
            if isinstance(message, MemoryAccepted):
                if entry['state'] != 'sram_sent':
                    raise ModelError('Unexpected SRAM acceptance')
                entry['state'] = 'sram_wait'
            elif isinstance(message, MemoryRejected):
                if entry['state'] != 'sram_sent':
                    raise ModelError('Unexpected SRAM rejection')
                if message.retryable:
                    entry['state'] = 'sram'
                else:
                    entry.update(state='response', queued=cycle,
                        result=MemoryDone(request.request_id, 'fault', error=message.reason))
            elif isinstance(message, MemoryDone):
                if entry['state'] != 'sram_wait':
                    raise ModelError('Unexpected SRAM completion')
                self.send(f'core:{request.dst_core}/sram', MemoryRetired(request.request_id))
                entry['stages'].extend(message.stages)
                entry.update(state='response', queued=cycle, result=message)
            elif isinstance(message, MemoryRetired):
                if entry['state'] != 'done':
                    raise ModelError('Premature completion retirement')
                del self.active[message.request_id]
            else:
                raise TypeError(message)
        for entry in self.active.values():
            sub = entry['submission']
            request, state = sub.request, entry['state']
            if state in ('request', 'response'):
                payload = (request.op == 'write' if state == 'request'
                           else request.op == 'read' and entry['result'].status == 'success')
                work = self.header_bytes + (request.nbytes if payload else self.control_bytes)
                event = self.service.start('noc/remote-memory', 'memory_' + state, work,
                                           ceil(work / self.throughput), self.latency, cycle)
                if event is not None:
                    entry.update(state=state + '_running', end=event['complete'])
                    entry['stages'].append(event)
                    self.events.append(dict(event, request_id=request.request_id,
                                            direction=state, queued=entry['queued']))
            elif state == 'request_running' and entry['end'] <= cycle:
                entry['state'] = 'sram'
            elif state == 'response_running' and entry['end'] <= cycle:
                result = entry['result']
                self.send(sub.endpoint, MemoryDone(request.request_id, result.status,
                    result.data, result.error, tuple(entry['stages'])))
                entry['state'] = 'done'
            if entry['state'] == 'sram':
                self.send(f'core:{request.dst_core}/sram', MemorySubmit(self.name, request))
                entry['state'] = 'sram_sent'

    def next_event(self):
        return min((e['end'] for e in self.active.values()
                    if e['state'].endswith('_running')), default=None)

    def is_idle(self):
        return super().is_idle() and not self.active

    def snapshot(self):
        return dict(super().snapshot(), requests={str(k): e['state'] for k, e in self.active.items()})
