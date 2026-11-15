"""Immutable request/data/completion contract shared by independent models."""
from dataclasses import dataclass
import hashlib
import json
from typing import NotRequired, TypedDict

from errors import ModelError


def digest(value):
    # Normalize integer object keys before sorting, so JSON round-trips preserve
    # the digest even when nodes include both single- and two-digit ids.
    normalized = json.loads(json.dumps(value, allow_nan=False))
    encoded = json.dumps(normalized, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()


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


class CommRequest(TypedDict):
    opcode: int
    route_bits: NotRequired[int]
    send_bytes: NotRequired[int]
    expected_receive_bytes: NotRequired[int]


def request_for(opcode, send, receive):
    request = CommRequest(opcode=opcode)
    if send is not None:
        request.update(route_bits=send.route_bits, send_bytes=send.row_count * send.row_bytes)
    if receive is not None:
        request['expected_receive_bytes'] = receive.exp_val
    return request


@dataclass(frozen=True)
class CommSubmit:
    node: int
    endpoint: str
    token: str  # Simulator correlation metadata, outside the submitted operands.
    request: CommRequest


@dataclass(frozen=True)
class DataAccess:
    token: str
    kind: str
    access_id: int
    ranges: tuple  # Compiled half-open storage ranges for this flit.
    node: int
    data: bytes | None = None


@dataclass(frozen=True)
class DataReply:
    token: str
    event: str
    access_id: int
    detail: object = None
    node: int | None = None  # Endpoint-scoped request identity, independent of origin.


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
