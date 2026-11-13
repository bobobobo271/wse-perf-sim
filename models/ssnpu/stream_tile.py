"""TileReg's streaming data-access path; all storage and port state stays local."""
from math import ceil

from errors import ModelError
from models.noc.protocol import StreamReply, coverage
from runtime.protocol import core_name


class StreamTileAccesses:
    def __init__(self):
        self.active, self.running = {}, []

    def reply(self, tile, request, event, detail=None):
        msg = request.access
        endpoint = core_name(tile.nodes[request.submit].core, 'comm')
        tile.send(endpoint, StreamReply(msg.token, event, msg.access_id, detail))

    def start(self, tile, request):
        command = request.access
        submit, wait = tile.nodes[request.submit], tile.nodes[request.wait]
        if command.kind == 'prepare':
            send, receive, read_service, write_service = command.detail
            src = next(iter(submit.inputs), None)
            dst = next(iter(wait.outputs), None)
            if request.submit in self.active or (bool(src), bool(dst)) != (bool(send), bool(receive)):
                raise ModelError('Invalid streaming prepare binding or duplicate request')
            for version, desc in ((src, send), (dst, receive)):
                if desc:
                    obj = tile.versions[version].obj
                    owner = (submit.core,) if obj['memory_space'] == 'SharedTileReg' else (submit.core, submit.pe)
                    if (obj['object_id'], obj['version'], obj['memory_space'], owner) != (
                            desc.binding.object_id, desc.binding.version, desc.binding.memory_space, desc.binding.owner):
                        raise ModelError('Streaming prepare uses a stale tile version or owner')
            bindings = (src, dst)
            for version, desc, allowed in ((src, send, tile.ready), (dst, receive, tile.active)):
                if desc is not None and (version not in allowed or version not in tile.active):
                    raise ModelError('Streaming prepare uses an inactive or unready tile')
            if send:
                layout = send.layout
                end = layout.src_offset + (layout.row_count - 1) * layout.src_row_stride + layout.row_bytes
                if end > tile.versions[src].size:
                    raise ModelError('Streaming source exceeds tile capacity')
            if receive and receive.layout.capacity > tile.versions[dst].size:
                raise ModelError('Streaming destination exceeds tile capacity')
            if any((src and ctx['src']) or (dst and ctx['dst']) for ctx in self.active.values()):
                raise ModelError('Streaming endpoint ports already reserved')
            self.active[request.submit] = dict(src=src, dst=dst, receive=receive, read=read_service, write=write_service,
                                              source_layout=send.layout if send else None,
                                              written=[], reserved=[], seen=set(), published=False)
            ready = max([tile.cycle] + [
                tile.service.available[core_name(submit.core, engine)]
                for version, engine in ((src, 'shared_read'), (dst, 'shared_write')) if version])
            self.running.append((ready, request, 'prepared', bindings))
            return

        ctx = self.active[request.submit]
        kind = command.kind
        version = ctx['src'] if kind == 'read' else ctx['dst']
        if version is None or command.handle != version or version not in tile.active:
            raise ModelError('Stale or unauthorized streaming handle')
        if kind == 'publish':
            layout = ctx['receive'].layout
            if (ctx['published'] or
                    coverage(ctx['written'], layout.capacity) != coverage(layout.valid_ranges, layout.capacity)):
                raise ModelError('Invalid streaming publication coverage')
            tile.ready.add(version)
            ctx['published'] = True
            self.reply(tile, request, 'published')
            return
        if kind not in ('read', 'write') or (kind, command.access_id) in ctx['seen']:
            raise ModelError('Invalid or duplicate streaming access')
        start, stop = command.offset, command.offset + command.nbytes
        capacity = tile.versions[version].size if kind == 'read' else ctx['receive'].layout.capacity
        if not 0 <= start < stop <= capacity:
            raise ModelError('Streaming access exceeds tile capacity')
        if kind == 'write':
            allowed = coverage(ctx['receive'].layout.valid_ranges, capacity)
            if not any(a <= start and stop <= b for a, b in allowed):
                raise ModelError('Streaming write outside authorized target range')
            if any(a < stop and start < b for a, b in ctx['reserved']):
                raise ModelError('Streaming write overlaps a pending or committed segment')
            ctx['reserved'].append((start, stop))
        else:
            layout = ctx['source_layout']
            row, row_offset = divmod(start - layout.src_offset, layout.src_row_stride)
            if not 0 <= row < layout.row_count or row_offset + command.nbytes > layout.row_bytes:
                raise ModelError('Streaming read outside authorized source rows')
        ctx['seen'].add((kind, command.access_id))
        engine = f'shared_{kind}'
        if kind == 'read' and not tile.versions[version].pool.endswith('/shared'):
            engine = f'local_comm_read:{submit.pe}'
        profile = ctx[kind]
        event = tile.service.start(core_name(submit.core, engine), engine, command.nbytes,
                                   ceil(command.nbytes / profile['throughput']), profile['latency'], tile.cycle)
        if event is None:
            raise ModelError('TileReg port missed streaming Calendar reservation')
        tile.observe(submit.id if kind == 'read' else wait.id, stage=event)
        self.running.append((event['complete'], request, f'{kind}_done', None))

    def advance(self, tile):
        for item in self.running[:]:
            end, request, event, detail = item
            if end <= tile.cycle:
                self.running.remove(item)
                if event == 'write_done':
                    access = request.access
                    self.active[request.submit]['written'].append((access.offset, access.offset + access.nbytes))
                self.reply(tile, request, event, detail)

    def fenced(self, write):
        return any(ctx['dst' if write else 'src'] is not None for ctx in self.active.values())
