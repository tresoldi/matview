# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The one thing that is **not** versioned by this file is the fingerprint specification.
It lives in [`SPEC.md`](SPEC.md) with its own `fingerprint_spec_version`, recorded in
every database's `meta` table. Changing it invalidates every existing cache, so it
changes only by bumping that number — never as part of an ordinary release.

## [Unreleased]

First working version, not yet published. Will be released as 0.1.0.

### Added

- Graph queries over the DAG the cache records as it runs, in a new `matview.graph`
  module and three read-only commands. `lineage` walks upward to the raw inputs that
  produced a value — the question the library recorded everything for and could not
  answer. `impact` answers what a changed artifact, a recipe version bump or a `drop`
  would reach. `cost` reports embodied `compute_ms` over an input closure, a per-recipe
  rollup, and the critical path through a closure. Four renderings each: indented tree,
  JSON, Graphviz DOT and Mermaid.

  None of them needs `--recipes`: they read the recorded graph, so they work against a
  database whose recipes the process has never imported. None of them writes, `impact
  --drop` included.

  `impact` prints per-recipe counts by default and enumerates only under `--all`. One
  shared root in the 250k benchmark profile reaches 255,000 dependents.

  `impact --bump` exists mainly to state plainly what rule 8 means: bumping a recipe's
  version marks **nothing** stale, because a dependency's recipe version is not part of a
  dependent's fingerprint. The artifacts become unreachable, each dependent re-resolves
  at the new version on its next read, and the cascade runs only if the recomputed bytes
  differ. That is early cutoff, and it is the part of the fingerprint spec that confuses
  every new reader.

- `SqliteStore.inputs_of()`, the transitive upward walk, mirroring `dependents_of()`.
  Both now take a `depth`. Both terminate on a cycle a corrupted database could contain
  rather than hanging. Also `deps_of_many()`, `refs_by_ids()`, `ids_of_recipe()`,
  `counts_by_recipe()` and `recipe_cost_rows()`.

- Generic recipe `max_age` freshness, backed by a distinct `refreshed_at` clock so a
  successful refresh with unchanged bytes becomes fresh without rewriting content
  history. Scheduling and domain calendars remain caller concerns. The new artifact
  column advances the unreleased SQLite schema to version 2.

- Content-addressed compute cache over a dependency graph discovered by execution: a
  suspending scheduler with constructive traces. Every `ctx.get` is recorded, and the
  artifact, its dep edges and the invalidation cascade land in one transaction.
- Three artifact states kept distinct — absent, stale, valid. A stale artifact is
  verified by recomputing its fingerprint from its stored edges, and returned without
  recomputing when it still matches. Invalidation never deletes.
- `SqliteStore`, WAL, connection per thread, writers serialized on `BEGIN IMMEDIATE`,
  with a configurable `max_artifact_bytes` (64 MiB) that raises rather than writing.
- Policy: an `on_miss` ceiling resolved against each recipe's own declaration, local
  `force` and transitive `force_deps`, a propagated wall-clock `deadline`, and batched
  reporting of every protected miss the walk could reach.
- `plan()`, which walks without computing and reports absent and stale separately.
- Codecs `json`, `arrow`, `arrow-series` and `npy`, each byte-deterministic, with
  partitioned output supported through `decode_parts`. The arrow codecs encode through
  pyarrow with pinned IPC options rather than through polars' writer, whose output is
  not stable across polars releases.
- Foreign recipes: a TOML manifest plus an executable, with inputs declared upfront so
  the graph can be cycle-checked and planned before anything runs. One JSON job file as
  the only argument; results read from a file, never from stdout.
- `SPEC.md` as the cross-language interoperability contract, with golden vectors for
  hashes and for codec bytes checked into the test suite.
- Two examples, one financial and one not, both exercised by the test suite.

- `arrow-year` and `arrow-year-pandas`, slicing a frame into one artifact per calendar
  year on a column named `date`, with `ArrowYearCodec(name=..., column=...)` for any other
  column. Byte-identical to each other, sorted so that a reordered frame encodes the same,
  reading timezone-aware timestamps in UTC, and refusing null dates. Appending a day
  rewrites one part and leaves the closed years untouched, which the tests assert end to
  end rather than assuming.
- An `arrow-pandas` codec, byte-identical to `arrow`, decoding to a pandas DataFrame.
  Either codec reads what the other wrote, so a cache is not partitioned by dataframe
  library, and a pandas consumer does not need polars installed.
- A `matview` command-line tool for inspecting a cache: `info`, `ls`, `show`, `stats`,
  `verify`, `cat` and `plan`. Read-only, `--json` everywhere, stdlib-only so it is present
  on a plain install. Metadata inspection imports nothing; `--recipes` is required only
  for a partial key and for `plan`.
- `SqliteStore(read_only=True)`, which requires the file to exist, creates no schema and
  leaves the journal mode alone.
- `inspect_database()`, which describes a database whatever version wrote it.
- `get_many` and `plan_many`: many roots in one walk, sharing a memo. A single root
  stops at its first blocked `ctx.get`; independent roots do not stop each other, which
  is what makes the spec's claim about cold runs — one report of everything that needs
  ingesting — actually true.
- `cache.stats.expired`, counting nodes whose `max_age` had elapsed. A reason rather
  than an outcome, so it is excluded from `lookups`: without it, a rebuild driven by a
  clock is indistinguishable from one driven by a changed input.
- `cache.stats`, and a `matview.scheduler` logger emitting one DEBUG event per node
  resolved. The counters separate a plain hit from a revalidation-without-recompute
  from a genuine rebuild, which a two-way hit/miss count hides.
- `MissingNode.path`, the resolution chain from the root, so a protected miss says which
  node wanted it rather than only that it is missing.
- `SqliteStore` refuses a database whose `schema_version` it does not implement, as it
  already did for `fingerprint_spec_version`.
- `matview --version`, on the top-level parser only. Several subcommands already take a
  `--version` that filters by recipe version, and the two meanings cannot share a
  command line.
- Python 3.14 is supported and classified. The full suite with every codec extra passes on
  it, and the golden codec fixtures match, so stored artifacts are portable across it.

### Fixed

- `SqliteStore.summary()` overcounted `compute_ms`. It summed the column over artifact
  rows, and every part of a partitioned value records the cost of the single compute that
  produced them all — so a three-part value was counted three times. It now groups by
  `(recipe, version, key_hash)` and reports a `values` count beside `artifacts`, so the
  deduplication is visible rather than implicit. Every cost aggregate added in this
  release does the same.

- Maintenance operations over large artifact sets no longer exceed SQLite's bound-variable
  limit, and cache verification now uses keyed rather than `OFFSET` pagination. SQLite
  JSON1 (included by default in SQLite 3.38+) is required and checked when opening a store.

- The codec-floor job silently verified a stale build of matview: `uv run --no-project
  --with .` reinstalls a cached wheel. It refreshes the build now, and a test asserts the
  installed codec set matches the checked-in fixture, because the symptom was invisible —
  fewer parametrized cases, reported as success.
- Arrow offset widths are now normalized, so the stored bytes no longer depend on which
  dataframe library produced the table. polars emits `large_string`, pandas 2.2 emits
  `string` and pandas 3.0 emits `large_string`, which made identical data hash differently
  by producer and moved with a pandas *minor* version. Nothing already stored changed:
  polars was already producing the wide variants, so the normalization is a no-op on that
  path and every existing digest holds.
- Enabling WAL could fail with "database is locked" when two processes opened the same
  new file at the same moment. SQLite does not invoke the busy handler for a
  journal-mode change, so `busy_timeout` did not cover it — the exact deployment the
  design targets. Found by the first cross-process test.
- `matview.__all__` named `CacheStats` and `BatchResult` without importing them, so
  `from matview import CacheStats` raised. A test now checks every name in every
  module's `__all__` actually resolves.

### Changed

- `BaseCodec.partitions` removed: it was read nowhere, and `decode_parts` refusing is the
  actual mechanism.
- The package now lives under `src/`, so the test suite exercises the installed
  distribution rather than the working directory and a packaging mistake fails locally
  rather than only for a user.

### Notes

- Every choice made where the design document was silent is recorded, with its
  reasoning, in [`DECISIONS.md`](DECISIONS.md).
- `plan_many()` remains roughly two orders of magnitude slower per root than
  `get_many()`, and the batching that was expected to close the gap does not: the loop it
  would batch is over the parts of one node, not over nodes. The real N+1 is across the
  depth-first descent and removing it is a scheduler change. Measured both ways; see
  DECISIONS.md D52 and [`docs/performance.md`](docs/performance.md).
