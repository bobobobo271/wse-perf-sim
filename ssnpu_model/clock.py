"""Deterministic next-cycle delivery, independent of component tick order."""
from dataclasses import asdict

from .components.protocol import Envelope


class Clock:
    def __init__(self, components, log_messages=False):
        self.components = components
        self.pending = []
        self.log_messages = log_messages
        self.messages = []

    def deliver(self, cycle):
        waiting = []
        for envelope in self.pending:
            if envelope.received <= cycle:
                self.components[envelope.destination].receive(envelope.port, envelope.message)
            else:
                waiting.append(envelope)
        self.pending = waiting

    def collect(self, cycle):
        # Canonical source order and each source's FIFO order define arrival order.
        for name, component in sorted(self.components.items()):
            for destination, port, message in component.drain_outputs():
                if destination not in self.components:
                    raise ValueError(f'Unknown destination {destination}')
                envelope = Envelope(name, destination, port, message, cycle, cycle + 1)
                self.pending.append(envelope)
                if self.log_messages:
                    self.messages.append(dict(source=name, destination=destination, port=port,
                                              type=type(message).__name__, sent=cycle, received=cycle+1,
                                              **asdict(message)))
