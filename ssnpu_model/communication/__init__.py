"""WSE communication boundary: no core instruction tables are required."""
from .protocol import CommRequest, Accepted, Rejected, Completion, DataRange, DataCommand, DataReply

__all__ = ['CommRequest', 'Accepted', 'Rejected', 'Completion', 'DataRange', 'DataCommand', 'DataReply']
