"""Small queue interface and local observations, with no peer references."""
from collections import deque


class Component:
    def __init__(self, name):
        self.name = name
        self.inbox = deque()
        self.outputs = []
        self.observations = []
        self.cycle = 0
        self.progress = 0

    def receive(self, port, message):
        self.inbox.append((port, message))

    def inputs(self, cycle):
        self.cycle = cycle
        while self.inbox:
            self.progress += 1
            yield self.inbox.popleft()

    def send(self, destination, message, port='commands'):
        self.outputs.append((destination, port, message))
        self.progress += 1

    def drain_outputs(self):
        result, self.outputs = self.outputs, []
        return result

    def observe(self, key, **fields):
        self.observations.append((self.cycle, key, fields))
        self.progress += 1

    def is_idle(self):
        return not self.inbox and not self.outputs

    def next_event(self):
        return None

    def snapshot(self):
        return {'component': self.name, 'inbox': len(self.inbox), 'outbox': len(self.outputs)}
