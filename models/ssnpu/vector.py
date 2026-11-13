"""PE Vector: elementwise, exp, cast and reductions share one service resource."""
import math

from errors import ModelError
from tiles import DTYPES
from models.ssnpu.execution import ExecutionUnit


class Vector(ExecutionUnit):
    ELEMENTWISE = {'full', 'add', 'adds', 'sub', 'subs', 'mul', 'muls', 'div', 'divs',
                   'maximum', 'minimum', 'neg', 'abs', 'sqrt', 'rsqrt', 'relu',
                   'row_expand_sub', 'row_expand_add', 'row_expand_mul', 'row_expand_div',
                   'slice', 'concat'}

    def start(self, instruction, index, cycle):
        r = instruction.raw
        op = r['ir_op'].removeprefix('tile.')
        if op in ('row_sum', 'row_max', 'row_min'):
            # 每行 N 个元素需要 N-1 次归约；其他向量操作按有效输出元素数计工作量。
            obj = r['inputs'][0]
            shape = obj.get('valid_shape', obj['shape'])
            engine, work = 'reduce', math.prod(shape[:-1]) * max(0, shape[-1] - 1)
        elif op in self.ELEMENTWISE | {'exp', 'cast'}:
            obj = r['outputs'][-1]
            engine = op if op in ('exp', 'cast') else 'vector'
            work = math.prod(obj.get('valid_shape', obj['shape']))
        else:
            raise ModelError(f'Unknown operation {r["ir_op"]} at {instruction.id}')
        e = self.config['engines'][engine]
        cost = e | e.get('dtype_overrides', {}).get(obj['dtype'], {})
        rate = cost['throughput']
        width = DTYPES[obj['dtype']]
        if cost['unit'] == 'byte/cycle':
            rate /= width
        if e['scope'] == 'Core':
            rate /= self.config['parameters']['pe_count']['value']
            vector = self.config['engines']['vector']
            if vector['scope'] == 'Core' and vector['unit'] == 'byte/cycle':
                rate = min(rate, vector['throughput'] / width / self.config['parameters']['pe_count']['value'])
        # engine 选择不同操作的 cost，但资源键统一为 self.name：exp/reduce/cast 竞争同一 Vector。
        # 输入访问和输出消息由 ExecutionUnit 驱动，此方法只负责计算服务的准入与计时。
        return self.service.start(self.name, engine, work,
                                  max(1, math.ceil(work / rate)), cost['latency'], cycle)
