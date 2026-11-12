"""Dependency tracking, one-shot output reservations and fair dispatch only."""
from collections import Counter, defaultdict

from .base import Component
from .protocol import Allocate, Allocated, Completed, Dispatch, Issued, core_name, unit_name


class Scheduler(Component):
    def __init__(self, name, instructions, config):
        super().__init__(name)
        self.nodes = {i.id: i for i in instructions}
        self.pending = sorted(instructions, key=lambda i: (i.task, i.age, str(i.id)))
        self.status = {i.id: 'waiting_dependencies' for i in instructions}
        self.done = set()
        self.config, self.rotation = config, 0
        self.waits = defaultdict(Counter)

    def tick(self, cycle):
        for _, message in self.inputs(cycle):
            if isinstance(message, Completed):
                self.done.add(message.instruction)
            elif isinstance(message, Allocated):
                assert self.status[message.instruction] == 'allocation_pending'
                self.status[message.instruction] = 'dispatch_ready'
                self.observe(message.instruction, allocated=cycle, state='dispatch_ready')
            else:
                raise TypeError(message)
        for i in self.pending:
            if self.status[i.id] == 'waiting_dependencies' and (i.deps | i.sync) <= self.done:
                if i.alias:
                    self.status[i.id] = 'dispatch_ready'
                else:
                    self.status[i.id] = 'allocation_pending'
                    self.observe(i.id, allocation_request=cycle, state='allocation_pending')
                    self.send(core_name(i.core, 'tile'), Allocate(i.id, f'allocate:{i.id}'))
        counts, core_count = Counter(), 0
        n = self.config['parameters']['pe_count']['value']
        pe_width = self.config['parameters']['pe_issue_width']['value']
        core_width = self.config['parameters']['core_issue_width']['value']
        # Repeated rounds allow wide dispatch without favoring one PE's entire queue.
        progress = True
        while progress:
            progress = False
            for offset in range(n):
                pe = (self.rotation + offset) % n
                ready = next((i for i in self.pending if i.pe == pe and self.status[i.id] == 'dispatch_ready'
                              and (i.alias or (counts[pe] < pe_width and core_count < core_width))), None)
                if ready is None:
                    continue
                i = ready
                self.status[i.id] = 'issued'
                self.observe(i.id, issue=cycle, component=unit_name(i),
                             execution_component=unit_name(i), state='dispatched')
                self.send(core_name(i.core, 'rob'), Issued(i.id))
                self.send(unit_name(i), Dispatch(i.id))
                if not i.alias:
                    counts[pe] += 1
                    core_count += 1
                progress = True
        self.pending = [i for i in self.pending if self.status[i.id] != 'issued']
        for i in self.pending:
            reasons = []
            if not i.deps <= self.done:
                reasons.append('dependency')
            if not i.sync <= self.done:
                reasons.append('synchronization')
            reasons.append(self.status[i.id] if self.status[i.id] != 'dispatch_ready' else 'issue_width')
            self.waits[str(i.id)]['|'.join(sorted(reasons))] += 1
        self.rotation = (self.rotation + 1) % n

    def is_idle(self):
        return super().is_idle() and not self.pending

    def snapshot(self):
        return dict(super().snapshot(), waiting={str(i.id): dict(state=self.status[i.id],
                    dependencies=sorted(map(str, (i.deps | i.sync) - self.done))) for i in self.pending})
