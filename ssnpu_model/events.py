"""Stable completion event queue ordered by cycle and insertion sequence."""
from __future__ import annotations

import heapq


class EventQueue:
    def __init__(self):
        self.heap, self.serial = [], 0

    def push(self, cycle, op):
        self.serial += 1
        heapq.heappush(self.heap, (cycle, self.serial, op))

    def pop_ready(self, cycle):
        while self.heap and self.heap[0][0] <= cycle:
            yield heapq.heappop(self.heap)[2]
