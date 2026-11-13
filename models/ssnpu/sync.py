"""Control nodes wait for completion broadcasts, including remote Core nodes."""
from runtime.component import Component
from runtime.protocol import Complete, Completed, Issued, core_name


class Sync(Component):
    def __init__(self, name, instructions, config):
        super().__init__(name)
        self.pending = {i.id: i for i in instructions}
        self.done, self.running = set(), {}
        self.latency = config['parameters']['barrier_latency']['value']

    def tick(self, cycle):
        # 只接收 ROB 的 Completed 广播。参与者/前序操作已由 TraceAdapter 转成依赖节点。
        for _, message in self.inputs(cycle):
            if not isinstance(message, Completed):
                raise TypeError(message)
            self.done.add(message.instruction)
        for key, i in list(self.pending.items()):
            if (i.deps | i.sync) <= self.done:
                # Sync → ROB：控制节点也登记 Issued；barrier 额外等待配置的同步周期。
                self.send(core_name(i.core, 'rob'), Issued(key))
                self.observe(key, issue=cycle, component=self.name,
                             execution_component=self.name, state='synchronizing')
                self.running[key] = (i, cycle + (self.latency if i.raw['ir_op'] == 'barrier' else 0))
                del self.pending[key]
        for key, (i, end) in list(self.running.items()):
            if end <= cycle:
                self.observe(key, complete=cycle, state='complete')
                # Sync → ROB：Complete 经 ROB 广播为 Completed 后，后续阶段才能被唤醒。
                # 零延迟控制节点的 Issued/Complete 同拍发送，按同一源的 FIFO 顺序交付。
                self.send(core_name(i.core, 'rob'), Complete(key))
                del self.running[key]

    def is_idle(self):
        return super().is_idle() and not self.pending and not self.running

    def next_event(self):
        return min((end for _, end in self.running.values()), default=None)

    def snapshot(self):
        return dict(super().snapshot(), waiting={str(k): sorted(map(str, (i.deps | i.sync) - self.done))
                    for k, i in self.pending.items()}, running={str(k): end for k, (_, end) in self.running.items()})
