"""Cycle scheduling, issue arbitration, internal stage progress, and completion."""
from __future__ import annotations

import json
from collections import Counter, defaultdict

from .cost import CostModel
from .errors import ModelError
from .events import EventQueue
from .report import build_report
from .resources import ResourceState


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
        return build_report(
            cycle=self.cycle, nodes=self.nodes, timeline=self.timeline,
            block_members=self.trace.block_members, source_hashes=self.trace.source_hashes,
            resources=self.resources, waits=self.waits, stage_waits=self.stage_waits,
            config=self.config,
        )
