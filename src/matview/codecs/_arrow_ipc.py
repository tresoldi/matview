"""The canonical Arrow IPC encoding, shared by every Arrow-backed codec.

Library-agnostic on purpose: it imports pyarrow and nothing else, so a consumer who
works in pandas does not have to install polars to read its own cache, and the bytes are
defined here rather than by whichever dataframe library happened to produce the table.

That last point is the whole reason this module exists separately. Two things had to be
taken out of the producing library's hands:

- **The writer.** polars' own `write_ipc_stream` is not byte-stable across polars
  releases (see DECISIONS.md D29), so the stream is written through pyarrow under
  explicit options.
- **The offset width.** `string`/`large_string`, `binary`/`large_binary` and
  `list`/`large_list` differ only in whether offsets are 32- or 64-bit. It carries no
  semantic difference, and each library picks a side: polars produces `large_string`,
  pandas 2 produces `string`, pandas 3 produces `large_string`. Left alone, the same
  logical table gets different digests depending on its producer and on a minor version
  of it. Normalizing to the large variant pins the choice to matview. See DECISIONS.md D39.
"""

from __future__ import annotations

import io
from typing import Any

import pyarrow as pa

from matview.errors import CodecError

__all__ = ["canonical_table", "encode_table", "read_table", "widen", "write_options"]


def write_options() -> Any:
    """Explicit and pinned, so no pyarrow default can drift into the bytes."""
    return pa.ipc.IpcWriteOptions(
        compression=None,
        metadata_version=pa.ipc.MetadataVersion.V5,
        allow_64bit=False,
        use_threads=False,
    )


def widen(kind: Any) -> Any:
    """Normalize a type's offset width, recursively.

    Only the 32/64-bit offset pairs are touched, because only they are a pure encoding
    choice. Notably a dictionary type's index width is *not* normalized: it varies with
    cardinality, so it is data, not an encoding detail, and forcing one width would make
    a categorical column larger than it needs to be. A recipe whose output includes a
    categorical should cast it explicitly if it wants a digest independent of its
    producer.
    """
    if pa.types.is_string(kind):
        return pa.large_string()
    if pa.types.is_binary(kind):
        return pa.large_binary()
    if pa.types.is_list(kind) or pa.types.is_large_list(kind):
        return pa.large_list(widen(kind.value_type))
    if pa.types.is_struct(kind):
        return pa.struct([pa.field(f.name, widen(f.type), f.nullable) for f in kind])
    if pa.types.is_map(kind):
        return pa.map_(widen(kind.key_type), widen(kind.item_type))
    return kind


def canonical_table(table: Any) -> Any:
    """Put a table into the one form matview stores.

    No schema or field metadata — polars and pandas both record their own there, and it
    would differ between producers of identical data. Offset widths normalized. One
    record batch, so that how a table happened to be assembled cannot leak into the bytes.
    """
    fields = [pa.field(f.name, widen(f.type), f.nullable) for f in table.schema]
    return table.cast(pa.schema(fields)).combine_chunks()


def encode_table(table: Any) -> bytes:
    sink = io.BytesIO()
    with pa.ipc.new_stream(sink, table.schema, options=write_options()) as writer:
        writer.write_table(table)
    return sink.getvalue()


def read_table(data: bytes) -> Any:
    try:
        with pa.ipc.open_stream(io.BytesIO(data)) as reader:
            return reader.read_all()
    except Exception as exc:
        raise CodecError(f"stored bytes are not an Arrow IPC stream: {exc}") from exc
