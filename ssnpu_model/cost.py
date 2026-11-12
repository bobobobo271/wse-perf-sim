"""Convert tile operations into sequential resource execution stages."""
from __future__ import annotations

import math

from .errors import ModelError
from .records import Stage
from .tiles import nbytes


class CostModel:
    VECTOR = {'full', 'add', 'adds', 'sub', 'subs', 'mul', 'muls', 'div', 'divs',
              'maximum', 'minimum', 'neg', 'abs', 'sqrt', 'rsqrt', 'relu',
              'row_expand_sub', 'row_expand_add', 'row_expand_mul', 'row_expand_div'}
    def __init__(self, config):
        self.config = config

    def stages(self, i):
        if i.alias or i.control:
            return []
        r, stages = i.raw, []
        ins, outs = r['inputs'], r['outputs']
        def add(engine, work, dtype, override=None):
            e = self.config['engines'][engine]
            d = dict(e.get('dtype_overrides', {}).get(dtype, {}))
            d.update(override or {})
            resource = f'core:{i.core}/' + (f'pe:{i.pe}/' if e['scope'] == 'PE' else '')
            # All vector categories share a single physical Vector engine.
            resource += 'vector' if engine in ('vector', 'reduce', 'exp', 'cast') else engine
            stages.append(Stage(engine, resource, work,
                                max(1, math.ceil(work / d.get('throughput', e['throughput']))),
                                d.get('latency', e['latency']), e['unit']))
        for o in ins:
            if o.get('memory_space') == 'SharedTileReg':
                add('shared_read', nbytes(o), o['dtype'])
        op = r['ir_op'].removeprefix('tile.')
        if op in ('load', 'store', 'ssnpu.load_into'):
            accesses = r.get('memory_accesses', [])
            if not accesses:
                raise ModelError(f'Missing memory access range at {i.id}')
            for access in accesses:
                obj = next((o for o in ins + outs if o['object_id'] == access['object_id']), None)
                if obj is None or access.get('shape') is None:
                    raise ModelError(f'Unresolved memory access range at {i.id}')
                path = obj['memory_space']
                if path not in self.config['paths']:
                    raise ModelError(f'Unknown memory path {path} at {i.id}')
                route = self.config['paths'][path]
                override = route if isinstance(route, dict) else {}
                add('memory', nbytes(obj, access['shape']), obj['dtype'], override)
        elif op in ('matmul', 'matmul_acc'):
            a, b = ins[-2:]
            m, k = a['shape']
            kb, n = b['shape']
            if k != kb:
                raise ModelError(f'Matmul shape mismatch at {i.id}')
            add('cube', m * n * k, a['dtype'])
        elif op in ('row_sum', 'row_max', 'row_min'):
            shape = ins[0].get('valid_shape', ins[0]['shape'])
            add('reduce', math.prod(shape[:-1]) * max(0, shape[-1] - 1), ins[0]['dtype'])
        elif op in self.VECTOR | {'exp', 'cast'}:
            out = outs[-1]
            add(op if op in ('exp', 'cast') else 'vector',
                math.prod(out.get('valid_shape', out['shape'])), out['dtype'])
        else:
            raise ModelError(f'Unknown operation {r["ir_op"]} at {i.id}')
        for o in outs:
            if o.get('memory_space') == 'SharedTileReg':
                add('shared_write', nbytes(o), o['dtype'])
        return stages
