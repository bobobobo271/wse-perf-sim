"""Typed component messages. The clock supplies source, destination and time."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Allocate:
    instruction: object
    request: str


@dataclass(frozen=True)
class Allocated:
    instruction: object
    request: str


@dataclass(frozen=True)
class Dispatch:
    instruction: object


@dataclass(frozen=True)
class Issued:
    instruction: object


@dataclass(frozen=True)
class Access:
    instruction: object
    request: str
    version: tuple
    write: bool
    work: int
    dtype: str


@dataclass(frozen=True)
class AccessDone:
    instruction: object
    request: str


@dataclass(frozen=True)
class Complete:
    instruction: object


@dataclass(frozen=True)
class Completed:
    instruction: object


@dataclass(frozen=True)
class Consumed:
    instruction: object


@dataclass(frozen=True)
class Envelope:
    source: str
    destination: str
    port: str
    message: object
    sent: int
    received: int


def core_name(core, unit):
    return f'core:{core}/{unit}'


def unit_name(i):
    op = i.raw['ir_op'].removeprefix('tile.')
    if i.alias:
        return core_name(i.core, 'tile')
    if op.startswith('calendar.') or op in ('load', 'store', 'ssnpu.load_into'):
        return core_name(i.core, 'tlsu')
    return core_name(i.core, f'pe:{i.pe}/' + ('cube' if op in ('matmul', 'matmul_acc') else 'vector'))


@dataclass(frozen=True)
class CalendarCommand:
    instruction: object


@dataclass(frozen=True)
class CalendarComplete:
    instruction: object


@dataclass(frozen=True)
class ReceivePrepare:
    instruction: object
    version: tuple
    expected: tuple  # (source, offset, nbytes)


@dataclass(frozen=True)
class PortFence:
    instruction: object


@dataclass(frozen=True)
class WriteReservations:
    instruction: object
    entries: tuple  # (source, start cycle, service cycles, result latency)


@dataclass(frozen=True)
class RouterWrite:
    instruction: object
    version: tuple
    source: int
    offset: int
    work: int


@dataclass(frozen=True)
class PublishReceive:
    instruction: object


@dataclass(frozen=True)
class CalendarReply:
    instruction: object
    event: str
    detail: object = None
