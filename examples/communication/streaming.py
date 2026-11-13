"""Run issue #4's byte-exact broadcast and gather against independent endpoints."""
import argparse
import json
from math import ceil
from pathlib import Path

from config import wse_config
from errors import ModelError
from models.noc.protocol import (Accepted, Completion, ReceiveDesc, ReceiveLayout, Rejected,
    SendDesc, SendLayout, StreamAccess, StreamReply, StreamRequest, StreamSubmit,
    TileBinding, coverage, streaming_platform)
from models.noc.routing import route_bitmap
from models.noc.stream import StreamingCommUnit
from models.noc.stream_plan import compile_stream_round, install_stream_rounds
from runtime.clock import Clock
from runtime.component import Component
from runtime.service import Service


class ByteEndpoint(Component):
    """A fake TileReg with byte storage and independent timed read/write ports."""

    def __init__(self, node, request, source, arrival, *, read_delay=0):
        super().__init__(f'endpoint:{node}:{request.epoch}')
        self.submission = StreamSubmit(node, self.name, request)
        self.source = source
        self.destination = bytearray(request.receive.layout.capacity) if request.receive else None
        self.arrival = arrival
        self.read_delay = read_delay
        self.handles = tuple(
            (desc.binding.object_id, desc.binding.version) if desc else None
            for desc in (request.send, request.receive))
        self.service = Service()
        self.pending, self.written = [], []
        self.accepted = self.completion = None
        self.published = False

    def reply(self, command, event, detail=None):
        self.send('communit', StreamReply(command.token, event, command.access_id, detail))

    def access(self, command):
        request = self.submission.request
        if command.token != request.request_id:
            raise ModelError('Unknown request at byte endpoint')
        kind = command.kind
        if kind == 'prepare':
            send, receive, self.read_service, self.write_service = command.detail
            if (send, receive) != (request.send, request.receive):
                raise ModelError('Prepare uses stale bindings')
            if send:
                layout = send.layout
                if layout.src_offset + (layout.row_count - 1) * layout.src_row_stride + layout.row_bytes > len(self.source):
                    raise ModelError('Source layout exceeds binding')
            self.reply(command, 'prepared', self.handles)
            return
        index = 0 if kind == 'read' else 1
        if command.handle is None or command.handle != self.handles[index]:
            raise ModelError('Stale or unauthorized data handle')
        if kind == 'publish':
            if self.published or coverage(self.written, len(self.destination)) != coverage(command.detail, len(self.destination)):
                raise ModelError('Incomplete or duplicate publication')
            self.published = True
            self.reply(command, 'published')
            return
        if kind not in ('read', 'write'):
            raise ModelError('Unknown byte access')
        storage = self.source if kind == 'read' else self.destination
        start, stop = command.offset, command.offset + command.nbytes
        if not 0 <= start < stop <= len(storage):
            raise ModelError('Byte access exceeds binding')
        if kind == 'write' and (not isinstance(command.detail[1], bytes) or len(command.detail[1]) != command.nbytes):
            raise ModelError('Write payload differs from declared size')
        profile = self.read_service if kind == 'read' else self.write_service
        event = self.service.start(kind, kind, command.nbytes, ceil(command.nbytes / profile['throughput']),
                                   profile['latency'], self.cycle)
        if event is None:
            raise ModelError('Endpoint cannot meet Calendar access service')
        self.pending.append((event['complete'] + (self.read_delay if kind == 'read' else 0), command))

    def tick(self, cycle):
        for _, message in self.inputs(cycle):
            if isinstance(message, Accepted):
                self.accepted = cycle
            elif isinstance(message, Completion):
                self.completion = message
            elif isinstance(message, Rejected):
                if not message.retryable:
                    raise ModelError(message.reason)
                self.arrival = cycle + 2
            elif isinstance(message, StreamAccess):
                try:
                    self.access(message)
                except ModelError as exc:
                    self.reply(message, 'fault', str(exc))
        if cycle == self.arrival:
            self.send('communit', self.submission)
        for end, command in self.pending[:]:
            if end > cycle:
                continue
            self.pending.remove((end, command))
            start, stop = command.offset, command.offset + command.nbytes
            if command.kind == 'read':
                self.reply(command, 'read_done', self.source[start:stop])
            else:
                self.written.append((start, stop))
                self.destination[start:stop] = command.detail[1]
                self.reply(command, 'write_done')


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
    platform = streaming_platform(config, access_bytes=32, tx_bytes=buffer_bytes, rx_bytes=buffer_bytes)
    opcode, layouts = demo_layouts(mode)
    installation = install_stream_rounds([compile_stream_round(opcode, 1, layouts, platform)], platform)
    unit = StreamingCommUnit(json.loads(json.dumps(installation)), platform)
    clients = {}
    for node, (send, receive) in layouts.items():
        request = StreamRequest(f'req:{node}', (node, 0, 'COMM.SUBMIT'), opcode, 1, tuple(layouts),
            installation['version'],
            SendDesc(TileBinding('X', 1, 'LocalTileReg', (node, 0)), send) if send else None,
            ReceiveDesc(TileBinding('Y', 1, 'SharedTileReg', (node,)), receive) if receive else None)
        source = bytes((node * 19 + i) % 256 for i in range(512)) if send else b''
        clients[node] = ByteEndpoint(node, request, source, node * 11, read_delay=read_delay)
    return unit, clients


def execute(unit, clients):
    components = {c.name: c for c in clients.values()} | {unit.name: unit}
    clock = Clock(components)
    for cycle in range(100000):
        clock.deliver(cycle)
        for component in components.values():
            component.tick(cycle)
        clock.collect(cycle)
        if not clock.pending and (unit.is_idle() or (
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
