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
def ohlcv(ctx, *, symbol: str, interval: str = "1d", adjust: str = "split"):
    return vendor.bars(symbol, interval, adjust)

@recipe(version=3, codec="arrow-series", index=("symbol",))
def sma(ctx, *, symbol: str, window: int, interval: str = "1d"):
    bars = ctx.get("ohlcv", symbol=symbol, interval=interval)
    return bars["close"].rolling_mean(window)

cache = Cache(store=SqliteStore("argos.db"))
cache.get("sma", symbol="AAPL", window=200)
```

The dependency graph is never declared. Every `ctx.get` a recipe makes is recorded and
written alongside the artifact in one transaction, so the graph is always an accurate
record of what actually happened: cycles are caught by the in-flight stack rather than
by static analysis, and a recipe that takes a different branch on different parameters
records different dependencies, correctly.

Staleness is decided by content digests rather than timestamps, which buys **early
cutoff**. Revise one bar from 2019 and every dependent is marked stale in one
transaction; ask for the 200-day moving average and it recomputes, comes out
byte-identical because the window never reaches 2019, and nothing above it is touched
at all.

This is a build system for data rather than files, in the sense of *Build Systems à la
Carte* (Mokhov, Mitchell, Peyton Jones): a suspending scheduler with constructive traces.

## Install

```bash
pip install matview              # core: SQLite and the json codec, nothing heavy
pip install 'matview[arrow]'     # polars DataFrames and Series via Arrow IPC
pip install 'matview[npy]'       # numpy arrays
pip install 'matview[all]'
```

## What it does

**Three states, never conflated.** An artifact is absent (no row), stale (a row whose
`valid` flag is clear) or valid. Stale is not absent: reading a stale artifact resolves
its dependencies, recomputes its fingerprint and compares. If it matches, the flag flips
back and the stored bytes are returned without recomputing anything. Nothing is ever
deleted on invalidation, because deleting collapses absent and stale and loses early
cutoff permanently.

**`on_miss` is a ceiling, not a default.** A recipe may declare `on_miss="raise"`, and
it is then protected everywhere it appears as a transitive dependency — a backtester
asking for a Sharpe ratio with `on_miss="compute"` cannot fire a paid vendor request six
levels down that it never mentioned. Ingesting is an explicit top-level act. Protected
misses are collected and reported in one batch, so a cold run tells you what to ingest
in a single pass rather than one symbol per attempt.

**`force` is local.** Forcing one indicator during development recomputes that node and
nothing beneath it. `force_deps` is the transitive one, and the ceiling still applies
under it.

**Deadlines, not budgets.** `deadline` is wall-clock seconds, converted to an absolute
instant at the top-level call and carried down the recursion, because per-recipe cost
estimates are guesses that go stale whereas elapsed time is a measurement.

**Recipes in any language.** A foreign recipe is a TOML manifest plus an executable. It
declares its inputs upfront, which makes it the *privileged* case: a declared graph can
be cycle-checked and planned before anything executes. One JSON job file is the only
argument; the result is read from a file, never from stdout, so a stray `print` cannot
corrupt a value.

## Examples

Two, one financial and one not, to keep domain assumptions out of the core. Both run
without network access and are exercised by the test suite.

```bash
uv run --extra arrow python examples/finance/pipeline.py        # early cutoff, end to end
uv run --extra npy   python examples/linguistics/pipeline.py    # a foreign recipe in the graph
```

## Design documents

| File | What it is |
| --- | --- |
| [`docs/design-spec.md`](docs/design-spec.md) | The full design spec. The source of truth for behaviour. |
| [`SPEC.md`](SPEC.md) | Canonicalization and fingerprints, verbatim and independently versioned. The cross-language interoperability contract. |
| [`PLAN.md`](PLAN.md) | Build order, stage by stage, and the ten invariants with the test that pins each one. |
| [`DECISIONS.md`](DECISIONS.md) | Every choice made where the spec was silent, with the reasoning. |
| [`docs/ci-workflow.yml`](docs/ci-workflow.yml) | The intended `.github/workflows/ci.yml`, not yet installed in place. |

`tests/golden_vectors.json` and `tests/golden_codecs.json` are fixed inputs to fixed
digests. They are the regression guard on the interoperability contract and the
reference a second-language implementation validates against; regenerating either to
make a test pass defeats the point of having them.

## Not in v1

Distributed locking or work leases; remote backends (the seam exists, the implementation
does not); a consumer for the `task` rows that `on_miss="enqueue"` writes; incremental
or append-only computation; point-in-time correctness; a web UI, daemon or server.

## Development

```bash
uv run --all-extras pytest                 # with every codec
uv run --extra dev --exact pytest          # and again with none, which must also pass
uv run --all-extras ruff check . && uv run --all-extras ruff format --check .
uv run --extra dev --exact mypy matview
```

## License

MIT. See [`LICENSE`](LICENSE).
