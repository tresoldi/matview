"""Arrow IPC codecs for polars.

The encoding itself lives in `_arrow_ipc`, which imports only pyarrow, so what is stored
does not depend on which dataframe library produced it. Parquet is disqualified out of
the box: it stamps a creation timestamp and writer version into the file, so two
encodings of the same table differ and every downstream fingerprint changes for no reason.

Two names share one encoding, because the spec is explicit that "the codec is a property
of the recipe, so decoding is a lookup and never an inference":

- `arrow` decodes to a DataFrame.
- `arrow-series` decodes to a Series, for a recipe whose output genuinely is one.

Both accept either on encode. Inspecting the table to guess which the caller wanted would
be exactly the inference the spec rules out. See DECISIONS.md D14.

polars is imported at module level on purpose. This module is itself imported lazily, only
when a recipe asks for one of these codecs, and that import failing is how `get_codec`
learns to raise CodecUnavailable with an install hint.
"""

from __future__ import annotations

from typing import Any

import polars as pl

from matview.codecs import BaseCodec, register_codec
from matview.codecs._arrow_ipc import canonical_table, encode_table, read_table
from matview.errors import CodecError

__all__ = ["ArrowCodec", "ArrowSeriesCodec"]


def _to_table(value: object) -> Any:
    if isinstance(value, pl.DataFrame):
        frame = value
    elif isinstance(value, pl.Series):
        # A Series becomes a one-column table under its own name, or "" when unnamed.
        frame = value.to_frame()
    else:
        raise CodecError(
            f"the arrow codecs encode a polars DataFrame or Series, got "
            f"{type(value).__module__}.{type(value).__qualname__}"
        )
    return canonical_table(frame.to_arrow())


class ArrowCodec(BaseCodec):
    name = "arrow"

    def encode(self, value: object) -> bytes | dict[str, bytes]:
        return encode_table(_to_table(value))

    def decode(self, data: bytes) -> object:
        return pl.from_arrow(read_table(data))


class ArrowSeriesCodec(ArrowCodec):
    name = "arrow-series"

    def decode(self, data: bytes) -> object:
        frame: Any = super().decode(data)
        if len(frame.columns) != 1:
            raise CodecError(
                f"codec 'arrow-series' expects one column, found {len(frame.columns)}: "
                f"{', '.join(frame.columns)}. A multi-column value wants codec 'arrow'."
            )
        return frame.to_series()


register_codec(ArrowCodec())
register_codec(ArrowSeriesCodec())
