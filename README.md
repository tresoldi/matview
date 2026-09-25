# matview

Materialized views for derived values.

[![CI](https://github.com/tresoldi/matview/actions/workflows/ci.yml/badge.svg)](https://github.com/tresoldi/matview/actions/workflows/ci.yml)
[![Docs](https://github.com/tresoldi/matview/actions/workflows/docs.yml/badge.svg)](https://tresoldi.github.io/matview/)
[![PyPI](https://img.shields.io/pypi/v/matview)](https://pypi.org/project/matview/)
[![Python versions](https://img.shields.io/pypi/pyversions/matview)](https://pypi.org/project/matview/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

**Documentation: <https://tresoldi.github.io/matview/>**

You declare *recipes*: functions that compute a value from parameters and from other
recipes' outputs. You ask the cache for a value; it returns the stored one if it is
current, and computes it if not, recursively resolving whatever that computation turns
out to need. Results are memoized in SQLite and reused across processes, machines and
runs.

```python
from matview import Cache, SqliteStore, recipe

@recipe(
    version=1, codec="arrow", index=("symbol",), on_miss="raise",
    deterministic=False, max_age=18 * 60 * 60,
)
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
pip install 'matview[pandas]'    # the same Arrow bytes, decoded to pandas
pip install 'matview[npy]'       # numpy arrays
pip install 'matview[all]'
```

matview requires SQLite with JSON1. SQLite 3.38 or newer includes JSON1 by default;
an older or minimal SQLite build must provide JSON1 itself. `SqliteStore` checks this on
every open, including read-only opens, and refuses the database with its linked SQLite
version when the capability is absent.

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

**Freshness is generic; scheduling is not.** A recipe may declare `max_age` in seconds.
After that interval since its last successful computation, matview treats the value as
needing recomputation. Successful checks advance a separate refresh clock even when the
bytes are unchanged; `computed_at` retains its content-history meaning. Matview does not
know about trading sessions, business days, or cron: callers decide when reads happen.
Expiry is checked when that recipe is resolved; matview does not walk dependencies of
every valid cache hit merely to inspect their clocks. A scheduler should therefore
refresh source roots before asking for their derived values.

**Deadlines, not budgets.** `deadline` is wall-clock seconds, converted to an absolute
instant at the top-level call and carried down the recursion, because per-recipe cost
estimates are guesses that go stale whereas elapsed time is a measurement.

**Recipes in any language.** A foreign recipe is a TOML manifest plus an executable. It
declares its inputs upfront, which makes it the *privileged* case: a declared graph can
be cycle-checked and planned before anything executes. One JSON job file is the only
argument; the result is read from a file, never from stdout, so a stray `print` cannot
corrupt a value.

## Inspecting a cache

```bash
matview info   --db cache.db      # what this file is, whatever version wrote it
matview stats  --db cache.db
matview ls     --db cache.db --recipe sma --invalid
matview show   --db cache.db --id 12
matview verify --db cache.db      # every payload against its recorded digest

matview lineage --db cache.db --id 12       # what produced this?
matview impact  --db cache.db --changed 12  # what breaks if this changes?
matview cost    --db cache.db --by-recipe   # where the compute time goes
```

Read-only, `--json` on everything, and no dependencies beyond the core. Metadata needs
nothing imported; only a key given as partial parameters and `plan` need
`--recipes MODULE`. See the [command line guide](https://tresoldi.github.io/matview/cli/).

The last three walk the dependency graph the cache recorded as it ran — the input closure
of a value, what a change or a version bump or a drop would reach, and what a number cost
to compute — and render it as an indented tree, JSON, Graphviz DOT or Mermaid. See
[graph queries](https://tresoldi.github.io/matview/graph/); `impact --bump` in particular
is the clearest explanation of why a dependency's recipe version is deliberately not in a
dependent's fingerprint.

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
| [`DECISIONS.md`](DECISIONS.md) | Every choice made where the spec was silent, with the reasoning. |
| [`ROADMAP.md`](ROADMAP.md) | Candidates for future work, and what is deliberately excluded. |
| [`CHANGELOG.md`](CHANGELOG.md) | Release notes. Note that `SPEC.md` is versioned separately and deliberately. |
| [`RELEASING.md`](RELEASING.md) | How a release is cut, and the account-level setup it assumes. |

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
make install        # sync the venv with every extra
make help           # what else there is
make check          # everything CI runs: quality, both test passes, the codec floor
```

`make test-core` runs the suite with no codec extras installed, which must pass too —
the core is meant to install without them. `make codec-floor` re-checks the golden codec
bytes against the oldest supported polars, pyarrow and numpy; a codec that drifts does
not error, it just stops finding everything already stored.

Install the git hooks once with `uv run --extra dev pre-commit install`. They run ruff,
ruff-format and mypy at exactly the versions pinned in the `dev` extra.

Documentation is MkDocs Material with an API reference generated from the docstrings:
`make docs-serve` to preview, `make docs` to build it the way CI does (`--strict`).

## Status and contributions

Published for use rather than for collaboration. It is released because it is useful, and
it is maintained for the projects that depend on it; there is no ambition to run it as a
community project.

Bug reports are welcome as [issues](https://github.com/tresoldi/matview/issues),
especially ones with a database or a recipe that reproduces the problem. Pull requests
may sit unreviewed — please open an issue before writing one, so you do not spend effort
on something that will not be merged. Anything under
[**Not in v1**](#not-in-v1) or under "Deliberately not doing" in
[`ROADMAP.md`](ROADMAP.md) is excluded on purpose rather than for want of a patch.

For anything exploitable, do not open an issue: see [`SECURITY.md`](SECURITY.md), which
also states plainly what matview trusts — recipes are code, and a cache database from an
untrusted source is untrusted input.

## Citing

[`CITATION.cff`](CITATION.cff) carries the metadata, and GitHub renders it as a "Cite this
repository" box in the sidebar.

## License

MIT. See [`LICENSE`](LICENSE).
