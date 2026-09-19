"""Calendar-year partitioning.

The spec justifies this codec by write amplification: a whole-series encoding means a
daily append rewrites everything, while yearly parts rewrite only the current year. That
is a checkable claim rather than a design note, and most of this file checks it — at the
codec level and then end to end through the store.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from matview import Cache, Registry, recipe
from matview.codecs import get_codec, register_codec
from matview.errors import CodecError

pl = pytest.importorskip("polars")

DAYS = [
    dt.date(2023, 12, 30),
    dt.date(2023, 12, 31),
    dt.date(2024, 1, 1),
    dt.date(2024, 6, 1),
    dt.date(2025, 3, 3),
]


def series(days: list[dt.date] | None = None) -> Any:
    days = DAYS if days is None else days
    return pl.DataFrame(
        {
            "date": days,
            "close": [float(i) for i in range(len(days))],
            "symbol": ["AAPL"] * len(days),
        }
    )


# --- the slicing itself ----------------------------------------------------


def test_parts_are_calendar_years() -> None:
    parts = get_codec("arrow-year").encode(series())
    assert isinstance(parts, dict)
    assert sorted(parts) == ["2023", "2024", "2025"]
    assert [len(get_codec("arrow-year").decode(parts[y])) for y in sorted(parts)] == [2, 2, 1]


def test_labels_are_zero_padded_so_text_order_is_year_order() -> None:
    """The core sorts parts as text, and "999" sorts after "1000" without padding."""
    parts = get_codec("arrow-year").encode(series([dt.date(999, 1, 1), dt.date(1000, 1, 1)]))
    assert sorted(parts) == ["0999", "1000"]


def test_reordered_input_encodes_identically() -> None:
    """Otherwise a merely reordered frame churns every fingerprint above it."""
    codec = get_codec("arrow-year")
    forwards = codec.encode(series())
    backwards = codec.encode(series(list(reversed(DAYS))))
    assert isinstance(forwards, dict)
    # The close values travel with their dates, so reversing the rows is a permutation.
    assert sorted(forwards) == sorted(backwards)  # type: ignore[arg-type]
    assert set(codec.decode_parts(forwards)["date"].to_list()) == set(DAYS)


def test_appending_one_day_rewrites_one_part() -> None:
    """The spec's actual argument for years over a whole-series encoding."""
    codec = get_codec("arrow-year")
    before = codec.encode(series())
    after = codec.encode(series([*DAYS, dt.date(2025, 3, 4)]))
    assert isinstance(before, dict) and isinstance(after, dict)

    changed = [k for k in sorted(set(before) | set(after)) if before.get(k) != after.get(k)]
    assert changed == ["2025"], "closed years must be byte-identical"


def test_a_whole_read_comes_back_in_year_order() -> None:
    codec = get_codec("arrow-year")
    merged = codec.decode_parts(codec.encode(series()))  # type: ignore[arg-type]
    assert merged["date"].to_list() == sorted(DAYS)


def test_an_empty_frame_is_a_value_with_no_parts() -> None:
    """Rule 9: empty is a value. It has no years, so it stores unpartitioned."""
    codec = get_codec("arrow-year")
    empty = pl.DataFrame(
        {
            "date": pl.Series("date", [], dtype=pl.Date),
            "close": pl.Series("close", [], dtype=pl.Float64),
        }
    )
    encoded = codec.encode(empty)
    assert isinstance(encoded, bytes), "not zero parts, which would store nothing at all"
    assert codec.decode(encoded).height == 0


# --- what it refuses -------------------------------------------------------


def test_a_missing_partition_column_says_how_to_configure_it() -> None:
    with pytest.raises(CodecError, match="column named 'date'") as excinfo:
        get_codec("arrow-year").encode(pl.DataFrame({"day": [dt.date(2024, 1, 1)]}))
    assert "column=" in str(excinfo.value)


def test_a_non_temporal_partition_column_is_refused() -> None:
    with pytest.raises(CodecError, match="no calendar year"):
        get_codec("arrow-year").encode(pl.DataFrame({"date": [1, 2]}))


def test_null_dates_are_refused_rather_than_silently_dropped() -> None:
    frame = pl.DataFrame({"date": [dt.date(2024, 1, 1), None], "close": [1.0, 2.0]})
    with pytest.raises(CodecError, match="belong to no year"):
        get_codec("arrow-year").encode(frame)


def test_a_timezone_aware_column_is_read_in_utc() -> None:
    """The same instant is 2023 in UTC and 2024 in Tokyo; the part must not depend on that."""
    instant = dt.datetime(2023, 12, 31, 23, 59, tzinfo=dt.UTC)
    frame = pl.DataFrame({"date": [instant], "close": [1.0]})
    tokyo = frame.with_columns(pl.col("date").dt.convert_time_zone("Asia/Tokyo"))

    codec = get_codec("arrow-year")
    assert sorted(codec.encode(frame)) == ["2023"]  # type: ignore[arg-type]
    assert sorted(codec.encode(tokyo)) == ["2023"], "same instant, same part"  # type: ignore[arg-type]


# --- configuration ---------------------------------------------------------


def test_a_different_column_and_name_can_be_registered() -> None:
    from matview.codecs._arrow import ArrowYearCodec

    register_codec(ArrowYearCodec(name="test-bars-year", column="day"))
    parts = get_codec("test-bars-year").encode(
        pl.DataFrame({"day": [dt.date(2024, 5, 5), dt.date(2025, 5, 5)], "v": [1, 2]})
    )
    assert sorted(parts) == ["2024", "2025"]  # type: ignore[arg-type]


def test_the_pandas_year_codec_writes_the_same_bytes() -> None:
    pd = pytest.importorskip("pandas")
    columns = {"date": DAYS, "close": [float(i) for i in range(len(DAYS))], "symbol": ["AAPL"] * 5}
    assert get_codec("arrow-year").encode(pl.DataFrame(columns)) == get_codec(
        "arrow-year-pandas"
    ).encode(pd.DataFrame(columns))


# --- end to end through the store -----------------------------------------


@pytest.fixture
def bars(cache: Cache, registry: Registry) -> dict[str, Any]:
    state: dict[str, Any] = {"days": list(DAYS), "calls": 0}

    @recipe(version=1, codec="arrow-year", index=("symbol",), registry=registry)
    def ohlcv(ctx, *, symbol: str):
        state["calls"] += 1
        return series(state["days"])

    @recipe(version=1, codec="json", index=("symbol",), registry=registry)
    def closed_year_total(ctx, *, symbol: str):
        state["closed"] = state.get("closed", 0) + 1
        frame = ctx.get("ohlcv", symbol=symbol, part="2023")
        return {"total": float(frame["close"].sum())}

    return state


def test_a_daily_append_rewrites_only_the_current_year(cache: Cache, bars: dict[str, Any]) -> None:
    """The write-amplification claim, at the level where it actually costs something."""
    cache.get("ohlcv", symbol="AAPL")
    before = {ref.part: (ref.digest, ref.computed_at) for ref in cache.query(recipe="ohlcv")}
    assert sorted(before) == ["2023", "2024", "2025"]

    bars["days"].append(dt.date(2025, 3, 4))
    cache.get("ohlcv", symbol="AAPL", force=True)
    after = {ref.part: (ref.digest, ref.computed_at) for ref in cache.query(recipe="ohlcv")}

    assert after["2023"] == before["2023"], "a closed year is not even rewritten"
    assert after["2024"] == before["2024"]
    assert after["2025"][0] != before["2025"][0], "and the current year is"


def test_a_closed_year_reader_is_untouched_by_a_current_year_write(
    cache: Cache, bars: dict[str, Any]
) -> None:
    """Closed years are immutable, and part-level dep edges are what make that pay off."""
    assert cache.get("closed_year_total", symbol="AAPL") == {"total": 1.0}
    assert bars["closed"] == 1

    bars["days"].append(dt.date(2025, 3, 4))
    cache.get("ohlcv", symbol="AAPL", force=True)

    assert cache.query(recipe="closed_year_total")[0].valid is True
    assert cache.get("closed_year_total", symbol="AAPL") == {"total": 1.0}
    assert bars["closed"] == 1, "nothing above a closed year recomputed"


def test_a_closed_year_reader_does_rebuild_when_that_year_changes(
    cache: Cache, bars: dict[str, Any]
) -> None:
    """The control: the cascade must still reach a reader when its own part moves."""
    cache.get("closed_year_total", symbol="AAPL")

    bars["days"].insert(0, dt.date(2023, 1, 1))
    cache.get("ohlcv", symbol="AAPL", force=True)

    assert cache.query(recipe="closed_year_total")[0].valid is False
    assert cache.get("closed_year_total", symbol="AAPL") != {"total": 1.0}
    assert bars["closed"] == 2


def test_a_year_that_stops_being_produced_is_removed(cache: Cache, bars: dict[str, Any]) -> None:
    cache.get("ohlcv", symbol="AAPL")
    bars["days"] = [day for day in DAYS if day.year != 2023]
    cache.get("ohlcv", symbol="AAPL", force=True)
    assert sorted(ref.part for ref in cache.query(recipe="ohlcv")) == ["2024", "2025"]


def test_a_series_that_becomes_empty_replaces_its_parts(cache: Cache, bars: dict[str, Any]) -> None:
    """The unpartitioned empty form and the partitioned one must not coexist."""
    cache.get("ohlcv", symbol="AAPL")
    bars["days"] = []
    assert cache.get("ohlcv", symbol="AAPL", force=True).height == 0
    assert [ref.part for ref in cache.query(recipe="ohlcv")] == [None]

    bars["days"] = list(DAYS)
    cache.get("ohlcv", symbol="AAPL", force=True)
    assert sorted(ref.part for ref in cache.query(recipe="ohlcv")) == ["2023", "2024", "2025"]
