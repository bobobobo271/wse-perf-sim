"""Composition root: construct, clock, diagnose and report components."""
import json

from .clock import Clock
from .components import Cube, Vector, TLSU, ROB, Scheduler, Sync, TileReg
from .components.protocol import core_name, unit_name
from .errors import ModelError
from .report import build_report


class Simulator:
    def __init__(self, trace, config, tick_order=None):
        self.trace, self.config = trace, config
        self.nodes = trace.instructions
        self.components = {}
        cores = sorted({i.core for i in self.nodes.values()})
        subscribers = [core_name(c, unit) for c in cores for unit in ('scheduler', 'sync')]
        for core in cores:
            nodes = {k: i for k, i in self.nodes.items() if i.core == core}
            real = [i for i in nodes.values() if not i.control]
            control = [i for i in nodes.values() if i.control]
            for unit, component in (
                ('scheduler', Scheduler(core_name(core, 'scheduler'), real, config)),
                ('rob', ROB(core_name(core, 'rob'), list(nodes.values()), subscribers)),
                ('tile', TileReg(core_name(core, 'tile'), nodes, trace.versions, config)),
                ('sync', Sync(core_name(core, 'sync'), control, config)),
                ('tlsu', TLSU(core_name(core, 'tlsu'),
                            {i.id: i for i in real if unit_name(i) == core_name(core, 'tlsu')}, config)),
            ):
                self.components[core_name(core, unit)] = component
            for pe in range(config['parameters']['pe_count']['value']):
                for unit, cls in (('cube', Cube), ('vector', Vector)):
                    name = core_name(core, f'pe:{pe}/{unit}')
                    self.components[name] = cls(name, {i.id: i for i in real if unit_name(i) == name}, config)
        self.clock = Clock(self.components, config.get('message_log', False))
        self.tick_order = list(tick_order) if tick_order is not None else sorted(self.components)
        if len(self.tick_order) != len(self.components) or set(self.tick_order) != set(self.components):
            raise ValueError('tick_order must contain each component exactly once')
        self.cycle = 0

    def snapshot(self):
        return {'cycle': self.cycle, 'messages_in_flight': len(self.clock.pending),
                'components': {name: c.snapshot() for name, c in sorted(self.components.items())}}

    def run(self):
        while True:
            before = sum(c.progress for c in self.components.values())
            self.clock.deliver(self.cycle)
            for name in self.tick_order:
                self.components[name].tick(self.cycle)
            self.clock.collect(self.cycle)
            if not self.clock.pending and all(c.is_idle() for c in self.components.values()):
                return build_report(self)
            progress = sum(c.progress for c in self.components.values()) > before
            future = any(c.next_event() is not None for c in self.components.values())
            if not progress and not future and not self.clock.pending:
                raise ModelError('Deadlock at cycle ' + str(self.cycle) + ': ' +
                                 json.dumps(self.snapshot(), sort_keys=True))
            self.cycle += 1
