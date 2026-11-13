"""Standalone byte AllGather: no capture, Instructions, TileReg or core simulator.

Each client prepares its buffers, submits at a different cycle, does independent
work after acceptance, waits on its token, then consumes the published bytes.
"""
import argparse
from dataclasses import asdict
import json
from pathlib import Path

from ssnpu_model.clock import Clock
from ssnpu_model.components.base import Component
from ssnpu_model.communication.compiler import compile_phase, make_installation
from ssnpu_model.communication.protocol import (
    Accepted, Rejected, Completion, CommRequest, DataRange, DataCommand, DataReply, platform_spec)
from ssnpu_model.communication.unit import CommUnit
from ssnpu_model.config import wse_config


class ByteClient(Component):
    """An example data-access owner; opaque handles include a generation."""
    def __init__(self, request, source, submit_cycle, independent_cycles=7):
        super().__init__(request.endpoint)
        self.request, self.source = request, bytes(source)
        self.destination = bytearray(request.destination.capacity)
        self.submit_cycle = submit_cycle
        self.independent_cycles = independent_cycles
        self.accepted = self.wait_cycle = self.completion = None
        self.source_released = self.consumed = False
        self.prepared = self.published = False
        self.expected, self.reservations, self.written = {}, {}, set()
        self.pending = []
        self.history = [dict(event='prepared', cycle=0)]

    def log(self, event, **fields):
        self.history.append(dict(event=event, cycle=self.cycle, **fields))

    def later(self, when, event, detail=None):
        self.pending.append((when, event, detail))

    def reply(self, event, detail=None):
        self.send('communit', DataReply(self.request.token, event, detail))

    def data(self, command):
        expected = self.request.source if command.kind == 'read' else self.request.destination
        if command.region != expected:
            self.reply('fault', 'Stale or unauthorized data binding/generation')
            return
        kind, detail = command.kind, command.detail
        if kind == 'prepare':
            self.expected = {source: (offset, size) for source, offset, size in detail}
            self.prepared = True
            self.reply('posted')
        elif kind == 'read':
            self.later(self.cycle+3, 'source_ready')
        elif kind == 'fence':
            self.reply('port_ready', self.cycle)
        elif kind == 'reserve':
            self.reservations = {source: (start, service, latency) for source, start, service, latency in detail}
            self.later(self.cycle+2, 'reserved')  # Honor the declared 4-cycle request/ack bound.
        elif kind == 'write':
            source, offset, size, payload = detail
            if not self.prepared or self.expected.get(source) != (offset, size):
                self.reply('fault', 'Write outside authorized segment')
                return
            start, service, latency = self.reservations[source]
            if self.cycle+1 != start or payload is None or len(payload) != size:
                self.reply('fault', 'Write missed endpoint service or payload geometry')
                return
            self.later(start+service+latency, 'written', (source, offset, bytes(payload)))
        elif kind == 'publish':
            if self.written != set(self.expected):
                self.reply('fault', 'Premature publication')
                return
            self.published = True
            self.reply('published')
        else:
            self.reply('fault', 'Unknown data command')

    def tick(self, cycle):
        for _, message in self.inputs(cycle):
            if isinstance(message, Accepted):
                self.accepted = cycle
                self.wait_cycle = cycle+self.independent_cycles
                self.log('accepted')
                self.log('independent_work_start')
            elif isinstance(message, Rejected):
                self.log('rejected', retryable=message.retryable, reason=message.reason)
                if not message.retryable:
                    raise ValueError(message.reason)
                self.submit_cycle = cycle+3
            elif isinstance(message, Completion):
                self.completion = message
                self.log('completion', status=message.status, safe_to_release=message.safe_to_release)
            elif isinstance(message, DataCommand):
                self.data(message)
            else:
                raise TypeError(message)
        if cycle == self.submit_cycle:
            self.send('communit', self.request)
            self.log('submitted')
        for when, event, detail in list(self.pending):
            if when > cycle:
                continue
            self.pending.remove((when, event, detail))
            if event == 'source_ready':
                r = self.request.source
                self.reply(event, self.source[r.offset:r.offset+r.nbytes])
                self.source_released = True
                self.log('source_released')
            elif event == 'written':
                source, offset, payload = detail
                if source not in self.written:
                    self.destination[offset:offset+len(payload)] = payload
                    self.written.add(source)
                self.reply(event, (source, cycle))
            else:
                self.reply(event, detail)
        if cycle == self.wait_cycle:
            self.log('independent_work_end')
            self.log('wait')
        if self.completion is not None and self.completion.status == 'success' and cycle >= self.wait_cycle and not self.consumed:
            assert self.published and self.source_released
            self.consumed = True
            self.log('consume')

    def is_idle(self):
        return super().is_idle() and self.consumed and not self.pending


def build_demo(members=(0, 1, 9), shard_bytes=64, rounds=2):
    platform = platform_spec(wse_config())
    phases, sources = [], {}
    for epoch in range(rounds):
        name = f'demo:{epoch}'
        expected = [dict(source=node, offset=rank*shard_bytes, nbytes=shard_bytes) for rank, node in enumerate(members)]
        phase = dict(name=name, opcode=1, epoch=epoch, routing='auto', nodes=[
            dict(core=node, nbytes=shard_bytes, source_offset=0, destination_offset=rank*shard_bytes,
                 expected=expected) for rank, node in enumerate(members)])
        compile_phase(phase, platform)
        phases.append(phase)
        for node in members:
            sources[name, node] = bytes((epoch*19+node+offset) % 256 for offset in range(shard_bytes))
    installation = make_installation(phases, platform)
    unit, clients = CommUnit(installation, platform), {}
    for phase in phases:
        for entry in phase['nodes']:
            node, epoch, name = entry['core'], phase['epoch'], phase['name']
            token = f'{name}/{node}'
            request = CommRequest(token, f'client:{token}', node, 'allgather', epoch, name,
                tuple(sorted(members)), installation['version'], tuple(r['route_bits'] for r in entry['routes']),
                DataRange(('source', node, epoch), 0, shard_bytes, shard_bytes),
                DataRange(('destination', node, epoch), 0, len(members)*shard_bytes, len(members)*shard_bytes),
                entry['destination_offset'])
            # Second domain submits while first domain awaits its last peer.
            arrival = 8 if epoch else members.index(node)*11
            client = ByteClient(request, sources[name, node], arrival)
            clients[client.name] = client
    return unit, clients, sources


def run_demo(members=(0, 1, 9), shard_bytes=64, rounds=2):
    unit, clients, sources = build_demo(members, shard_bytes, rounds)
    components = dict(clients, communit=unit)
    clock = Clock(components)
    for cycle in range(100000):
        for destination, message, when in unit.advance(cycle):
            clock.external(destination, message, cycle, when)
        clock.deliver(cycle)
        for name in sorted(components):
            components[name].tick(cycle)
        clock.collect(cycle)
        if not clock.pending and all(c.is_idle() for c in components.values()):
            break
    else:
        raise RuntimeError('Standalone driver failed to drain')
    for client in clients.values():
        expected = b''.join(sources[client.request.domain, node] for node in members)
        if bytes(client.destination) != expected:
            raise AssertionError('AllGather bytes differ from rank-ordered source concatenation')
    return dict(passed=True, total_cycles=cycle+1, drained=unit.drained(),
        conditions=dict(members=members, shard_bytes=shard_bytes, rounds=rounds,
                        source_pattern='(epoch*19+node+offset) % 256', arrivals='rank*11; later domains at cycle 8'),
        installation=unit.installation, platform=unit.platform, network=unit.report(),
        clients={name: dict(request=asdict(c.request), history=c.history, output_hex=bytes(c.destination).hex())
                 for name, c in clients.items()})


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('workspace/communit-driver/report.json'))
    args = parser.parse_args()
    report = run_demo()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps({k: report[k] for k in ('passed', 'total_cycles', 'drained', 'conditions')}, indent=2))
