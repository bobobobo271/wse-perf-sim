"""Public protocol exports; implementations import runtime.communication."""
from runtime.communication import (CommRequest, CommSubmit, Accepted, Rejected, Completion,
    DataAccess, DataReply, TileBinding, SendDesc, ReceiveDesc, SendLayout, ReceiveLayout,
    digest, coverage, request_from_dict)
from config import platform_spec
