# Roadmap and desiderata

Things worth doing, in rough order of value per unit of effort. This is a list of
candidates, not commitments: entries are here because someone should decide about them,
not because they are scheduled.

Two standing constraints apply to everything below.

- The design spec's **[Not in v1](docs/design-spec.md)** list is not a backlog. Items on
  it were excluded on purpose, and the last section here says which ones and why they
  should stay excluded. Reconsidering one is a decision for the spec's author, not a
  ticket.
- **`SPEC.md` is frozen** except by bumping `fingerprint_spec_version`, which
  invalidates every existing cache. Nothing on this list should need to touch it.

---

## Tier 1 — gaps that are closer to defects than to features

**All four are done.** Kept here with their reasoning; see `CHANGELOG.md`.

### `schema_version` is written and never read

`SqliteStore` refuses to open a database whose `fingerprint_spec_version` does not match
the running build, because every lookup in it would silently miss. It writes
`schema_version` into the same `meta` table and then never reads it back. A future
schema change would therefore open an old database without complaint and operate
against the wrong shape.

The check is a handful of lines and belongs in the code *before* there is a v2 to
migrate from, so that the failure mode when v2 arrives is a clear refusal rather than
undefined behaviour. An actual migration path can come later; refusing is the thing
that has to exist first.

### `MissingArtifacts` says what is missing, not who wanted it

A protected miss reports `ohlcv(v1)[symbol='AAPL'] (protected)`. On a graph six levels
deep that leaves the reader to work out which indicator wanted it and why. The walk
already carries the in-flight stack, so recording the requesting path on `MissingNode`
turns a fact into a diagnosis at nearly no cost.

### The scheduler emits no telemetry

The only logger in the package is `matview.foreign`. There is no way to observe a hit
rate, see which node forced a recompute, or tell a revalidation-without-recompute from a
genuine rebuild — which are the three things anyone debugging a cache actually wants.

### The concurrency assumption has no test behind it

The spec says duplicate computation by concurrent runners is acceptable and expected to
be rare, and that idempotent writes handle it. The implementation backs that with
`BEGIN IMMEDIATE` and the rule 4 no-op path, and nothing in the suite races two writers
on the same node, in threads or in processes. It is a stated design assumption with no
evidence, which is where surprises live.

---

## Tier 2 — features that deliver promises already made

### `get_many` / `plan_many` — done

The spec's headline story for the `on_miss` ceiling is that a cold backtest "tells you
in a single run exactly which symbols and intervals need ingesting, instead of one per
attempt". `DECISIONS.md` D28 records how far batched reporting can actually reach today,
and a large part of that limit is an artifact of the API taking one root per call: a
recipe blocked on its first `ctx.get` cannot continue, but five hundred *independent*
roots in one walk can.

This is the highest value per line on the list, because it makes an existing promise
true rather than adding a new one.

### A calendar-year partitioned codec — done

Built as `arrow-year` and `arrow-year-pandas`. Shipped in matview rather than as an
example, with the reasoning in D42: the spec's "none of this belongs in the core" is about
the core, and the same spec puts partitioning in the codec layer, where the domain-neutral
core cannot see it. The write-amplification argument is asserted by tests at both the codec
and the storage level rather than taken on trust.

### Benchmarks at the spec's own scale — done, see [docs/performance.md](docs/performance.md)

The design cites roughly 5,000 symbols and 50 indicators, which is 250,000 artifact
rows. There are no measurements at all.

Investigating it found a defect rather than just slowness: seven `IN` clauses build one
SQL placeholder per artifact id, and SQLite's compiled limit is 32,766 on builds since
3.32 and 999 before that — so `drop()` over tens of thousands of artifacts is a hard
failure on a plausible interpreter, not a slow one. `iter_payloads` also paginated with
`OFFSET`, making `matview verify` quadratic. Both are fixed; the recorded baseline is in
[docs/performance.md](docs/performance.md) and the harness in `benchmarks/`. The plan that
drove the work is kept at [plans/250k-scale.md](plans/250k-scale.md).

What the baseline leaves open: `plan_many` runs at 291 roots/s against `get_many`'s
24,880, because the scheduler asks the store for one node's dependencies at a time.

### A CLI — done

`matview ls`, `plan`, `show`, `gc`, `drop`, `stats`. A cache that cannot be inspected
from a terminal is hard to trust. argos already uses typer, so there is a house pattern
to follow. The one design problem is that any CLI must import the caller's recipe
modules to know the registry, so it needs a `--recipes` argument or a config key.

### Property-based tests for canonicalization

`freqprob` already uses hypothesis, and key canonicalization is the ideal target: it is
the layer where a bug surfaces as a permanently cold cache rather than an exception, and
its properties are easy to state (order independence, default filling, integral-float
normalization, scalar-only rejection).

### A conformance harness for `SPEC.md`

`tests/golden_vectors.json` exists precisely so another implementation can be validated
against it. A small documented runner — feed it vectors, report mismatches — is what
turns that file from a regression test into an actual cross-language contract.

### An `arrow-pandas` codec — done

argos, the first intended consumer, is pandas + pyarrow + duckdb; the `arrow` codec
decodes to polars. Built, and the byte-identity claim turned out to need work first: the
Arrow offset width was being chosen by whichever library produced the table, so identical
data hashed differently under polars and pandas 2.2. Normalizing it (D39) made the claim
true, and it is now pinned by golden fixtures and checked at the version floor.

---

### Lineage, impact and cost — planned, see [plans/graph-queries.md](plans/graph-queries.md)

matview records a complete observed DAG and offers almost no way to interrogate it:
`deps_of` returns one level, `dependents_of` walks downward only and returns bare ids, and
there is no upward walk at all — so "which raw inputs produced this number?" cannot be
asked. `compute_ms` is recorded on every artifact and never aggregated along the graph it
was recorded against.

Planned as three features over one shared foundation: lineage (the input closure), impact
(what a digest change, a version bump or a drop would do), and cost (embodied compute, a
per-recipe rollup, and the critical path). The plan also records a verified trap —
`compute_ms` is carried whole on every part of a partitioned value, so summing over
artifact rows overcounts, which `summary()` does today.

## Tier 3 — architecture

### A remote `Store`

The storage seam was designed for this: `store()` takes the dep edges so that artifact,
edges and cascade are one request a server runs in a transaction, and fingerprinting is
client-side so that two backends cannot disagree about a hash.

Two shapes are possible and they are not variations on one thing.

**A live remote store** is what the seam anticipates. Implementation is largely
mechanical, and the spec already warns against the failure mode: do not design an API
where the client walks the graph over the wire.

**Bulk artifact upload/download** is replication rather than storage, and it is much
easier to deploy across local runs, GitHub Actions and Hugging Face Jobs. It also raises
a question v1 has no answer to: two machines compute the same `(recipe, key_hash, part)`
and produce different bytes — float nondeterminism across architectures is the spec's
own example — and merging two databases then requires a rule for which digest wins. Rule
4 covers a matching row and says nothing about a conflicting one. **That rule needs
writing before the sync tool, not after.**

### Schema migrations

Once `schema_version` is checked (tier 1), the question of what to do about a mismatch
other than refusing becomes real. Not urgent while there is exactly one schema version.

---

## Deliberately not doing

From the spec's "Not in v1", unchanged and still excluded:

- **Distributed locking or work leases.** Idempotent writes handle duplicate work. A
  lease table can be added later without touching the artifact schema.
- **A scheduler or queue.** `on_miss="enqueue"` writes a `task` row and nothing consumes
  it, as specified.
- **Incremental or append-only computation.** Every recompute is a full recompute.
  Incrementality is a large design of its own and must not be smuggled in.
- **Point-in-time correctness.** If an as-of dimension is ever needed it is an ordinary
  key field, not a core concept.
- **A web UI, a daemon, or a server.**

And one item the spec defers rather than excludes:

- **A declared lookback window,** so that appending one observation avoids rebuilding a
  full history. The spec says the decision waits for a real workload to measure, and
  rests on `compute_ms`, which is recorded faithfully. Build nothing toward it until
  argos has produced numbers.

Parallel execution of independent roots is not on the spec's exclusion list, but the
scheduler is deliberately plain recursion — "graph depth in practice is single digits,
so coroutines or trampolining are unnecessary complexity". Any parallelism should be a
considered decision against that sentence, not a side effect of something else.

---

## Waiting on the repository owner

- Rename the default branch to `main`.
- Install `ops/workflows/ci.yml` and `ops/workflows/docs.yml` under `.github/workflows/`.
  Until then, every change to how CI checks things needs a hand-off, and the `src/`
  layout move is blocked because it breaks the installed workflow's `mypy matview` step.
- Enable GitHub Pages with source **GitHub Actions**, or the docs deploy fails while the
  build passes.
