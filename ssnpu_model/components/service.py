"""Resource service clocks and accounting; instruction costs belong to each unit."""
from collections import Counter, defaultdict


class Service:
    def __init__(self):
        self.available = defaultdict(int)
        self.busy, self.bytes = Counter(), Counter()

    def start(self, resource, engine, work, service, latency, cycle):
        if self.available[resource] > cycle:
            return None
        end = cycle + service
        self.available[resource] = end
        self.busy[resource] += service
        if engine in ('memory', 'shared_read', 'shared_write'):
            self.bytes[resource] += work
        return dict(engine=engine, resource=resource, work=work,
                    start=cycle, service_end=end, complete=end + latency)
