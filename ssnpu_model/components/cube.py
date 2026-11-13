"""PE Cube: matmul work and latency are determined here from the instruction."""
import math

from ..errors import ModelError
from .execution import ExecutionUnit


class Cube(ExecutionUnit):
    def start(self, instruction, index, cycle):
        # matmul_acc 前面可能有累加器输入，最后两个输入才是 A/B；工作量按 MAC 计。
        a, b = instruction.raw['inputs'][-2:]
        m, k = a['shape']
        kb, n = b['shape']
        if k != kb:
            raise ModelError(f'Matmul shape mismatch at {instruction.id}')
        work = m * n * k
        e = self.config['engines']['cube']
        cost = e | e.get('dtype_overrides', {}).get(a['dtype'], {})
        # self.name 含 Core/PE，保证每个 PE 独立争用自己的 Cube。
        # 操作数请求、写回和完成消息均由继承的 ExecutionUnit.tick() 发送。
        return self.service.start(self.name, 'cube', work,
                                  max(1, math.ceil(work / cost['throughput'])), cost['latency'], cycle)
