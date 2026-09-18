"""The ten rules from the spec's "Rules the implementation must not violate".

Each rule is enough on its own to make the library silently wrong rather than loudly
broken, so each gets one test named after it: a violation names itself in the CI output.
The spec requires these to exist before the behaviour they constrain.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from matview import Cache, Registry, SqliteStore, recipe
from matview.codecs import CODEC_NAMES, CodecUnavailable, get_codec
from matview.errors import CodecError, MissingArtifacts
from matview.fingerprint import fingerprint
from matview.types import DepEdge

from .conftest import make_artifact

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "matview"


# ---------------------------------------------------------------------------
# 1. Never delete an artifact on invalidation. Clear `valid`, keep the bytes.
# ---------------------------------------------------------------------------


def test_rule01_invalidation_keeps_bytes(cache: Cache, registry: Registry) -> None:
    scale = [1]

    @recipe(version=1, codec="json", registry=registry)
    def root(ctx, *, n: int = 1):
        return {"v": n * scale[0]}

    @recipe(version=1, codec="json", registry=registry)
    def leaf(ctx, *, n: int = 1):
        return {"v": ctx.get("root", n=n)["v"] * 10}

    assert cache.get("leaf") == {"v": 10}
    (before,) = cache.query(recipe="leaf")
    original_bytes = cache.store.fetch("leaf", 1, before.key_hash, None).data

    scale[0] = 2
    assert cache.get("root", force=True) == {"v": 2}

    (after,) = cache.query(recipe="leaf")
    assert after.valid is False, "the dependent must be marked stale"
    kept = cache.store.fetch("leaf", 1, after.key_hash, None)
    assert kept is not None, "invalidation must not delete the row"
    assert kept.data == original_bytes, "invalidation must keep the bytes"


# ---------------------------------------------------------------------------
# 2. Never cache a failed compute, in any form.
# ---------------------------------------------------------------------------


def test_rule02_failed_compute_not_cached(cache: Cache, registry: Registry) -> None:
    @recipe(version=1, codec="json", registry=registry)
    def boom(ctx, *, n: int = 1):
        raise RuntimeError("vendor is down")

    with pytest.raises(RuntimeError, match="vendor is down"):
        cache.get("boom")

    assert cache.query(recipe="boom") == [], "no row, not even an invalid one"
    assert cache.store.pending_tasks() == [], "and no tombstone in the task table either"

    # A later success is unaffected by the earlier failure.
    @recipe(version=1, codec="json", registry=registry)
    def fine(ctx, *, n: int = 1):
        return {"v": n}

    assert cache.get("fine") == {"v": 1}


# ---------------------------------------------------------------------------
# 3. Artifact, dep edges and cascade land in one transaction or not at all.
# ---------------------------------------------------------------------------


def test_rule03_store_is_atomic(store: SqliteStore, monkeypatch: pytest.MonkeyPatch) -> None:
    dep = make_artifact(recipe="dep", data=b'{"a":1}')
    dep_result = store.store(dep, ())

    parent = make_artifact(
        recipe="parent",
        data=b'{"b":2}',
        deps=(
            DepEdge(
                recipe="dep",
                version=1,
                key_hash=dep.key_hash,
                part=None,
                digest=dep.digest,
                artifact_id=dep_result.artifact_id,
            ),
        ),
    )

    def explode(*args: object, **kwargs: object) -> None:
        raise OSError("disk died between the artifact insert and the dep insert")

    monkeypatch.setattr(SqliteStore, "_insert_dep_edges", explode)

    with pytest.raises(OSError):
        store.store(
            parent,
            (
                DepEdge(
                    recipe="dep",
                    version=1,
                    key_hash=dep.key_hash,
                    part=None,
                    digest=dep.digest,
                    artifact_id=dep_result.artifact_id,
                ),
            ),
        )

    assert store.query(recipe="parent") == [], "the artifact row must have rolled back too"
    assert store.dependents_of([dep_result.artifact_id]) == []


# ---------------------------------------------------------------------------
# 4. Never rewrite a row whose digest and fingerprint already match.
# ---------------------------------------------------------------------------


def test_rule04_idempotent_write_is_noop(store: SqliteStore) -> None:
    first = make_artifact(recipe="r", data=b'{"a":1}')
    first_result = store.store(first, ())
    assert first_result.written is True

    # A duplicate runner recomputes the same value and writes it again.
    again = make_artifact(recipe="r", data=b'{"a":1}')
    again_result = store.store(again, ())

    assert again_result.written is False, "an identical write must not rewrite the row"
    assert again_result.digest_changed is False
    assert again_result.invalidated == 0, "and must not cascade"

    stored = store.fetch("r", 1, first.key_hash, None)
    assert stored.computed_at == first.computed_at, "computed_at must not be touched"


# ---------------------------------------------------------------------------
# 5. `force` is local; only `force_deps` is transitive.
# ---------------------------------------------------------------------------


def test_rule05_force_is_local(cache: Cache, registry: Registry) -> None:
    calls = {"root": 0, "leaf": 0}

    @recipe(version=1, codec="json", registry=registry)
    def root(ctx, *, n: int = 1):
        calls["root"] += 1
        return {"v": n}

    @recipe(version=1, codec="json", registry=registry)
    def leaf(ctx, *, n: int = 1):
        calls["leaf"] += 1
        return {"v": ctx.get("root", n=n)["v"] + 1}

    cache.get("leaf")
    assert calls == {"root": 1, "leaf": 1}

    cache.get("leaf", force=True)
    assert calls == {"root": 1, "leaf": 2}, "force must recompute that node only"

    cache.get("leaf", force_deps=True)
    assert calls == {"root": 2, "leaf": 3}, "force_deps is the transitive one"


# ---------------------------------------------------------------------------
# 6. `on_miss` resolves to the strictest of caller and recipe, never the caller's alone.
# ---------------------------------------------------------------------------


def test_rule06_on_miss_is_a_ceiling(cache: Cache, registry: Registry) -> None:
    fetched = {"n": 0}

    @recipe(version=1, codec="json", on_miss="raise", registry=registry)
    def protected_root(ctx, *, symbol: str):
        fetched["n"] += 1  # a paid vendor request the caller never mentioned
        return {"bars": symbol}

    @recipe(version=1, codec="json", registry=registry)
    def derived(ctx, *, symbol: str):
        return {"v": ctx.get("protected_root", symbol=symbol)["bars"]}

    # The caller is as permissive as it gets, six levels up. The recipe still wins.
    with pytest.raises(MissingArtifacts) as excinfo:
        cache.get("derived", symbol="AAPL", on_miss="compute")

    assert fetched["n"] == 0, "a protected root must never be computed by a transitive ask"
    assert [n.recipe for n in excinfo.value.nodes] == ["protected_root"]
    assert excinfo.value.nodes[0].key == {"symbol": "AAPL"}

    # And the caller may always restrict further than the recipe declares.
    @recipe(version=1, codec="json", registry=registry)
    def ordinary(ctx, *, symbol: str):
        return {"v": symbol}

    with pytest.raises(MissingArtifacts):
        cache.get("ordinary", symbol="AAPL", on_miss="raise")

    # Ingesting is an explicit top-level act, and that one works.
    assert cache.get("protected_root", symbol="AAPL", on_miss="compute") == {"bars": "AAPL"}
    assert fetched["n"] == 1


# ---------------------------------------------------------------------------
# 7. Digests come from codec output; codecs must be byte-deterministic.
# ---------------------------------------------------------------------------


def _sample_for(codec_name: str) -> object:
    """A representative value per codec, built lazily so an absent extra just skips."""
    if codec_name == "json":
        return {"b": 1, "a": [1, 2.5, None, True], "z": "ü", "nested": {"y": 1, "x": 2}}
    if codec_name == "npy":
        np = pytest.importorskip("numpy")
        return np.arange(12, dtype="int64").reshape(3, 4)
    if codec_name in {"arrow", "arrow-series"}:
        pl = pytest.importorskip("polars")
        frame = pl.DataFrame({"close": [1.0, 2.5, 3.0], "volume": [10, 20, 30]})
        return frame["close"] if codec_name == "arrow-series" else frame
    raise AssertionError(f"no sample defined for codec {codec_name!r}")


@pytest.mark.parametrize("codec_name", sorted(CODEC_NAMES))
def test_rule07_codec_byte_determinism(codec_name: str) -> None:
    try:
        codec = get_codec(codec_name)
    except CodecUnavailable:
        pytest.skip(f"{codec_name} extra not installed")

    value = _sample_for(codec_name)
    once = codec.encode(value)
    assert isinstance(once, bytes), "sample values are deliberately unpartitioned"

    # Identical input, identical bytes, every time.
    assert codec.encode(value) == once
    # And a round trip is a fixed point, not merely lossless.
    assert codec.encode(codec.decode(once)) == once


# ---------------------------------------------------------------------------
# 8. A dependency's recipe version never enters a dependent's fingerprint.
# ---------------------------------------------------------------------------


def test_rule08_dep_version_not_in_fingerprint() -> None:
    def fp(dep_version: int) -> str:
        return fingerprint(
            recipe="sma",
            version=3,
            key_hash="a" * 64,
            part=None,
            deps=(
                DepEdge(
                    recipe="ohlcv",
                    version=dep_version,
                    key_hash="b" * 64,
                    part=None,
                    digest="c" * 64,
                    artifact_id=1,
                ),
            ),
        )

    assert fp(1) == fp(99), "a dependency contributes only its content digest"

    # The dependent's own version, by contrast, is in there.
    assert fingerprint(
        recipe="sma", version=3, key_hash="a" * 64, part=None, deps=()
    ) != fingerprint(recipe="sma", version=4, key_hash="a" * 64, part=None, deps=())


def test_rule08_end_to_end_dep_version_bump_does_not_invalidate(
    cache: Cache, registry: Registry
) -> None:
    @recipe(version=1, codec="json", registry=registry)
    def root(ctx, *, n: int = 1):
        return {"v": n}

    @recipe(version=1, codec="json", registry=registry)
    def leaf(ctx, *, n: int = 1):
        return {"v": ctx.get("root", n=n)["v"] + 1}

    assert cache.get("leaf") == {"v": 2}

    # Same bytes, new version: this is exactly what early cutoff is for.
    @recipe(version=2, codec="json", registry=registry)
    def root(ctx, *, n: int = 1):  # noqa: F811
        return {"v": n}

    assert cache.get("leaf") == {"v": 2}
    (ref,) = cache.query(recipe="leaf")
    assert ref.valid is True, "identical bytes under a new version invalidate nothing"


# ---------------------------------------------------------------------------
# 9. Empty or `None` results are values, not misses.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", [None, {}, []])
def test_rule09_empty_is_a_value(cache: Cache, registry: Registry, value: object) -> None:
    calls = {"n": 0}

    @recipe(version=1, codec="json", registry=registry)
    def empty(ctx, *, tag: str = "x"):
        calls["n"] += 1
        return value

    assert cache.get("empty") == value
    assert calls["n"] == 1

    assert len(cache.query(recipe="empty")) == 1, "presence is row existence, not truthiness"

    assert cache.get("empty") == value
    assert calls["n"] == 1, "a falsy stored value is a hit, not a miss"


# ---------------------------------------------------------------------------
# 10. Never use pickle.
# ---------------------------------------------------------------------------


def test_rule10_no_pickle_anywhere() -> None:
    offenders = []
    for path in PACKAGE_ROOT.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        # Strip comments so the explanatory note about *why* not, in the codec docs,
        # does not trip the scan.
        code = "\n".join(line.split("#", 1)[0] for line in source.splitlines())
        if re.search(r"\b(pickle|cPickle|dill|cloudpickle|shelve|marshal)\b", code):
            offenders.append(str(path.relative_to(PACKAGE_ROOT.parent)))
    assert offenders == [], (
        f"pickle would make the cache Python-only and unsafe to share: {offenders}"
    )


def test_rule10_numpy_codec_refuses_object_arrays() -> None:
    """`numpy.save` pickles object arrays unless told not to. It must be told not to."""
    np = pytest.importorskip("numpy")
    try:
        codec = get_codec("npy")
    except CodecUnavailable:  # pragma: no cover - importorskip already covers this
        pytest.skip("npy extra not installed")

    with pytest.raises(CodecError, match="object"):
        codec.encode(np.array([{"a": 1}, {"b": 2}], dtype=object))
