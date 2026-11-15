"""Core-local storage and port bindings for installed Calendar transfers."""
from math import ceil

from errors import ModelError
from runtime.communication import DataReply, coverage
from runtime.protocol import TileStreamReady, core_name


class StreamTileAccesses:
    def __init__(self):
        self.active, self.running, self.opening = {}, [], []

    def open(self, tile, request):
        submit, wait = tile.nodes[request.submit], tile.nodes[request.wait]
        src = next(iter(submit.inputs), None)
        dst = next(iter(wait.outputs), None)
        meta = submit.raw['communication']
        send, receive = meta['send_layout'], meta['receive_layout']
        read_service, write_service = meta['services']
        if request.submit in self.active:
            raise ModelError('Duplicate Tile stream binding')
        if src and (src not in tile.active or src not in tile.ready):
            raise ModelError('Tile stream source is not ready')
        if dst and dst not in tile.active:
            raise ModelError('Tile stream target has no storage')
        if send and send.src_offset + (send.row_count - 1) * send.src_row_stride + send.row_bytes > tile.versions[src].size:
            raise ModelError('Tile stream source exceeds Tile capacity')
        if receive and receive.capacity > tile.versions[dst].size:
            raise ModelError('Tile stream destination exceeds Tile capacity')
        if any((src and ctx['src']) or (dst and ctx['dst']) for ctx in self.active.values()):
            raise ModelError('Tile stream endpoint ports already reserved')
        self.active[request.submit] = dict(src=src, dst=dst, receive=receive,
            read=read_service, write=write_service, written=[], written_bytes=0,
            valid_bytes=sum(b-a for a, b in receive.valid_ranges) if receive else 0, seen=set())
        ready = max([tile.cycle] + [
            tile.service.available[core_name(submit.core, engine)]
            for version, engine in ((src, 'shared_read'), (dst, 'shared_write')) if version])
        self.opening.append((ready, submit.core, request.submit))

    def reply(self, tile, request, event, detail=None):
        msg = request.access
        endpoint = core_name(tile.nodes[request.submit].core, 'comm')
        tile.send(endpoint, DataReply(msg.token, event, msg.access_id, detail, msg.node))

    def start(self, tile, request):
        command = request.access
        submit = tile.nodes[request.submit]
        ctx = self.active[request.submit]
        kind = command.kind
        if kind not in ('read', 'write') or (kind, command.access_id) in ctx['seen']:
            raise ModelError('Invalid or duplicate Tile stream access')
        version = ctx['src'] if kind == 'read' else ctx['dst']
        if version is None or version not in tile.active:
            raise ModelError('Inactive Tile stream binding')
        capacity = tile.versions[version].size if kind == 'read' else ctx['receive'].capacity
        if any(not 0 <= a < b <= capacity for a, b in command.ranges):
            raise ModelError('Tile stream access exceeds capacity')
        ctx['seen'].add((kind, command.access_id))
        engine = f'shared_{kind}'
        if kind == 'read' and not tile.versions[version].pool.endswith('/shared'):
            engine = f'local_comm_read:{submit.pe}'
        profile = ctx[kind]
        work = sum(b-a for a, b in command.ranges)
        cycles = sum(ceil((b-a) / profile['throughput']) for a, b in command.ranges)
        event = tile.service.start(core_name(submit.core, engine), engine, work,
                                   cycles, profile['latency'], tile.cycle)
        if event is None:
            raise ModelError('TileReg port missed Calendar reservation')
        tile.observe(submit.id if kind == 'read' else request.wait, stage=event)
        self.running.append((event['complete'], request))

    def advance(self, tile):
        for item in self.opening[:]:
            ready, core, submit = item
            if ready <= tile.cycle:
                self.opening.remove(item)
                tile.send(core_name(core, 'comm'), TileStreamReady(submit))
        for item in self.running[:]:
            end, request = item
            if end > tile.cycle:
                continue
            self.running.remove(item)
            access = request.access
            if access.kind == 'write':
                ctx = self.active[request.submit]
                layout = ctx['receive']
                ctx['written'].extend(access.ranges)
                ctx['written_bytes'] += sum(b-a for a, b in access.ranges)
                if ctx['written_bytes'] >= ctx['valid_bytes']:
                    if coverage(ctx['written'], layout.capacity) != coverage(layout.valid_ranges, layout.capacity):
                        self.reply(tile, request, 'fault', 'Invalid Tile stream receive coverage')
                        continue
                    tile.ready.add(ctx['dst'])
            self.reply(tile, request, f'{access.kind}_done')

    def fenced(self, write):
        return any(ctx['dst' if write else 'src'] is not None for ctx in self.active.values())
