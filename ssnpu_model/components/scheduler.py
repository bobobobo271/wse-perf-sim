"""Dependency tracking, one-shot output reservations and fair dispatch only."""
from collections import Counter, defaultdict

from .base import Component
from .protocol import Admitted, Allocate, Allocated, Completed, Dispatch, Issued, core_name, unit_name


class Scheduler(Component):
    """waiting_admission → waiting_dependencies → allocation_pending → dispatch_ready → issued。

    只管理依赖、输出预留和派发；执行单元收到命令后自行等待端口/计算资源。
    pending 按原始年龄排序，但会跳过未就绪指令，实现 tile 粒度乱序发射。
    """
    def __init__(self, name, instructions, config):
        super().__init__(name)
        self.nodes = {i.id: i for i in instructions}
        self.pending = sorted(instructions, key=lambda i: (i.task, i.age, str(i.id)))
        self.status = {i.id: 'waiting_admission' for i in instructions}
        self.done = set()
        self.config, self.rotation = config, 0
        self.waits = defaultdict(Counter)

    def tick(self, cycle):
        # 1. 接收上一拍的反馈。Admitted/Completed 来自 ROB；Allocated 来自 TileReg。
        for _, message in self.inputs(cycle):
            if isinstance(message, Admitted):
                # 只有 ROB 准入通知能打开窗口；记录接收周期，与 ROB 的准入周期区分。
                assert self.status[message.instruction] == 'waiting_admission'
                self.status[message.instruction] = 'waiting_dependencies'
                self.observe(message.instruction, admission_received=cycle, state='waiting_dependencies')
            elif isinstance(message, Completed):
                self.done.add(message.instruction)
            elif isinstance(message, Allocated):
                # 分配成功仅表示输出容量已经保留，不表示指令已经执行或输出 ready。
                assert self.status[message.instruction] == 'allocation_pending'
                self.status[message.instruction] = 'dispatch_ready'
                self.observe(message.instruction, allocated=cycle, state='dispatch_ready')
            else:
                raise TypeError(message)
        # 2. 数据依赖与同步依赖都满足后申请输出容量；状态切换保证请求只发送一次。
        for i in self.pending:
            if self.status[i.id] == 'waiting_dependencies' and (i.deps | i.sync) <= self.done:
                if i.alias:
                    # view 使用既有存储，不申请新容量；仍经 Dispatch/Complete 消息完成。
                    self.status[i.id] = 'dispatch_ready'
                else:
                    self.status[i.id] = 'allocation_pending'
                    self.observe(i.id, allocation_request=cycle, state='allocation_pending')
                    # Scheduler → TileReg：指令 ID + 分配请求 ID；由 TileReg 查出全部输出。
                    self.send(core_name(i.core, 'tile'), Allocate(i.id, f'allocate:{i.id}'))
        counts, core_count = Counter(), 0
        n = self.config['parameters']['pe_count']['value']
        pe_width = self.config['parameters']['pe_issue_width']['value']
        core_width = self.config['parameters']['core_issue_width']['value']
        # 3. 每轮每 PE 最多派发一条，多轮填满本周期额度；每拍轮换起始 PE。
        # ready 从按年龄排列的 pending 中筛选，旧指令等依赖/容量时可被年轻指令绕过。
        # 无限命令队列允许提前派发；issue_width 限制派发数，计算吞吐由执行单元限制。
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
                # 同时发送两条消息：Issued 让 ROB 登记发射；Dispatch 让目标单元开始管理命令。
                # 两者都在下一拍交付；此处不会直接开始 Cube/Vector 的计算。
                self.send(core_name(i.core, 'rob'), Issued(i.id))
                self.send(unit_name(i), Dispatch(i.id))
                if not i.alias:
                    counts[pe] += 1
                    core_count += 1
                progress = True
        # 4. 已派发命令移交执行单元；只对仍未派发的指令记录本拍等待原因。
        self.pending = [i for i in self.pending if self.status[i.id] != 'issued']
        for i in self.pending:
            if self.status[i.id] == 'waiting_admission':
                # 窗口外仅计准入等待，不提前参与依赖检查或输出容量申请。
                self.waits[str(i.id)]['waiting_admission'] += 1
                continue
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
