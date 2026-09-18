"""Generic age-based freshness, independent of any domain calendar."""

from __future__ import annotations

import pytest

from matview import Cache, Registry, recipe
from matview.errors import MissingArtifacts


def test_max_age_refreshes_at_the_ceiling_and_unchanged_bytes_become_fresh(
    cache: Cache, registry: Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [1_000]
    calls = [0]
    monkeypatch.setattr("matview.cache.time.time", lambda: now[0])

    @recipe(version=1, codec="json", max_age=60, registry=registry)
    def source(ctx):
        del ctx
        calls[0] += 1
        return {"unchanged": True}

    assert cache.get("source") == {"unchanged": True}
    before = cache.query("source")[0]

    now[0] = 1_059
    cache.get("source")
    assert calls[0] == 1

    now[0] = 1_060
    cache.get("source")
    after = cache.query("source")[0]
    assert calls[0] == 2
    assert after.computed_at == before.computed_at
    assert after.refreshed_at == 1_060

    now[0] = 1_061
    cache.get("source")
    assert calls[0] == 2, "an identical refresh must not remain expired"


def test_plan_reports_an_expired_value_as_stale_without_computing(
    cache: Cache, registry: Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [1_000]
    calls = [0]
    monkeypatch.setattr("matview.cache.time.time", lambda: now[0])

    @recipe(version=1, codec="json", max_age=60, registry=registry)
    def source(ctx):
        del ctx
        calls[0] += 1
        return {"v": 1}

    cache.get("source")
    now[0] = 1_060

    report = cache.plan("source")

    assert len(report.stale) == 1
    assert report.valid == ()
    assert calls[0] == 1


def test_expired_protected_dependency_requires_explicit_ingest(
    cache: Cache, registry: Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [1_000]
    calls = [0]
    monkeypatch.setattr("matview.cache.time.time", lambda: now[0])

    @recipe(
        version=1,
        codec="json",
        on_miss="raise",
        deterministic=False,
        max_age=60,
        registry=registry,
    )
    def vendor(ctx):
        del ctx
        calls[0] += 1
        return {"v": 1}

    @recipe(version=1, codec="json", registry=registry)
    def derived(ctx):
        return {"v": ctx.get("vendor")["v"] + 1}

    cache.get("vendor", on_miss="compute")
    cache.get("derived")
    now[0] = 1_060

    with pytest.raises(MissingArtifacts) as excinfo:
        cache.get("derived", force=True)

    assert [node.recipe for node in excinfo.value.nodes] == ["vendor"]
    assert calls[0] == 1

    cache.get("vendor", on_miss="compute")
    assert calls[0] == 2


@pytest.mark.parametrize("bad", [0, -1, 1.5, True, "60"])
def test_max_age_must_be_positive_integer_seconds(registry: Registry, bad: object) -> None:
    with pytest.raises(Exception, match="max_age must be a positive integer"):

        @recipe(version=1, codec="json", max_age=bad, registry=registry)  # type: ignore[arg-type]
        def wrong(ctx):
            return {}


def test_the_expired_counter_separates_a_clock_from_a_changed_input(
    cache: Cache, registry: Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both cost the same and mean entirely different things. See DECISIONS.md D35."""
    now = [1_000_000]
    monkeypatch.setattr("matview.cache.time.time", lambda: now[0])

    @recipe(version=1, codec="json", max_age=60, registry=registry)
    def source(ctx, *, n: int = 1):
        return {"v": n, "at": now[0]}

    cache.get("source")
    assert (cache.stats.recomputed, cache.stats.expired) == (1, 0)

    # Still inside the window: an ordinary hit.
    cache.stats.reset()
    cache.get("source")
    assert (cache.stats.hits, cache.stats.expired) == (1, 0)

    # Past the ceiling: recomputed, and the counter says why.
    cache.stats.reset()
    now[0] += 61
    cache.get("source")
    assert cache.stats.expired == 1
    assert cache.stats.recomputed == 1
    assert cache.stats.lookups == 1, "expired is a reason, so it is not in the partition"
    assert "expired by max_age" in cache.stats.describe()


def test_an_expired_node_that_is_blocked_counts_under_blocked_not_recomputed(
    cache: Cache, registry: Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [2_000_000]
    monkeypatch.setattr("matview.cache.time.time", lambda: now[0])

    @recipe(version=1, codec="json", max_age=60, on_miss="raise", registry=registry)
    def vendor(ctx, *, symbol: str):
        return {"v": symbol}

    @recipe(version=1, codec="json", registry=registry)
    def derived(ctx, *, symbol: str):
        return ctx.get("vendor", symbol=symbol)

    cache.get("vendor", symbol="AAPL", on_miss="compute")
    cache.get("derived", symbol="AAPL")

    now[0] += 61
    cache.stats.reset()
    # The dependent is stale, so the walk actually reaches the expired root.
    cache.store.invalidate([cache.query(recipe="derived")[0].id])
    with pytest.raises(MissingArtifacts):
        cache.get("derived", symbol="AAPL")

    assert cache.stats.expired == 1
    assert cache.stats.blocked == 1
    assert cache.stats.recomputed == 0


def test_a_ttl_is_not_transitive(
    cache: Cache, registry: Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A valid dependent is returned without walking its graph to inspect clocks.

    Doing otherwise would turn every ordinary cache hit into a graph traversal and make
    a TTL silently transitive. Intended, and documented in DECISIONS.md D34.
    """
    now = [3_000_000]
    monkeypatch.setattr("matview.cache.time.time", lambda: now[0])
    calls = {"source": 0, "derived": 0}

    @recipe(version=1, codec="json", max_age=60, registry=registry)
    def source(ctx, *, n: int = 1):
        calls["source"] += 1
        return {"v": n}

    @recipe(version=1, codec="json", registry=registry)
    def derived(ctx, *, n: int = 1):
        calls["derived"] += 1
        return {"v": ctx.get("source", n=n)["v"] + 1}

    cache.get("derived")
    assert calls == {"source": 1, "derived": 1}

    now[0] += 61
    cache.stats.reset()
    assert cache.get("derived") == {"v": 2}
    assert calls == {"source": 1, "derived": 1}, "the dependent is valid; no traversal"
    assert cache.stats.expired == 0, "the expired root's clock was never consulted"
    assert cache.stats.hits == 1

    # Refreshing the root first is how a scheduler is meant to drive this.
    cache.get("source")
    assert calls["source"] == 2
    assert cache.stats.expired == 1
