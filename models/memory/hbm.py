"""Concurrent per-core transfers sharing one chip-wide HBM bandwidth limit."""
import math
from collections import defaultdict, deque
from fractions import Fraction

from config import HBM_DEFAULTS
from runtime.component import Component
from runtime.protocol import HBMRequest, HBMDone, core_name
from runtime.service import Service


class HBM(Component):
    def __init__(self, config):
        super().__init__('hbm')
        frequency = config['wse']['core_clock_hz']['value']
        spec = HBM_DEFAULTS | config['hbm']
        self.chip_rate = Fraction(str(spec['bandwidth_bytes_per_second'])) / frequency
        self.core_rate = Fraction(str(spec['per_core_bytes_per_cycle']))
        self.startup = math.ceil(Fraction(str(spec['startup_ns'])) * frequency / 10**9)
        self.queues = defaultdict(deque)
        self.starting, self.active = {}, {}
        self.updated, self.event_cycle = 0, None
        self.service = Service()
        self.events = []

    def transfer_rate(self):
        return min(self.core_rate, self.chip_rate / len(self.active))

    def tick(self, cycle):
        if not self.inbox and (self.event_cycle is None or cycle < self.event_cycle):
            return
        for _, request in self.inputs(cycle):
            if not isinstance(request, HBMRequest):
                raise TypeError(request)
            self.queues[request.core].append((cycle, request))

        # Apply the previous share up to this edge, before starting new transfers.
        elapsed = cycle - self.updated
        self.updated = cycle
        if self.active:
            transferred = elapsed * self.transfer_rate()
            self.service.busy['chip/hbm'] += elapsed
            for core, (request, event, remaining) in list(self.active.items()):
                if remaining > transferred:
                    self.active[core] = request, event, remaining - transferred
                    continue
                del self.active[core]
                event['end_cycle'] = cycle
                self.service.bytes['chip/hbm'] += request.work
                self.observe(request.instruction, stage=dict(
                    engine='memory', resource='chip/hbm', work=request.work,
                    start=event['start_cycle'], service_end=cycle, complete=cycle))
                self.send(core_name(core, 'tlsu'), HBMDone(request.instruction))

        for core, queue in list(self.queues.items()):
            if core in self.starting or core in self.active:
                continue
            arrived, request = queue.popleft()
            self.starting[core] = cycle + self.startup, arrived, request
            if not queue:
                del self.queues[core]

        for core, (ready, arrived, request) in list(self.starting.items()):
            if ready > cycle:
                continue
            del self.starting[core]
            event = dict(op_id=request.instruction, core=core, direction=request.direction,
                         queued_cycle=arrived, start_cycle=cycle, bytes=request.work)
            self.events.append(event)
            self.active[core] = request, event, request.work
            if request.first_stage:
                self.observe(request.instruction, compute_start=cycle)

        self.event_cycle = min((ready for ready, _, _ in self.starting.values()), default=None)
        if self.active:
            # Completion is visible on a core edge. The share is fixed within each cycle.
            remaining = min(remaining for _, _, remaining in self.active.values())
            completion = cycle + math.ceil(remaining / self.transfer_rate())
            self.event_cycle = completion if self.event_cycle is None else min(self.event_cycle, completion)

    def is_idle(self):
        return super().is_idle() and not self.queues and not self.starting and not self.active

    def next_event(self):
        return self.event_cycle

    def snapshot(self):
        return dict(super().snapshot(), queued=sum(map(len, self.queues.values())),
                    starting={core: request.instruction for core, (_, _, request) in self.starting.items()},
                    active={core: request.instruction for core, (request, _, _) in self.active.items()})
