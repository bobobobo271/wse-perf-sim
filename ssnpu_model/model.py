"""Deterministic tile scheduler. All times are integer cycles, intervals half-open."""
from __future__ import annotations

import copy
import heapq
import hashlib
import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path


class ModelError(ValueError):
    pass


# Values are deliberately synthetic. No hardware calibration is implied.
PARAMETERS = {
    'pe_count': (4, 'PE', 'Core', 'Number of independent compute engines'),
    'pe_issue_width': (2, 'tile/cycle', 'PE', 'Maximum tile issues per cycle'),
    'core_issue_width': (4, 'tile/cycle', 'Core', 'Maximum tile issues per cycle'),
    'local_capacity': (1048576, 'byte', 'PE', 'LocalTileReg capacity'),
    'shared_capacity': (1048576, 'byte', 'Core', 'SharedTileReg capacity'),
    'barrier_latency': (2, 'cycle', 'block', 'Full phase synchronization delay'),
}
ENGINES = {
    'cube': (4096, 4, 'MAC/cycle', 'PE'),
    'vector': (256, 2, 'element/cycle', 'PE'),
    'exp': (64, 4, 'element/cycle', 'PE'),
    'cast': (256, 2, 'element/cycle', 'PE'),
    'reduce': (128, 3, 'reduction/cycle', 'PE'),
    'memory': (256, 8, 'byte/cycle', 'Core'),
    'shared_read': (256, 2, 'byte/cycle', 'Core'),
    'shared_write': (256, 2, 'byte/cycle', 'Core'),
}


def default_config():
    return {
        'name': 'logic-validation-only',
        'parameters': {k: {'value': v, 'unit': u, 'scope': s, 'meaning': m,
                           'source': 'synthetic logical validation; uncalibrated'}
                       for k, (v, u, s, m) in PARAMETERS.items()},
        'engines': {k: {'throughput': t, 'latency': l, 'unit': u, 'scope': s,
                        'meaning': 'Service rate and result latency after service',
                        'source': 'synthetic logical validation; uncalibrated',
                        'dtype_overrides': {}} for k, (t, l, u, s) in ENGINES.items()},
        'paths': {'DDR': 'memory', 'L1': 'memory', 'L2': 'memory'},
    }


def validate_config(c):
    try:
        for k in PARAMETERS:
            v = c['parameters'][k]['value']
            if type(v) is not int or v < (0 if k == 'barrier_latency' else 1):
                raise ModelError(f'Invalid parameter {k}: {v}')
        for k, (_, _, _, scope) in ENGINES.items():
            e = c['engines'][k]
            if e['scope'] != scope:
                raise ModelError(f'Invalid scope for {k}')
            for v in [e] + list(e.get('dtype_overrides', {}).values()):
                rate, latency = v.get('throughput', e['throughput']), v.get('latency', e['latency'])
                if not isinstance(rate, (int, float)) or not math.isfinite(rate) or rate <= 0:
                    raise ModelError(f'Invalid throughput for {k}')
                if type(latency) is not int or latency < 0:
                    raise ModelError(f'Invalid latency for {k}')
        for path, engine in c['paths'].items():
            if isinstance(engine, dict):
                if engine.get('engine') != 'memory' or not isinstance(engine.get('throughput', 1), (int, float)) or not math.isfinite(engine.get('throughput', 1)) or engine.get('throughput', 1) <= 0 or type(engine.get('latency', 0)) is not int or engine.get('latency', 0) < 0:
                    raise ModelError(f'Invalid memory path {path}: {engine}')
            elif engine != 'memory':
                raise ModelError(f'Unsupported path engine {path}: {engine}')
    except (KeyError, TypeError) as exc:
        raise ModelError(f'Incomplete configuration: {exc}') from exc
    return c


DTYPES = {'float16': 2, 'fp16': 2, 'bfloat16': 2, 'bf16': 2,
          'float32': 4, 'fp32': 4, 'int8': 1, 'uint8': 1, 'int16': 2,
          'int32': 4, 'int64': 8, 'float64': 8, 'bool': 1}


def nbytes(obj, shape=None):
    try:
        dims = obj.get('valid_shape', obj['shape']) if shape is None else shape
        if any(type(n) is not int or n < 0 for n in dims):
            raise ModelError(f'Invalid shape {dims}')
        return math.prod(dims) * DTYPES[obj['dtype']]
    except KeyError as exc:
        raise ModelError(f'Missing shape or unsupported dtype: {obj}') from exc


def version_key(o):
    if o.get('shared_version') is not None:
        return ('shared', o['shared_version'])
    return ('storage', o.get('storage_id', o['object_id']), o['version'])


@dataclass
class Instruction:
    id: object
    core: int
    pe: int
    task: int
    block: int
    age: int
    raw: dict
    deps: set = field(default_factory=set)
    sync: set = field(default_factory=set)
    inputs: set = field(default_factory=set)
    outputs: set = field(default_factory=set)
    alias: bool = False
    control: bool = False


@dataclass
class Version:
    key: tuple
    obj: dict
    producer: object
    pool: str
    slot: tuple
    size: int
    consumers: set = field(default_factory=set)
    retained: bool = False


class TraceAdapter:
    """Capture v2 / SSNPU v1 boundary; order edges only establish age."""
    def __init__(self, capture_dir, config):
        self.path, self.config = Path(capture_dir), config
        self.instructions, self.versions = {}, {}
        self.source_hashes = {}
        self.block_members = defaultdict(set)
        manifest = self.read('manifest.json')
        if manifest.get('format_version') != 2 or manifest.get('ssnpu_spec_version') != 1:
            raise ModelError('Expected capture format_version=2, ssnpu_spec_version=1')
        if manifest.get('complete') is not True:
            raise ModelError('Capture is incomplete')
        self.capture_config = self.read(manifest['config_file']) if manifest.get('config_file') else {}
        self.objects = self.read(manifest['objects_file'])
        self.tasks = self.read(manifest['tasks_file'])
        events = self.lines(manifest['events_file'])
        instances = manifest['instances']
        seen_threads = set()
        for instance in instances:
            if instance.get('status') != 'completed':
                raise ModelError(f'Incomplete instance: {instance}')
            core, pe = instance['mapping']['core_id'], instance['mapping']['pe_id']
            if not 0 <= pe < config['parameters']['pe_count']['value']:
                raise ModelError(f'PE mapping outside configuration: {pe}')
            self.block_members[instance['task_id'], instance['block_id']].add((core, pe))
            identity = tuple(instance[k] for k in ('task_id', 'block_id', 'subblock_id', 'thread_id'))
            if identity in seen_threads:
                raise ModelError(f'Duplicate thread {identity}')
            seen_threads.add(identity)
            records = self.lines(instance['trace_file'])
            if len(records) != instance['operation_count']:
                raise ModelError(f'Operation count mismatch: {identity}')
            previous = None
            for sequence, r in enumerate(records):
                if tuple(r[k] for k in ('task_id', 'block_id', 'subblock_id', 'thread_id')) != identity:
                    raise ModelError(f'Thread identity mismatch: {r["op_id"]}')
                if r['instance_op_sequence'] != sequence:
                    raise ModelError(f'Non-contiguous sequence: {r["op_id"]}')
                if r.get('order_dependencies', []) != ([] if previous is None else [previous]):
                    raise ModelError(f'Unsupported order_dependencies contract at {r["op_id"]}')
                previous = r['op_id']
                r['_identity'] = identity
                self.add(Instruction(r['op_id'], core, pe, r['task_id'], r['block_id'],
                                     r['instance_op_sequence'], r,
                                     set(r['dependencies']), set(r.get('sync_dependencies', [])),
                                     alias=r['ir_op'] in {'tile.transpose_view', 'tile.reshape', 'tile.reshape_view', 'tile.view'}))
        real = list(self.instructions.values())
        self._versions(real)
        self._barriers(events, instances, real)
        self._tasks(real)
        self._validate_graph()

    def read(self, name):
        path = (self.path / name).resolve()
        if not path.is_relative_to(self.path.resolve()):
            raise ModelError(f'Capture path escapes directory: {name}')
        data = path.read_bytes()
        self.source_hashes[name] = hashlib.sha256(data).hexdigest()
        return json.loads(data)

    def lines(self, name):
        path = (self.path / name).resolve()
        if not path.is_relative_to(self.path.resolve()):
            raise ModelError(f'Capture path escapes directory: {name}')
        data = path.read_bytes()
        self.source_hashes[name] = hashlib.sha256(data).hexdigest()
        return [json.loads(line) for line in data.splitlines() if line.strip()]

    def add(self, i):
        if i.id in self.instructions:
            raise ModelError(f'Duplicate instruction: {i.id}')
        self.instructions[i.id] = i

    def _versions(self, real):
        aliases = {}
        for i in real:
            if i.alias:
                if len(i.raw['inputs']) != 1:
                    raise ModelError(f'View requires one source: {i.id}')
                for o in i.raw['outputs']:
                    aliases[version_key(o)] = version_key(i.raw['inputs'][0])
        def canonical(key):
            visited = set()
            while key in aliases and aliases[key] != key:
                if key in visited:
                    raise ModelError(f'Alias cycle: {key}')
                visited.add(key)
                key = aliases[key]
            return key
        for i in real:
            if i.alias:
                continue
            for o in i.raw['outputs']:
                if o['kind'] != 'tile':
                    continue
                key = canonical(version_key(o))
                shared = o['memory_space'] == 'SharedTileReg'
                if o['memory_space'] not in ('SharedTileReg', 'LocalTileReg'):
                    raise ModelError(f'Unsupported tile memory: {o["memory_space"]}')
                pool = f'core:{i.core}/shared' if shared else f'core:{i.core}/pe:{i.pe}/local'
                slot = (pool, o.get('storage_id', o['object_id']))
                if key in self.versions:
                    raise ModelError(f'Duplicate version producer: {key}')
                self.versions[key] = Version(key, o, i.id, pool, slot, nbytes(o, o['shape']), retained=shared)
                i.outputs.add(key)
        uses = defaultdict(list)
        for i in real:
            for o in i.raw['inputs']:
                producer = o.get('producer')
                if producer is not None and producer != i.id:
                    i.deps.add(producer)
                if o.get('view_producer') is not None:
                    i.deps.add(o['view_producer'])
                if o['kind'] != 'tile':
                    continue
                key = canonical(version_key(o))
                if key not in self.versions:
                    raise ModelError(f'Missing producer for tile {key} at op {i.id}')
                v = self.versions[key]
                if v.pool.split('/')[0] != f'core:{i.core}' or ('/local' in v.pool and v.pool != f'core:{i.core}/pe:{i.pe}/local'):
                    raise ModelError(f'Tile accessed outside owning Core/PE: {key}')
                i.inputs.add(key)
                i.deps.add(v.producer)
                v.consumers.add(i.id)
            for use in i.raw.get('ssnpu', {}).get('shared_inputs', []):
                uses[(use['use_id'], use['operand'])].append((i, use))
        terminal_uses = defaultdict(list)
        version_uses = defaultdict(list)
        for group, entries in uses.items():
            first = entries[0][1]
            if any(u != first for _, u in entries) or sorted(i.raw['thread_id'] for i, _ in entries) != sorted(first['participants']):
                raise ModelError(f'Shared-use participant mismatch: {group}')
            key = ('shared', first['version_id'])
            if key not in self.versions or any(key not in i.inputs for i, _ in entries):
                raise ModelError(f'Invalid shared-use version: {group}')
            version_uses[key].append(entries)
            if first['retention'] == 'last-use':
                terminal_uses[key].append(entries)
                self.versions[key].retained = False
            elif first['retention'] != 'reuse':
                raise ModelError(f'Unknown retention: {first}')
        for key, terminals in terminal_uses.items():
            if len(terminals) != 1:
                raise ModelError(f'Multiple last-use groups: {key}')
            terminal = terminals[0]
            for entries in version_uses[key]:
                if entries is terminal:
                    continue
                for i, _ in entries:
                    if not any(i.raw['ssnpu']['phase'] < end.raw['ssnpu']['phase'] or
                               (i.raw['_identity'] == end.raw['_identity'] and i.age < end.age)
                               for end, _ in terminal):
                        raise ModelError(f'Read after last-use or unordered lifetime: {key}')
        slots = defaultdict(list)
        for v in self.versions.values():
            slots[v.slot].append(v)
        # Fixed locations cannot be overwritten until all old readers finish.
        # The overwriting instruction may itself read the old version in-place.
        for versions in slots.values():
            versions.sort(key=lambda v: (self.instructions[v.producer].task,
                                         self.instructions[v.producer].raw['ssnpu']['phase'],
                                         self.instructions[v.producer].age))
            for old, new in zip(versions, versions[1:]):
                older, newer = self.instructions[old.producer], self.instructions[new.producer]
                if (older.task == newer.task and older.raw['ssnpu']['phase'] == newer.raw['ssnpu']['phase']
                        and older.raw['_identity'] != newer.raw['_identity']):
                    pending, seen = list(newer.deps | newer.sync), set()
                    while pending:
                        dep = pending.pop()
                        if dep in seen or dep not in self.instructions:
                            continue
                        seen.add(dep)
                        pending.extend(self.instructions[dep].deps | self.instructions[dep].sync)
                    if older.id not in seen:
                        raise ModelError(f'Unordered fixed-position writers: {old.key}, {new.key}')
                writer = self.instructions[new.producer]
                writer.deps.update(({old.producer} | old.consumers) - {writer.id})

    def _barriers(self, events, instances, real):
        groups = defaultdict(list)
        for e in events:
            if e['kind'] == 'ssnpu.barrier':
                groups[(e['task_id'], e['block_id'], e['parameters']['round'])].append(e)
        blocks = {(x['task_id'], x['block_id']) for x in instances}
        for task, block in sorted(blocks):
            threads = [x['thread_id'] for x in instances if (x['task_id'], x['block_id']) == (task, block)]
            rows = [i for i in real if (i.task, i.block) == (task, block)]
            rounds = sorted(r for t, b, r in groups if (t, b) == (task, block))
            if rounds != list(range(len(rounds))):
                raise ModelError(f'Non-contiguous barrier rounds: {task}/{block}')
            for r in rounds:
                es = groups[task, block, r]
                p = es[0]['parameters']
                if sorted(e['thread_id'] for e in es) != sorted(threads) or any(
                        e['parameters']['barrier_id'] != p['barrier_id'] or
                        sorted(e['parameters']['participants']) != sorted(threads) for e in es):
                    raise ModelError(f'Barrier participants/site mismatch: {task}/{block}/{r}')
                ids = {i.id for i in rows if i.raw['ssnpu']['phase'] <= r}
                for e in es:
                    if set(e.get('completion_dependencies', [])) != ids:
                        raise ModelError(f'Barrier completion dependencies mismatch: {e["event_id"]}')
                bid = f'barrier:{task}:{block}:{r}'
                deps = ids | ({f'barrier:{task}:{block}:{r-1}'} if r else set())
                owner = next(x for x in instances if (x['task_id'], x['block_id']) == (task, block))
                self.add(Instruction(bid, owner['mapping']['core_id'], 0, task, block, r,
                                     {'ir_op': 'barrier'}, deps=deps, control=True))
            for i in rows:
                phase = i.raw['ssnpu']['phase']
                if type(phase) is not int or not 0 <= phase <= len(rounds):
                    raise ModelError(f'Invalid/missing barrier phase at {i.id}')
                if phase:
                    i.sync.add(f'barrier:{task}:{block}:{phase-1}')

    def _tasks(self, real):
        task_ids = {t['task_id'] for t in self.tasks}
        if any(i.task not in task_ids for i in real):
            raise ModelError('Instruction references missing task')
        for t in self.tasks:
            tid = t['task_id']
            rows = [i for i in self.instructions.values() if i.task == tid]
            if t.get('status') not in ('completed', 'skipped'):
                raise ModelError(f'Incomplete task {tid}')
            for dep in t.get('dependencies', []):
                if dep not in task_ids:
                    raise ModelError(f'Missing task dependency {dep}')
                for i in rows:
                    i.sync.add(f'task:{dep}')
            owner = next((i for i in rows), None)
            self.add(Instruction(f'task:{tid}', owner.core if owner else 0, 0, tid, -1, 0,
                                 {'ir_op': 'task_complete'},
                                 deps={i.id for i in rows} | {f'task:{d}' for d in t.get('dependencies', [])},
                                 control=True))

    def _validate_graph(self):
        edges = {k: i.deps | i.sync for k, i in self.instructions.items()}
        followers = defaultdict(list)
        counts = {}
        for key, deps in edges.items():
            missing = deps - edges.keys()
            if missing:
                raise ModelError(f'Missing dependencies at {key}: {missing}')
            counts[key] = len(deps)
            for d in deps:
                followers[d].append(key)
        ready = [k for k, n in counts.items() if not n]
        visited = 0
        while ready:
            visited += 1
            for k in followers[ready.pop()]:
                counts[k] -= 1
                if counts[k] == 0:
                    ready.append(k)
        if visited != len(edges):
            raise ModelError(f'Dependency cycle involving {[k for k, n in counts.items() if n][:20]}')


@dataclass
class Stage:
    engine: str
    resource: str
    work: int
    service: int
    latency: int
    unit: str


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


class EventQueue:
    def __init__(self):
        self.heap, self.serial = [], 0

    def push(self, cycle, op):
        self.serial += 1
        heapq.heappush(self.heap, (cycle, self.serial, op))

    def pop_ready(self, cycle):
        while self.heap and self.heap[0][0] <= cycle:
            yield heapq.heappop(self.heap)[2]


class ResourceState:
    def __init__(self, config):
        self.config = config
        self.available = defaultdict(int)
        self.busy, self.bytes = Counter(), Counter()
        self.active, self.usage, self.peak = set(), Counter(), Counter()
        self.storage_events = []

    def release(self, key, versions, cycle):
        if key in self.active:
            self.active.remove(key)
            v = versions[key]
            self.usage[v.pool] -= v.size
            self.storage_events.append({'cycle': cycle, 'action': 'free', 'version': list(key), 'pool': v.pool, 'bytes': v.size})

    def allocation(self, i, versions, done):
        delta = Counter()
        replace = set()
        for key in i.outputs:
            v = versions[key]
            for old in self.active:
                ov = versions[old]
                if ov.slot == v.slot and old != key:
                    if ov.retained or not (ov.consumers - {i.id}) <= done:
                        return None, f'fixed_position:{old}'
                    replace.add(old)
            delta[v.pool] += v.size
        for old in replace:
            delta[versions[old].pool] -= versions[old].size
        for pool, amount in delta.items():
            limit = self.config['parameters']['shared_capacity' if pool.endswith('/shared') else 'local_capacity']['value']
            if self.usage[pool] + amount > limit:
                return None, f'capacity:{pool}'
        return replace, None

    def allocate(self, i, versions, replace, cycle):
        for key in replace:
            self.release(key, versions, cycle)
        for key in i.outputs:
            v = versions[key]
            self.active.add(key)
            self.usage[v.pool] += v.size
            self.peak[v.pool] = max(self.peak[v.pool], self.usage[v.pool])
            self.storage_events.append({'cycle': cycle, 'action': 'allocate', 'version': list(key), 'pool': v.pool, 'bytes': v.size})


class Scheduler:
    def __init__(self, trace, config):
        self.trace, self.config = trace, config
        self.nodes, self.versions = trace.instructions, trace.versions
        self.cost = CostModel(config)
        self.stages = {key: self.cost.stages(i) for key, i in self.nodes.items()}
        self.resources, self.events = ResourceState(config), EventQueue()
        self.done, self.issued = set(), set()
        self.index, self.timeline = {}, {}
        self.waits = defaultdict(Counter)
        self.stage_waits = Counter()
        self.cycle, self.rotation = 0, defaultdict(int)
        self.pending_stage = []

    def finish(self, key):
        self.done.add(key)
        self.timeline[key]['complete'] = self.cycle
        for vk in self.nodes[key].inputs | self.nodes[key].outputs:
            v = self.versions[vk]
            if not v.retained and v.producer in self.done and v.consumers <= self.done:
                self.resources.release(vk, self.versions, self.cycle)

    def start_stage(self, key):
        stage = self.stages[key][self.index[key]]
        r = self.resources
        if r.available[stage.resource] > self.cycle:
            return False
        end = self.cycle + stage.service
        r.available[stage.resource] = end
        r.busy[stage.resource] += stage.service
        if stage.unit == 'byte/cycle':
            r.bytes[stage.resource] += stage.work
        self.timeline[key]['stages'].append({
            'engine': stage.engine, 'resource': stage.resource, 'work': stage.work,
            'start': self.cycle, 'service_end': end, 'complete': end + stage.latency})
        self.events.push(end + stage.latency, key)
        return True

    def reasons(self, i, pe_counts, core_counts):
        reasons = set()
        if not i.deps <= self.done:
            reasons.add('dependency')
        if not i.sync <= self.done:
            reasons.add('synchronization')
        _, problem = self.resources.allocation(i, self.versions, self.done)
        if problem:
            reasons.add(problem)
        if not (i.alias or i.control):
            if pe_counts[i.core, i.pe] >= self.config['parameters']['pe_issue_width']['value'] or core_counts[i.core] >= self.config['parameters']['core_issue_width']['value']:
                reasons.add('issue_width')
            if self.resources.available[self.stages[i.id][0].resource] > self.cycle:
                reasons.add('resource:' + self.stages[i.id][0].resource)
        return reasons

    def issue(self, i):
        replacement, problem = self.resources.allocation(i, self.versions, self.done)
        assert problem is None
        self.resources.allocate(i, self.versions, replacement, self.cycle)
        self.issued.add(i.id)
        self.timeline[i.id] = {'op_id': i.id, 'ir_op': i.raw['ir_op'], 'core': i.core, 'pe': i.pe,
                               'issue': self.cycle, 'stages': []}
        if i.control and i.raw['ir_op'] == 'barrier':
            latency = self.config['parameters']['barrier_latency']['value']
            if latency:
                self.index[i.id] = -1
                self.events.push(self.cycle + latency, i.id)
            else:
                self.finish(i.id)
        elif not self.stages[i.id]:
            self.finish(i.id)
        else:
            self.index[i.id] = 0
            assert self.start_stage(i.id)

    def run(self):
        real = [i for i in self.nodes.values() if not i.control]
        cores = sorted({i.core for i in self.nodes.values()})
        by_pe = {(c, p): sorted([i for i in real if (i.core, i.pe) == (c, p)],
                               key=lambda i: (i.task, i.age, str(i.id)))
                 for c in cores for p in range(self.config['parameters']['pe_count']['value'])}
        while len(self.done) < len(self.nodes):
            for key in self.events.pop_ready(self.cycle):
                if self.nodes[key].control:
                    self.finish(key)
                else:
                    self.index[key] += 1
                    if self.index[key] == len(self.stages[key]):
                        self.finish(key)
                    else:
                        self.pending_stage.append(key)
            waiting = []
            for key in self.pending_stage:
                if not self.start_stage(key):
                    waiting.append(key)
                    self.stage_waits[str(key)] += 1
            self.pending_stage = waiting
            pe_counts, core_counts = Counter(), Counter()
            # Resolve zero-cost views/control to a fixed point; control has no tile quota.
            progress = True
            while progress:
                progress = False
                for i in self.nodes.values():
                    if i.id not in self.issued and (i.alias or i.control) and not self.reasons(i, pe_counts, core_counts):
                        self.issue(i)
                        progress = True
                for c in cores:
                    n = self.config['parameters']['pe_count']['value']
                    for offset in range(n):
                        p = (self.rotation[c] + offset) % n
                        for i in by_pe[c, p]:
                            if i.id not in self.issued and not i.alias and not self.reasons(i, pe_counts, core_counts):
                                self.issue(i)
                                pe_counts[c, p] += 1
                                core_counts[c] += 1
                                progress = True
                                break
            for c in cores:
                self.rotation[c] = (self.rotation[c] + 1) % self.config['parameters']['pe_count']['value']
            blocked = {}
            for i in self.nodes.values():
                if i.id not in self.issued:
                    reasons = self.reasons(i, pe_counts, core_counts)
                    reason_set = '|'.join(sorted(reasons))
                    self.waits[str(i.id)][reason_set] += 1
                    blocked[str(i.id)] = {'reasons': sorted(reasons),
                                         'dependencies': sorted(map(str, (i.deps | i.sync) - self.done)),
                                         'outputs': [list(k) for k in sorted(i.outputs, key=str)]}
            if len(self.done) == len(self.nodes):
                break
            if not self.events.heap and not self.pending_stage and not any(
                    v > self.cycle for v in self.resources.available.values()):
                # A quota-only stall recovers at the next cycle.
                if not any('issue_width' in b['reasons'] for b in blocked.values()):
                    raise ModelError('Deadlock at cycle ' + str(self.cycle) + ': ' + json.dumps(blocked, sort_keys=True))
            self.cycle += 1
        return self.report()

    def report(self):
        core_end, pe_end, pe_tile_end = {}, {}, {}
        for key, row in self.timeline.items():
            core = str(row['core'])
            core_end[core] = max(core_end.get(core, 0), row['complete'])
            if not self.nodes[key].control:
                pe = f'{core}:{row["pe"]}'
                pe_tile_end[pe] = max(pe_tile_end.get(pe, 0), row['complete'])
                pe_end[pe] = max(pe_end.get(pe, 0), row['complete'])
            elif self.nodes[key].raw['ir_op'] == 'barrier':
                for c, p in self.trace.block_members[self.nodes[key].task, self.nodes[key].block]:
                    pe = f'{c}:{p}'
                    pe_end[pe] = max(pe_end.get(pe, 0), row['complete'])
        return {'total_cycles': self.cycle, 'instruction_count': sum(not i.control for i in self.nodes.values()),
                'core_completion': core_end, 'pe_completion': pe_end, 'pe_tile_completion': pe_tile_end,
                'source_sha256': self.trace.source_hashes,
                'timeline': list(self.timeline.values()),
                'resources': {k: {'service_cycles': v, 'utilization': v / self.cycle if self.cycle else 0,
                                  'bytes': self.resources.bytes[k]} for k, v in sorted(self.resources.busy.items())},
                'storage_peak_bytes': dict(self.resources.peak),
                'storage_live_bytes': dict(self.resources.usage),
                'storage_events': self.resources.storage_events,
                'wait_reason_sets': {k: dict(v) for k, v in self.waits.items()},
                'internal_stage_resource_wait_cycles': dict(self.stage_waits),
                'config': self.config,
                'assumptions': [
                    'Synthetic rates; hardware cycle accuracy has not been calibrated.',
                    'Unlimited window/ROB; order_dependencies establish age, not completion.',
                    'Sequential internal stages; private TileReg access included in compute cost.',
                    'Shared reads charged separately per consumer; no broadcast combining.',
                    'Output allocated at issue; fixed slot overwrites await prior readers.',
                    'Views are zero-cost aliases; reuse-only shared versions remain live-out.',
                    'Memory paths use configured costs, without cache residency simulation.',
                    'Wait reason sets count instruction-cycles; overlapping categories are not additive.']}


def simulate(capture_dir, config=None):
    """Simulate a capture directory and return a JSON-serializable report."""
    if config is None:
        config = default_config()
    elif isinstance(config, (str, Path)):
        config = json.loads(Path(config).read_text())
    config = validate_config(copy.deepcopy(config))
    try:
        return Scheduler(TraceAdapter(capture_dir, config), config).run()
    except (KeyError, TypeError, OSError, json.JSONDecodeError) as exc:
        raise ModelError(f'Invalid capture: {exc}') from exc
