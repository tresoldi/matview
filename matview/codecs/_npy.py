"""The `npy` codec: NumPy's own array format.

The .npy header records dtype, shape and element order and nothing else — no timestamp,
hostname, PID or library version — so the bytes are stable across machines and releases.

Object arrays are refused rather than serialized. NumPy would fall back to serializing
the Python object graph for them, which would make the cache Python-only and unsafe to
share, and would foreclose the whole foreign-recipe design. Every call here passes
`allow_pickle=False`, on write and on read.
"""

from __future__ import annotations

import io
from typing import Any

import numpy as np

from matview.codecs import BaseCodec, register_codec
from matview.errors import CodecError

# numpy is imported at module level on purpose; see the note in _arrow.py.

__all__ = ["NpyCodec"]


class NpyCodec(BaseCodec):
    name = "npy"

    def encode(self, value: object) -> bytes | dict[str, bytes]:
        if not isinstance(value, np.ndarray):
            raise CodecError(f"the npy codec encodes a numpy ndarray, got {type(value).__name__}")
        if value.dtype.hasobject:
            raise CodecError(
                "the npy codec refuses object arrays: storing one would mean serializing "
                "a Python object graph, which is neither portable nor safe to share"
            )
        buffer = io.BytesIO()
        np.save(buffer, value, allow_pickle=False)
        return buffer.getvalue()

    def decode(self, data: bytes) -> object:
        try:
            array: Any = np.load(io.BytesIO(data), allow_pickle=False)
        except Exception as exc:
            raise CodecError(f"stored bytes are not a valid .npy payload: {exc}") from exc
        return array


register_codec(NpyCodec())
