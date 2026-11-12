"""Synthetic model parameters and configuration validation."""
from __future__ import annotations

import math

from .errors import ModelError


PARAMETERS = {
    'pe_count': (4, 'PE', 'Core', 'Number of independent compute engines'),
    'pe_issue_width': (2, 'tile/cycle', 'PE', 'Maximum tile issues per cycle'),
    'core_issue_width': (4, 'tile/cycle', 'Core', 'Maximum tile issues per cycle'),
    'local_capacity': (1048576, 'byte', 'PE', 'LocalTileReg capacity'),
    'shared_capacity': (1048576, 'byte', 'Core', 'SharedTileReg capacity'),
    'barrier_latency': (2, 'cycle', 'block', 'Full phase synchronization delay'),
}


ENGINES = {
    'cube': (4096, 4, 'MAC/cycle', 'PE'),
    'vector': (256, 2, 'element/cycle', 'PE'),
    'exp': (64, 4, 'element/cycle', 'PE'),
    'cast': (256, 2, 'element/cycle', 'PE'),
    'reduce': (128, 3, 'reduction/cycle', 'PE'),
    'memory': (256, 8, 'byte/cycle', 'Core'),
    'shared_read': (256, 2, 'byte/cycle', 'Core'),
    'shared_write': (256, 2, 'byte/cycle', 'Core'),
}


def default_config():
    return {
        'name': 'logic-validation-only',
        'parameters': {k: {'value': v, 'unit': u, 'scope': s, 'meaning': m,
                           'source': 'synthetic logical validation; uncalibrated'}
                       for k, (v, u, s, m) in PARAMETERS.items()},
        'engines': {k: {'throughput': t, 'latency': l, 'unit': u, 'scope': s,
                        'meaning': 'Service rate and result latency after service',
                        'source': 'synthetic logical validation; uncalibrated',
                        'dtype_overrides': {}} for k, (t, l, u, s) in ENGINES.items()},
        'paths': {'DDR': 'memory', 'L1': 'memory', 'L2': 'memory'},
    }


def validate_config(c):
    try:
        for k in PARAMETERS:
            v = c['parameters'][k]['value']
            if type(v) is not int or v < (0 if k == 'barrier_latency' else 1):
                raise ModelError(f'Invalid parameter {k}: {v}')
        for k, (_, _, _, scope) in ENGINES.items():
            e = c['engines'][k]
            if e['scope'] != scope:
                raise ModelError(f'Invalid scope for {k}')
            for v in [e] + list(e.get('dtype_overrides', {}).values()):
                rate, latency = v.get('throughput', e['throughput']), v.get('latency', e['latency'])
                if not isinstance(rate, (int, float)) or not math.isfinite(rate) or rate <= 0:
                    raise ModelError(f'Invalid throughput for {k}')
                if type(latency) is not int or latency < 0:
                    raise ModelError(f'Invalid latency for {k}')
        for path, engine in c['paths'].items():
            if isinstance(engine, dict):
                if engine.get('engine') != 'memory' or not isinstance(engine.get('throughput', 1), (int, float)) or not math.isfinite(engine.get('throughput', 1)) or engine.get('throughput', 1) <= 0 or type(engine.get('latency', 0)) is not int or engine.get('latency', 0) < 0:
                    raise ModelError(f'Invalid memory path {path}: {engine}')
            elif engine != 'memory':
                raise ModelError(f'Unsupported path engine {path}: {engine}')
    except (KeyError, TypeError) as exc:
        raise ModelError(f'Incomplete configuration: {exc}') from exc
    return c
