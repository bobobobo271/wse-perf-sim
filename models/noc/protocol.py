"""Immutable request/data/completion contract shared by independent models."""
from dataclasses import dataclass
import hashlib
import json

from errors import ModelError


def digest(value):
    # Normalize integer object keys before sorting, so JSON round-trips preserve
    # the digest even when nodes include both single- and two-digit ids.
    normalized = json.loads(json.dumps(value, allow_nan=False))
    encoded = json.dumps(normalized, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class DataRange:
    handle: tuple  # Opaque owner-issued binding + generation; WSE never decodes it.
    offset: int
    nbytes: int
    capacity: int


@dataclass(frozen=True)
class CommRequest:
    token: str
    endpoint: str
    node: int
    operation: str
    epoch: int
    domain: str
    members: tuple
    version: str
    route_bits: tuple
    source: DataRange
    destination: DataRange
    destination_offset: int


@dataclass(frozen=True)
class Accepted:
    token: str


@dataclass(frozen=True)
class Rejected:
    token: str
    reason: str
    retryable: bool


@dataclass(frozen=True)
class Completion:
    token: str
    status: str
    error: str = ''
    safe_to_release: bool = True


@dataclass(frozen=True)
class DataCommand:
    token: str
    kind: str
    region: DataRange
    detail: object = None


@dataclass(frozen=True)
class DataReply:
    token: str
    event: str
    detail: object = None


def request_from_dict(data):
    data = dict(data)
    for key in ('source', 'destination'):
        region = dict(data[key])
        region['handle'] = tuple(region['handle'])
        data[key] = DataRange(**region)
    data['members'], data['route_bits'] = tuple(data['members']), tuple(data['route_bits'])
    return CommRequest(**data)


def platform_spec(config):
    """Only network clocks/rates and declared endpoint write service cross over."""
    return dict(network={k: v['value'] for k, v in config['wse'].items() if not k.startswith('sram_')},
                write_service={k: config['engines']['shared_write'][k] for k in ('throughput', 'latency')},
                endpoint_command_cycles=1, reservation_roundtrip_cycles=4)


@dataclass(frozen=True)
class TileBinding:
    object_id: str
    version: int
    memory_space: str
    owner: tuple


@dataclass(frozen=True)
class SendLayout:
    src_offset: int
    row_count: int
    row_bytes: int
    src_row_stride: int
    recv_sym_base: str
    self_off: int
    recv_row_stride: int
    route_bits: int
    self_copy: bool = False


@dataclass(frozen=True)
class ReceiveLayout:
    recv_sym_base: str
    capacity: int
    exp_val: int
    valid_ranges: tuple  # Half-open logical ranges, including all non-padding bytes.


@dataclass(frozen=True)
class SendDesc:
    binding: TileBinding
    layout: SendLayout


@dataclass(frozen=True)
class ReceiveDesc:
    binding: TileBinding
    layout: ReceiveLayout


@dataclass(frozen=True)
class StreamRequest:
    request_id: str
    origin: tuple
    opcode: int
    epoch: int
    members: tuple
    version: str
    send: SendDesc | None = None
    receive: ReceiveDesc | None = None


@dataclass(frozen=True)
class StreamSubmit:
    node: int
    endpoint: str
    request: StreamRequest


@dataclass(frozen=True)
class StreamAccess:
    token: str
    kind: str
    access_id: int
    handle: tuple | None = None
    offset: int = 0
    nbytes: int = 0
    detail: object = None


@dataclass(frozen=True)
class StreamReply:
    token: str
    event: str
    access_id: int
    detail: object = None


def streaming_platform(config, *, access_bytes, tx_bytes, rx_bytes):
    """Use core service declarations and explicitly supplied endpoint limits."""
    platform = platform_spec(config)
    platform['read_service'] = {
        k: config['engines']['shared_read'][k] for k in ('throughput', 'latency')
    }
    platform.update(access_bytes=access_bytes, tx_bytes=tx_bytes, rx_bytes=rx_bytes)
    return platform


def stream_request_from_dict(data):
    data = dict(data)
    data['origin'], data['members'] = tuple(data['origin']), tuple(data['members'])
    for key, desc_type, layout_type in (
            ('send', SendDesc, SendLayout), ('receive', ReceiveDesc, ReceiveLayout)):
        if data[key] is None:
            continue
        desc = data[key]
        binding, layout = dict(desc['binding']), dict(desc['layout'])
        binding['owner'] = tuple(binding['owner'])
        if key == 'receive':
            layout['valid_ranges'] = tuple(map(tuple, layout['valid_ranges']))
        data[key] = desc_type(TileBinding(**binding), layout_type(**layout))
    return StreamRequest(**data)


def coverage(ranges, capacity):
    """Normalize adjacent ranges while rejecting overlaps and invalid bounds."""
    merged = []
    for start, end in sorted(ranges):
        if (type(start) is not int or type(end) is not int or not 0 <= start < end <= capacity
                or (merged and start < merged[-1][1])):
            raise ModelError('Overlapping or out-of-range receive bytes')
        if merged and start == merged[-1][1]:
            merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return merged
