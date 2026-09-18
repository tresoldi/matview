# matview — design spec and build prompt

2026-09-18 · @Someone

## Instructions to the implementing agent

This document is the complete specification for a Python library called **matview** (short for *materialized view*). Build it.

- **Read the whole document before writing any code.** Several rules here look like mistakes and are not; each one is marked where it occurs, with the reason. Do not fix them.
- Build in this order, testing each stage before starting the next: canonicalization and fingerprints → schema and `SqliteStore` → registry and context → scheduler → policy → codecs → foreign recipes.
- The section **Non-goals and hard rules** lists ten invariants. Each is enough on its own to make the library silently wrong rather than loudly broken. Write tests for all ten before implementing the behaviour they constrain.
- Where this document specifies behaviour, implement it as specified rather than improving it. Where it is silent, use your judgement and record each such choice in a `DECISIONS.md` at the repo root.
- The one part that cannot be revised later without invalidating every existing cache is the fingerprint specification. If you believe it is wrong, stop and say so rather than changing it.
- Anything listed under **Not in v1** must not be implemented, including as a hidden fast path or an unused code path.

## What this is

A Python library that makes derived values transparently cacheable across a dependency graph. You declare *recipes*: functions that compute a value from parameters and from other recipes' outputs. You ask the cache for a value; it returns the stored one if it is current, and computes it if not, recursively resolving whatever that computation turns out to need. Results are memoized in SQLite and reused across processes, machines and runs.

The problem it solves is the one every pipeline eventually grows badly. You have expensive derived values, they depend on each other in a graph, the inputs change, and you need to know what is still good. Hand-rolled solutions either recompute everything, which is slow, or trust stale data, which is wrong.

This is a build system for data rather than files, in the sense of *Build Systems à la Carte* (Mokhov, Mitchell, Peyton Jones): a **suspending scheduler** with **constructive traces**. Dependencies are discovered by execution rather than declared, and staleness is decided by content digests rather than timestamps.

The first consumer is a financial pipeline (OHLCV bars and a growing set of derived indicators, computed across local runs, GitHub Actions and Hugging Face Jobs). The design is deliberately domain-neutral: the core knows about opaque keys and opaque bytes, and nothing about time series, symbols or dates. A second intended consumer is computational linguistics work, where artifacts are distance matrices and fitted trees rather than series.

## Core model

Five nouns. The whole system is these plus the rules relating them.

**Recipe.** A named, versioned computation. Identified by `(name, version)`. Carries a codec, an optional set of indexed key fields, and a policy ceiling. A recipe is a *kind* of computation, not a particular result.

**Key.** The parameters of one invocation, as a flat dict of scalars. The core never interprets keys; it canonicalizes and hashes them. `{"symbol": "AAPL", "window": 200}` is a key. A recipe may declare some key fields as `index`, which promotes them to real columns for queryability, but they remain ordinary key fields semantically.

**Artifact.** One stored result: `(recipe, key_hash, part)` → bytes. `part` is an opaque partition label, `NULL` for unpartitioned values. Partitioning is a codec concern, not a scheduler concern; a time-series codec may emit `"2026"` while a whole-value codec emits nothing.

**Digest.** The hash of an artifact's stored bytes. This is what downstream artifacts key on, and it is what makes early cutoff possible.

**Fingerprint.** The hash of the inputs that produced an artifact: its own recipe version plus the digests of everything it read. Stored on the artifact. An artifact is current when its recomputed fingerprint matches the stored one.

The DAG is never declared. When a recipe runs, every `ctx.get` call it makes is recorded, and the resulting edges are written to a `dep` table alongside the artifact in one transaction. The graph is therefore always an accurate record of what actually happened, cycles are detected by the in-flight stack rather than by static analysis, and a recipe that takes a different branch on different parameters records different dependencies, correctly.

## Canonicalization and fingerprints

This section is a specification, not an implementation detail. It is the layer that determines whether a non-Python implementation can ever share a cache. Two implementations that disagree here will not error; they will silently miss on every lookup and look like an ordinary cold cache. **Write it down before writing the code, and change it only by bumping a format version.**

### Hash function

BLAKE2b, 32-byte digest, no key or salt. Hex-encoded lowercase where stored as text.

### Key canonicalization

Keys serialize as JSON Canonicalization Scheme (RFC 8785): keys sorted by UTF-16 code unit, no insignificant whitespace, UTF-8 output. On top of JCS:

- Key fields must be scalars: string, int, float, bool, or null. No nested structures. This is a restriction, and it is deliberate; nested params make canonicalization far harder to get right across languages.
- A float whose value is integral is normalized to an int. `{"window": 14.0}` and `{"window": 14}` must produce the same hash. Getting this wrong silently splits a cache in two.
- `NaN` and infinities are rejected at registration and at call time.
- Omitted parameters are filled from the recipe's declared defaults **before** hashing, so `sma(symbol="AAPL", window=200)` and `sma(symbol="AAPL", window=200, interval="1d")` are the same artifact when `interval` defaults to `"1d"`.

`key_hash = BLAKE2b(jcs(key))`.

### Digest

`digest = BLAKE2b(artifact_bytes)`, over the encoded payload exactly as stored.

### Fingerprint

```
fingerprint(A) = BLAKE2b(
    jcs({
      "v":       1,
      "recipe":  name(A),
      "version": version(A),
      "key":     key_hash(A),
      "part":    part(A),
      "deps":    [ [d.recipe, d.key_hash, d.part, d.digest]
                   for d in sorted(deps(A)) ]
    })
)
```

Dependencies sort by `(recipe, key_hash, part)`. The crucial asymmetry, which will look like a bug to anyone reading the code: **A's own recipe version is in the fingerprint; its dependencies' recipe versions are not.** Dependencies contribute only their content digests.

This is a deliberate choice and the comment in the code should say so. It buys early cutoff: if a dependency is recomputed under a new version and produces identical bytes, nothing downstream is invalidated. It costs the ability to force a downstream rebuild by bumping an upstream version, which is occasionally wanted during development and is served instead by `force_deps`.

## Schema

```sql
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE meta (
  k TEXT PRIMARY KEY,
  v TEXT NOT NULL
);  -- holds schema_version, fingerprint_spec_version

CREATE TABLE recipe (
  id          INTEGER PRIMARY KEY,
  name        TEXT NOT NULL,
  version     INTEGER NOT NULL,
  codec       TEXT NOT NULL,
  source_hash TEXT,                  -- advisory only, never fingerprinted
  UNIQUE (name, version)
);

CREATE TABLE artifact (
  id           INTEGER PRIMARY KEY,
  recipe_id    INTEGER NOT NULL REFERENCES recipe(id),
  key_hash     TEXT NOT NULL,
  key_json     TEXT NOT NULL,        -- canonical form, for introspection
  part         TEXT,                 -- NULL when unpartitioned
  digest       TEXT NOT NULL,
  fingerprint  TEXT NOT NULL,
  valid        INTEGER NOT NULL DEFAULT 1,
  n_bytes      INTEGER NOT NULL,
  data         BLOB NOT NULL,
  computed_at  INTEGER NOT NULL,
  compute_ms   INTEGER,
  UNIQUE (recipe_id, key_hash, part)
);

CREATE INDEX artifact_invalid ON artifact(valid) WHERE valid = 0;
CREATE INDEX artifact_recipe  ON artifact(recipe_id);

-- indexed key fields, promoted for queryability
CREATE TABLE artifact_key (
  artifact_id INTEGER NOT NULL REFERENCES artifact(id) ON DELETE CASCADE,
  field       TEXT NOT NULL,
  value       TEXT NOT NULL,
  PRIMARY KEY (artifact_id, field)
);
CREATE INDEX artifact_key_lookup ON artifact_key(field, value);

-- the observed DAG; written post-hoc, never declared
CREATE TABLE dep (
  artifact_id     INTEGER NOT NULL REFERENCES artifact(id) ON DELETE CASCADE,
  dep_artifact_id INTEGER NOT NULL REFERENCES artifact(id),
  dep_digest      TEXT NOT NULL,     -- digest as seen at compute time
  PRIMARY KEY (artifact_id, dep_artifact_id)
);
CREATE INDEX dep_reverse ON dep(dep_artifact_id);

-- on_miss="enqueue" writes here; nothing in v1 consumes it
CREATE TABLE task (
  id           INTEGER PRIMARY KEY,
  recipe       TEXT NOT NULL,
  version      INTEGER NOT NULL,
  key_json     TEXT NOT NULL,
  part         TEXT,
  requested_at INTEGER NOT NULL,
  UNIQUE (recipe, version, key_json, part)
);
```

`dep_reverse` is the index the invalidation walk lives on; without it the cascade degrades to a table scan per level.

Note that `artifact` is keyed on `recipe_id`, and `recipe` is unique on `(name, version)`. A version bump therefore creates a *new* recipe row, and old artifacts remain in place, unreachable but intact. Dropping the version back finds them again. `gc()` is what actually removes them.

**Payloads live entirely in the row.** There is no spill path to an object store in v1, and no `uri` column, so `data` is `NOT NULL`. Instead, `store()` enforces a configurable `max_artifact_bytes` (default 64 MiB) and raises when a payload exceeds it, rather than silently writing a blob large enough to make SQLite unpleasant. Raising loudly is the point: it surfaces a codec or partitioning problem at the moment it appears rather than as mysterious slowness months later.

If spilling is added later, it goes in as a nullable `uri` column plus content-addressed storage keyed on the digest, which also deduplicates identical payloads across recipes for free. Do not build any part of that now.

## States and invalidation

An artifact is in exactly one of three states, and conflating the first two is the single most common way to get this wrong:

| State | Condition | Action on read |
| --- | --- | --- |
| **absent** | no row | must compute |
| **stale** | row exists, `valid = 0` | must verify; may or may not recompute |
| **valid** | row exists, `valid = 1` | return it |

Stale is not absent. A build system that deletes artifacts on invalidation collapses these two and permanently loses early cutoff. **Never delete on invalidation.** Clear the flag and keep the bytes.

### The write path

`store()` does the cascade, not the read path. When an artifact is written:

1. Compute its digest.
2. If a row already exists with the same `(recipe_id, key_hash, part)` **and** the same digest and fingerprint, do nothing. Redundant work from a duplicate runner must not rewrite a good row, because float nondeterminism across machines can yield equivalent-but-different bytes and flip every downstream fingerprint for no reason.
3. Otherwise upsert the row, replace its `dep` edges, and if the digest changed, walk `dep_reverse` transitively and set `valid = 0` on every dependent.
4. All of the above in **one transaction**. A reader must never see an artifact whose dep edges have not landed.

The recursive walk:

```sql
WITH RECURSIVE affected(id) AS (
  SELECT :changed_id
  UNION
  SELECT d.artifact_id FROM dep d JOIN affected a ON d.dep_artifact_id = a.id
)
UPDATE artifact SET valid = 0
WHERE id IN (SELECT id FROM affected WHERE id != :changed_id);
```

### The read path

Reading a stale artifact resolves its dependencies (which are usually already valid, so this costs nothing), recomputes the fingerprint, and compares it to the stored one. If they match, flip `valid = 1` and return the existing bytes without recomputing. If they differ, recompute, and `store()` handles whatever cascade follows.

### Worked example

A vendor revises one AAPL bar from 2019. You force `ohlcv(AAPL)`, the bytes change, and every dependent is marked stale in one transaction. Next time the backtester asks for `sma(AAPL, 200)`, the scheduler sees stale, resolves `ohlcv` (valid, no work), recomputes the SMA, and finds it byte-identical because a 200-day window never reaches 2019. Digest unchanged, `valid` flips back to 1, and the cascade stops there. Nothing further down the graph is touched.

This is early cutoff, and it is the entire payoff of digest-based fingerprints over version-based ones.

### Non-determinism

A recipe whose output varies run to run with identical inputs (sampling, unseeded RNG, wall-clock, a network fetch) defeats early cutoff and cascades unconditionally. This is correct behaviour but expensive. Recipes declare `deterministic=False` so the cost is visible, `plan()` can report it, and nobody spends an afternoon wondering why one branch always rebuilds.

## Python API

### Registration

```python
from matview import recipe, Cache, SqliteStore

@recipe(version=1, codec="arrow", index=("symbol",),
        on_miss="raise", deterministic=False)
def ohlcv(ctx, *, symbol: str, interval: str = "1d", adjust: str = "split"):
    return vendor.bars(symbol, interval, adjust)

@recipe(version=3, codec="arrow", index=("symbol",))
def sma(ctx, *, symbol: str, window: int, interval: str = "1d"):
    bars = ctx.get("ohlcv", symbol=symbol, interval=interval)
    return bars["close"].rolling_mean(window)
```

Registration is import-time and global. The registry maps `name` to the function, its declared version, codec, index fields and policy ceiling. Parameter defaults are read off the signature and applied before key hashing.

Note that `interval` and `adjust` are ordinary key fields. Anything that changes the values while leaving the recipe identical belongs in the key, not in the recipe name. This is how the design avoids a special-cased notion of "series identity".

### Reading

```python
cache = Cache(store=SqliteStore("argos.db"))

val = cache.get("sma", symbol="AAPL", window=200)          # decoded object
raw = cache.get_bytes("sma", symbol="AAPL", window=200)    # stored bytes
```

`get` returns the decoded object; the codec is a property of the recipe, so decoding is a lookup and never an inference. `get_bytes` is the escape hatch and must be used internally for replication and any future HTTP serving, since decode-then-reencode both wastes work and risks failing to round-trip.

Inside a recipe, `ctx.get` decodes by the same rule. A recipe body should never contain a manual decode call.

### Scheduler

Plain recursion. The context carries an explicit stack of in-flight `(recipe, key_hash, part)` triples; re-entry is a cycle and raises with the full path. Graph depth in practice is single digits, so coroutines or trampolining are unnecessary complexity. Each `ctx.get` appends the resolved dependency to the context's collected set, and that set is handed to `store()` with the artifact.

### Planning and maintenance

```python
cache.plan("sharpe", symbol="AAPL")   # walk without computing
cache.gc()                            # drop superseded versions with no dependents
cache.drop("sma", version=2)          # remove one recipe version's artifacts
```

`plan` runs the resolution walk in a mode where nothing computes and every miss is collected. It reports absent and stale separately, because stale work is often near-free while absent work at a protected root means you cannot proceed at all.

### Three behaviours to fix from the start

- A failed compute is **never** cached, in any form. A poisoned artifact costs far more than a recompute.
- A recipe returning `None` or an empty frame has produced a **value**, not a miss. Presence checks are on row existence, never on truthiness.
- The SQLite connection is per-thread with WAL on. The decorator registry being global will tempt an implementer into making the `Cache` global too; it must not be.

## Policy

```python
cache.get("sma", symbol="AAPL", window=200,
          on_miss="compute",   # compute | enqueue | raise
          force=False,
          force_deps=False,
          deadline=30.0)
```

### on\_miss is a ceiling, not a default

This is the rule most likely to be implemented wrongly, because the obvious reading of "a call-site argument" is wrong.

If `on_miss` were purely a call-site default, a backtester asking for `sharpe` with `on_miss="compute"` would, six levels down, hit a missing `ohlcv` and fire a paid vendor request the caller never mentioned and could not reasonably anticipate.

So the effective policy at any node is **the less permissive of what the caller asked for and what the recipe declares**. Ordering from permissive to strict: `compute` < `enqueue` < `raise`. A caller may restrict further; a caller may never loosen. A recipe declaring `on_miss="raise"` is protected everywhere it appears as a transitive dependency.

Ingesting is then an explicit top-level act: the fetch script calls `cache.get("ohlcv", symbol=..., on_miss="compute")` directly, which reads as deliberate intent rather than a leaked default.

### force is local

`force=True` recomputes **that node only**. It must not be transitive. If it were, forcing one indicator during development would refetch every root beneath it.

`force_deps=True` handles the rare deep case, and the ceiling rule still applies, so protected roots are skipped even then.

### deadline propagates

`deadline` is wall-clock seconds, converted to an absolute monotonic instant at the top-level call and carried down the recursion. Each `ctx.get` checks the remaining budget before descending and raises `DeadlineExceeded` when the budget is already exhausted.

A deadline is deliberately preferred over a "compute budget" in abstract cost units, because per-recipe cost estimates are guesses that go stale, whereas elapsed time is a measurement. Recorded `compute_ms` can inform a future estimator, but it is not load-bearing in v1.

### Misses are reported in batch

Do not raise on the first protected miss. Walk as much of the graph as possible, collect **every** protected node that came up missing, and raise once with the full set attached to the exception.

A cold backtest then tells you in a single run exactly which symbols and intervals need ingesting, instead of one per attempt. This turns the protection from an obstacle into a planning tool, and it is the same machinery that `plan()` uses.

## Codecs

A codec encodes a Python object to bytes and back, and optionally splits a value into parts. Codecs are named and versioned, and the name is recorded per recipe.

```python
class Codec(Protocol):
    name: str
    def encode(self, value) -> bytes | dict[str, bytes]: ...
    def decode(self, data: bytes) -> object: ...
```

Returning a dict means partitioned output, with the dict keys becoming `part` labels. A codec that returns plain bytes produces one unpartitioned artifact. Partitioning is therefore entirely a codec concern; the scheduler treats `part` as an opaque label throughout.

v1 codecs: `arrow` (Arrow IPC → polars DataFrame), `npy` (→ numpy ndarray), `json` (→ dict). Each ships as an optional extra so the core installs with no heavy dependencies, which matters if this is published.

### The byte-canonical requirement

Because fingerprints key on content digests, **a codec must produce identical bytes for identical input, on every machine, every time.** A codec that fails this defeats early cutoff silently: nothing errors, the cascade simply never stops.

Concrete rules:

- **Parquet is disqualified out of the box.** It stamps a creation timestamp and writer version into the file, so two encodings of the same table differ. Arrow IPC with compression off, fixed schema field ordering and no custom metadata is the safe default.
- The `json` codec sorts keys and pins float formatting (shortest round-trip repr, no locale dependence).
- No codec may embed a timestamp, hostname, PID, or library version.
- Compression, if ever added, must be at a pinned level with a pinned implementation, or it silently breaks the guarantee across environments.

The test suite must include, for every codec, a round-trip determinism test asserting `encode(decode(encode(x))) == encode(x)` byte-for-byte, plus a cross-version check in CI.

### Time-partitioned codec (finance, illustrative)

The financial case wants calendar-year partitions, which is a codec built on `arrow` that slices by year. The reasoning is write amplification against a future remote store rather than anything about SQLite: with roughly 5,000 symbols and 50 indicators, a whole-series encoding is around 40 KB and a daily append rewrites all of it, pushing on the order of 10 GB per refresh. Yearly parts rewrite only the current year, cutting that to roughly 500 MB. Monthly would cut it further but multiplies row count by 240 and makes range reads stitch many pieces.

Closed years become immutable, so they can be cached client-side and never re-fetched. None of this belongs in the core.

## Foreign recipes

Recipes may be written in any language. Unlike Python recipes, they **declare their dependencies upfront** in a manifest rather than discovering them at runtime. This is the privileged case, not a degraded one: a declared graph can be cycle-checked and planned before anything executes, whereas a Python recipe's graph is only knowable by running it. The documentation should say so, and present dynamic discovery as the escape hatch.

### Manifest

A manifest is data, not code, so that registering a foreign recipe never requires its language to be installed. One TOML file per recipe, discovered by directory scan:

```toml
name = "phylo_tree"
version = 2
codec = "json"
deterministic = false
index = ["dataset"]
command = ["Rscript", "recipes/phylo_tree.R"]
source = "recipes/phylo_tree.R"      # hashed into source_hash

[params]
dataset = { type = "string" }
model   = { type = "string", default = "GTR" }

[[inputs]]
name   = "matrix"
recipe = "distance_matrix"
key    = { dataset = "${dataset}" }   # mapped from this recipe's own key
```

The host reads these at startup and registers them exactly as if a decorator had. Because a foreign recipe is usually a single file rather than a package with an import graph, hashing `source` is reliable here in a way it is not for Python, and the host should warn when the hash changes without a version bump.

### Invocation contract

One JSON job file, passed as the **only** argument. Never parameters on the command line, never positional input paths; both fall apart at a dozen inputs or a nested params dict.

```json
{
  "params":  { "dataset": "ie-ielex", "model": "GTR" },
  "inputs":  { "matrix": "/tmp/cc-8a1f/matrix.arrow" },
  "output":  "/tmp/cc-8a1f/out.json",
  "deadline_s": 120
}
```

The host resolves all declared inputs, materializes them as files, writes the job file, and invokes. The process writes its result to `output` and exits 0.

Rules the host enforces:

- **stdout and stderr are logging only.** The result is read from `output`, never from stdout, so a stray `print` in an R script cannot corrupt a value.
- Nonzero exit, a missing output file, or an unparseable one is a compute failure and is **not cached**. A poisoned artifact from a half-written output is much worse than a recompute.
- The job file is left on disk on failure, so a failed job is reproducible by hand by rerunning the same command.
- `deadline_s` is the remaining budget from the propagated deadline; the host also enforces it by killing the process.

Since the manifest is the user-facing surface for everyone not writing Python, it wants to be stable early.

## Storage seam

v1 ships **only** `SqliteStore`. But the interface is defined now, because retrofitting a transaction boundary is far harder than designing for one.

```python
class Store(Protocol):
    def fetch(self, recipe: str, version: int, key_hash: str,
              part: str | None) -> Artifact | None: ...

    def store(self, artifact: Artifact,
              deps: Sequence[DepEdge]) -> StoreResult: ...

    def dependents_of(self, artifact_ids: Sequence[int]) -> list[int]: ...

    def invalidate(self, artifact_ids: Sequence[int]) -> int: ...

    def revalidate(self, artifact_id: int) -> None: ...

    def drop(self, recipe: str, version: int | None = None) -> int: ...

    def query(self, recipe: str | None = None,
              **indexed_fields) -> list[ArtifactRef]: ...
```

Two constraints make this interface worth having rather than being ceremony:

**`store()` takes the dep edges.** It does not expose a separate `add_deps` call. Artifact, edges and the invalidation cascade must land in one transaction, and if the caller made two calls an HTTP-backed store could never make that atomic. Putting the transaction boundary inside the interface is precisely what keeps a remote implementation possible.

**Fingerprinting is client-side, always.** The store is handed finished digests and fingerprints; it never computes them. If a local SQLite store and a future HTTP store could disagree about a hash, cross-backend lookups would miss silently and look like cold cache. The spec governs the client; backends only persist what they are given.

The corollary for a future remote backend: `store()` becomes one request carrying artifact, edges and the cascade instruction, and the server executes it in a transaction. Do not design an API where the client walks the graph over the wire.

## Non-goals and hard rules

### Not in v1

- **Distributed locking or work leases.** Duplicate computation by concurrent runners is acceptable and expected to be rare. Idempotent writes handle it. A lease table can be added later without touching the artifact schema.
- **Remote backends.** The seam exists; the implementation does not.
- **A scheduler or queue.** `on_miss="enqueue"` writes a task row; nothing in v1 consumes it.
- **Incremental or append-only computation.** Every recompute is a full recompute. Incrementality is a large design of its own and must not be smuggled in.
- **Point-in-time correctness.** If an as-of dimension is ever needed, it is an ordinary key field, not a core concept.
- **A web UI, a daemon, or a server.**

### Rules the implementation must not violate

These are the ones where a plausible-looking shortcut silently breaks correctness:

1. Never delete an artifact on invalidation. Clear `valid`, keep the bytes.
2. Never cache a failed compute, in any form.
3. Artifact, dep edges and cascade land in one transaction or not at all.
4. Never rewrite a row whose digest and fingerprint already match.
5. `force` is local; only `force_deps` is transitive.
6. `on_miss` resolves to the strictest of caller and recipe, never the caller's alone.
7. Digests come from codec output; codecs must be byte-deterministic.
8. A dependency's recipe version never enters a dependent's fingerprint.
9. Empty or `None` results are values, not misses.
10. Never use pickle. It would make the cache Python-only and unsafe to share, which forecloses the whole foreign-recipe design.

### Deliverables

- `matview/` package: registry, context, scheduler, policy, codecs, `SqliteStore`.
- A `SPEC.md` containing the canonicalization and fingerprint section verbatim, versioned independently of the code and recorded in the `meta` table as `fingerprint_spec_version`.
- A `DECISIONS.md` recording every choice made where this document was silent.
- Test suite covering: cycle detection, early cutoff (the worked example above, asserted end to end), the ceiling rule for `on_miss`, batched miss reporting, codec byte-determinism, transaction atomicity under a simulated mid-write crash, float key normalization, and stale-but-valid revalidation without recompute.
- Two examples, one financial and one not, to keep domain assumptions out of the core.

### Naming

The package is **matview**, short for *materialized view*. An incrementally revalidated materialized view is the closest existing thing to what this library provides, and the name should read as obvious to anyone from a database background. Import as `matview`; the distribution name on PyPI is `matview`.

### Still open

One question is deliberately deferred rather than unresolved: whether recipes may declare a lookback window so that appending one new observation avoids rebuilding a full history. Full recompute stands for v1, and the decision will be made once there is a real workload to measure. Build nothing toward it in the meantime, but do record `compute_ms` faithfully, since that measurement is what the decision will rest on.

Everything else is settled. `on_miss="enqueue"` and the `task` table ship as specified even though nothing in v1 consumes the rows.
