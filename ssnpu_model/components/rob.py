"""Unlimited ROB, preloaded in original thread order, independent completion."""
from collections import defaultdict

from .base import Component
from .protocol import Complete, Completed, Issued


class ROB(Component):
    def __init__(self, name, instructions, subscribers):
        super().__init__(name)
        self.entries = {i.id: 'pending' for i in instructions}
        self.threads = defaultdict(list)
        for i in sorted(instructions, key=lambda i: (i.task, i.age, str(i.id))):
            identity = ('control', i.id) if i.control else i.raw['_identity']
            self.threads[identity].append(i.id)
        self.heads = {thread: 0 for thread in self.threads}
        self.subscribers = tuple(subscribers)

    def tick(self, cycle):
        for _, message in self.inputs(cycle):
            key = message.instruction
            if isinstance(message, Issued):
                assert self.entries[key] == 'pending', (key, self.entries[key])
                self.entries[key] = 'issued'
            elif isinstance(message, Complete):
                assert self.entries[key] == 'issued', (key, self.entries[key])
                self.entries[key] = 'complete'
                self.observe(key, rob_complete=cycle, component=self.name, state='rob_complete')
                for destination in self.subscribers:
                    self.send(destination, Completed(key), 'completion')
            else:
                raise TypeError(message)
        for thread, keys in self.threads.items():
            head = self.heads[thread]
            while head < len(keys) and self.entries[keys[head]] == 'complete':
                key = keys[head]
                self.entries[key] = 'retired'
                self.observe(key, retire=cycle, component=self.name, state='retired')
                head += 1
            self.heads[thread] = head

    def is_idle(self):
        return super().is_idle() and all(s == 'retired' for s in self.entries.values())

    def snapshot(self):
        return dict(super().snapshot(), entries={str(k): s for k, s in self.entries.items() if s != 'retired'},
                    heads={str(t): self.threads[t][h:h+1] for t, h in self.heads.items()})
