"""One chip-wide FIFO bandwidth server; no banks, caches or DRAM timing."""
import math
from collections import deque

from .base import Component
from .protocol import HBMRequest, HBMDone, core_name
from .service import Service


class HBM(Component):
    def __init__(self, config):
        super().__init__('hbm')
        self.config = config
        self.queue, self.active = deque(), None
        self.service = Service()
        self.events = []

    def tick(self, cycle):
        for _, request in self.inputs(cycle):
            if not isinstance(request, HBMRequest):
                raise TypeError(request)
            self.queue.append((cycle, request))
        if self.active is not None:
            request, end = self.active
            if end <= cycle:
                self.send(core_name(request.core, 'tlsu'), HBMDone(request.instruction))
                self.active = None
        if self.active is None and self.queue:
            arrived, request = self.queue.popleft()
            frequency = self.config['wse']['core_clock_hz']['value']
            spec = self.config['hbm']
            # Startup occupies this simple serialized server too. It is explicit
            # and defaults to zero for a pure bandwidth model.
            duration = max(1, math.ceil(frequency * (
                spec['startup_ns'] / 1e9 + request.work / spec['bandwidth_bytes_per_second'])))
            event = self.service.start('chip/hbm', 'memory', request.work, duration, 0, cycle)
            if request.first_stage:
                self.observe(request.instruction, compute_start=cycle)
            self.observe(request.instruction, stage=event)
            self.events.append(dict(op_id=request.instruction, core=request.core, direction=request.direction,
                                    queued_cycle=arrived, start_cycle=cycle, end_cycle=event['complete'],
                                    bytes=request.work))
            self.active = request, event['complete']

    def is_idle(self):
        return super().is_idle() and not self.queue and self.active is None

    def next_event(self):
        return None if self.active is None else self.active[1]

    def snapshot(self):
        return dict(super().snapshot(), queued=len(self.queue),
                    active=None if self.active is None else self.active[0].instruction)
