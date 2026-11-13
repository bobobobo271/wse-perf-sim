"""Sole owner of storage, readiness, consumers and shared access ports."""
import math
from collections import Counter, defaultdict

from .base import Component
from .protocol import (Access, AccessDone, Allocate, Allocated, Complete, Consumed,
                       Dispatch, core_name, unit_name)
from .service import Service


class TileReg(Component):
    """Core 内 local/shared 存储的唯一状态管理者。

    active 表示占用容量，ready 表示版本可读，done 表示生产/消费指令已完成。
    allocations 等容量，accesses 等 ready/端口，running 等共享访问服务返回。
    """
    def __init__(self, name, instructions, versions, config):
        super().__init__(name)
        self.nodes, self.versions, self.config = instructions, versions, config
        self.active = set()
        self.usage, self.peak = Counter(), Counter()
        self.storage_events = []
        self.service = Service()
        self.ready, self.done = set(), set()
        self.allocations, self.accesses, self.running = [], [], []
        self.blocked = {}
        self.waits = defaultdict(Counter)

    def finish_instruction(self, key):
        # 消费完成按整条指令计账，保守地保留输入直到输出写回结束。
        # consumers 是 trace 中该版本的全部消费者，包含还没派发的命令。
        i = self.nodes[key]
        self.done.add(key)
        for vk in sorted(i.inputs | i.outputs, key=str):
            v = self.versions[vk]
            if not v.retained and v.producer in self.done and v.consumers <= self.done:
                # retained 表示需要继续保留的共享版本；其余版本在生产及全部消费结束后回收。
                self.release(vk)
                self.ready.discard(vk)

    def release(self, key, superseded_by=None):
        if key not in self.active:
            return
        self.active.remove(key)
        v = self.versions[key]
        self.usage[v.pool] -= v.size
        event = dict(cycle=self.cycle, action='free', version=list(key), pool=v.pool, bytes=v.size)
        if superseded_by is not None:
            event['superseded_by'] = superseded_by
        self.storage_events.append(event)

    def allocate(self, instruction):
        # 先计算全部输出所需容量和可替换旧版本，所有检查通过后才修改状态（原子预留）。
        delta, replace = Counter(), set()
        for key in sorted(instruction.outputs, key=str):
            v = self.versions[key]
            for old in sorted(self.active, key=str):
                ov = self.versions[old]
                if ov.slot == v.slot and old != key:
                    # 固定槽位覆写必须等旧生产者和其他读者完成。
                    # 排除当前指令自身，允许读旧值、写新值的原地操作。
                    if ov.retained or ov.producer not in self.done or not (ov.consumers - {instruction.id}) <= self.done:
                        return f'fixed_position:{old}'
                    replace.add(old)
            delta[v.pool] += v.size
        for old in replace:
            delta[self.versions[old].pool] -= self.versions[old].size
        for pool, amount in delta.items():
            limit = self.config['parameters']['shared_capacity' if pool.endswith('/shared') else 'local_capacity']['value']
            if self.usage[pool] + amount > limit:
                return f'capacity:{pool}'
        for key in sorted(replace, key=str):
            # 原地替换只转移容量归属；旧版本的 ready 仍保留，供该指令先读旧值。
            self.release(key, superseded_by=instruction.id)
        for key in sorted(instruction.outputs, key=str):
            v = self.versions[key]
            self.active.add(key)
            self.usage[v.pool] += v.size
            self.peak[v.pool] = max(self.peak[v.pool], self.usage[v.pool])
            self.storage_events.append(dict(cycle=self.cycle, action='allocate', version=list(key), pool=v.pool, bytes=v.size))
        return None

    def start_access(self, request):
        # shared 读写各用独立 Core 端口，按字节/周期计费；不同消费者分别占用读带宽。
        engine = 'shared_write' if request.write else 'shared_read'
        e = self.config['engines'][engine]
        cost = e | e.get('dtype_overrides', {}).get(request.dtype, {})
        i = self.nodes[request.instruction]
        return self.service.start(core_name(i.core, engine), engine, request.work,
                                  max(1, math.ceil(request.work / cost['throughput'])), cost['latency'], self.cycle)

    def respond(self, request):
        if request.write:
            # 写完成才发布版本 ready；Allocated 仅预留容量，不会到这里。
            self.ready.add(request.version)
        # TileReg → 原执行单元：仅返回指令/请求 ID，通知对应读或写已完成，不携带数值。
        self.send(unit_name(self.nodes[request.instruction]),
                  AccessDone(request.instruction, request.request), 'access')

    def tick(self, cycle):
        # 1. 收取请求和消费通知。Allocate/Access 入等待队列，在本组件内自主重试。
        for _, message in self.inputs(cycle):
            if isinstance(message, Allocate):
                self.allocations.append(message)
            elif isinstance(message, Access):
                i = self.nodes[message.instruction]
                assert message.version in (i.outputs if message.write else i.inputs), message
                self.accesses.append(message)
            elif isinstance(message, Consumed):
                self.finish_instruction(message.instruction)
            elif isinstance(message, Dispatch):
                i = self.nodes[message.instruction]
                assert i.alias
                self.observe(i.id, complete=cycle, state='complete')
                # view 由 TileReg 直接完成：别名已在 trace 适配时绑定，无需计算或新分配。
                # TileReg → ROB 发送 Complete，同时在本地登记该 view 已完成。
                self.send(core_name(i.core, 'rob'), Complete(i.id))
                self.finish_instruction(i.id)
            else:
                raise TypeError(message)
        # 2. 返回到期的共享访问；respond 发送 AccessDone，执行单元下一拍才能消费。
        for request, end in list(self.running):
            if end <= cycle:
                self.respond(request)
                self.running.remove((request, end))
        # 3. 尝试各输出分配请求；容量不足时留在队列，允许后续可满足的请求继续尝试。
        self.blocked = {}
        for request in list(self.allocations):
            i = self.nodes[request.instruction]
            reason = self.allocate(i)
            if reason:
                self.blocked[str(i.id)] = reason
                self.waits[str(i.id)][reason] += 1
                continue
            # TileReg → Scheduler：Allocated 携带原请求 ID，允许该指令进入待派发状态。
            # 原地写指令随后仍可读取保留的旧版本 ready；新输出要等实际写回才 ready。
            self.send(core_name(i.core, 'scheduler'), Allocated(i.id, request.request), 'allocation')
            self.allocations.remove(request)
        # 4. 按确定性交付顺序仲裁访问；未 ready 的读请求可被绕过，读写端口彼此独立。
        for request in list(self.accesses):
            if request.write:
                assert request.version in self.active, request
            elif request.version not in self.ready:
                self.blocked[request.request] = f'tile_not_ready:{request.version}'
                continue
            shared = self.versions[request.version].pool.endswith('/shared')
            if shared:
                event = self.start_access(request)
                if event is None:
                    self.blocked[request.request] = 'shared_port_busy'
                    continue
                self.observe(request.instruction, stage=event)
                self.running.append((request, event['complete']))
            else:
                # 私有访问的服务成本已折入计算；本拍即可发送 AccessDone，但仍有消息往返延迟。
                self.respond(request)
            self.accesses.remove(request)

    def is_idle(self):
        return super().is_idle() and not (self.allocations or self.accesses or self.running)

    def next_event(self):
        return min((end for _, end in self.running), default=None)

    def snapshot(self):
        return dict(super().snapshot(), allocations=[r.request for r in self.allocations],
                    accesses=[r.request for r in self.accesses],
                    running=[(r.request, end) for r, end in self.running], blocked=self.blocked.copy(),
                    usage=dict(self.usage), active=[list(k) for k in sorted(self.active, key=str)],
                    ready=[list(k) for k in sorted(self.ready, key=str)],
                    consumers={str(k): sorted(map(str, self.versions[k].consumers - self.done))
                               for k in sorted(self.active, key=str)})
