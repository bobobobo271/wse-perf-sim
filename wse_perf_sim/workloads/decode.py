"""Parameterized GQA decode capture. Q/K/V are inputs; no projections.

Numerical execution is optional (NumPy); timing always uses the component model.
HBM chunks stage into two SRAM slots, then compute tiles feed shared K/V reads.
"""
from dataclasses import asdict, dataclass
from collections import defaultdict
from pathlib import Path
import copy
import json
import math

from wse_perf_sim.config import wse_config
from wse_perf_sim.errors import ModelError
from wse_perf_sim.frontend.communication import allgather_group
from wse_perf_sim.tiles import nbytes, version_key


@dataclass(frozen=True)
class DecodeSpec:
    batch: int = 1
    query_heads: int = 8
    kv_heads: int = 2
    head_dim: int = 32
    sequence_length: int = 512
    sequence_splits: int = 4
    tile_tokens: int = 64
    hbm_chunk_tokens: int = 128
    hbm_gbps: float = 256.0
    hbm_startup_ns: float = 0.0
    pe_count: int = 4
    seed: int = 7

    def validate(self):
        for key in ('batch', 'query_heads', 'kv_heads', 'head_dim', 'sequence_length',
                    'sequence_splits', 'tile_tokens', 'hbm_chunk_tokens', 'pe_count'):
            if type(getattr(self, key)) is not int or getattr(self, key) <= 0:
                raise ModelError(f'{key} must be a positive integer')
        if self.query_heads % self.kv_heads:
            raise ModelError('query_heads must be divisible by kv_heads')
        if self.sequence_splits > 40:
            raise ModelError('sequence_splits exceeds the WSE40 core count')
        if self.hbm_chunk_tokens < self.tile_tokens:
            raise ModelError('hbm_chunk_tokens must be at least tile_tokens')
        for name in ('hbm_gbps', 'hbm_startup_ns'):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ModelError(f'{name} must be finite')
        if self.hbm_gbps <= 0 or self.hbm_startup_ns < 0:
            raise ModelError('HBM bandwidth must be positive and startup nonnegative')
        return self


class Capture:
    def __init__(self, spec):
        self.spec, self.rows, self.phases = spec, [], []
        self.instances, self.objects = defaultdict(list), {}
        self.number = 0
        self.core = self.pe = self.task = 0

    def tensor(self, name, shape, dtype='float16', space='SRAM', base=0):
        obj = dict(object_id=name, kind='tensor', shape=list(shape), dtype=dtype,
                   memory_space=space, base_bytes=base)
        self.objects[name] = obj
        return obj

    def tile(self, name, shape, dtype='float32', shared=False, producer=None):
        obj = dict(object_id=name, storage_id=name, version=0, kind='tile',
                   shape=list(shape), valid_shape=list(shape), dtype=dtype,
                   memory_space='SharedTileReg' if shared else 'LocalTileReg', producer=producer)
        if shared:
            obj['shared_version'] = name + ':0'
        # Enforce the documented Local Tile object limit for this workload.
        if not shared and nbytes(obj) > 32768:
            raise ModelError(f'Local Tile {name} exceeds 32 KiB; reduce tile_tokens or query heads per PE')
        self.objects[name] = obj
        return obj

    def emit(self, operation, inputs=(), *, shape=None, dtype='float32', shared=False,
             parameters=None, deps=(), output=None, accesses=(), label=None):
        key = f'{self.number:06d}:{label or operation}'
        self.number += 1
        if output is None:
            output = self.tile(key, shape, dtype, shared, key)
        thread = self.core * self.spec.pe_count + self.pe
        identity = dict(task_id=self.task, block_id=self.core, subblock_id=0, thread_id=thread)
        instance = self.instances[self.task, self.core, self.pe]
        raw = dict(identity, op_id=key, ir_op=operation, inputs=copy.deepcopy(list(inputs)),
                   outputs=[copy.deepcopy(output)], parameters=parameters or {}, dependencies=list(deps),
                   sync_dependencies=[], memory_accesses=list(accesses),
                   ssnpu=dict(phase=0, participants=[thread], shared_inputs=[]),
                   instance_op_sequence=len(instance),
                   order_dependencies=[] if not instance else [instance[-1]['op_id']])
        self.rows.append(raw)
        instance.append(raw)
        return output if output['kind'] == 'tile' else key

    def full(self, shape, value, deps=()):
        return self.emit('tile.full', shape=shape, parameters={'value': value}, deps=deps)

    def unary(self, op, x, *, shape=None, dtype=None, **parameters):
        return self.emit('tile.' + op, [x], shape=shape or x['shape'],
                         dtype=dtype or x['dtype'], parameters=parameters)

    def binary(self, op, x, y, shape=None):
        return self.emit('tile.' + op, [x, y], shape=shape or x['shape'])

    def slice(self, x, row, count, col, width):
        return self.unary('slice', x, shape=[count, width], row=row, count=count, col=col, width=width)

    def concat(self, values, axis, shared=False):
        shape = list(values[0]['shape'])
        shape[axis] = sum(value['shape'][axis] for value in values)
        return self.emit('tile.concat', values, shape=shape, shared=shared, parameters={'axis': axis})

    def access(self, obj, shape, row=0):
        return dict(object_id=obj['object_id'], shape=list(shape),
                    offset_bytes=obj.get('base_bytes', 0) + row * obj['shape'][1] * (2 if obj['dtype'] == 'float16' else 4))

    def copy(self, source, target, rows, *, src_row=0, dst_row=0, deps=(), label='HBM→SRAM'):
        shape = [rows, source['shape'][1]]
        return self.emit('memory.copy', [source], output=target, deps=deps, label=label,
                         accesses=[self.access(source, shape, src_row), self.access(target, shape, dst_row)],
                         parameters=dict(src_row=src_row, dst_row=dst_row, rows=rows))

    def load(self, source, rows, *, row=0, deps=(), shared=False):
        shape = [rows, source['shape'][1]]
        return self.emit('tile.load', [source], shape=shape, dtype=source['dtype'], shared=shared,
                         deps=deps, parameters=dict(row=row, rows=rows), accesses=[self.access(source, shape, row)])

    def write(self, directory):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        # Declare the final consumer in each thread as one shared last-use group.
        uses = defaultdict(dict)
        for row in self.rows:
            for obj in row['inputs']:
                if obj.get('shared_version'):
                    uses[obj['shared_version']][row['thread_id']] = row
        for version, consumers in uses.items():
            use = dict(use_id='last:' + version, operand=0, version_id=version,
                       retention='last-use', participants=sorted(consumers))
            for row in consumers.values():
                row['ssnpu']['shared_inputs'].append(use)
        instances = []
        for (task, core, pe), rows in sorted(self.instances.items()):
            filename = f'task_{task}_core_{core}_pe_{pe}.jsonl'
            (directory / filename).write_text(''.join(json.dumps(row, allow_nan=False) + '\n' for row in rows))
            instances.append(dict(task_id=task, block_id=core, subblock_id=0,
                                  thread_id=core*self.spec.pe_count+pe, mapping=dict(core_id=core, pe_id=pe),
                                  operation_count=len(rows), status='completed', trace_file=filename))
        tasks = sorted({key[0] for key in self.instances})
        files = {'objects.json': list(self.objects.values()),
                 'tasks.json': [dict(task_id=t, dependencies=[] if t == 0 else [t-1], status='completed') for t in tasks],
                 'spec.json': asdict(self.spec),
                 'manifest.json': dict(format_version=2, ssnpu_spec_version=1, complete=True,
                     instances=instances, objects_file='objects.json', tasks_file='tasks.json',
                     config_file='spec.json', events_file='events.jsonl')}
        for name, data in files.items():
            (directory / name).write_text(json.dumps(data, indent=2, allow_nan=False) + '\n')
        (directory / 'events.jsonl').write_text('')
        plan = None if not self.phases else dict(schema='wse-communication/1', non_speculative=True, phases=self.phases)
        if plan is not None:
            (directory / 'communication.json').write_text(json.dumps(plan, indent=2) + '\n')
        return plan


def make_decode_capture(directory, spec, *, single_core=False):
    spec.validate()
    if single_core and spec.sequence_splits != 1:
        raise ModelError('A single-core reference requires sequence_splits=1')
    b = Capture(spec)
    g, d, splits = spec.query_heads // spec.kv_heads, spec.head_dim, spec.sequence_splits
    columns = [0] if single_core else [row*8+col for col in range(8) for row in range(5)]
    groups_per_wave = len(columns) // splits
    mapping, sram_bytes = [], {}
    for group in range(spec.batch * spec.kv_heads):
        batch, head = divmod(group, spec.kv_heads)
        b.task = group // groups_per_wave
        members = columns[(group % groups_per_wave)*splits: (group % groups_per_wave+1)*splits]
        sources, owners = {}, {}
        for rank, core in enumerate(members):
            b.core, b.pe = core, 0
            prefix = f'b{batch}:h{head}:c{core}'
            start, end = spec.sequence_length*rank//splits, spec.sequence_length*(rank+1)//splits
            mapping.append(dict(batch=batch, kv_head=head, core=core, rank=rank, start=start, end=end, wave=b.task))
            qh = b.tensor(f'Q:{batch}:{head}', [g, d], space='HBM')
            kh = b.tensor(f'K:{batch}:{head}', [spec.sequence_length, d], space='HBM')
            vh = b.tensor(f'V:{batch}:{head}', [spec.sequence_length, d], space='HBM')
            qs = b.tensor(prefix+':Q', [g, d])
            qcopy = b.copy(qh, qs, g, label='Q HBM→SRAM')
            qshared = b.load(qs, g, deps=[qcopy], shared=True)
            states, queries = {}, {}
            for pe in range(min(spec.pe_count, g)):
                b.pe = pe
                lo, hi = g*pe//min(spec.pe_count, g), g*(pe+1)//min(spec.pe_count, g)
                queries[pe] = b.slice(qshared, lo, hi-lo, 0, d)
                states[pe] = (b.full([hi-lo, 1], '-inf'), b.full([hi-lo, 1], 0), b.full([hi-lo, d], 0))
            buffers, slot_done = {}, {0: [], 1: []}
            base = 2*g*d
            for slot in range(2):
                for kind in ('K', 'V'):
                    buffers[slot, kind] = b.tensor(prefix+f':{kind}:{slot}', [spec.hbm_chunk_tokens, d], base=base)
                    base += 2*spec.hbm_chunk_tokens*d
            os = b.tensor(prefix+':O', [g, d], 'float32', base=base)
            sram_bytes[core] = max(sram_bytes.get(core, 0), base+4*g*d)
            for chunk, chunk_start in enumerate(range(start, end, spec.hbm_chunk_tokens)):
                b.pe = 0
                slot = chunk % 2
                count = min(spec.hbm_chunk_tokens, end-chunk_start)
                kcopy = b.copy(kh, buffers[slot, 'K'], count, src_row=chunk_start, deps=slot_done[slot], label='K HBM→SRAM')
                vcopy = b.copy(vh, buffers[slot, 'V'], count, src_row=chunk_start, deps=slot_done[slot], label='V HBM→SRAM')
                done = []
                for offset in range(0, count, spec.tile_tokens):
                    b.pe = 0
                    rows = min(spec.tile_tokens, count-offset)
                    kj = b.load(buffers[slot, 'K'], rows, row=offset, deps=[kcopy], shared=True)
                    vj = b.load(buffers[slot, 'V'], rows, row=offset, deps=[vcopy], shared=True)
                    for pe, qi in queries.items():
                        b.pe = pe
                        m, ell, u = states[pe]
                        kt = b.emit('tile.transpose_view', [kj], shape=[d, rows], dtype='float16',
                                    parameters={'axes': [1, 0]})
                        scores = b.binary('matmul', qi, kt, [qi['shape'][0], rows])
                        scores = b.unary('muls', scores, scalar=1/math.sqrt(d))
                        block_max = b.unary('row_max', scores, shape=[qi['shape'][0], 1])
                        mn = b.binary('maximum', m, block_max)
                        alpha = b.unary('exp', b.binary('sub', m, mn))
                        p = b.unary('exp', b.binary('row_expand_sub', scores, mn))
                        ln = b.binary('add', b.binary('mul', ell, alpha), b.unary('row_sum', p, shape=ell['shape']))
                        ph = b.unary('cast', p, dtype='float16')
                        pv = b.binary('matmul', ph, vj, u['shape'])
                        un = b.binary('add', b.binary('row_expand_mul', u, alpha), pv)
                        states[pe] = mn, ln, un
                        done.extend([ln['producer'], un['producer']])
                slot_done[slot] = done
            packed = []
            for pe, state in states.items():
                b.pe = pe
                packed.append(b.concat(state, 1, shared=True))
            b.pe = 0
            sources[core] = b.concat(packed, 0, shared=True)
            owners[core] = sources[core]['producer']
        targets = sources
        if splits > 1:
            targets = {core: b.tile(f'group:{group}:gather:{core}', [splits*g, d+2], shared=True) for core in members}
            phase = allgather_group(sources, targets, owners, 4*g*(d+2),
                                    name=f'split_kv:{group}', epoch=len(b.phases) % 256)['phases'][0]
            for entry in phase['nodes']:
                entry['release_source'] = True
            b.phases.append(phase)
        for rank, core in enumerate(members):
            b.core, b.pe = core, 0
            state = targets[core]
            ms = [b.slice(state, r*g, g, 0, 1) for r in range(splits)]
            maximum = b.unary('row_max', b.concat(ms, 1), shape=[g, 1])
            ell, u = b.full([g, 1], 0), b.full([g, d], 0)
            for r in range(splits):
                weight = b.unary('exp', b.binary('sub', ms[r], maximum))
                lr = b.slice(state, r*g, g, 1, 1)
                ur = b.slice(state, r*g, g, 2, d)
                ell = b.binary('add', ell, b.binary('mul', lr, weight))
                u = b.binary('add', u, b.binary('row_expand_mul', ur, weight))
            output = b.binary('row_expand_div', u, ell)
            if rank == 0:
                os = b.objects[f'b{batch}:h{head}:c{core}:O']
                stored = b.emit('tile.store', [output], output=os, accesses=[b.access(os, [g, d])],
                                parameters={'row': 0, 'rows': g}, label='O Tile→SRAM')
                oh = b.tensor(f'O:{batch}:{head}', [g, d], 'float32', 'HBM')
                b.copy(os, oh, g, deps=[stored], label='O SRAM→HBM')
    config = wse_config()
    config['parameters']['pe_count']['value'] = spec.pe_count
    config['hbm'] = dict(bandwidth_bytes_per_second=spec.hbm_gbps*1e9, startup_ns=spec.hbm_startup_ns)
    if max(sram_bytes.values()) > config['wse']['sram_capacity_bytes']['value']:
        raise ModelError('Decode double buffers exceed per-core SRAM capacity')
    plan = b.write(directory)
    layout = dict(mapping=mapping, sram_reserved_bytes=sram_bytes,
                  q_distribution='Each worker reads its Q copy from HBM; counted explicitly.',
                  output='Every worker merges; only rank 0 writes O to HBM.',
                  kv_residency='Cold per invocation; no cross-step SRAM KV reuse.',
                  collectives='Group AllGather, conservatively serialized automatic Calendar schedule.')
    Path(directory, 'layout.json').write_text(json.dumps(layout, indent=2)+'\n')
    return config, plan, layout
