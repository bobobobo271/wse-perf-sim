"""Offline placement for the v1 copy protocol, with finite endpoint buffers."""
from collections import Counter, defaultdict
from dataclasses import asdict
from fractions import Fraction
from math import ceil

from errors import ModelError
from models.noc.protocol import coverage, digest
from models.noc.routing import integer, node_id, route_tree


def compile_stream_round(opcode, epoch, layouts, platform):
    """Compile one phase from {node: (SendLayout | None, ReceiveLayout | None)}."""
    p = platform['network']
    integer(opcode, 'copy opcode', 1)
    if opcode >= 7 or opcode in (2, 3, 4):
        raise ModelError('Streaming v1 accepts only copy opcodes')
    if integer(epoch, 'epoch') >= 1 << p['epoch_bits']:
        raise ModelError('Epoch exceeds configured tag width')
    if not layouts:
        raise ModelError('No participating nodes')
    for node in layouts:
        node_id(node)
    payload = integer(p['link_bytes_per_cycle'] - p['header_bytes'], 'flit payload', 1)
    tx = integer(platform['tx_bytes'], 'TX capacity', 1)
    rx = integer(platform['rx_bytes'], 'RX capacity', 1)
    chunk = min(integer(platform['access_bytes'], 'access size', 1), tx, rx)
    ratio = Fraction(p['noc_clock_hz'], p['core_clock_hz'])
    # Both clocks meet every ratio.denominator core cycles. At those edges the
    # read/NoC/write pipeline can use one repeatable relative schedule.
    # Two queue handoffs in each direction through CommPort, plus one guard edge.
    service = max(
        ceil(chunk / integer(platform[k]['throughput'], k, 1))
        + integer(platform[k]['latency'], k + ' latency') + 5
        for k in ('read_service', 'write_service')
    )
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
        children, landing, distance = route_tree(node, send.route_bits)
        if node in landing or not landing <= layouts.keys():
            raise ModelError('Route must land at registered remote participants; use selfCopy for local data')
        targets = sorted(landing | ({node} if send.self_copy else set()))
        for target in targets:
            dest = layouts[target][1]
            if dest is None or dest.recv_sym_base != send.recv_sym_base:
                raise ModelError('Missing matching receive registration')
        routes[node] = dict(children=children, distance=distance)
        max_network_cycles = max(max_network_cycles, p['tx_cdc_cycles'] + ceil(chunk / payload)
                                 + max(distance.values()) * p['hop_cycles'] + p['rx_cdc_cycles'] + p['guard_cycles'])
        for row in range(send.row_count):
            start = send.self_off + row * send.recv_row_stride
            for target in targets:
                expected[target].append((start, start + send.row_bytes))
                if target != node:
                    remote[target] += send.row_bytes
            for offset in range(0, send.row_bytes, chunk):
                segments.append(dict(source=node, targets=targets, nbytes=min(chunk, send.row_bytes - offset),
                                     src=send.src_offset + row * send.src_row_stride + offset,
                                     dst=start + offset))
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

    stride = ratio.denominator * ceil(max(service, ceil(max_network_cycles / ratio) + 1)
                                     / ratio.denominator)
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
        segment['read_cycle'] = slot * stride
        slot += 1
    entries = {node: dict(send=asdict(send) if send else None, receive=asdict(receive) if receive else None)
               for node, (send, receive) in sorted(layouts.items())}
    return dict(opcode=opcode, epoch=epoch, entries=entries, routes=routes, segments=segments, stride=stride)


def install_stream_rounds(rounds, platform):
    """Seal precompiled schedules and resource parameters into one version."""
    keys = [(r['opcode'], r['epoch']) for r in rounds]
    if not keys or len(keys) != len(set(keys)):
        raise ModelError('Provide unique opcode/epoch rounds within one launch')
    body = dict(schema='wse-communit-stream/1', platform_hash=digest(platform), calreg=rounds)
    return dict(body, version=digest(body))
