"""Core tile load/store unit, including memory path work and latency."""
import math

from ..errors import ModelError
from ..tiles import nbytes
from .execution import ExecutionUnit
from .protocol import core_name


class TLSU(ExecutionUnit):
    def operation_count(self, instruction):
        # 多段内存访问按 trace 顺序逐段执行，每段结果到期后再尝试下一段。
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
            # 显式路径 cost 覆盖 dtype 默认值；路径描述延迟/带宽，不模拟缓存命中状态。
            cost |= route
        work = nbytes(obj, access['shape'])
        # 所有 PE 的 load/store 竞争本 Core 的 memory 通道；服务量按实际访问范围计算。
        # load 在搬运后写 TileReg，store 先读 TileReg 再搬运；握手由 ExecutionUnit 负责。
        return self.service.start(core_name(instruction.core, 'memory'), 'memory', work,
                                  max(1, math.ceil(work / cost['throughput'])), cost['latency'], cycle)
