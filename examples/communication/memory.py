"""Byte-checked remote SRAM write/read using the public memory contract."""
from runtime.component import Component
from runtime.memory import MemorySubmit, MemoryAccepted, MemoryRejected, MemoryDone, MemoryRetired


class MemoryClient(Component):
    """Fake core: submit at specified cycles, retain on backpressure, retire Done."""
    def __init__(self, name, requests, *, retire_delay=0, sequential=False):
        super().__init__(name)
        self.requests = list(requests)  # (cycle, MemoryRequest, service endpoint)
        self.pending, self.done, self.retirements = {}, {}, []
        self.accepted, self.rejected = {}, []
        self.retire_delay, self.sequential = retire_delay, sequential
        self.history = []

    def tick(self, cycle):
        for _, msg in self.inputs(cycle):
            self.history.append((cycle, msg))
            key = msg.request_id
            if isinstance(msg, MemoryAccepted):
                self.accepted[key] = cycle
            elif isinstance(msg, MemoryRejected):
                self.rejected.append(msg)
                r, destination = self.pending.pop(key)
                if msg.retryable:
                    self.requests.append((cycle+1, r, destination))
                else:
                    self.done[key] = msg
            elif isinstance(msg, MemoryDone):
                if key in self.done or key not in self.accepted:
                    raise ValueError('Premature or duplicate completion')
                r, destination = self.pending.pop(key)
                self.done[key] = msg
                self.retirements.append((cycle+self.retire_delay, key, destination))
            else:
                raise TypeError(msg)
        for item in self.retirements[:]:
            when, key, destination = item
            if when <= cycle:
                self.send(destination, MemoryRetired(key))
                self.retirements.remove(item)
        for item in self.requests[:]:
            when, r, destination = item
            if when <= cycle and (not self.sequential or not self.pending):
                self.pending[r.request_id] = r, destination
                self.send(destination, MemorySubmit(self.name, r))
                self.requests.remove(item)

    def next_event(self):
        times = [x[0] for x in self.requests + self.retirements if x[0] > self.cycle]
        return min(times, default=None)

    def is_idle(self):
        return super().is_idle() and not (self.requests or self.pending or self.retirements)


if __name__ == '__main__':
    from models.noc.memory import MemoryNetwork
    from models.memory.sram import SRAM
    from runtime.memory import MemoryRequest
    from runtime.runner import Runner
    sram = SRAM(1, capacity=128, throughput=16, read_latency=3, write_latency=3, storage=bytearray(128))
    network = MemoryNetwork((0,1), throughput=16, latency=2, control_bytes=8, header_bytes=4, inflight=2)
    payload = bytes(range(32))
    core = MemoryClient('core', [(0,MemoryRequest((0,0),0,1,'write',16,32,payload),'memory-network'),
                                (0,MemoryRequest((0,1),0,1,'read',16,32),'memory-network')], sequential=True)
    cycles = Runner({c.name:c for c in (core,sram,network)}).run()
    assert core.done[(0,1)].data == payload
    print(dict(cycles=cycles, checked_bytes=len(payload), read_after_write='passed'))
