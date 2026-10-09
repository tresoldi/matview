# matview

Materialized views for derived values.

You declare *recipes*: functions that compute a value from parameters and from other
recipes' outputs. You ask the cache for a value; it returns the stored one if it is
current, and computes it if not, recursively resolving whatever that computation turns
out to need. Results are memoized in SQLite and reused across processes, machines and
runs.

```python
from matview import Cache, SqliteStore, recipe

@recipe(version=1, codec="arrow", index=("symbol",), on_miss="raise", deterministic=False)
def ohlcv(ctx, *, symbol: str, interval: str = "1d"):
    return vendor.bars(symbol, interval)

@recipe(version=3, codec="arrow-series", index=("symbol",))
def sma(ctx, *, symbol: str, window: int, interval: str = "1d"):
    bars = ctx.get("ohlcv", symbol=symbol, interval=interval)
    return bars["close"].rolling_mean(window)

cache = Cache(store=SqliteStore("cache.db"))
cache.get("sma", symbol="AAPL", window=200)
```

The dependency graph is never declared. Every `ctx.get` a recipe makes is recorded and
written alongside the artifact in one transaction, so the graph is always an accurate
record of what actually happened.

Staleness is decided by content digests rather than timestamps, which buys **early
cutoff**: revise one bar from 2019 and every dependent is marked stale, but the 200-day
moving average recomputes to identical bytes and nothing above it is touched at all.

This is a build system for data rather than files, in the sense of *Build Systems à la
Carte* (Mokhov, Mitchell, Peyton Jones): a suspending scheduler with constructive traces.

## Install

```bash
pip install matview              # core: SQLite and the json codec, nothing heavy
pip install 'matview[arrow]'     # polars DataFrames and Series via Arrow IPC
pip install 'matview[pandas]'    # the same Arrow bytes, decoded to pandas
pip install 'matview[npy]'       # numpy arrays
pip install 'matview[all]'
```

matview requires SQLite with JSON1. SQLite 3.38 or newer includes JSON1 by default;
an older or minimal SQLite build must provide JSON1 itself. `SqliteStore` checks this on
every open, including read-only opens, and refuses the database with its linked SQLite
version when the capability is absent.

## Where to go next

- **[Guide](guide.md)** — recipes, keys, policy, partitions and foreign recipes.
- **[API reference](reference.md)** — generated from the source.
- **[Design spec](design-spec.md)** — the full design document this was built from.

Two documents live at the repository root rather than here, deliberately:

- [`SPEC.md`](https://github.com/tresoldi/matview/blob/main/SPEC.md) is the
  cross-language interoperability contract for canonicalization and fingerprints. A
  second copy of it that could drift from the first would be worse than no copy.
- [`DECISIONS.md`](https://github.com/tresoldi/matview/blob/main/DECISIONS.md) records
  every choice made where the design spec was silent.
