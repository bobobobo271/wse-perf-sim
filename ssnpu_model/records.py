"""Internal instruction, storage version, and execution stage records."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Instruction:
    id: object
    core: int
    pe: int
    task: int
    block: int
    age: int
    raw: dict
    deps: set = field(default_factory=set)
    sync: set = field(default_factory=set)
    inputs: set = field(default_factory=set)
    outputs: set = field(default_factory=set)
    alias: bool = False
    control: bool = False


@dataclass
class Version:
    key: tuple
    obj: dict
    producer: object
    pool: str
    slot: tuple
    size: int
    consumers: set = field(default_factory=set)
    retained: bool = False


@dataclass
class Stage:
    engine: str
    resource: str
    work: int
    service: int
    latency: int
    unit: str
