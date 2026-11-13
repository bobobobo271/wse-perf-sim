"""Public simulation entry point composing configuration, trace and Simulator."""
from __future__ import annotations

import copy
import json
from pathlib import Path

from .config import default_config, validate_config, wse_config
from .errors import ModelError
from .simulator import Simulator
from .trace import TraceAdapter


def simulate(capture_dir, config=None, *, communication_plan=None):
    """Simulate a capture directory and return a JSON-serializable report."""
    if config is None:
        config = wse_config() if communication_plan is not None else default_config()
    elif isinstance(config, (str, Path)):
        config = json.loads(Path(config).read_text())
    config = validate_config(copy.deepcopy(config))
    try:
        return Simulator(TraceAdapter(capture_dir, config, communication_plan), config).run()
    except (KeyError, TypeError, OSError, json.JSONDecodeError) as exc:
        raise ModelError(f'Invalid capture: {exc}') from exc
