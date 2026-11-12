"""Public simulation entry point composing configuration, trace, and scheduler."""
from __future__ import annotations

import copy
import json
from pathlib import Path

from .config import default_config, validate_config
from .errors import ModelError
from .scheduler import Scheduler
from .trace import TraceAdapter


def simulate(capture_dir, config=None):
    """Simulate a capture directory and return a JSON-serializable report."""
    if config is None:
        config = default_config()
    elif isinstance(config, (str, Path)):
        config = json.loads(Path(config).read_text())
    config = validate_config(copy.deepcopy(config))
    try:
        return Scheduler(TraceAdapter(capture_dir, config), config).run()
    except (KeyError, TypeError, OSError, json.JSONDecodeError) as exc:
        raise ModelError(f'Invalid capture: {exc}') from exc
