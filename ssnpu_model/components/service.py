"""Resource service clocks and accounting; instruction costs belong to each unit."""
from collections import Counter, defaultdict


class Service:
    """组件内部的资源计时器；不传递消息，也不负责推进命令状态。"""
    def __init__(self):
        self.available = defaultdict(int)
        self.busy, self.bytes = Counter(), Counter()

    def start(self, resource, engine, work, service, latency, cycle):
        # 准入失败不修改资源；调用方保留命令，下一拍再尝试。
        if self.available[resource] > cycle:
            return None
        end = cycle + service
        # service_end 决定下一操作何时可开始；complete = service_end + latency。
        # 因此上一条结果仍在返回时，资源可以服务下一条；busy 不包含返回等待。
        self.available[resource] = end
        self.busy[resource] += service
        if engine in ('memory', 'shared_read', 'shared_write'):
            self.bytes[resource] += work
        return dict(engine=engine, resource=resource, work=work,
                    start=cycle, service_end=end, complete=end + latency)
