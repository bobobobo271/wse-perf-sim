"""PE Cube: matmul work and latency are determined here from the instruction."""
import math

from ..errors import ModelError
from .execution import ExecutionUnit


class Cube(ExecutionUnit):
    def start(self, instruction, index, cycle):
        a, b = instruction.raw['inputs'][-2:]
        m, k = a['shape']
        kb, n = b['shape']
        if k != kb:
            raise ModelError(f'Matmul shape mismatch at {instruction.id}')
        work = m * n * k
        e = self.config['engines']['cube']
        cost = e | e.get('dtype_overrides', {}).get(a['dtype'], {})
        rate = cost['throughput']
        if e['scope'] == 'Core':
            rate /= self.config['parameters']['pe_count']['value']
        return self.service.start(self.name, 'cube', work,
                                  max(1, math.ceil(work / rate)), cost['latency'], cycle)
