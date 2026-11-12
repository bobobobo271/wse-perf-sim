"""Tile byte counts and canonical storage version identifiers."""
from __future__ import annotations

import math

from .errors import ModelError


DTYPES = {'float16': 2, 'fp16': 2, 'bfloat16': 2, 'bf16': 2,
          'float32': 4, 'fp32': 4, 'int8': 1, 'uint8': 1, 'int16': 2,
          'int32': 4, 'int64': 8, 'float64': 8, 'bool': 1}


def nbytes(obj, shape=None):
    try:
        dims = obj.get('valid_shape', obj['shape']) if shape is None else shape
        if any(type(n) is not int or n < 0 for n in dims):
            raise ModelError(f'Invalid shape {dims}')
        return math.prod(dims) * DTYPES[obj['dtype']]
    except KeyError as exc:
        raise ModelError(f'Missing shape or unsupported dtype: {obj}') from exc


def version_key(o):
    if o.get('shared_version') is not None:
        return ('shared', o['shared_version'])
    return ('storage', o.get('storage_id', o['object_id']), o['version'])
