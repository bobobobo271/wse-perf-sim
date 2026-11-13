"""Legacy public API. New code should import wse_perf_sim."""
from wse_perf_sim import ModelError, default_config, wse_config, simulate
from wse_perf_sim.simulator import Simulator

__all__ = ['ModelError', 'default_config', 'wse_config', 'simulate', 'Simulator']
