"""WSE communication boundary: no core instruction tables are required."""
from models.noc.protocol import CommRequest, Accepted, Rejected, Completion, DataRange, DataCommand, DataReply

__all__ = ['CommRequest', 'Accepted', 'Rejected', 'Completion', 'DataRange', 'DataCommand', 'DataReply']
