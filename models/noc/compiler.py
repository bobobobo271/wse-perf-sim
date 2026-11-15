"""Offline placement for the v1 copy protocol, with finite endpoint buffers."""
from collections import Counter, defaultdict
from dataclasses import asdict
from fractions import Fraction
from math import ceil

from errors import ModelError
from runtime.communication import coverage, digest, request_for
from models.noc.routing import integer, node_id, route_tree


def storage_ranges(offset, size, base, row_bytes, stride, access_bytes):
    """Map a flit's valid bytes to local accesses, including across padded rows."""
    ranges = []
    while size:
        row, col = divmod(offset, row_bytes)
        start = base + row * stride + col
        stop = start + (size if stride == row_bytes else min(size, row_bytes - col))
        for begin in range(start, stop, access_bytes):
            ranges.append((begin, min(begin + access_bytes, stop)))
        offset += stop - start
        size -= stop - start
    return ranges


def compile_calendar(opcode, layouts, platform):
    """Compile one phase from {node: (SendLayout | None, ReceiveLayout | None)}."""
    p = platform['network']
    integer(opcode, 'copy opcode', 1)
    if not layouts:
        raise ModelError('No participating nodes')
    for node in layouts:
        node_id(node)
    payload = integer(p['link_bytes_per_cycle'] - p['header_bytes'], 'flit payload', 1)
    tx = integer(platform['tx_bytes'], 'TX capacity', 1)
    rx = integer(platform['rx_bytes'], 'RX capacity', 1)
    access_bytes = integer(platform['access_bytes'], 'access size', 1)
    ratio = Fraction(p['noc_clock_hz'], p['core_clock_hz'])
    service = 1
    expected, remote, routes, segments = defaultdict(list), Counter(), {}, []
    if any(not isinstance(desc.recv_sym_base, str) or not desc.recv_sym_base
           for pair in layouts.values() for desc in pair if desc is not None):
        raise ModelError('Receive base must be a nonempty logical name')
    max_network_cycles = 0
    for node, (send, receive) in sorted(layouts.items()):
        if send is None and receive is None:
            raise ModelError('Each participant must send or receive')
        if send is None:
            continue
        integer(send.src_offset, 'source offset')
        integer(send.self_off, 'destination offset')
        integer(send.row_count, 'row count', 1)
        integer(send.row_bytes, 'row bytes', 1)
        integer(send.src_row_stride, 'source stride', send.row_bytes)
        integer(send.recv_row_stride, 'destination stride', send.row_bytes)
        if send.route_bits == 0 and send.self_copy:
            children, landing, distance = {node: ()}, set(), {node: 0}
        else:
            children, landing, distance = route_tree(node, send.route_bits)
        if node in landing or not landing <= layouts.keys():
            raise ModelError('Route must land at registered remote participants; use selfCopy for local data')
        targets = sorted(landing | ({node} if send.self_copy else set()))
        for target in targets:
            dest = layouts[target][1]
            if dest is None or dest.recv_sym_base != send.recv_sym_base:
                raise ModelError('Missing matching receive registration')
        routes[node] = dict(children=children, distance=distance)
        max_network_cycles = max(max_network_cycles, p['tx_cdc_cycles'] + 1
                                 + max(distance.values()) * p['hop_cycles'] + p['rx_cdc_cycles'] + p['guard_cycles'])
        for row in range(send.row_count):
            start = send.self_off + row * send.recv_row_stride
            for target in targets:
                expected[target].append((start, start + send.row_bytes))
                if target != node:
                    remote[target] += send.row_bytes
        total = send.row_bytes * send.row_count
        if min(payload, total) > min(tx, rx):
            raise ModelError('Endpoint buffers must hold one flit of valid data')
        for offset in range(0, total, payload):
            size = min(payload, total - offset)
            src = storage_ranges(offset, size, send.src_offset, send.row_bytes,
                                 send.src_row_stride, access_bytes)
            dst = storage_ranges(offset, size, send.self_off, send.row_bytes,
                                 send.recv_row_stride, access_bytes)
            segments.append(dict(source=node, targets=targets, nbytes=size, src=src, dst=dst))
            # One command/reply per flit, with local access cost for every span.
            for ranges, key in ((src, 'read_service'), (dst, 'write_service')):
                rate = integer(platform[key]['throughput'], key, 1)
                latency = integer(platform[key]['latency'], key + ' latency')
                service = max(service, sum(ceil((b-a) / rate) for a, b in ranges) + latency + 5)
    senders = set(routes)
    receivers = {node for node, (_, receive) in layouts.items() if receive is not None}
    if not (len(senders) == 1 or len(receivers) == 1):
        raise ModelError('Streaming v1 supports one-to-many broadcast or many-to-one gather')
    for node in receivers:
        receive = layouts[node][1]
        capacity = integer(receive.capacity, 'capacity', 1)
        if remote[node] != integer(receive.exp_val, 'expVal'):
            raise ModelError('Remote traffic differs from expVal')
        valid = coverage(receive.valid_ranges, capacity)
        if not valid or coverage(expected[node], capacity) != valid:
            raise ModelError('Transfers do not cover the target valid ranges')

    stride = max(service, ceil(max_network_cycles / ratio) + 1)
    tx_slots, rx_slots = Counter(), Counter()
    slot = 0
    for segment in segments:
        size, source, targets = segment['nbytes'], segment['source'], segment['targets']
        while (any(tx_slots[source, s] + size > tx for s in range(slot, slot + 3))
               or any(rx_slots[node, s] + size > rx for node in targets for s in (slot + 1, slot + 2))):
            slot += 1
        for s in range(slot, slot + 3):
            tx_slots[source, s] += size
        for node in targets:
            for s in (slot + 1, slot + 2):
                rx_slots[node, s] += size
        segment.update(read_cycle=slot * stride, inject_cycle=(slot + 1) * stride,
                       write_cycle=(slot + 2) * stride, check_cycle=(slot + 3) * stride)
        # Fully expand relative link events offline. Runtime only adds align origin.
        route = routes[source]
        start = ceil(segment['inject_cycle'] * ratio) + p['tx_cdc_cycles']
        segment['links'] = [dict(link=(parent, child), resource=f'link:{parent}->{child}',
            flit=slot, noc_cycle=start + route['distance'][parent] * p['hop_cycles'],
            payload_bytes=size, wire_bytes=p['link_bytes_per_cycle'])
            for parent, children in route['children'].items() for child in children]
        segment['send_cycle'] = ceil((start + 1) / ratio) if segment['links'] else segment['inject_cycle']
        slot += 1
    entries = {node: request_for(opcode, send, receive)
               for node, (send, receive) in sorted(layouts.items())}
    layout_hashes = {node: digest([asdict(desc) if desc else None for desc in pair])
                     for node, pair in layouts.items()}
    return dict(opcode=opcode, entries=entries, layout_hashes=layout_hashes,
                segments=segments, stride=stride)


def make_installation(calendars, platform):
    """Install reusable CalReg[opcode] profiles, for sequential rounds."""
    profiles = {}
    for calendar in calendars:
        opcode = calendar['opcode']
        if opcode in profiles and digest(profiles[opcode]) != digest(calendar):
            raise ModelError('Different layouts/routes need different CalReg opcodes')
        profiles[opcode] = calendar
    if not profiles:
        raise ModelError('No Calendar profiles to install')
    body = dict(schema='wse-communit/3', platform_hash=digest(platform), calreg=profiles)
    return dict(body, version=digest(body))
