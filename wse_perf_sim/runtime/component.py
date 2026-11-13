"""Small queue interface and local observations, with no peer references."""
from collections import deque


class Component:
    """组件只操作自己的队列和状态，不持有其他组件的可变状态引用。"""
    def __init__(self, name):
        self.name = name
        self.inbox = deque()
        self.outputs = []
        self.observations = []
        self.cycle = 0
        self.progress = 0

    def receive(self, port, message):
        # 由 Clock 在周期开始调用；port 标记消息用途，公共 inbox 保持交付顺序。
        self.inbox.append((port, message))

    def inputs(self, cycle):
        # tick 先消费本周期已经交付的消息；本周期其他组件的新消息不会出现在这里。
        self.cycle = cycle
        while self.inbox:
            self.progress += 1
            yield self.inbox.popleft()

    def send(self, destination, message, port='commands'):
        # 这里只写本组件 outbox，不直接调用接收方。Clock 在所有 tick 后收集，下一拍交付。
        self.outputs.append((destination, port, message))
        self.progress += 1

    def drain_outputs(self):
        # 转移 outbox 所有权并清空，保证每条消息只交付一次。
        result, self.outputs = self.outputs, []
        return result

    def observe(self, key, **fields):
        # 只记录本地打点供报告汇总；observe 不是发送消息，也不会唤醒其他组件。
        self.observations.append((self.cycle, key, fields))
        self.progress += 1

    def is_idle(self):
        return not self.inbox and not self.outputs

    def next_event(self):
        # 子类返回未来的本地完成周期；顶层据此区分“等待服务完成”和真正死锁。
        return None

    def snapshot(self):
        return {'component': self.name, 'inbox': len(self.inbox), 'outbox': len(self.outputs)}
