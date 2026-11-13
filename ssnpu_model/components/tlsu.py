"""Core tile load/store unit, including memory path work and latency."""
import math
from fractions import Fraction

from ..errors import ModelError
from ..tiles import nbytes
from .execution import ExecutionUnit
from .protocol import (core_name, Dispatch, Access, AccessDone, Complete, Consumed,
                       HBMRequest, HBMDone)


class TLSU(ExecutionUnit):
    def __init__(self, name, instructions, config):
        super().__init__(name, instructions, config)
        self.copy_jobs = {}

    def tick(self, cycle):
        ordinary = []
        for port, message in self.inputs(cycle):
            i = self.nodes[message.instruction]
            if i.raw['ir_op'] == 'memory.copy':
                self.copy_message(i, message, cycle)
                continue
            ordinary.append((port, message))
        self.inbox.extend(ordinary)
        super().tick(cycle)
        for key, job in list(self.copy_jobs.items()):
            i = self.nodes[key]
            if job['state'] == 'sram':
                p = {k: v['value'] for k, v in self.config['wse'].items()}
                direction = 'write' if job['direction'] == 'read' else 'read'
                duration = math.ceil(p['core_clock_hz'] * (
                    p[f'sram_{direction}_startup_ns'] / 1e9 + job['work'] / p['sram_bandwidth']))
                event = self.service.start(core_name(i.core, 'memory'), 'memory', job['work'],
                                           max(1, duration), 0, cycle)
                if event is not None:
                    if job['direction'] == 'write':
                        self.observe(key, compute_start=cycle)
                    self.observe(key, stage=event)
                    job.update(state='sram_running', end=event['complete'])
            elif job['state'] == 'sram_running' and job['end'] <= cycle:
                if job['direction'] == 'write':
                    self.send('hbm', HBMRequest(key, i.core, job['work'], 'write', False))
                    job['state'] = 'hbm'
                else:
                    self.finish_copy(i, cycle)

    def copy_message(self, instruction, message, cycle):
        key = instruction.id
        if isinstance(message, Dispatch):
            r = instruction.raw
            source, = r['inputs']
            target, = r['outputs']
            spaces = source['memory_space'], target['memory_space']
            if spaces not in (('HBM', 'SRAM'), ('SRAM', 'HBM')) or 'hbm' not in self.config:
                raise ModelError('memory.copy requires configured HBM <-> SRAM')
            accesses = r['memory_accesses']
            if len(accesses) != 2:
                raise ModelError('memory.copy requires source and destination ranges')
            sizes = [nbytes(obj, access['shape']) for obj, access in zip((source, target), accesses)]
            if sizes[0] != sizes[1] or sizes[0] <= 0:
                raise ModelError('memory.copy source and destination byte counts differ or are empty')
            for obj, access in zip((source, target), accesses):
                if obj['kind'] != 'tensor' or access['object_id'] != obj['object_id']:
                    raise ModelError('memory.copy requires matching tensor ranges')
                offset = access.get('offset_bytes', 0)
                if type(offset) is not int or offset < 0:
                    raise ModelError('Invalid memory.copy offset')
                if obj['memory_space'] == 'SRAM' and offset + sizes[0] > self.config['wse']['sram_capacity_bytes']['value']:
                    raise ModelError('memory.copy exceeds SRAM capacity')
            direction = 'read' if spaces[0] == 'HBM' else 'write'
            self.copy_jobs[key] = dict(direction=direction, work=sizes[0],
                                       state='hbm' if direction == 'read' else 'sram')
            self.observe(key, received=cycle, operands_ready=cycle, state='memory_copy')
            if direction == 'read':
                self.send('hbm', HBMRequest(key, instruction.core, sizes[0], direction, True))
        elif isinstance(message, HBMDone):
            if self.copy_jobs[key]['direction'] == 'read':
                self.copy_jobs[key]['state'] = 'sram'
            else:
                self.finish_copy(instruction, cycle)
        else:
            raise TypeError(message)

    def finish_copy(self, instruction, cycle):
        self.observe(instruction.id, compute_end=cycle, writeback_start=cycle,
                     writeback_end=cycle, complete=cycle, state='complete')
        self.send(core_name(instruction.core, 'rob'), Complete(instruction.id))
        self.send(core_name(instruction.core, 'tile'), Consumed(instruction.id))
        del self.copy_jobs[instruction.id]

    def is_idle(self):
        return super().is_idle() and not self.copy_jobs

    def next_event(self):
        events = [super().next_event()] + [j.get('end') for j in self.copy_jobs.values() if j['state'] == 'sram_running']
        return min((event for event in events if event is not None), default=None)

    def snapshot(self):
        return dict(super().snapshot(), memory_copies=self.copy_jobs.copy())

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
