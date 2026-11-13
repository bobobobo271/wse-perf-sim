"""WSE performance simulation API; workloads and plotting are optional."""
from wse_perf_sim.api import simulate
from wse_perf_sim.config import default_config, wse_config
from wse_perf_sim.errors import ModelError

__all__ = ['ModelError', 'default_config', 'wse_config', 'simulate']
