"""Common operand handshake and result return for small execution units."""
from dataclasses import dataclass, field
from collections import Counter

from wse_perf_sim.tiles import nbytes, version_key
from wse_perf_sim.runtime.component import Component
from wse_perf_sim.runtime.protocol import Access, AccessDone, Complete, Consumed, Dispatch, core_name
from wse_perf_sim.runtime.service import Service


@dataclass
class Command:
    """单条在途命令；游标表示下一个操作数/阶段/输出，request 表示尚未应答的访问。"""
    instruction: object
    state: str = 'operands'
    operand: int = 0
    stage: int = 0
    output: int = 0
    request: str | None = None
    access_start: int | None = None
    result: int | None = None
    outputs: list = field(default_factory=list)


class ExecutionUnit(Component):
    """Cube/Vector/TLSU 共用的消息握手：operands → ready → computing → writing。

    子类 start() 只定义一次计算/搬运的工作量和计时；本类负责操作数访问、
    在途命令推进和完成通知。各命令独立等待，计算资源由本组件的 Service 仲裁。
    """
    def __init__(self, name, instructions, config):
        super().__init__(name)
        self.nodes, self.config = instructions, config
        self.commands = {}
        self.service = Service()
        self.waits = Counter()

    def operation_count(self, instruction):
        # Cube/Vector 一条命令一次计算；TLSU 可按多个 memory_accesses 顺序搬运。
        return 1

    def request_access(self, job, obj, key, write):
        i = self.nodes[job.instruction]
        index = job.output if write else job.operand
        job.request = f'{i.id}:{"write" if write else "read"}:{index}'
        job.access_start = self.cycle
        # 执行单元 → TileReg：指定版本、读/写方向、字节数、dtype 和唯一请求 ID。
        # 每条命令一次只等待一个 AccessDone；请求发出后保留游标，不重复发送。
        self.send(core_name(i.core, 'tile'), Access(i.id, job.request, key, write, nbytes(obj), obj['dtype']))
        self.observe(i.id, state='writing' if write else 'waiting_operand')

    def finish(self, job):
        i = self.nodes[job.instruction]
        self.observe(i.id, complete=self.cycle, state='complete')
        # 两条独立通知：Complete → ROB 用于依赖唤醒/退休；Consumed → TileReg 用于消费计账。
        # 发送时所有输出都已收到写回确认；TileReg 的回收不需要等待 ROB 退休。
        self.send(core_name(i.core, 'rob'), Complete(i.id))
        self.send(core_name(i.core, 'tile'), Consumed(i.id), 'consumption')
        del self.commands[i.id]

    def tick(self, cycle):
        for _, message in self.inputs(cycle):
            key = message.instruction
            if isinstance(message, Dispatch):
                # Scheduler 派发只建立本地 Command；输入握手完成后才能争用计算资源。
                assert key not in self.commands
                i = self.nodes[key]
                self.commands[key] = Command(key, outputs=[o for o in i.raw['outputs'] if o['kind'] == 'tile'])
                self.observe(key, received=cycle, state='operands')
            elif isinstance(message, AccessDone):
                # 按 request 匹配 TileReg 应答；只有收到应答才推进读/写游标。
                job = self.commands[key]
                assert job.request == message.request, (job, message)
                write = job.state == 'writing'
                self.observe(key, access=dict(kind='write' if write else 'read',
                             operand=job.output if write else job.operand,
                             start=job.access_start, complete=cycle, request=job.request))
                job.request = None
                if write:
                    job.output += 1
                else:
                    job.operand += 1
            else:
                raise TypeError(message)
        # 每拍遍历所有命令，按年龄选取可推进者；旧命令等输入/结果时不挡住其他就绪命令。
        jobs = sorted(self.commands.values(), key=lambda j: (
            self.nodes[j.instruction].task, self.nodes[j.instruction].age, str(j.instruction)))
        for job in jobs:
            i = self.nodes[job.instruction]
            if job.state == 'computing' and job.result <= cycle:
                # 结果到期才进入下一搬运阶段或写回；资源可能早在 service_end 就已经空闲。
                job.stage += 1
                job.state = 'ready' if job.stage < self.operation_count(i) else 'writing'
                if job.state == 'writing':
                    self.observe(i.id, compute_end=cycle, writeback_start=cycle, state='writing')
            if job.state == 'operands' and job.request is None:
                # 依次访问 tile 操作数；GM 范围由 TLSU.start() 处理，不走此 tile 握手。
                if job.operand < len(i.operands):
                    obj, key = i.operands[job.operand]
                    self.request_access(job, obj, key, False)
                else:
                    job.state = 'ready'
                    self.observe(i.id, operands_ready=cycle, state='compute_ready')
            if job.state == 'ready':
                # 子类 start 返回 None 表示本拍资源忙；否则已经保留服务时段，记录结果周期。
                event = self.start(i, job.stage, cycle)
                if event is None:
                    self.waits[str(i.id)] += 1
                else:
                    if job.stage == 0:
                        self.observe(i.id, compute_start=cycle)
                    self.observe(i.id, stage=event, state='computing')
                    job.result, job.state = event['complete'], 'computing'
            if job.state == 'writing' and job.request is None:
                # 顺序写回各 tile 输出。最后一个 AccessDone 到达后，才发送整条指令完成通知。
                if job.output < len(job.outputs):
                    obj = job.outputs[job.output]
                    self.request_access(job, obj, version_key(obj), True)
                else:
                    self.observe(i.id, writeback_end=cycle)
                    self.finish(job)

    def is_idle(self):
        return super().is_idle() and not self.commands

    def next_event(self):
        return min((j.result for j in self.commands.values() if j.state == 'computing'), default=None)

    def snapshot(self):
        return dict(super().snapshot(), commands={str(k): dict(state=j.state, request=j.request,
                    operand=j.operand, stage=j.stage, output=j.output, result=j.result)
                    for k, j in self.commands.items()}, available=dict(self.service.available))
