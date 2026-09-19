"""The `json` codec: JSON Canonicalization Scheme in, plain `json.loads` out.

The spec asks this codec to sort keys and pin float formatting. JCS does both, is
already a dependency for key hashing, and is specified well enough that another
language produces the same bytes, so it is one canonicalization to get right instead
of two. See DECISIONS.md D22.
"""

from __future__ import annotations

import json

from matview.canon import jcs
from matview.codecs import BaseCodec, register_codec
from matview.errors import CodecError

__all__ = ["JsonCodec"]


class JsonCodec(BaseCodec):
    name = "json"

    def encode(self, value: object) -> bytes | dict[str, bytes]:
        try:
            return jcs(value)
        except Exception as exc:
            raise CodecError(f"value is not JSON-canonicalizable: {exc}") from exc

    def decode(self, data: bytes) -> object:
        try:
            return json.loads(data.decode("utf-8"))
        except Exception as exc:
            raise CodecError(f"stored bytes are not valid JSON: {exc}") from exc


register_codec(JsonCodec())
