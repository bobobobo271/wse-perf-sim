"""Addressed SRAM contract (#9). No Tile bindings, routes or slot calculation."""
from dataclasses import dataclass


@dataclass(frozen=True)
class MemoryRequest:
    request_id: tuple  # (source node, sequence), unique within one launch
    src_core: int
    dst_core: int
    op: str
    offset_bytes: int
    nbytes: int
    data: bytes | None = None


@dataclass(frozen=True)
class MemorySubmit:
    endpoint: str
    request: MemoryRequest


@dataclass(frozen=True)
class MemoryAccepted:
    request_id: tuple


@dataclass(frozen=True)
class MemoryRejected:
    request_id: tuple
    reason: str
    retryable: bool


@dataclass(frozen=True)
class MemoryDone:
    request_id: tuple
    status: str
    data: bytes | None = None
    error: str = ''
    stages: tuple = ()  # Timing evidence; no instruction/Tile identities.


@dataclass(frozen=True)
class MemoryRetired:
    request_id: tuple  # Completion consumed: return entry credit.


def valid_request(r):
    return (isinstance(r.request_id, tuple) and len(r.request_id)==2
            and all(type(n) is int and n>=0 for n in r.request_id)
            and type(r.src_core) is int and type(r.dst_core) is int
            and r.request_id[0]==r.src_core and r.op in ('read','write')
            and type(r.offset_bytes) is int and r.offset_bytes>=0
            and type(r.nbytes) is int and r.nbytes>0
            and (r.data is None or (r.op=='write' and isinstance(r.data,bytes) and len(r.data)==r.nbytes)))
