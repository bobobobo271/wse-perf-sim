"""Deterministic next-cycle delivery, independent of component tick order."""
from dataclasses import asdict

from runtime.protocol import Envelope


class Clock:
    def __init__(self, components, log_messages=False):
        self.components = components
        self.pending = []
        self.log_messages = log_messages
        self.messages = []

    def external(self, destination, message, cycle, time_ns):
        # Called before deliver/tick on the first core edge at or after NoC completion.
        self.pending.append(Envelope('communit', destination, 'router', message, cycle, cycle))
        if self.log_messages:
            self.messages.append(dict(source='communit', destination=destination, port='router',
                                      type=type(message).__name__, sent=cycle, received=cycle,
                                      network_time_ns_exact=str(time_ns), **asdict(message)))

    def deliver(self, cycle):
        # 在任何组件 tick 之前交付到期 Envelope，接收方本拍即可消费 inbox。
        waiting = []
        for envelope in self.pending:
            if envelope.received <= cycle:
                self.components[envelope.destination].receive(envelope.port, envelope.message)
            else:
                waiting.append(envelope)
        self.pending = waiting

    def collect(self, cycle):
        # 所有组件 tick 完才收集：按源组件名称排序，同一源保持 FIFO。
        # 因此共享端口收到请求的顺序不依赖 Simulator 的 tick_order。
        for name, component in sorted(self.components.items()):
            for destination, port, message in component.drain_outputs():
                if destination not in self.components:
                    raise ValueError(f'Unknown destination {destination}')
                envelope = Envelope(name, destination, port, message, cycle, cycle + 1)
                # 消息即使能立即处理也必须跨一拍；这里是组件通信延迟的唯一来源。
                self.pending.append(envelope)
                if self.log_messages:
                    self.messages.append(dict(source=name, destination=destination, port=port,
                                              type=type(message).__name__, sent=cycle, received=cycle+1,
                                              **asdict(message)))
