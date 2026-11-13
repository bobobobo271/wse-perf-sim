"""WSE communication boundary: no core instruction tables are required."""
from wse_perf_sim.models.noc.protocol import CommRequest, Accepted, Rejected, Completion, DataRange, DataCommand, DataReply

__all__ = ['CommRequest', 'Accepted', 'Rejected', 'Completion', 'DataRange', 'DataCommand', 'DataReply']
