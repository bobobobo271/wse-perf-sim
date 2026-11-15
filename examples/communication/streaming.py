"""Run issue #4's byte-exact broadcast and gather against independent endpoints."""
from config import platform_spec
import argparse
import json
from math import ceil
from pathlib import Path

from config import wse_config
from errors import ModelError
from runtime.communication import (Completion, ReceiveLayout, Rejected, SendLayout,
                                   DataAccess, DataReply, CommSubmit, request_for, coverage)
from models.noc.routing import route_bitmap
from models.noc.unit import CommUnit
from models.noc.compiler import compile_calendar, make_installation
from runtime.clock import Clock
from runtime.component import Component
from runtime.service import Service


class ByteEndpoint(Component):
    """A fake TileReg with byte storage and independent timed read/write ports."""

    def __init__(self, node, request, source, arrival, *, token=None, receive=None,
                 read_service=None, write_service=None, read_delay=0):
        token = f'req:{node}' if token is None else token
        super().__init__(f'endpoint:{node}:{token}')
        self.submission = CommSubmit(node, self.name, token, request)
        self.source, self.receive_layout = source, receive
        self.destination = bytearray(receive.capacity) if receive else None
        self.read_service = read_service or dict(throughput=256, latency=2)
        self.write_service = write_service or dict(throughput=256, latency=2)
        self.arrival, self.read_delay = arrival, read_delay
        self.service = Service()
        self.pending, self.written = [], []
        self.submitted = False
        self.completion = None
        self.published = False

    def reply(self, command, event, detail=None):
        self.send('communit', DataReply(command.token, event, command.access_id, detail, command.node))

    def access(self, command):
        if command.token != self.submission.token or command.node != self.submission.node:
            raise ModelError('Unknown request at byte endpoint')
        kind = command.kind
        if kind not in ('read', 'write'):
            raise ModelError('Unknown byte access')
        storage = self.source if kind == 'read' else self.destination
        if storage is None or any(not 0 <= a < b <= len(storage) for a, b in command.ranges):
            raise ModelError('Byte access exceeds binding')
        size = sum(b-a for a, b in command.ranges)
        if kind == 'write' and (not isinstance(command.data, bytes) or len(command.data) != size):
            raise ModelError('Write payload differs from declared size')
        profile = self.read_service if kind == 'read' else self.write_service
        cycles = sum(ceil((b-a) / profile['throughput']) for a, b in command.ranges)
        event = self.service.start(kind, kind, size, cycles, profile['latency'], self.cycle)
        if event is None:
            raise ModelError('Endpoint cannot meet Calendar access service')
        self.pending.append((event['complete'] + (self.read_delay if kind == 'read' else 0), command))

    def tick(self, cycle):
        for _, message in self.inputs(cycle):
            if isinstance(message, Completion):
                if self.receive_layout and message.status == 'success':
                    if coverage(self.written, len(self.destination)) != coverage(
                            self.receive_layout.valid_ranges, len(self.destination)):
                        raise ModelError('Incomplete receive on successful completion')
                    self.published = True
                self.completion = message
            elif isinstance(message, Rejected):
                if not message.retryable:
                    raise ModelError(message.reason)
                self.arrival = cycle + 2
                self.submitted = False
            elif isinstance(message, DataAccess):
                try:
                    self.access(message)
                except ModelError as exc:
                    self.reply(message, 'fault', str(exc))
        if cycle == self.arrival and not self.submitted:
            self.send('communit', self.submission)
            self.submitted = True
        for end, command in self.pending[:]:
            if end > cycle:
                continue
            self.pending.remove((end, command))
            if command.kind == 'read':
                self.reply(command, 'read_done', b''.join(self.source[a:b] for a, b in command.ranges))
            else:
                offset = 0
                for a, b in command.ranges:
                    self.written.append((a, b))
                    self.destination[a:b] = command.data[offset:offset + b-a]
                    offset += b-a
                self.reply(command, 'write_done')

    def is_idle(self):
        return super().is_idle() and self.completion is not None and not self.pending

    def next_event(self):
        times = [end for end, _ in self.pending]
        if not self.submitted and self.completion is None and self.arrival > self.cycle:
            times.append(self.arrival)
        return min(times, default=None)


def demo_layouts(mode):
    """Both cases use four nodes on a row of the 5×8 mesh and 16×16 FP16 tiles."""
    if mode == 'broadcast':
        receive = ReceiveLayout('OUTPUT', 512, 512, ((0, 512),))
        return 1, {0: (SendLayout(0, 16, 32, 32, 'OUTPUT', 0, 32,
                                  route_bitmap((0, 1, 2, 3), (1, 2, 3))), None),
                   **dict.fromkeys((1, 2, 3), (None, receive))}
    receive = ReceiveLayout('OUTPUT', 1536, 1536, ((0, 1536),))
    return 6, {**{node: (SendLayout(0, 16, 32, 32, 'OUTPUT', node * 32, 96,
                                   route_bitmap(range(node, 4), (3,))), None)
                  for node in range(3)}, 3: (None, receive)}


def build_demo(mode, *, buffer_bytes=64, read_delay=0):
    config = wse_config()
    platform = platform_spec(config, access_bytes=32, tx_bytes=buffer_bytes, rx_bytes=buffer_bytes)
    opcode, layouts = demo_layouts(mode)
    installation = make_installation([compile_calendar(opcode, layouts, platform)], platform)
    unit = CommUnit(json.loads(json.dumps(installation)), platform)
    clients = {}
    for node, (send, receive) in layouts.items():
        request = request_for(opcode, send, receive)
        source = bytes((node * 19 + i) % 256 for i in range(512)) if send else b''
        clients[node] = ByteEndpoint(node, request, source, node * 11, receive=receive,
                                     read_service=platform['read_service'],
                                     write_service=platform['write_service'], read_delay=read_delay)
    return unit, clients


def execute(unit, clients):
    components = {c.name: c for c in clients.values()} | {unit.name: unit}
    clock = Clock(components)
    for cycle in range(100000):
        clock.deliver(cycle)
        for component in components.values():
            component.tick(cycle)
        clock.collect(cycle)
        if not clock.pending and (all(c.completion for c in clients.values()) and unit.is_idle() or (
                unit.failure and all(c.completion for c in clients.values()))):
            return cycle
    raise ModelError(f'Streaming demo stalled: {unit.snapshot()}')


def run_demo(mode, *, buffer_bytes=64):
    unit, clients = build_demo(mode, buffer_bytes=buffer_bytes)
    cycle = execute(unit, clients)
    if mode == 'broadcast':
        expected = clients[0].source
    else:
        expected = b''.join(clients[node].source[row:row + 32]
                            for row in range(0, 512, 32) for node in range(3))
    for client in clients.values():
        if client.completion.status != 'success' or (
                client.destination is not None and bytes(client.destination) != expected):
            raise ModelError('Streaming byte check failed')
    return dict(mode=mode, cycle=cycle, drained=unit.drained(), tx_peak=dict(unit.tx_peak),
                rx_peak=dict(unit.rx_peak), platform=unit.platform, events=unit.events)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('broadcast', 'gather'))
    parser.add_argument('--buffer-bytes', type=int, default=64)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    report = run_demo(args.mode, buffer_bytes=args.buffer_bytes)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({k: v for k, v in report.items() if k not in ('events', 'platform')}))
