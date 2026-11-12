"""Compatibility imports for callers of the original single-file model.

New code should import the public API from ssnpu_model, or components from
config, trace, cost, records, events, resources, and scheduler directly.
"""
from .api import simulate
from .config import ENGINES, PARAMETERS, default_config, validate_config
from .cost import CostModel
from .errors import ModelError
from .events import EventQueue
from .records import Instruction, Stage, Version
from .resources import ResourceState
from .scheduler import Scheduler
from .tiles import DTYPES, nbytes, version_key
from .trace import TraceAdapter

__all__ = [
    'ModelError', 'PARAMETERS', 'ENGINES', 'default_config', 'validate_config',
    'DTYPES', 'nbytes', 'version_key', 'Instruction', 'Version', 'Stage',
    'TraceAdapter', 'CostModel', 'EventQueue', 'ResourceState', 'Scheduler', 'simulate',
]
