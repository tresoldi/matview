"""Calendar-year slicing for the Arrow codecs. Library-agnostic: pyarrow only.

The financial case wants calendar-year partitions, and the reason is write amplification
rather than anything about SQLite. With roughly 5,000 symbols and 50 indicators, a
whole-series encoding is around 40 KB, so a daily append rewrites all of it — on the order
of 10 GB per refresh against a remote store. Yearly parts rewrite only the current year,
cutting that to roughly 500 MB. Monthly would cut it further but multiplies row count by
240 and makes range reads stitch many pieces, which is why this is years and not months.

A closed year is then immutable, and matview needs nothing extra for that to pay off: a
recipe reading one part records a dep edge on *that part alone*, so a write to the current
year does not invalidate anything built from a closed one.
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa
import pyarrow.compute as pc

from matview.codecs._arrow_ipc import canonical_table
from matview.errors import CodecError

__all__ = ["combine_years", "slice_by_year", "year_labels"]

#: Zero-padded, so that sorting labels as text agrees with sorting years as numbers.
#: The core treats `part` as an opaque string and the fingerprint sorts dependencies by
#: UTF-16 code unit, under which "999" sorts after "1000". Padding removes the question.
_LABEL = "{:04d}"


def _partition_values(table: Any, column: str) -> Any:
    """Validate the partitioning column and return its values, read in UTC.

    Timezone-aware timestamps are read **in UTC**. This is not a detail: the same instant
    is 2023 in UTC and 2024 in Asia/Tokyo, so without a fixed rule a row near a year
    boundary would land in a different part depending on a column's timezone metadata, and
    its digest would move with it. A `Date` column has no such ambiguity.
    """
    if column not in table.schema.names:
        raise CodecError(
            f"the year codecs partition on a column named {column!r}, which this value does "
            f"not have. Columns present: {', '.join(table.schema.names) or '(none)'}. Register "
            f"your own instance with column=... if it is called something else."
        )

    values = table[column]
    kind = values.type
    if pa.types.is_timestamp(kind):
        if kind.tz is not None:
            # Reinterpreting as naive reads the stored instant, which Arrow keeps in UTC.
            values = values.cast(pa.timestamp(kind.unit))
    elif not pa.types.is_date(kind):
        raise CodecError(
            f"column {column!r} is {kind}, which has no calendar year. The year codecs "
            f"partition on a date or timestamp column."
        )

    if values.null_count:
        raise CodecError(
            f"column {column!r} has {values.null_count} null value(s), which belong to no "
            f"year. Drop or fill them before storing: silently bucketing them would lose "
            f"rows on the next read."
        )
    return values


def year_labels(table: Any, column: str) -> list[str]:
    """The part labels this table would produce, without slicing it."""
    years = pc.year(_partition_values(table, column))
    return sorted({_LABEL.format(year) for year in years.to_pylist()})


def slice_by_year(table: Any, column: str) -> dict[str, Any]:
    """Split a table into one canonical table per calendar year.

    Rows are sorted by the partitioning column first, so that a frame which is merely
    reordered encodes to the same bytes rather than churning every fingerprint above it.
    Arrow's sort is stable, so rows sharing a date keep their input order — which makes
    this fully canonical for the one-row-per-date case the design has, and no more than
    that. A recipe with several rows per date and no meaningful order of its own should
    sort them itself.
    """
    # Validated before sorting, not after: sort_by on a column that is absent or of the
    # wrong type raises a raw Arrow error, and a codec should say which column it wanted
    # and how to change it.
    _partition_values(table, column)
    ordered = table.sort_by([(column, "ascending")])
    years = pc.year(_partition_values(ordered, column))
    return {
        _LABEL.format(year): canonical_table(ordered.filter(pc.equal(years, year)))
        for year in sorted(set(years.to_pylist()))
    }


def combine_years(parts: dict[str, Any]) -> Any:
    """Reassemble the parts in label order, which is year order because labels are padded."""
    return pa.concat_tables([parts[label] for label in sorted(parts)])
