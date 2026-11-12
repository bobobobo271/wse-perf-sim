"""Common operand handshake and result return for small execution units."""
from dataclasses import dataclass, field
from collections import Counter

from ..tiles import nbytes, version_key
from .base import Component
from .protocol import Access, AccessDone, Complete, Consumed, Dispatch, core_name
from .service import Service


@dataclass
class Command:
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
    def __init__(self, name, instructions, config):
        super().__init__(name)
        self.nodes, self.config = instructions, config
        self.commands = {}
        self.service = Service()
        self.waits = Counter()

    def operation_count(self, instruction):
        return 1

    def request_access(self, job, obj, key, write):
        i = self.nodes[job.instruction]
        index = job.output if write else job.operand
        job.request = f'{i.id}:{"write" if write else "read"}:{index}'
        job.access_start = self.cycle
        self.send(core_name(i.core, 'tile'), Access(i.id, job.request, key, write, nbytes(obj), obj['dtype']))
        self.observe(i.id, state='writing' if write else 'waiting_operand')

    def finish(self, job):
        i = self.nodes[job.instruction]
        self.observe(i.id, complete=self.cycle, state='complete')
        self.send(core_name(i.core, 'rob'), Complete(i.id))
        self.send(core_name(i.core, 'tile'), Consumed(i.id), 'consumption')
        del self.commands[i.id]

    def tick(self, cycle):
        for _, message in self.inputs(cycle):
            key = message.instruction
            if isinstance(message, Dispatch):
                assert key not in self.commands
                i = self.nodes[key]
                self.commands[key] = Command(key, outputs=[o for o in i.raw['outputs'] if o['kind'] == 'tile'])
                self.observe(key, received=cycle, state='operands')
            elif isinstance(message, AccessDone):
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
        # An old command waiting for a tile or result never blocks another ready command.
        jobs = sorted(self.commands.values(), key=lambda j: (
            self.nodes[j.instruction].task, self.nodes[j.instruction].age, str(j.instruction)))
        for job in jobs:
            i = self.nodes[job.instruction]
            if job.state == 'computing' and job.result <= cycle:
                job.stage += 1
                job.state = 'ready' if job.stage < self.operation_count(i) else 'writing'
                if job.state == 'writing':
                    self.observe(i.id, compute_end=cycle, writeback_start=cycle, state='writing')
            if job.state == 'operands' and job.request is None:
                if job.operand < len(i.operands):
                    obj, key = i.operands[job.operand]
                    self.request_access(job, obj, key, False)
                else:
                    job.state = 'ready'
                    self.observe(i.id, operands_ready=cycle, state='compute_ready')
            if job.state == 'ready':
                event = self.start(i, job.stage, cycle)
                if event is None:
                    self.waits[str(i.id)] += 1
                else:
                    if job.stage == 0:
                        self.observe(i.id, compute_start=cycle)
                    self.observe(i.id, stage=event, state='computing')
                    job.result, job.state = event['complete'], 'computing'
            if job.state == 'writing' and job.request is None:
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
