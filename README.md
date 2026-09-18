# matview

Materialized views for derived values.

You declare *recipes*: functions that compute a value from parameters and from other
recipes' outputs. You ask the cache for a value; it returns the stored one if it is
current, and computes it if not, recursively resolving whatever that computation turns
out to need. Results are memoized in SQLite and reused across processes, machines and
runs.

```python
from matview import recipe, Cache, SqliteStore

@recipe(version=1, codec="arrow", index=("symbol",), on_miss="raise", deterministic=False)
def ohlcv(ctx, *, symbol: str, interval: str = "1d", adjust: str = "split"):
    return vendor.bars(symbol, interval, adjust)

@recipe(version=3, codec="arrow", index=("symbol",))
def sma(ctx, *, symbol: str, window: int, interval: str = "1d"):
    bars = ctx.get("ohlcv", symbol=symbol, interval=interval)
    return bars["close"].rolling_mean(window)

cache = Cache(store=SqliteStore("argos.db"))
cache.get("sma", symbol="AAPL", window=200)
```

The dependency graph is never declared. Every `ctx.get` a recipe makes is recorded and
written alongside the artifact in one transaction, so the graph is always an accurate
record of what actually happened. Staleness is decided by content digests rather than
timestamps, which buys early cutoff: a dependency that is recomputed and produces
identical bytes invalidates nothing downstream.

This is a build system for data rather than files, in the sense of *Build Systems à la
Carte* (Mokhov, Mitchell, Peyton Jones): a suspending scheduler with constructive traces.

## Status

Pre-implementation. The repository currently holds the design and the plan:

| File | What it is |
| --- | --- |
| [`docs/design-spec.md`](docs/design-spec.md) | The full design spec. The source of truth for behaviour. |
| [`SPEC.md`](SPEC.md) | Canonicalization and fingerprints, verbatim and independently versioned. The cross-language interoperability contract. |
| [`PLAN.md`](PLAN.md) | Build order, stage by stage, and the ten invariants with the test that pins each one. |
| [`DECISIONS.md`](DECISIONS.md) | Every choice made where the spec was silent. |
| [`docs/ci-workflow.yml`](docs/ci-workflow.yml) | The intended `.github/workflows/ci.yml`, not yet installed in place. |

## License

MIT. See [`LICENSE`](LICENSE).
