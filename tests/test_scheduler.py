"""Stages 4 and 5: the scheduler and the policy layer."""

from __future__ import annotations

import json
import time
from typing import Any

import pytest

from matview import Cache, Registry, SqliteStore, recipe
from matview.codecs import BaseCodec, register_codec
from matview.errors import (
    CodecError,
    CycleError,
    DeadlineExceeded,
    MissingArtifacts,
    RecipeNotFound,
)
from matview.fingerprint import key_hash

# --- cycle detection -------------------------------------------------------


def test_a_cycle_raises_with_the_full_path(cache: Cache, registry: Registry) -> None:
    @recipe(version=1, codec="json", registry=registry)
    def a(ctx, *, n: int = 1):
        return ctx.get("b", n=n)

    @recipe(version=1, codec="json", registry=registry)
    def b(ctx, *, n: int = 1):
        return ctx.get("c", n=n)

    @recipe(version=1, codec="json", registry=registry)
    def c(ctx, *, n: int = 1):
        return ctx.get("a", n=n)

    with pytest.raises(CycleError) as excinfo:
        cache.get("a")

    assert [step.split("(")[0] for step in excinfo.value.path] == ["a", "b", "c", "a"]
    assert cache.query() == [], "nothing partial was written on the way down"


def test_self_reference_is_a_cycle(cache: Cache, registry: Registry) -> None:
    @recipe(version=1, codec="json", registry=registry)
    def loop(ctx, *, n: int = 1):
        return ctx.get("loop", n=n)

    with pytest.raises(CycleError):
        cache.get("loop")


def test_the_same_recipe_with_a_different_key_is_not_a_cycle(
    cache: Cache, registry: Registry
) -> None:
    @recipe(version=1, codec="json", registry=registry)
    def countdown(ctx, *, n: int):
        if n <= 0:
            return {"v": 0}
        return {"v": n + ctx.get("countdown", n=n - 1)["v"]}

    assert cache.get("countdown", n=4) == {"v": 10}


# --- early cutoff: the spec's worked example -------------------------------


@pytest.fixture
def early_cutoff(cache: Cache, registry: Registry) -> dict[str, Any]:
    """ohlcv -> sma -> sharpe, with a 3-bar window that never reaches the oldest bar."""
    state: dict[str, Any] = {"bars": [10.0, 20.0, 30.0, 40.0, 50.0], "calls": {}}

    def count(name: str) -> None:
        state["calls"][name] = state["calls"].get(name, 0) + 1

    @recipe(version=1, codec="json", index=("symbol",), registry=registry)
    def ohlcv(ctx, *, symbol: str, interval: str = "1d"):
        count("ohlcv")
        return {"close": list(state["bars"])}

    @recipe(version=3, codec="json", index=("symbol",), registry=registry)
    def sma(ctx, *, symbol: str, window: int, interval: str = "1d"):
        count("sma")
        close = ctx.get("ohlcv", symbol=symbol, interval=interval)["close"]
        return {"sma": sum(close[-window:]) / window}

    @recipe(version=1, codec="json", index=("symbol",), registry=registry)
    def sharpe(ctx, *, symbol: str):
        count("sharpe")
        return {"sharpe": ctx.get("sma", symbol=symbol, window=3)["sma"] / 10}

    return state


def test_early_cutoff_stops_the_cascade_at_identical_bytes(
    cache: Cache, early_cutoff: dict[str, Any]
) -> None:
    """The spec's worked example, asserted end to end.

    A vendor revises one bar from the distant past. Everything downstream is marked
    stale in one transaction. The SMA recomputes and comes out byte-identical, because
    a 3-bar window never reaches that far back, so its digest is unchanged and nothing
    above it is recomputed. That is the entire payoff of digest-based fingerprints.
    """
    calls = early_cutoff["calls"]
    assert cache.get("sharpe", symbol="AAPL") == {"sharpe": 4.0}
    assert calls == {"ohlcv": 1, "sma": 1, "sharpe": 1}

    # The vendor revises the oldest bar. Forcing ohlcv changes its bytes.
    early_cutoff["bars"][0] = 999.0
    cache.get("ohlcv", symbol="AAPL", force=True)
    assert calls["ohlcv"] == 2

    stale = {ref.recipe for ref in cache.query() if not ref.valid}
    assert stale == {"sma", "sharpe"}, "the cascade marked both, in one transaction"

    assert cache.get("sharpe", symbol="AAPL") == {"sharpe": 4.0}
    assert calls["sma"] == 2, "the SMA is stale and its fingerprint changed, so it reruns"
    assert calls["sharpe"] == 1, "but its bytes did not change, so nothing above it does"
    assert calls["ohlcv"] == 2, "and the root is valid, so it is not touched"

    assert all(ref.valid for ref in cache.query()), "everything is current again"


def test_a_change_inside_the_window_does_propagate(
    cache: Cache, early_cutoff: dict[str, Any]
) -> None:
    """The control case: without this, the test above would pass on a broken cascade."""
    calls = early_cutoff["calls"]
    cache.get("sharpe", symbol="AAPL")

    early_cutoff["bars"][-1] = 500.0  # inside the 3-bar window
    cache.get("ohlcv", symbol="AAPL", force=True)

    assert cache.get("sharpe", symbol="AAPL") == {"sharpe": 19.0}
    assert calls["sma"] == 2
    assert calls["sharpe"] == 2, "this one really did change"


def test_a_stale_artifact_whose_fingerprint_still_matches_is_not_recomputed(
    cache: Cache, registry: Registry
) -> None:
    calls = {"n": 0}

    @recipe(version=1, codec="json", registry=registry)
    def leaf(ctx, *, n: int = 1):
        calls["n"] += 1
        return {"v": n}

    assert cache.get("leaf") == {"v": 1}
    (ref,) = cache.query(recipe="leaf")
    cache.store.invalidate([ref.id])
    assert cache.query(recipe="leaf")[0].valid is False

    assert cache.get("leaf") == {"v": 1}
    assert calls["n"] == 1, "stale is not absent: verify, then return the stored bytes"
    assert cache.query(recipe="leaf")[0].valid is True, "and flip the flag back"


# --- policy ----------------------------------------------------------------


def test_defaults_are_filled_before_hashing(cache: Cache, registry: Registry) -> None:
    calls = {"n": 0}

    @recipe(version=1, codec="json", registry=registry)
    def bars(ctx, *, symbol: str, interval: str = "1d"):
        calls["n"] += 1
        return {"s": symbol, "i": interval}

    cache.get("bars", symbol="AAPL")
    cache.get("bars", symbol="AAPL", interval="1d")
    assert calls["n"] == 1, "the same artifact, spelled two ways"
    assert len(cache.query(recipe="bars")) == 1
    assert cache.query(recipe="bars")[0].key_hash == key_hash({"symbol": "AAPL", "interval": "1d"})


def test_unknown_and_missing_parameters_are_rejected(cache: Cache, registry: Registry) -> None:
    @recipe(version=1, codec="json", registry=registry)
    def bars(ctx, *, symbol: str):
        return {"s": symbol}

    with pytest.raises(Exception, match="no parameter"):
        cache.get("bars", symbol="AAPL", typo=1)
    with pytest.raises(Exception, match="missing required"):
        cache.get("bars")


def test_an_unregistered_recipe_says_what_is_registered(cache: Cache, registry: Registry) -> None:
    @recipe(version=1, codec="json", registry=registry)
    def known(ctx, *, n: int = 1):
        return {"v": n}

    with pytest.raises(RecipeNotFound, match="registered: known"):
        cache.get("unknown")


def test_every_protected_miss_is_reported_at_once(cache: Cache, registry: Registry) -> None:
    """A cold run says exactly what needs ingesting, rather than one per attempt.

    Batching is only possible where the dependency set is knowable *without* running
    the recipe: stored edges, declared foreign inputs, or a plan. Here it is the stored
    edges of a stale artifact whose protected inputs have been superseded by a version
    bump. See DECISIONS.md D28.
    """

    @recipe(version=1, codec="json", on_miss="raise", index=("symbol",), registry=registry)
    def vendor(ctx, *, symbol: str):
        return {"bars": symbol}

    @recipe(version=1, codec="json", registry=registry)
    def spread(ctx, *, a: str, b: str):
        return {
            "v": ctx.get("vendor", symbol=a)["bars"] + ctx.get("vendor", symbol=b)["bars"],
        }

    cache.get("vendor", symbol="AAPL", on_miss="compute")
    cache.get("vendor", symbol="MSFT", on_miss="compute")
    assert cache.get("spread", a="AAPL", b="MSFT") == {"v": "AAPLMSFT"}

    # The vendor recipe is revised. Its old artifacts stay in place, unreachable, and
    # nothing is ingested at the new version yet.
    @recipe(version=2, codec="json", on_miss="raise", index=("symbol",), registry=registry)
    def vendor(ctx, *, symbol: str):  # noqa: F811
        return {"bars": symbol.lower()}

    cache.store.invalidate([cache.query(recipe="spread")[0].id])

    with pytest.raises(MissingArtifacts) as excinfo:
        cache.get("spread", a="AAPL", b="MSFT")

    reported = sorted(node.key["symbol"] for node in excinfo.value.nodes)
    assert reported == ["AAPL", "MSFT"], "both, in one raise"
    assert all(node.version == 2 for node in excinfo.value.nodes)
    assert "AAPL" in str(excinfo.value) and "MSFT" in str(excinfo.value)


def test_plan_reports_every_missing_input_of_a_superseded_dependency(
    cache: Cache, registry: Registry
) -> None:
    """The same machinery, used the way the spec intends it: ask before you run."""

    @recipe(version=1, codec="json", on_miss="raise", index=("symbol",), registry=registry)
    def vendor(ctx, *, symbol: str):
        return {"bars": symbol}

    @recipe(version=1, codec="json", registry=registry)
    def spread(ctx, *, a: str, b: str):
        return {
            "v": ctx.get("vendor", symbol=a)["bars"] + ctx.get("vendor", symbol=b)["bars"],
        }

    cache.get("vendor", symbol="AAPL", on_miss="compute")
    cache.get("vendor", symbol="MSFT", on_miss="compute")
    cache.get("spread", a="AAPL", b="MSFT")

    @recipe(version=2, codec="json", on_miss="raise", index=("symbol",), registry=registry)
    def vendor(ctx, *, symbol: str):  # noqa: F811
        return {"bars": symbol.lower()}

    report = cache.plan("spread", a="AAPL", b="MSFT")
    assert sorted(node.key["symbol"] for node in report.absent) == ["AAPL", "MSFT"]
    assert report.is_current is False


def test_enqueue_writes_a_task_row_and_still_reports_the_miss(
    cache: Cache, registry: Registry
) -> None:
    @recipe(version=1, codec="json", on_miss="enqueue", registry=registry)
    def queued(ctx, *, symbol: str):
        return {"bars": symbol}

    @recipe(version=1, codec="json", registry=registry)
    def uses(ctx, *, symbol: str):
        return ctx.get("queued", symbol=symbol)

    with pytest.raises(MissingArtifacts) as excinfo:
        cache.get("uses", symbol="AAPL")

    assert [node.action for node in excinfo.value.nodes] == ["enqueue"]
    tasks = cache.store.pending_tasks()
    assert len(tasks) == 1
    assert tasks[0]["recipe"] == "queued"
    assert json.loads(tasks[0]["key_json"]) == {"symbol": "AAPL"}
    assert cache.query(recipe="queued") == [], "nothing in v1 consumes the row"


def test_force_deps_skips_a_protected_node_rather_than_failing(
    cache: Cache, registry: Registry
) -> None:
    calls = {"vendor": 0, "derived": 0}

    @recipe(version=1, codec="json", on_miss="raise", registry=registry)
    def vendor(ctx, *, symbol: str):
        calls["vendor"] += 1
        return {"bars": symbol}

    @recipe(version=1, codec="json", registry=registry)
    def derived(ctx, *, symbol: str):
        calls["derived"] += 1
        return ctx.get("vendor", symbol=symbol)

    cache.get("vendor", symbol="AAPL", on_miss="compute")
    cache.get("derived", symbol="AAPL")
    assert calls == {"vendor": 1, "derived": 1}

    cache.get("derived", symbol="AAPL", force_deps=True)
    assert calls["derived"] == 2, "the unprotected node rebuilds"
    assert calls["vendor"] == 1, "the protected root is skipped, not refetched or raised"


def test_a_deadline_propagates_down_the_recursion(cache: Cache, registry: Registry) -> None:
    @recipe(version=1, codec="json", registry=registry)
    def slow(ctx, *, n: int = 1):
        return {"v": n}

    @recipe(version=1, codec="json", registry=registry)
    def waits(ctx, *, n: int = 1):
        time.sleep(0.05)
        return ctx.get("slow", n=n)

    with pytest.raises(DeadlineExceeded, match="slow"):
        cache.get("waits", deadline=0.02)

    assert cache.query() == [], "a compute that ran out of budget is not cached"


def test_a_deadline_that_is_already_gone_is_rejected_up_front(
    cache: Cache, registry: Registry
) -> None:
    @recipe(version=1, codec="json", registry=registry)
    def anything(ctx, *, n: int = 1):
        return {"v": n}

    with pytest.raises(DeadlineExceeded):
        cache.get("anything", deadline=0)


def test_a_failed_compute_leaves_its_dependencies_alone(cache: Cache, registry: Registry) -> None:
    @recipe(version=1, codec="json", registry=registry)
    def good(ctx, *, n: int = 1):
        return {"v": n}

    @recipe(version=1, codec="json", registry=registry)
    def bad(ctx, *, n: int = 1):
        ctx.get("good", n=n)
        raise ValueError("halfway through")

    with pytest.raises(ValueError, match="halfway"):
        cache.get("bad")

    assert [ref.recipe for ref in cache.query()] == ["good"]
    assert cache.query(recipe="good")[0].valid is True


# --- diamonds, bytes, planning ---------------------------------------------


def test_a_shared_dependency_resolves_once_per_call(cache: Cache, registry: Registry) -> None:
    calls = {"shared": 0}

    @recipe(version=1, codec="json", deterministic=False, registry=registry)
    def shared(ctx, *, n: int = 1):
        calls["shared"] += 1
        return {"v": n}

    @recipe(version=1, codec="json", registry=registry)
    def left(ctx, *, n: int = 1):
        return ctx.get("shared", n=n)

    @recipe(version=1, codec="json", registry=registry)
    def right(ctx, *, n: int = 1):
        return ctx.get("shared", n=n)

    @recipe(version=1, codec="json", registry=registry)
    def top(ctx, *, n: int = 1):
        return {"v": ctx.get("left", n=n)["v"] + ctx.get("right", n=n)["v"]}

    assert cache.get("top") == {"v": 2}
    assert calls["shared"] == 1, "a diamond must not build the shared node twice"


def test_get_bytes_returns_what_is_stored(cache: Cache, registry: Registry) -> None:
    @recipe(version=1, codec="json", registry=registry)
    def doc(ctx, *, n: int = 1):
        return {"b": n, "a": n}

    assert cache.get_bytes("doc") == b'{"a":1,"b":1}'
    assert cache.get("doc") == {"a": 1, "b": 1}


def test_plan_reports_absent_and_stale_separately(cache: Cache, registry: Registry) -> None:
    @recipe(version=1, codec="json", deterministic=False, registry=registry)
    def root(ctx, *, n: int = 1):
        return {"v": n}

    @recipe(version=1, codec="json", registry=registry)
    def leaf(ctx, *, n: int = 1):
        return {"v": ctx.get("root", n=n)["v"] + 1}

    cold = cache.plan("leaf")
    assert [node.recipe for node in cold.absent] == ["leaf"]
    assert cold.stale == ()
    assert cold.is_current is False

    cache.get("leaf")
    warm = cache.plan("leaf")
    assert warm.absent == ()
    assert warm.stale == ()
    assert {ref.recipe for ref in warm.valid} == {"leaf", "root"}
    assert warm.is_current is True
    assert warm.nondeterministic == ("root",), "the flag is reported, not acted on"

    cache.store.invalidate([cache.query(recipe="root")[0].id])
    after = cache.plan("leaf")
    assert {ref.recipe for ref in after.stale} == {"leaf", "root"}
    assert after.absent == ()
    assert after.is_current is False
    assert "stale" in after.describe()


def test_plan_computes_nothing(cache: Cache, registry: Registry) -> None:
    calls = {"n": 0}

    @recipe(version=1, codec="json", registry=registry)
    def never(ctx, *, n: int = 1):
        calls["n"] += 1
        return {"v": n}

    cache.plan("never")
    assert calls["n"] == 0
    assert cache.query() == []


# --- partitioned values ----------------------------------------------------


class YearCodec(BaseCodec):
    """A codec that slices a series by calendar year, the way the finance case wants."""

    name = "test-years"
    partitions = True

    def encode(self, value: object) -> bytes | dict[str, bytes]:
        assert isinstance(value, dict)
        return {
            year: json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
            for year, rows in value.items()
        }

    def decode(self, data: bytes) -> object:
        return json.loads(data)

    def decode_parts(self, parts: dict[str, bytes]) -> object:
        return {year: json.loads(data) for year, data in sorted(parts.items())}


@pytest.fixture(autouse=True)
def _year_codec() -> None:
    register_codec(YearCodec())


def test_a_partitioned_value_round_trips_whole_and_by_part(
    cache: Cache, registry: Registry
) -> None:
    @recipe(version=1, codec="test-years", registry=registry)
    def series(ctx, *, symbol: str):
        return {"2024": [1, 2], "2025": [3, 4], "2026": [5]}

    assert cache.get("series", symbol="AAPL") == {"2024": [1, 2], "2025": [3, 4], "2026": [5]}
    assert cache.get("series", symbol="AAPL", part="2025") == [3, 4]

    refs = cache.query(recipe="series")
    assert [ref.part for ref in refs] == ["2024", "2025", "2026"]
    assert len({ref.digest for ref in refs}) == 3
    assert len({ref.fingerprint for ref in refs}) == 3, "part is in the fingerprint"

    raw = cache.get_bytes("series", symbol="AAPL")
    assert isinstance(raw, dict)
    assert set(raw) == {"2024", "2025", "2026"}


def test_reading_one_part_depends_on_that_part_alone(
    cache: Cache, registry: Registry, store: SqliteStore
) -> None:
    state = {"2026": [5]}
    calls = {"closed": 0}

    @recipe(version=1, codec="test-years", registry=registry)
    def series(ctx, *, symbol: str):
        return {"2024": [1, 2], "2026": list(state["2026"])}

    @recipe(version=1, codec="json", registry=registry)
    def closed_year(ctx, *, symbol: str):
        calls["closed"] += 1
        return {"sum": sum(ctx.get("series", symbol=symbol, part="2024"))}

    assert cache.get("closed_year", symbol="AAPL") == {"sum": 3}
    assert calls["closed"] == 1

    (ref,) = cache.query(recipe="closed_year")
    edges = store.deps_of(ref.id)
    assert [edge.part for edge in edges] == ["2024"], "one edge, for the part it read"

    # The current year is rewritten. A closed year is immutable, so nothing downstream
    # of it should notice.
    state["2026"] = [5, 6]
    cache.get("series", symbol="AAPL", force=True)
    assert cache.query(recipe="closed_year")[0].valid is True

    assert cache.get("closed_year", symbol="AAPL") == {"sum": 3}
    assert calls["closed"] == 1, "a write to another part is not this node's business"


def test_a_part_that_stops_being_produced_is_removed(cache: Cache, registry: Registry) -> None:
    years = {"2024": [1], "2025": [2]}

    @recipe(version=1, codec="test-years", registry=registry)
    def series(ctx, *, symbol: str):
        return dict(years)

    assert set(cache.get("series", symbol="AAPL")) == {"2024", "2025"}

    del years["2024"]
    assert set(cache.get("series", symbol="AAPL", force=True)) == {"2025"}
    assert [ref.part for ref in cache.query(recipe="series")] == ["2025"], (
        "a phantom partition would be merged into every later whole-value read"
    )


def test_cache_drop_and_gc_are_reachable_from_the_cache(cache: Cache, registry: Registry) -> None:
    @recipe(version=1, codec="json", registry=registry)
    def thing(ctx, *, n: int = 1):
        return {"v": 1}

    cache.get("thing")

    @recipe(version=2, codec="json", registry=registry)
    def thing(ctx, *, n: int = 1):  # noqa: F811
        return {"v": 2}

    cache.get("thing")
    assert sorted(ref.version for ref in cache.query(recipe="thing")) == [1, 2]

    assert cache.gc() == 1, "the superseded version has no dependents"
    assert [ref.version for ref in cache.query(recipe="thing")] == [2]
    assert cache.drop("thing", version=2) == 1
    assert cache.query(recipe="thing") == []


def test_asking_for_a_part_an_unpartitioned_codec_never_produces(
    cache: Cache, registry: Registry
) -> None:
    @recipe(version=1, codec="json", registry=registry)
    def whole(ctx, *, n: int = 1):
        return {"v": n}

    with pytest.raises(CodecError, match="no such part"):
        cache.get("whole", part="2026")


def test_a_codec_that_splits_a_value_into_zero_parts_is_an_error(
    cache: Cache, registry: Registry
) -> None:
    """Storing nothing at all would look exactly like a value that is legitimately empty."""

    @recipe(version=1, codec="test-years", registry=registry)
    def nothing(ctx, *, symbol: str):
        return {}

    with pytest.raises(CodecError, match="zero parts"):
        cache.get("nothing", symbol="AAPL")
    assert cache.query(recipe="nothing") == []


def test_a_skipped_protected_node_is_still_reported(cache: Cache, registry: Registry) -> None:
    """force_deps past a protected root returns a value *and* says what it could not honour."""

    @recipe(version=1, codec="json", on_miss="raise", registry=registry)
    def vendor(ctx, *, symbol: str):
        return {"bars": symbol}

    @recipe(version=1, codec="json", registry=registry)
    def derived(ctx, *, symbol: str):
        return ctx.get("vendor", symbol=symbol)

    cache.get("vendor", symbol="AAPL", on_miss="compute")
    cache.get("derived", symbol="AAPL")
    cache.drop("vendor")

    with pytest.raises(MissingArtifacts):
        cache.get("derived", symbol="AAPL", force_deps=True)


def test_a_protected_miss_reports_who_wanted_it(cache: Cache, registry: Registry) -> None:
    """Knowing `ohlcv(AAPL)` is missing is a fact; knowing what asked is the diagnosis."""

    @recipe(version=1, codec="json", on_miss="raise", registry=registry)
    def vendor(ctx, *, symbol: str):
        return {"bars": symbol}

    @recipe(version=3, codec="json", registry=registry)
    def indicator(ctx, *, symbol: str, window: int):
        return ctx.get("vendor", symbol=symbol)

    @recipe(version=1, codec="json", registry=registry)
    def portfolio(ctx, *, symbol: str):
        return ctx.get("indicator", symbol=symbol, window=200)

    with pytest.raises(MissingArtifacts) as excinfo:
        cache.get("portfolio", symbol="AAPL")

    (node,) = excinfo.value.nodes
    assert [step.split("(")[0] for step in node.path] == ["portfolio", "indicator", "vendor"]
    assert node.path[-1].startswith("vendor"), "the chain ends at the node that is missing"

    rendered = node.describe()
    assert "wanted by" in rendered
    assert "portfolio" in rendered and "window=200" in rendered
    assert "wanted by" in str(excinfo.value)


def test_the_path_is_recorded_through_a_stale_verification(
    cache: Cache, registry: Registry
) -> None:
    """Nothing is computing during a verification, so a compute-time stack would be empty."""

    @recipe(version=1, codec="json", on_miss="raise", registry=registry)
    def vendor(ctx, *, symbol: str):
        return {"bars": symbol}

    @recipe(version=1, codec="json", registry=registry)
    def derived(ctx, *, symbol: str):
        return ctx.get("vendor", symbol=symbol)

    cache.get("vendor", symbol="AAPL", on_miss="compute")
    cache.get("derived", symbol="AAPL")

    @recipe(version=2, codec="json", on_miss="raise", registry=registry)
    def vendor(ctx, *, symbol: str):  # noqa: F811
        return {"bars": symbol.lower()}

    cache.store.invalidate([cache.query(recipe="derived")[0].id])

    with pytest.raises(MissingArtifacts) as excinfo:
        cache.get("derived", symbol="AAPL")

    (node,) = excinfo.value.nodes
    assert [step.split("(")[0] for step in node.path] == ["derived", "vendor"]


def test_a_plan_records_the_path_too(cache: Cache, registry: Registry) -> None:
    @recipe(version=1, codec="json", registry=registry)
    def leaf(ctx, *, n: int = 1):
        return {"v": n}

    report = cache.plan("leaf")
    assert [step.split("(")[0] for step in report.absent[0].path] == ["leaf"]
    assert "wanted by" not in report.absent[0].describe(), "a root has nobody above it"
