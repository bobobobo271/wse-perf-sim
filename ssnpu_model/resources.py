"""Resource availability, service accounting, and tile storage allocation."""
from __future__ import annotations

from collections import Counter, defaultdict


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
