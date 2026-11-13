"""NumPy executor for the actual decoded capture DAG and Calendar byte plan.

No timing is inferred here. The independent dense reference does not use the
capture's partition, online recurrence, or communication implementation.
"""
import heapq
import numpy as np

from wse_perf_sim.errors import ModelError
from wse_perf_sim.tiles import version_key


def make_inputs(spec):
    rng = np.random.default_rng(spec.seed)
    return {name: rng.normal(0, 0.5, shape).astype(np.float16) for name, shape in (
        ('Q', (spec.batch, spec.query_heads, spec.head_dim)),
        ('K', (spec.batch, spec.kv_heads, spec.sequence_length, spec.head_dim)),
        ('V', (spec.batch, spec.kv_heads, spec.sequence_length, spec.head_dim)))}


def validate_inputs(spec, arrays):
    shapes = dict(Q=(spec.batch, spec.query_heads, spec.head_dim),
                  K=(spec.batch, spec.kv_heads, spec.sequence_length, spec.head_dim),
                  V=(spec.batch, spec.kv_heads, spec.sequence_length, spec.head_dim))
    for name, shape in shapes.items():
        if name not in arrays or arrays[name].shape != shape or arrays[name].dtype != np.float16:
            raise ModelError(f'{name} must be float16 with shape {shape}')
        if not np.isfinite(arrays[name]).all():
            raise ModelError(f'{name} must contain finite inputs')


def execute_capture(trace, spec, arrays):
    validate_inputs(spec, arrays)
    g = spec.query_heads // spec.kv_heads
    memory, values = {}, {}
    for batch in range(spec.batch):
        for head in range(spec.kv_heads):
            memory[f'Q:{batch}:{head}'] = arrays['Q'][batch, head*g:(head+1)*g].copy()
            for name in ('K', 'V'):
                memory[f'{name}:{batch}:{head}'] = arrays[name][batch, head].copy()
    phases = {phase['name']: phase for phase in trace.calendar_phases}
    pending, dependents = {}, {key: [] for key in trace.instructions}
    ready = []
    for key, instruction in trace.instructions.items():
        deps = instruction.deps | instruction.sync
        if instruction.raw['ir_op'] == 'comm.wait':
            # In the timing model this is enforced by Calendar's alignment and
            # arrival accounting, not by scheduler DAG edges. Mirror that
            # collective contract before reading any sender's payload.
            phase = phases[instruction.raw['communication']['phase']]
            deps = deps | {f'comm:{phase["name"]}:{entry["core"]}:submit' for entry in phase['nodes']}
        pending[key] = len(deps)
        for dep in deps:
            dependents[dep].append(key)
        if not deps:
            heapq.heappush(ready, (instruction.task, instruction.age, str(key), key))

    def tensor(obj):
        name = obj['object_id']
        if name not in memory:
            memory[name] = np.zeros(obj['shape'], dtype=obj['dtype'])
        return memory[name]

    def read(obj):
        return tensor(obj) if obj['kind'] == 'tensor' else values[version_key(obj)]

    completed = 0
    while ready:
        _, _, _, key = heapq.heappop(ready)
        instruction = trace.instructions[key]
        row = instruction.raw
        op, params = row['ir_op'], row.get('parameters', {})
        result = None
        if instruction.control or op in ('comm.submit',):
            pass
        elif op == 'comm.wait':
            phase = phases[row['communication']['phase']]
            entry = next(entry for entry in phase['nodes'] if entry['core'] == instruction.core)
            output, = row['outputs']
            result = np.zeros(output['shape'], dtype=output['dtype'])
            raw = result.view(np.uint8).reshape(-1)
            covered = np.zeros(raw.size, dtype=np.bool_)
            for expected in entry['expected']:
                sender = next(e for e in phase['nodes'] if e['core'] == expected['source'])
                source = np.ascontiguousarray(values[tuple(sender['source_version'])]).view(np.uint8).reshape(-1)
                start, size = expected['offset'], expected['nbytes']
                offset = sender.get('source_offset', 0)
                if covered[start:start+size].any() or source[offset:offset+size].size != size:
                    raise ModelError('Numerical Calendar byte coverage is invalid')
                raw[start:start+size] = source[offset:offset+size]
                covered[start:start+size] = True
            if not covered.all():
                raise ModelError('Numerical Calendar target has uncovered bytes')
        elif op == 'memory.copy':
            source, target = tensor(row['inputs'][0]), tensor(row['outputs'][0])
            sr, dr, count = params['src_row'], params['dst_row'], params['rows']
            target[dr:dr+count] = source[sr:sr+count]
        elif op == 'tile.load':
            result = tensor(row['inputs'][0])[params['row']:params['row']+params['rows']].copy()
        elif op == 'tile.store':
            tensor(row['outputs'][0])[params['row']:params['row']+params['rows']] = read(row['inputs'][0])
        else:
            args = [read(obj) for obj in row['inputs']]
            name = op.removeprefix('tile.')
            if name == 'full':
                result = np.full(row['outputs'][0]['shape'], float(params['value']), dtype=np.float32)
            elif name == 'slice':
                result = args[0][params['row']:params['row']+params['count'],
                                 params['col']:params['col']+params['width']].copy()
            elif name == 'concat':
                result = np.concatenate(args, axis=params['axis'])
            elif name == 'transpose_view':
                result = args[0].T
            elif name == 'matmul':
                # FP16 inputs with FP32 accumulation, also for QK and PV.
                result = args[0].astype(np.float32) @ args[1].astype(np.float32)
            elif name == 'muls':
                result = args[0] * np.float32(params['scalar'])
            elif name in ('row_max', 'row_sum'):
                result = (np.max if name == 'row_max' else np.sum)(args[0], axis=1, keepdims=True)
            elif name == 'exp':
                result = np.exp(args[0])
            elif name == 'cast':
                result = args[0].astype(row['outputs'][0]['dtype'])
            elif name == 'maximum':
                result = np.maximum(*args)
            elif name in ('add', 'sub', 'mul', 'row_expand_sub', 'row_expand_mul', 'row_expand_div'):
                function = {'add': np.add, 'sub': np.subtract, 'mul': np.multiply,
                            'row_expand_sub': np.subtract, 'row_expand_mul': np.multiply,
                            'row_expand_div': np.divide}[name]
                result = function(*args)
            else:
                raise ModelError(f'Unsupported numerical operation: {op}')
        if result is not None:
            output, = row['outputs']
            if list(result.shape) != output['shape']:
                raise ModelError(f'Numerical shape mismatch at {key}')
            values[version_key(output)] = result.astype(output['dtype'], copy=False)
        completed += 1
        for consumer in dependents[key]:
            pending[consumer] -= 1
            if not pending[consumer]:
                item = trace.instructions[consumer]
                heapq.heappush(ready, (item.task, item.age, str(consumer), consumer))
    if completed != len(trace.instructions):
        raise ModelError('Numerical execution did not complete its dependency graph')
    return np.stack([np.concatenate([memory[f'O:{batch}:{head}'] for head in range(spec.kv_heads)])
                     for batch in range(spec.batch)])


def dense_reference(spec, arrays):
    """Independent full-sequence float64 softmax, one query per head."""
    validate_inputs(spec, arrays)
    g = spec.query_heads // spec.kv_heads
    output = np.empty((spec.batch, spec.query_heads, spec.head_dim), dtype=np.float64)
    for batch in range(spec.batch):
        for head in range(spec.kv_heads):
            q = arrays['Q'][batch, head*g:(head+1)*g].astype(np.float64)
            k, v = (arrays[name][batch, head].astype(np.float64) for name in ('K', 'V'))
            scores = q @ k.T / np.sqrt(spec.head_dim)
            p = np.exp(scores - scores.max(axis=1, keepdims=True))
            output[batch, head*g:(head+1)*g] = (p / p.sum(axis=1, keepdims=True)) @ v
    return output


def compare(actual, expected, *, atol=5e-4, rtol=5e-3):
    difference = np.abs(actual.astype(np.float64) - expected.astype(np.float64))
    limits = atol + rtol*np.abs(expected)
    return dict(passed=bool(np.isfinite(actual).all() and np.all(difference <= limits)),
                max_abs_error=float(difference.max()), rmse=float(np.sqrt(np.mean(difference**2))),
                max_tolerance_ratio=float((difference/limits).max()), atol=atol, rtol=rtol)
