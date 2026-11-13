"""Per-thread admission windows, independent completion and ordered retirement."""
from collections import defaultdict

from .base import Component
from .protocol import Admitted, Complete, Completed, Issued, core_name


class ROB(Component):
    """按线程建表：waiting_admission → pending → issued → complete → retired。"""
    def __init__(self, name, instructions, subscribers, rob_depth=None):
        super().__init__(name)
        self.nodes = {i.id: i for i in instructions}
        self.entries = {i.id: 'pending' if i.control else 'waiting_admission' for i in instructions}
        self.threads = defaultdict(list)
        for i in sorted(instructions, key=lambda i: (i.task, i.age, str(i.id))):
            # 不能按发射顺序建 ROB，否则乱序发射会改变退休顺序；控制节点单独成队列。
            identity = ('control', i.id) if i.control else i.raw['_identity']
            self.threads[identity].append(i.id)
        self.heads = {thread: 0 for thread in self.threads}
        self.depth = rob_depth
        self.cursors = {t: 0 for t, keys in self.threads.items() if not self.nodes[keys[0]].control}
        self.occupancy = {t: 0 for t in self.cursors}
        self.peak = {t: 0 for t in self.cursors}
        self.full_cycles = {t: 0 for t in self.cursors}
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
                # ROB → 全部 Scheduler/Sync：广播指令 ID，下一拍唤醒数据/控制依赖。
                # 广播发生在退休检查之前，完成的年轻指令无需等旧指令退休。
                for destination in self.subscribers:
                    self.send(destination, Completed(key), 'completion')
            else:
                raise TypeError(message)
        # 每线程仅退休头部连续完成的指令；初版退休带宽无限，不把退休作为 tile 释放条件。
        for thread, keys in self.threads.items():
            head = self.heads[thread]
            while head < len(keys) and self.entries[keys[head]] == 'complete':
                key = keys[head]
                self.entries[key] = 'retired'
                self.observe(key, retire=cycle, component=self.name, state='retired')
                # 容量只在按序退休时归还；年轻指令完成或广播均不释放窗口槽位。
                if thread in self.occupancy:
                    self.occupancy[thread] -= 1
                head += 1
            self.heads[thread] = head
        for thread, cursor in self.cursors.items():
            keys = self.threads[thread]
            # 先退休再按程序顺序补入；None 也走同一握手路径，只取消容量上限。
            while cursor < len(keys) and (self.depth is None or self.occupancy[thread] < self.depth):
                key = keys[cursor]
                self.entries[key] = 'pending'
                self.occupancy[thread] += 1  # 准入立即消耗槽位，尚未派发也占容量。
                self.peak[thread] = max(self.peak[thread], self.occupancy[thread])
                self.observe(key, admitted=cycle, component=self.name, state='admitted')
                # ROB → 本 Core Scheduler：下一拍交付，窗口外指令不能预留输出或派发。
                self.send(core_name(self.nodes[key].core, 'scheduler'), Admitted(key), 'admission')
                cursor += 1
            self.cursors[thread] = cursor
            # 以本拍退休、补入后的状态统计：窗口满且确实仍有指令等待准入。
            if cursor < len(keys) and self.depth is not None and self.occupancy[thread] == self.depth:
                self.full_cycles[thread] += 1

    def window_stats(self):
        return {str(t): dict(capacity=self.depth, occupancy=self.occupancy[t],
                             peak=self.peak[t], full_cycles=self.full_cycles[t]) for t in self.cursors}

    def is_idle(self):
        return super().is_idle() and all(s == 'retired' for s in self.entries.values())

    def snapshot(self):
        stats = self.window_stats()
        return dict(super().snapshot(), entries={str(k): s for k, s in self.entries.items() if s != 'retired'},
                    heads={str(t): self.threads[t][h:h+1] for t, h in self.heads.items()},
                    windows={str(t): dict(stats[str(t)], instructions=self.threads[t][self.heads[t]:cursor],
                              retirement_head=self.threads[t][self.heads[t]:self.heads[t]+1],
                              next_admission=self.threads[t][cursor:cursor+1])
                             for t, cursor in self.cursors.items()})
