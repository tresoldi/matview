"""Stage 6: codecs.

Because fingerprints key on content digests, a codec must produce identical bytes for
identical input, on every machine, every time. A codec that fails this defeats early
cutoff silently: nothing errors, the cascade simply never stops.
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import Any

import pytest

from matview.codecs import CODEC_NAMES, BaseCodec, CodecUnavailable, get_codec, register_codec
from matview.errors import CodecError

GOLDEN = Path(__file__).with_name("golden_codecs.json")


def _sample(codec_name: str) -> Any:
    if codec_name == "json":
        return {"b": 1, "a": [1, 2.5, None, True], "z": "ü", "nested": {"y": 1, "x": 2}}
    if codec_name == "npy":
        np = pytest.importorskip("numpy")
        return np.arange(12, dtype="int64").reshape(3, 4)
    pl = pytest.importorskip("polars")
    frame = pl.DataFrame(
        {
            "close": [1.0, 2.5, 3.0, -0.5],
            "volume": [10, 20, 30, 40],
            "symbol": ["AAPL", "AAPL", "AAPL", "AAPL"],
            "flag": [True, False, True, False],
        }
    )
    return frame["close"] if codec_name == "arrow-series" else frame


def _codec(name: str) -> Any:
    try:
        return get_codec(name)
    except CodecUnavailable:
        pytest.skip(f"{name} extra not installed")


@pytest.mark.parametrize("name", sorted(CODEC_NAMES))
def test_golden_bytes_still_hold(name: str) -> None:
    """Fixed value, fixed bytes, checked in.

    This is the guard that a library upgrade has not silently invalidated every stored
    artifact under this codec.
    """
    codec = _codec(name)
    expected = json.loads(GOLDEN.read_text(encoding="utf-8"))["codecs"][name]
    encoded = codec.encode(_sample(name))
    assert isinstance(encoded, bytes)
    assert encoded.hex() == expected["bytes_hex"], (
        f"{name} no longer produces the bytes it used to; every artifact stored under "
        f"it is now unreachable. This needs a migration, not a regenerated fixture."
    )


@pytest.mark.parametrize("name", sorted(CODEC_NAMES))
def test_no_timestamp_hostname_or_version_leaks_into_the_payload(name: str) -> None:
    """No codec may embed a timestamp, hostname, PID, or library version."""
    import os
    import socket

    codec = _codec(name)
    encoded = codec.encode(_sample(name))
    assert isinstance(encoded, bytes)

    forbidden = [socket.gethostname().encode(), str(os.getpid()).encode()]
    for module in ("polars", "numpy", "pyarrow"):
        with contextlib.suppress(ImportError):
            forbidden.append(__import__(module).__version__.encode())
    for needle in forbidden:
        if len(needle) >= 3:
            assert needle not in encoded, f"{name} leaked {needle!r} into the payload"


@pytest.mark.parametrize("name", sorted(CODEC_NAMES))
def test_a_round_trip_preserves_the_value(name: str) -> None:
    codec = _codec(name)
    value = _sample(name)
    decoded = codec.decode(codec.encode(value))  # type: ignore[arg-type]

    if name == "json":
        assert decoded == value
    elif name == "npy":
        import numpy as np

        assert np.array_equal(decoded, value)
        assert decoded.dtype == value.dtype
    else:
        assert decoded.equals(value)


def test_arrow_accepts_a_series_but_decodes_to_a_frame() -> None:
    pl = pytest.importorskip("polars")
    codec = _codec("arrow")
    series = pl.Series("close", [1.0, 2.0])
    decoded = codec.decode(codec.encode(series))  # type: ignore[arg-type]
    assert isinstance(decoded, pl.DataFrame)
    assert decoded.columns == ["close"]


def test_arrow_series_refuses_a_multi_column_value_rather_than_guessing() -> None:
    pl = pytest.importorskip("polars")
    codec = _codec("arrow-series")
    encoded = codec.encode(pl.DataFrame({"a": [1], "b": [2]}))
    with pytest.raises(CodecError, match="one column"):
        codec.decode(encoded)  # type: ignore[arg-type]


def test_column_order_is_preserved_and_is_part_of_the_bytes() -> None:
    pl = pytest.importorskip("polars")
    codec = _codec("arrow")
    forwards = codec.encode(pl.DataFrame({"a": [1], "b": [2]}))
    backwards = codec.encode(pl.DataFrame({"b": [2], "a": [1]}))
    assert forwards != backwards, "field ordering is fixed, not normalized"
    assert codec.decode(forwards).columns == ["a", "b"]  # type: ignore[arg-type,union-attr]


@pytest.mark.parametrize("name", sorted(CODEC_NAMES))
def test_wrong_types_are_refused_clearly(name: str) -> None:
    codec = _codec(name)
    if name == "json":
        with pytest.raises(CodecError):
            codec.encode(object())
    else:
        with pytest.raises(CodecError, match="got "):
            codec.encode("not an array or a frame")


@pytest.mark.parametrize("name", sorted(CODEC_NAMES))
def test_garbage_bytes_are_refused_rather_than_half_decoded(name: str) -> None:
    codec = _codec(name)
    with pytest.raises(CodecError):
        codec.decode(b"\x00 definitely not a valid payload \xff")


def test_a_non_partitioning_codec_refuses_to_reassemble_parts() -> None:
    with pytest.raises(CodecError, match="does not partition"):
        get_codec("json").decode_parts({"2025": b"{}", "2026": b"{}"})


def test_an_unknown_codec_names_the_known_ones() -> None:
    with pytest.raises(CodecError, match="known codecs"):
        get_codec("parquet")


def test_a_custom_codec_can_be_registered() -> None:
    class Upper(BaseCodec):
        name = "test-upper"

        def encode(self, value: object) -> bytes | dict[str, bytes]:
            return str(value).upper().encode()

        def decode(self, data: bytes) -> object:
            return data.decode().lower()

    register_codec(Upper())
    assert get_codec("test-upper").decode(get_codec("test-upper").encode("Hi")) == "hi"  # type: ignore[arg-type]


def test_a_codec_must_have_a_name() -> None:
    class Nameless(BaseCodec):
        pass

    with pytest.raises(CodecError, match="must have a name"):
        register_codec(Nameless())


def test_arrow_bytes_do_not_depend_on_how_the_frame_was_chunked() -> None:
    """Chunking is an artifact of assembly, not of contents.

    A frame built by concatenating two pieces and the same frame built in one call are
    the same value, and a digest that disagreed would split the cache in two.
    """
    pl = pytest.importorskip("polars")
    codec = _codec("arrow")
    whole = pl.DataFrame({"a": [1, 2, 3, 4], "b": ["w", "x", "y", "z"]})
    pieced = pl.concat(
        [
            pl.DataFrame({"a": [1, 2], "b": ["w", "x"]}),
            pl.DataFrame({"a": [3, 4], "b": ["y", "z"]}),
        ],
        rechunk=False,
    )
    assert codec.encode(whole) == codec.encode(pieced)


def test_arrow_bytes_do_not_carry_polars_schema_metadata() -> None:
    pytest.importorskip("polars")
    pa = pytest.importorskip("pyarrow")
    import io as _io

    encoded = _codec("arrow").encode(_sample("arrow"))
    assert isinstance(encoded, bytes)
    with pa.ipc.open_stream(_io.BytesIO(encoded)) as reader:
        schema = reader.schema
    assert schema.metadata in (None, {})
    assert all(field.metadata in (None, {}) for field in schema)


def test_an_empty_frame_is_a_value(store, registry) -> None:
    """Rule 9 at the codec layer: an empty frame encodes and comes back empty."""
    pl = pytest.importorskip("polars")
    codec = _codec("arrow")
    empty = pl.DataFrame({"a": pl.Series("a", [], dtype=pl.Int64)})
    decoded = codec.decode(codec.encode(empty))  # type: ignore[arg-type]
    assert decoded.height == 0
    assert decoded.columns == ["a"]
