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
