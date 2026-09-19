"""The `arrow-pandas` codec: the same bytes as `arrow`, decoded to a pandas DataFrame.

A consumer working in pandas should not have to install polars to read its own cache, so
this module imports pandas and pyarrow and not polars. The encoding is shared, and
because offset widths are normalized in `_arrow_ipc`, a table stored here is
byte-identical to the same table stored by the `arrow` codec — verified across pandas 2.2
and 3.0, which disagree about how they map strings.

The index is deliberately not preserved. A pandas index is presentation rather than data,
and `preserve_index=True` would both add a column and write pandas metadata into the
schema, which would make the digest depend on the producing library after all.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import pyarrow as pa

from matview.codecs import BaseCodec, register_codec
from matview.codecs._arrow_ipc import canonical_table, encode_table, read_table
from matview.errors import CodecError

__all__ = ["ArrowPandasCodec"]


class ArrowPandasCodec(BaseCodec):
    name = "arrow-pandas"

    def encode(self, value: object) -> bytes | dict[str, bytes]:
        if isinstance(value, pd.Series):
            # A Series becomes a one-column table under its own name, as in the polars codec.
            value = value.to_frame(name=value.name if value.name is not None else "")
        if not isinstance(value, pd.DataFrame):
            raise CodecError(
                f"the arrow-pandas codec encodes a pandas DataFrame or Series, got "
                f"{type(value).__module__}.{type(value).__qualname__}"
            )
        return encode_table(canonical_table(pa.Table.from_pandas(value, preserve_index=False)))

    def decode(self, data: bytes) -> object:
        table: Any = read_table(data)
        return table.to_pandas()


register_codec(ArrowPandasCodec())
