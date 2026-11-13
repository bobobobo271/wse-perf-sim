"""Core tile load/store unit, including memory path work and latency."""
import math
from fractions import Fraction

from ..errors import ModelError
from ..tiles import nbytes
from .execution import ExecutionUnit
from .protocol import (core_name, Dispatch, Access, AccessDone, Complete, Consumed,
                       CalendarCommand, CalendarComplete)


class TLSU(ExecutionUnit):
    def __init__(self, name, instructions, config):
        super().__init__(name, instructions, config)
        self.calendar_jobs = {}

    def tick(self, cycle):
        ordinary = []
        for port, message in self.inputs(cycle):
            i = self.nodes[message.instruction]
            if not i.raw['ir_op'].startswith('calendar.'):
                ordinary.append((port, message))
                continue
            if isinstance(message, Dispatch):
                self.calendar_jobs[i.id] = i
                self.observe(i.id, received=cycle, state='calendar_wait')
                if i.raw['ir_op'] == 'calendar.send':
                    obj, version = i.operands[0]
                    self.send(core_name(i.core, 'tile'), Access(i.id, f'noc:{i.id}', version,
                              False, i.raw['calendar']['nbytes'], obj['dtype']), 'access')
                else:
                    self.send('noc', CalendarCommand(i.id))
            elif isinstance(message, AccessDone):
                # The whole packet now lives in the adapter's reserved staging buffer.
                self.send(core_name(i.core, 'tile'), Consumed(i.id))
                self.observe(i.id, operands_ready=cycle, state='calendar_staged')
                self.send('noc', CalendarCommand(i.id))
            elif isinstance(message, CalendarComplete):
                self.observe(i.id, complete=cycle, state='complete')
                self.send(core_name(i.core, 'rob'), Complete(i.id))
                if i.raw['ir_op'] != 'calendar.send':
                    self.send(core_name(i.core, 'tile'), Consumed(i.id))
                del self.calendar_jobs[i.id]
            else:
                raise TypeError(message)
        self.inbox.extend(ordinary)
        super().tick(cycle)

    def is_idle(self):
        return super().is_idle() and not self.calendar_jobs

    def snapshot(self):
        return dict(super().snapshot(), calendar_waiting=list(self.calendar_jobs))

    def operation_count(self, instruction):
        return len(instruction.raw.get('memory_accesses', []))

    def start(self, instruction, index, cycle):
        r = instruction.raw
        accesses = r.get('memory_accesses', [])
        if not accesses:
            raise ModelError(f'Missing memory access range at {instruction.id}')
        access = accesses[index]
        obj = next((o for o in r['inputs'] + r['outputs'] if o['object_id'] == access['object_id']), None)
        if obj is None or access.get('shape') is None:
            raise ModelError(f'Unresolved memory access range at {instruction.id}')
        path = obj['memory_space']
        if path not in self.config['paths']:
            raise ModelError(f'Unknown memory path {path} at {instruction.id}')
        route = self.config['paths'][path]
        e = self.config['engines']['memory']
        cost = e | e.get('dtype_overrides', {}).get(obj['dtype'], {})
        if isinstance(route, dict):
            cost |= route
        work = nbytes(obj, access['shape'])
        if 'wse' in self.config and path == 'SRAM':
            p = {k: v['value'] for k, v in self.config['wse'].items()}
            offset = access.get('offset_bytes', 0)
            if type(offset) is not int or offset < 0 or offset + work > p['sram_capacity_bytes']:
                raise ModelError('Local SRAM access exceeds configured capacity')
            direction = 'write' if any(o['object_id'] == obj['object_id'] for o in r['outputs']) else 'read'
            duration = Fraction(p[f'sram_{direction}_startup_ns'], 10**9) + Fraction(work, p['sram_bandwidth'])
            service = max(1, math.ceil(duration * p['core_clock_hz']))
            return self.service.start(core_name(instruction.core, 'memory'), 'memory', work, service, 0, cycle)
        return self.service.start(core_name(instruction.core, 'memory'), 'memory', work,
                                  max(1, math.ceil(work / cost['throughput'])), cost['latency'], cycle)
