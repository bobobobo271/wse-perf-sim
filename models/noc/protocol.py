"""Public exports for the common communication contract."""
from runtime.communication import (CommRequest, CommSubmit, Completion, DataAccess, DataReply,
                                   Rejected, SendLayout, ReceiveLayout, digest, coverage)
from config import platform_spec
