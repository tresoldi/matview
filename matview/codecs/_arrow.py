"""Arrow IPC codecs.

Arrow IPC stream format, compression off, metadata version pinned, schema metadata
stripped, chunking normalized. Parquet is disqualified out of the box: it stamps a
creation timestamp and writer version into the file, so two encodings of the same table
differ and every downstream fingerprint changes for no reason.

**The encoder is pyarrow's, not polars'.** polars' own `write_ipc_stream` is not
byte-stable across polars releases: polars 1.20 and 1.44 produce different bytes for
the same frame, differing in metadata padding, which would silently invalidate every
stored artifact on a routine dependency upgrade. Going through `pa.ipc.new_stream` with
explicit `IpcWriteOptions` produces identical bytes under polars 1.20 + pyarrow 16.1 and
polars 1.44 + pyarrow 25.0. See DECISIONS.md D29.

Chunking is normalized because it is an artifact of how a frame was built rather than
of its contents: the same data assembled by `concat` and by a single constructor call
must encode identically.

Two names share one encoding, because the spec is explicit that "the codec is a
property of the recipe, so decoding is a lookup and never an inference":

- `arrow` decodes to a DataFrame.
- `arrow-series` decodes to a Series, for a recipe whose output genuinely is one.

Both accept either on encode. Inspecting the table to guess which the caller wanted
would be exactly the inference the spec rules out. See DECISIONS.md D14.
"""

from __future__ import annotations

import io
from typing import Any

import polars as pl
import pyarrow as pa

from matview.codecs import BaseCodec, register_codec
from matview.errors import CodecError

# polars and pyarrow are imported at module level on purpose. This module is itself
# imported lazily, only when a recipe asks for one of these codecs, and that import
# failing is exactly how `get_codec` learns to raise CodecUnavailable with an install
# hint. Importing them inside the functions instead would let the module register
# successfully and then fail with a bare ModuleNotFoundError at the first encode.

__all__ = ["ArrowCodec", "ArrowSeriesCodec"]


def _to_table(value: object) -> Any:
    if isinstance(value, pl.DataFrame):
        frame = value
    elif isinstance(value, pl.Series):
        # A Series becomes a one-column table under its own name, or "" when unnamed.
        frame = value.to_frame()
    else:
        raise CodecError(
            f"the arrow codecs encode a polars DataFrame or Series, got {type(value).__name__}"
        )

    table = frame.to_arrow()
    # No custom metadata, at the schema level or the field level: polars records its own
    # there, and it would differ between producers of identical data.
    fields = [pa.field(f.name, f.type, f.nullable) for f in table.schema]
    table = table.cast(pa.schema(fields))
    # One record batch, so that chunking cannot leak into the bytes.
    return table.combine_chunks()


def _write_options() -> Any:
    return pa.ipc.IpcWriteOptions(
        compression=None,
        metadata_version=pa.ipc.MetadataVersion.V5,
        allow_64bit=False,
        use_threads=False,
    )


class ArrowCodec(BaseCodec):
    name = "arrow"

    def encode(self, value: object) -> bytes | dict[str, bytes]:
        table = _to_table(value)
        sink = io.BytesIO()
        with pa.ipc.new_stream(sink, table.schema, options=_write_options()) as writer:
            writer.write_table(table)
        return sink.getvalue()

    def decode(self, data: bytes) -> object:
        try:
            with pa.ipc.open_stream(io.BytesIO(data)) as reader:
                table = reader.read_all()
            return pl.from_arrow(table)
        except CodecError:
            raise
        except Exception as exc:
            raise CodecError(f"stored bytes are not an Arrow IPC stream: {exc}") from exc


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
