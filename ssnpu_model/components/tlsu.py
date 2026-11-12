"""Core tile load/store unit, including memory path work and latency."""
import math

from ..errors import ModelError
from ..tiles import nbytes
from .execution import ExecutionUnit
from .protocol import core_name


class TLSU(ExecutionUnit):
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
        return self.service.start(core_name(instruction.core, 'memory'), 'memory', work,
                                  max(1, math.ceil(work / cost['throughput'])), cost['latency'], cycle)
