# Decisions

The design spec says: *"Where this document specifies behaviour, implement it as
specified rather than improving it. Where it is silent, use your judgement and record
each such choice in a `DECISIONS.md` at the repo root."*

This is that file. Each entry states the gap, the choice and the reason. Entries
marked **provisional** are ones where the choice is cheap to reverse now and expensive
later, so they are flagged for confirmation rather than settled quietly.

---

## D1 — Python floor is 3.11 *(confirmed)*

3.11 gives `tomllib` in the stdlib, which the foreign-recipe manifest reader needs,
and `ExceptionGroup`, which fits batched miss reporting. Supporting 3.10 costs a
`tomli` dependency in the core, which the spec asks to keep dependency-free.

## D2 — Packaging: `hatchling` + `uv`, src-less layout *(provisional)*

Licensed MIT (confirmed).

Distribution name `matview`, import name `matview` (spec fixes both). The name is
unregistered on PyPI as of 2026-09-18. Codecs ship as extras: `matview[arrow]`,
`matview[npy]`, `matview[json]` (json is stdlib-only and therefore always available;
the extra exists only so the three read symmetrically), plus `matview[all]`.

## D3 — JCS comes from the `rfc8785` package, not a hand-rolled serializer *(confirmed)*

The spec fixes RFC 8785 as the canonicalization scheme and warns that two
implementations disagreeing here fail silently. The hard part is ECMAScript number
formatting, where Python's `repr` and the ES6 shortest-round-trip algorithm diverge on
some exponent forms. `rfc8785` is pure Python, has no dependencies of its own, and is
the reference implementation. The cost is one core dependency; the risk of getting
number formatting subtly wrong by hand is worse, and it is precisely the kind of error
the spec says shows up as a cold cache rather than an exception.

Golden hash vectors are checked into the repo regardless, so the dependency can be
swapped later without silently changing any hash.

## D4 — `deterministic=False` is advisory in v1 *(confirmed)*

The spec introduces the flag so "the cost is visible, `plan()` can report it, and
nobody spends an afternoon wondering why one branch always rebuilds". It does not say
the flag changes scheduling. Making it force a recompute on every read would refetch
paid vendor data on every call, which contradicts the ingest design elsewhere in the
spec. So: the flag is recorded, surfaced by `plan()`, and does not alter the read
path. A stored non-deterministic artifact is trusted exactly like any other.

## D5 — Failure to compute leaves no trace at all

Rule 2 says a failed compute is never cached "in any form". Read as: no row, no
tombstone, no negative cache, no failure counter in the database. Failures propagate
as exceptions and are logged, nothing more. `compute_ms` is recorded only on success.

## D6 — `compute_ms` measures the recipe body only

The spec defers the lookback-window question and says the decision "will rest on"
`compute_ms`, so the measurement must mean something specific. It is wall-clock
milliseconds around the recipe function call itself, excluding dependency resolution
(which is attributed to the dependency) and excluding encode/store time. Monotonic
clock, rounded to the nearest millisecond.

## D7 — `computed_at` is Unix epoch seconds, UTC

The schema types it `INTEGER` without saying what it counts. Epoch seconds, not
milliseconds, matching the column name's lack of a unit suffix where `compute_ms` has one.

## D8 — `artifact_key.value` stores the JCS scalar form

The column is `TEXT NOT NULL` but indexed key fields may be ints, floats or bools.
Values are stored as their canonical JCS scalar rendering (so `200`, not `"200"`;
`true`, not `"True"`), which keeps `query()` results consistent with `key_json` and
keeps the promotion lossless. `NULL`-valued key fields are not promoted, since the
column is `NOT NULL`; they remain in `key_json`.

## D9 — Errors live in `matview.errors` and share a base

`MatviewError` as the root, with `CycleError`, `DeadlineExceeded`, `MissingArtifacts`
(carrying the full batched set), `ArtifactTooLarge`, `CodecError`, `ForeignRecipeError`.
One root means a caller can catch the library without catching everything.

## D10 — Logging via the stdlib, under the `matview` logger, no handler installed

A library that configures logging is a library that fights its host.

## D11 — Partitioned artifacts are read whole by default, via a codec merge hook *(confirmed)*

The spec lets a codec return `dict[str, bytes]` for partitioned output but gives
`decode` a single-bytes signature, gives `get()` no `part` argument, and specifies no
way to put parts back together. The gap is closed on the codec side, where the spec
already puts partitioning ("Partitioning is a codec concern, not a scheduler concern"):

```python
class Codec(Protocol):
    name: str
    def encode(self, value) -> bytes | dict[str, bytes]: ...
    def decode(self, data: bytes) -> object: ...
    def decode_parts(self, parts: dict[str, bytes]) -> object: ...   # partitioned codecs only
```

- `get(...)` with no `part` on a partitioned recipe fetches every part and calls
  `decode_parts`, returning the whole value. Parts are passed in sorted-label order so
  a merge cannot depend on fetch order.
- `get(..., part="2026")` returns that one part, decoded with `decode`.
- A dependent that selects a single part records a dep edge on **that part only**, so a
  recipe reading one closed year is not invalidated by a write to the current year. A
  dependent reading the whole value records one edge per part.
- A codec that never partitions does not implement `decode_parts`; calling it is a
  `CodecError`, not a silent fallback.

`part` stays opaque to the scheduler throughout: the core sorts labels and counts edges,
and never interprets them.

## D12 — Delivery: stage by stage on one branch *(confirmed)*

v1 is built in the spec's prescribed order on `claude/sleepy-planck-diq55j`, one commit
per stage, each stage green before the next begins. The repository has no default-branch
content, so there is nothing to merge into yet and no PR is opened until asked.

## D13 — Foreign manifest `${...}` substitutes typed scalars, interpolates otherwise *(confirmed)*

A manifest value that is *exactly* `"${name}"` is replaced by that parameter's value
with its type intact, so `window = "${window}"` yields the integer `14`. A value with
any surrounding text (`"${dataset}-v2"`) is string interpolation and yields a string.
These feed `key_hash`, where `14` and `"14"` are different artifacts, so the
distinction is the same class of hazard as the integral-float rule.

## D14 — `arrow` decodes to a DataFrame; `arrow-series` decodes to a Series *(confirmed)*

The spec's own `sma` example returns a polars Series under `codec="arrow"`, but the
spec never says how a Series round-trips, and this is byte-visible.

`arrow` accepts a DataFrame or a Series on encode (a Series becomes a one-column table
under its own name, or `""` when unnamed) and always decodes to a DataFrame, so the
spec's example works exactly as written. `arrow-series` is the same encoding with a
decode that returns the single column as a Series, for a recipe whose output genuinely
is a series.

Discriminating by inspecting the table instead would be inference, and the spec is
explicit that "the codec is a property of the recipe, so decoding is a lookup and never
an inference". Two names is the version of that which cannot guess wrong.

## D15 — Dependency sort order is fully defined, including `NULL` parts

The spec says dependencies sort by `(recipe, key_hash, part)` but `part` is nullable and
the sort has to be reproducible in another language. Strings compare by UTF-16 code unit,
matching JCS, and a `NULL` part sorts before every string part. Implemented by comparing
UTF-16BE encodings, which orders byte-wise exactly as JCS orders code unit sequences.

## D16 — Integral floats normalize to int only within the exact-integer range

The spec requires `14.0` and `14` to hash identically. Applied without a bound, `1e300`
would become a 301-digit integer, which no IEEE-754 double can represent and which
serializes differently from what any other language would produce for that same double.
So the normalization applies when the float is integral **and** `abs(v) <= 2**53`, where
the two forms are genuinely the same number. Outside that range the float keeps its
ES6 rendering.

## D17 — The no-op write still fixes `valid` and reconciles dep edges

Rule 4 forbids rewriting a row whose digest and fingerprint already match, and the spec
gives the reason: a duplicate runner must not flip downstream fingerprints for no reason.
Read literally as "do nothing at all", it has two holes:

- A forced recompute of a stale row that produces identical bytes would leave the row
  stale forever, even though it has just been proven current.
- Because a dependency's recipe version is not in the fingerprint (rule 8), a dependency
  version bump that produces identical bytes leaves the fingerprint unchanged, so the
  no-op path hits — while the stored `dep` edge still points at the *old* version's
  artifact row. A later write to the new row would not cascade here. That is a silent
  correctness hole, which is exactly the class of bug rule 4 exists to prevent.

So the no-op path may set `valid = 1` and may repoint dep edges, and may do nothing else:
no `data`, `digest`, `fingerprint`, `computed_at` or `compute_ms` write, and never a
cascade. The cascade is what rule 4 protects, and neither correction touches it.

## D18 — Five reserved key-field names

`on_miss`, `force`, `force_deps`, `deadline` and `part` are policy arguments to `get`,
which the spec's own API mixes with key fields in one `**kwargs`. A recipe declaring a
parameter with one of those names is rejected at registration rather than silently losing
it to the policy layer.

## D19 — `gc()` and `drop()` semantics

`gc()` removes artifacts belonging to a recipe version lower than the highest version
present for that name in the `recipe` table, when nothing in `dep` points at them. It
iterates to a fixed point, since removing one artifact can orphan another.

`drop()` marks every transitive dependent stale first, then deletes the targeted
artifacts and the dep edges pointing at them, in one transaction. Dependents then fail
verification on their next read and recompute, which is correct: their input is gone.

## D20 — A foreign recipe's output is re-encoded canonically before storing

The process writes `output` in whatever its language produces. Storing those bytes
directly would put byte-determinism (rule 7) in the hands of an R script. The host
decodes the output file with the declared codec and re-encodes it canonically, so the
digest is always the host's own canonical form.

## D21 — `on_miss="enqueue"` reports as a miss

The spec has `enqueue` write a task row and says nothing in v1 consumes it. The value is
therefore still unavailable, so an enqueued node is reported in the same batched
`MissingArtifacts` as a protected one, tagged with which action was taken.

## D22 — The `json` codec canonicalizes with JCS

The spec asks the json codec to sort keys and pin float formatting. JCS does both, is
already a dependency for key hashing, and is specified well enough that another language
produces the same bytes. Using it here rather than a second hand-tuned `json.dumps`
configuration means one canonicalization to get right instead of two.

## D23 — `NULL` parts are not covered by the schema's UNIQUE constraints

`artifact` is `UNIQUE (recipe_id, key_hash, part)` and `task` is
`UNIQUE (recipe, version, key_json, part)`, but SQLite treats `NULL`s as distinct in a
unique index, so neither constraint actually covers an unpartitioned row. Two
unpartitioned artifacts for the same `(recipe, key_hash)` would not collide.

The schema is reproduced from the spec verbatim rather than being quietly corrected, so
uniqueness for `NULL` parts is enforced in the write path: a `part IS ?` lookup before
the upsert, and a `WHERE NOT EXISTS` guard on the task insert. Writers serialize on
`BEGIN IMMEDIATE`, so select-then-write is safe across processes as well as threads.

The alternative — adding `part TEXT NOT NULL DEFAULT ''` and using the empty string for
unpartitioned values — would make the constraint do the work, but it changes a column the
spec fixes, and `part` being `NULL` when unpartitioned is stated twice in the spec.

## D24 — `store_parts()` alongside `store()`

The spec's `Store` protocol has `store(artifact, deps)`, one artifact at a time, and
justifies it by the transaction boundary: artifact, edges and cascade land together or
not at all. A partitioned codec produces several artifacts from a single compute, and
writing them through that method one at a time would put them in separate transactions,
so a concurrent whole-value read could merge the new 2026 with the old 2025.

`store_parts(artifacts, deps)` is the partitioned generalization: same transaction, same
cascade rules, all parts of one `(recipe, version, key_hash)`. `store()` stays exactly
as the spec fixes it, for the single-artifact case.

`store_parts` also removes parts the recomputation no longer produced. That is not
invalidation — rule 1 forbids deleting on invalidation, and this is not that. It is a
part that no longer exists, and keeping it would let a whole-value read merge a phantom
partition forever, since no future compute would ever refresh it. Dependents of a
removed part are marked stale first.

## D25 — One node resolves once per top-level call

A diamond in the graph — two recipes both reading the same dependency — would otherwise
resolve the shared node twice in a single walk. For a deterministic recipe that is only
wasted work; for one declared `deterministic=False` the two resolutions could return
different values, and the dependent would be built from an inconsistent snapshot.

So a walk memoizes `(recipe, key_hash, part)` for its own duration and no longer. Across
calls nothing is cached in memory: the store is the cache.

## D26 — A forced recompute of a protected node is skipped, not an error

`on_miss` governs *misses*. Under `force_deps=True` the walk reaches nodes that are
present and current, and the spec says protected roots are "skipped even then" — so a
node whose effective policy is stricter than `compute` keeps its stored value instead of
being recomputed, and no error is raised.

The exception is a node that is stale *and* failed verification: it is provably not
current, so there is no good value to keep, and it is reported as missing like any other
node that could not be produced.

## D27 — The `on_miss` ceiling applies below the root, not at it

The spec states the ceiling twice, and the two statements only agree under one reading:

> the effective policy at any node is **the less permissive of what the caller asked for
> and what the recipe declares**

> A recipe declaring `on_miss="raise"` is protected everywhere it appears **as a
> transitive dependency**.

> Ingesting is then an explicit top-level act: the fetch script calls
> `cache.get("ohlcv", symbol=..., on_miss="compute")` directly.

Applied literally at every node including the root, the third of these can never work:
`ohlcv` declares `on_miss="raise"`, so the ingest script's own explicit call would
refuse to ingest. The ceiling is therefore enforced at every node the walk *descends*
to, and the caller's ask governs at the node it named. That is where the hazard actually
is — a permissive top-level ask firing a paid vendor request six levels down that the
caller never mentioned — and it leaves a caller's explicit, deliberate request intact.

A caller may still restrict at the root: `on_miss="raise"` on an ordinary recipe raises,
because the caller's ask governs there in both directions.

## D28 — How far batched miss reporting can actually reach

The spec says not to raise on the first protected miss: walk as much of the graph as
possible, collect every protected node that came up missing, and raise once with the
full set.

How far "as much as possible" reaches is decided by the spec's own central choice, that
dependencies are discovered by execution. A Python recipe blocked on its first `ctx.get`
cannot continue to its second, because it has no value to continue with, and inventing a
sentinel to let it run on would be worse than useless. So batching collects everything
wherever the dependency set is knowable *without* running the recipe:

- **Stale verification.** Every stored edge of a stale artifact is resolved, and one
  blocked edge does not stop the rest. This is the common case in practice.
- **`plan()`.** The walk follows stored edges through valid and stale nodes alike, so a
  plan over a warm graph reports every missing input at once.
- **Foreign recipes,** whose inputs are declared in the manifest. The spec calls the
  declared graph the privileged case for exactly this reason.

An absent Python node with no stored edges is where the walk necessarily stops, and the
documentation says so rather than implying a guarantee the design cannot make.

Note also that `drop()` removes the dep edges pointing at what it deletes, so dropping a
dependency also discards the record of who read it. A superseded version bump leaves
those edges intact, which is why it, and not `drop()`, is the case where batching helps.

## D29 — The `arrow` codec encodes through pyarrow, not through polars

The spec says "Arrow IPC with compression off, fixed schema field ordering and no
custom metadata is the safe default". That is right about the format and not sufficient
in practice, because the writer matters too.

Measured: `polars.DataFrame.write_ipc_stream(compression="uncompressed")` produces
**different bytes** for the same frame under polars 1.20.0 and polars 1.44.2 — 904 bytes
against 848, first differing at offset 4, in the metadata padding. Nothing errors. A
routine `uv lock --upgrade` would have silently invalidated every artifact stored under
this codec, and the cache would have looked cold forever afterwards.

So the codec converts to an Arrow table and writes it with pyarrow under explicit
options: stream format, `compression=None`, `metadata_version=V5`, `allow_64bit=False`,
`use_threads=False`, schema and field metadata stripped, and `combine_chunks()` so that
how a frame happened to be assembled cannot leak into the bytes. That combination
produces identical bytes under polars 1.20.0 + pyarrow 16.1.0 + numpy 1.26.4 and under
polars 1.44.2 + pyarrow 25.0.1 + numpy 2.4.6.

The extras' version floors are the oldest combination actually verified, and CI re-checks
the golden bytes against exactly that combination on every push. The guarantee the spec
asks for is only as good as something that tests it.

## D30 — A foreign recipe's input must name a part if the dependency is partitioned

The invocation contract materializes each declared input as one file. A partitioned
dependency is several artifacts, and there is no obvious single file to hand over: a
directory would need a layout convention the manifest format does not have, and
concatenating would require the host to understand the codec's partitioning, which is
exactly what the spec keeps out of the core.

So `[[inputs]]` takes an optional `part`, and an input resolving to a partitioned
artifact without one is an error that says so. The common case in the finance design —
a recipe reading one closed year — is served directly, and the error names the fix.

## D31 — Foreign parameter types are checked, but the key is canonical either way

A manifest declares `dataset = { type = "string" }`. Declared defaults are type-checked
when the manifest loads, and call-time values when the recipe runs. Neither check is
load-bearing for correctness: a value of the wrong type simply canonicalizes to a
different key and therefore a different artifact. The checks exist to turn that into an
error at the call rather than a mysteriously cold cache.

## D32 — WAL is enabled with a retry, because `busy_timeout` does not cover it

The spec requires WAL and excludes distributed locking, on the grounds that concurrent
runners are handled by idempotent writes. Enabling WAL is itself the exception.

Switching a database to WAL takes a brief exclusive lock, and SQLite does **not** invoke
the busy handler for a journal-mode change. Two processes opening the same *new* file at
the same instant therefore both attempt the switch, and the loser gets `SQLITE_BUSY`
immediately, however large `busy_timeout` is. It fails before any of this library's own
transactions are involved, so none of the care taken over `BEGIN IMMEDIATE` helps.

This is not hypothetical: it is what the first run of the cross-process test did, on the
very deployment the spec describes.

`_enable_wal` therefore returns immediately when the mode is already `wal` — it is a
persistent property of the file, so this is the case for every connection after the
first — and otherwise retries until `busy_timeout` elapses. It checks the mode the pragma
reports rather than the absence of an exception, because the pragma can decline to change
the mode without raising.

`busy_timeout` is also now set *before* the switch rather than after, and is configurable.

## D33 — `get_many` resolves roots sequentially in one shared walk

A batch exists to make the spec's promise about cold runs true: "a cold backtest tells
you in a single run exactly which symbols and intervals need ingesting, instead of one
per attempt". D28 records why a single root cannot get there — a recipe blocked on its
first `ctx.get` has no value to continue with — but independent roots do not block each
other, so a batch reaches what a sequence of single calls cannot.

Roots resolve in order, sharing one `_Walk` and therefore one memo, so a dependency
common to several roots is computed once and a non-deterministic shared node cannot
produce two different values within a batch.

They are **not** run in parallel. The spec is explicit that the scheduler is plain
recursion because "graph depth in practice is single digits, so coroutines or
trampolining are unnecessary complexity", and parallel roots would make the memo, the
in-flight set and the miss accumulator concurrent structures while several threads
raced on the same rows. If the tier-2 benchmarks show this is the bottleneck, it can be
reconsidered against measurements rather than against a guess.

Two further choices inside `BatchResult`:

- **`values` is keyed by request index, not a list.** `None` and an empty frame are
  legitimate values (rule 9), so a positional list would make "resolved to `None`" and
  "did not resolve" indistinguishable. A missing key means unresolved, and nothing else.
- **A protected miss is collected; an exception propagates.** A miss is a modelled
  policy outcome, so reporting all of them at once is the point. An exception raised by
  a recipe is a bug, and collecting bugs into a dict that callers may never inspect is
  how silent failures are built. `raise_if_missing()` is there for callers who want a
  batch to behave exactly like `get` once they have looked at what did resolve.

Note that each request in a batch is a *root*, so D27's exemption applies to it: naming
a protected recipe directly is deliberate intent, which is what lets one batch call
serve as an ingest script.

## D34 — `max_age` uses a refresh clock, not `computed_at`

A recipe may declare a positive integer `max_age` in seconds. At or beyond that age,
matview treats its stored value as needing recomputation. This policy is deliberately
generic: the caller owns schedules, calendars, and the meaning of an appropriate age.

`computed_at` cannot carry this policy. Rule 4 preserves it when recomputation produces
the same digest and fingerprint; using it for freshness would make an unchanged source
refetch on every read after its first expiry. Artifacts therefore also carry
`refreshed_at`, advanced after every successful computation, including an otherwise
idempotent write. Payload bytes, digest, fingerprint, dependency edges, and
`computed_at` remain untouched on that path.

Expiry is ephemeral scheduler state, not dependency invalidation: it does not clear the
stored `valid` bit or cascade. `plan()` nevertheless reports an expired artifact under
`stale`, because it needs work before it is current for that read. An expired protected
dependency obeys its `on_miss` ceiling and must be refreshed explicitly as a root.

Expiry does not make every valid dependent read walk its stored graph: that would turn
ordinary cache hits into graph traversals and quietly make a TTL transitive. Schedulers
refresh age-limited source roots first; a changed digest then performs the ordinary
invalidation cascade. If a walk reaches an expired dependency for another reason, its
age and `on_miss` policy are still enforced.

## D35 — `expired` is counted as a reason, not an outcome

`CacheStats` separates the outcomes of resolving a node — hit, revalidated, recomputed,
blocked — and those four partition the nodes a walk touched, which is what makes
`lookups` and `hit_rate` mean anything.

An expiry under `max_age` (D34) is not a fifth outcome. An expired node goes on to be
recomputed, or to be blocked by its `on_miss` ceiling, and is counted there. So `expired`
records *why* that happened and is deliberately excluded from `lookups`, rather than
being folded in and quietly breaking the partition.

It is also deduplicated per walk, exactly as the missing set is, because one node can be
reached twice in a single walk: a blocked node is never memoized, so a stale dependent
resolves its expired dependency once while verifying its fingerprint and again from its
own body. Counting that twice would overstate how much a clock actually drove.

It exists because the counters cannot otherwise answer the question a TTL raises. A
rebuild caused by a changed input and a rebuild caused by a clock cost the same and mean
entirely different things, and without this counter a graph with short `max_age` values
is indistinguishable from one with an invalidation bug.

## D36 — `matview info` is version-blind, and nothing else is

`SqliteStore` refuses to open a database whose `schema_version` or
`fingerprint_spec_version` it does not implement, which is right: the columns may not be
the ones it selects, and the hashes may not be the ones it computes. But a diagnostic
tool is wanted precisely when something is already wrong, and "it refuses to tell you
anything" sends the reader back to `sqlite3` on the command line — which is what the CLI
exists to replace.

`inspect_database()` therefore reads only what does not depend on the schema version:
the `meta` table, the SQLite catalogue, the column lists and the row counts. It reports
what the file is, what wrote it, and why the other commands refuse. It does not degrade
the real queries to work against older columns, because that would mean a second,
version-tolerant read path that nothing else exercises and that would rot silently.

It opens with `mode=ro`, so it can never create or migrate the file it is describing.

## D37 — Decoding a stored value does not need the application's recipes

The obvious division for the CLI is "metadata needs nothing, values need `--recipes`",
and it is very nearly right — but a value's codec is recorded on its `recipe` row, not
looked up in the registry. `cat --id 12` can therefore decode without importing anything.

What genuinely needs the registry is narrower than it looks:

- **A key given as parameters**, because declared defaults are filled in *before* the key
  is hashed. `show sma symbol=AAPL` cannot know that `interval` defaults to `"1d"`, so
  the hash will not match. A *complete* key needs nothing.
- **`plan`**, because a plan resolves recipes.
- **A codec the application registered itself**, which no amount of reading the database
  can conjure. `cat` says so by name and suggests `--recipes` or `--raw`.

Being accurate about this matters: every `--recipes` a tool demands unnecessarily is a
case where inspection fails because an application's imports have side effects, or its
dependencies are not installed on the machine doing the diagnosing.

## D38 — `SqliteStore(read_only=True)`, and the CLI always uses it

A plain `SqliteStore(path)` creates the database if it is absent. For a library that is
correct. For an inspection tool it is a bug with two faces, and both were observed
before this existed: `matview ls --db typo.db` created an empty database and reported it
as containing nothing, and opening someone's cache switched its journal mode to WAL while
they were in the middle of diagnosing it.

`read_only=True` requires the file to exist, connects through SQLite's `mode=ro`, creates
no schema, seeds no `meta`, and skips the journal-mode switch — while still enforcing the
version checks. Write methods raise rather than surfacing a raw SQLite error.

It is on the store rather than in the CLI because the guarantee belongs to whatever holds
the connection, and because a replication or audit tool would want exactly the same thing.

## D39 — Arrow offset widths are normalized, so the producing library cannot pick them

`string`/`large_string`, `binary`/`large_binary` and `list`/`large_list` differ only in
whether offsets are 32- or 64-bit. There is no semantic difference between them, and each
dataframe library picks a side without being asked: polars 1.x produces `large_string`,
pandas 2.2 produces `string`, pandas 3.0 produces `large_string`.

Measured, for one three-column table: polars and pandas 3.0.6 agree at 560 bytes, while
pandas 2.2.3 produces 552. Nothing errors. This is the same class of problem as D29 — the
bytes were being decided by a dependency rather than by matview — with the added sting
that a *pandas minor version* moves them.

`_arrow_ipc.widen()` therefore normalizes those three pairs to the large variant,
recursively through structs, maps and lists, before the stream is written. Verified
byte-identical across polars 1.20/1.44 and pandas 2.2.3/3.0.6, and the golden fixtures pin
`arrow` and `arrow-pandas` to the same bytes so it cannot drift back.

Crucially, this changed **nothing** already stored: polars was already producing the large
variants, so the normalization is a no-op on that path and every existing digest holds.
Had it gone the other way it would have needed a `fingerprint_spec_version` bump.

A dictionary type's index width is deliberately *not* normalized. It varies with
cardinality, so it is data rather than an encoding choice, and pinning one width would
make every categorical column pay for the widest case. A recipe that wants a
producer-independent digest for a categorical should cast it explicitly.

## D40 — `arrow-pandas` is a separate module, not a branch in the polars codec

argos, the first consumer, is pandas/pyarrow/duckdb. A pandas user should not have to
install polars to read their own cache, so the pandas codec lives in its own module
importing only pandas and pyarrow, and the shared encoding lives in `_arrow_ipc`, which
imports only pyarrow. `get_codec` maps each codec name to the extra that installs it
explicitly, because `arrow-pandas` shares the Arrow encoding but needs a different library
and the old `name.startswith("arrow")` rule would have advised the wrong one.

Because the encoding is shared and offset widths are pinned (D39), `arrow` and
`arrow-pandas` are two views of one stored form: either can read what the other wrote, and
a cache is not partitioned by which dataframe library a recipe happens to use.

A pandas index is not preserved. It is presentation rather than data, and
`preserve_index=True` would add a column *and* write pandas metadata into the schema —
putting the producing library back into the digest, which is the thing D39 removes.

## D41 — The package lives under `src/`

Without it, `pytest` run from the repository root imports the working-directory copy
rather than the installed distribution, so a packaging mistake — a missing `py.typed`, an
unshipped subpackage, a module absent from the wheel — passes every local check and fails
only for a user. Under `src/` the tests exercise what is actually shipped.

The move was held back until CI called `make` targets rather than spelling commands out,
because it changes the path that gets type-checked and that path lives in one file now
rather than in a workflow this account cannot push.

## D42 — The calendar-year codec ships in matview, under the `arrow` extra

The spec describes this codec and then says "None of this belongs in the core." It ships
here anyway, as `arrow-year` and `arrow-year-pandas`, and the reasoning is that the
sentence is about the *core* rather than about the distribution. The same spec says
"partitioning is a codec concern, not a scheduler concern" and puts domain shape in the
codec layer deliberately; codecs are optional extras that the domain-neutral core knows
nothing about, and `part` stays an opaque label everywhere above them.

The alternative — an example each consumer copies — means a codec written twice, and two
copies of a byte-canonical encoding can disagree about bytes. That is the failure this
library is most careful about everywhere else.

It partitions on a column named `date` by default, so `arrow-year` works out of the box
and `matview cat` can still decode a year-partitioned artifact with nothing imported
(D37). Any other column name is a consumer-registered instance:

    register_codec(ArrowYearCodec(name="bars-year", column="day"))

Inference was rejected: if a recipe later grew a second date column, an inferring codec
could pick differently and move every stored digest.

Rows are sorted by the partitioning column before slicing, so a frame that is merely
reordered encodes identically instead of churning every fingerprint above it. Arrow's sort
is stable, so rows sharing a date keep their input order — which makes this canonical for
the one-row-per-date case the design has, and the documentation claims no more than that.

Timezone-aware timestamps are read **in UTC**. The same instant is 2023 in UTC and 2024 in
Asia/Tokyo, so without a fixed rule a row near a year boundary would land in a different
part depending on a column's timezone metadata. A `Date` column has no such ambiguity.

Years are rejected if the column has nulls: a null date belongs to no year, and bucketing
it anywhere would lose rows on the next read. Labels are zero-padded to four digits,
because the core sorts parts as text and "999" sorts after "1000" without padding.

## D43 — An empty frame stores unpartitioned, not as zero parts

A year-partitioned value with no rows has no years. Returning `{}` would ask the store to
write nothing at all, which `_persist` rightly refuses — but an empty series is a *value*,
not a miss (rule 9), and a recipe is entitled to return one.

So the year codecs return plain `bytes` for an empty frame, storing it as a single
unpartitioned artifact, and a dict of parts otherwise. Both shapes are within the `Codec`
protocol, which is `bytes | dict[str, bytes]` precisely because a codec may legitimately
do either.

The transition either way is handled by `store_parts` pruning what a recomputation no
longer produces (D24): a series that becomes empty replaces its year parts with the single
unpartitioned row, and one that fills up again replaces that row with year parts. Both
directions are tested, because a stale unpartitioned row coexisting with year parts would
make a whole-value read merge a phantom.

## D44 — `BaseCodec.partitions` removed

It was declared and read nowhere. `decode_parts` raising for a codec that does not
partition is the actual mechanism, and a flag that nothing consults is a claim that can
drift out of agreement with the behaviour it describes. A partitioning codec is evident
from what `encode` returns.

## D45 — The codec-floor job must refresh its build of matview

`uv run --no-project --with .` caches the wheel it builds and will reinstall a stale one.
This was not theoretical: after the `src/` move added `--with .`, the job silently verified
an *older* matview, and the symptom was invisible — the tests parametrize over
`CODEC_NAMES` from the installed package, so a stale build simply generates fewer cases and
reports success.

`--refresh-package matview` fixes the job. Because the failure mode is silence, there is
also a test asserting that the installed codec set matches the checked-in fixture, which
fails loudly if this regresses. A job whose entire purpose is catching drift must not be
able to test yesterday's code.

## D46 — SQLite JSON1 is a required capability

Scale-correct maintenance operations pass artifact IDs to SQLite as one JSON array and
expand them with `json_each`, rather than creating one bound variable per ID. That avoids
SQLite's variable limit everywhere the store removes, invalidates, or follows a large set
of artifacts.

`json_each` needs JSON1, which is included by default in SQLite 3.38 and later but is not
guaranteed by every Python 3.11+ build. Supporting both JSON1 and no-JSON1 builds with a
chunking fallback was rejected: it would restore the seven-call-site plumbing this design
removes, while leaving a platform-specific path unexercised on any one machine. A store
therefore probes JSON1 on every open, including read-only opens, and refuses with the
linked SQLite version when unavailable. SQLite 3.38 is the documented floor for a build
that provides JSON1 by default.

## D47 — The scale harness runs one named operation at a time

The scale harness has a `cold-write` operation that creates the database, then exposes
every measurement as a separately named operation against that existing file. That keeps
iteration on one operation from rebuilding a 250k-artifact fixture each time, and makes
the state change of `cascade`, `drop`, and `gc` explicit rather than concealing it in an
all-in-one benchmark run.

The generated graph has one protected root shared by every indicator, and one aggregate
per symbol over its indicators. This gives the cascade measurement a genuine wide fan-out
while retaining a finance-shaped symbols × indicators row count. A small pair of legacy
versions is included solely to give `gc()` candidates while it scans the full database.

The default is one sample, because creating or walking the full profile is deliberately
a minutes-scale manual action and several listed operations mutate the fixture. Every
result still uses the same median/p95 schema; callers can opt into more samples for
read-only operations with `--repeats`.

## D48 — Record short read samples, but single destructive samples

The first 250k baseline uses five samples for warm reads, stale verification, queries,
and listings; three for the larger batched scheduler walks; and one for cold write,
verification, and destructive operations. The full fixture is deliberately minutes-scale
to create or materially mutate, so repeated destructive runs would consume time while
measuring different database states.

Each result still records median and p95 together with its sample count. For a
one-sample operation they are the same value, which is more honest than presenting a
fabricated percentile or silently treating a mutation as repeatable.

## D49 — Graph queries live in `matview.graph`, not on the store

Lineage, impact and cost are assembled and rendered in a `matview/graph.py` module that
takes a store. `SqliteStore` gains only the SQL it needs — the upward walk, batched edge
reads, an id-set-to-recipe-count rollup and a per-recipe cost aggregate.

Three placements were considered. Putting everything on `SqliteStore` puts Graphviz and
Mermaid rendering on the persistence seam, which is the wrong thing for a seam whose
entire purpose is to make an HTTP backend possible later. Putting the entry points on
`Cache` requires a registry that none of these queries use, and forces a read-only
inspection tool to construct one. The module wins on both counts, and library users
reach it as `matview.graph.lineage(cache.store, artifact_id)`.

The read surface the queries need is declared as a `GraphStore` protocol *in that
module* rather than added to `Store`. `SqliteStore` satisfies both structurally. A
remote backend must be able to hold artifacts without also implementing a lineage walk.

## D50 — Nothing from this work goes on the `Store` protocol

`Store` is what the *core* calls. `deps_of_many`, `inputs_of`, `refs_by_ids`,
`ids_of_recipe`, `counts_by_recipe` and `recipe_cost_rows` are all read methods on
`SqliteStore` that only `matview.graph` and the CLI use, so none of them is a protocol
member. `deps_of_many` was briefly put there on the assumption the scheduler would call
it; D52 records why it does not, and it came back off.

Every requested id is a key of `deps_of_many`'s result, so a root maps to an empty list
rather than being absent and the caller never has to decide what a missing key means.

It carries a single-id fast path: `json_each` is measurably more expensive than an
equality test, and one id is the overwhelmingly common call, because `deps_of` delegates
to it. Delegating rather than keeping two queries means the batched and single reads
cannot drift apart; a test asserts they agree.

## D51 — Cost is summed per value, never per artifact row

`compute_ms` is recorded once per artifact, and `Cache._persist` writes the same figure
on every part of a partitioned value, because the compute happened once. Any aggregate
over artifact rows therefore multiplies such a value by its part count. Measured on a
three-year `arrow-year` value, `SUM(compute_ms)` reports 117 ms for 39 ms of work.

Every cost aggregate in this work groups on `(recipe, version, key_hash)` and takes
`MAX(compute_ms)` within the group. `SqliteStore.summary()` had the bug and is fixed in
the same pass; it now also reports a `values` count beside `artifacts`.

The reports print the value count next to the total on purpose, so that the
deduplication is visible to a reader rather than something they have to trust. This
failure mode produces no error and no symptom other than a wrong number, which is
exactly why it is stated three times — here, in the code, and in the output.

## D52 — The `plan_many` N+1 is in the walk, not in the loop the plan named

The handoff plan for this work identified `deps_of(row.id)` in `Cache._verify` and
`Cache._walk_stored_deps` as an N+1 and expected switching both to `deps_of_many` to be
a free speedup. It is not, and the premise is wrong: `rows` there is the *parts of one
node*, so the loop is a single iteration for every unpartitioned value and there is
nothing to batch across.

Measured on the 250k profile, five samples per run: `plan_many` at 274–275 roots/s
before the change, 235 roots/s with a naive `deps_of_many`, and 259–269 roots/s with the
single-id fast path of D50. The change is a regression at every size, so the two call
sites keep `deps_of` and carry a comment saying why the obvious-looking batching is not
a cleanup. With the core no longer calling it, `deps_of_many` also came back off the
`Store` protocol; see D50.

`plan_many` *is* genuinely slow against `get_many`, and its N+1 is real — it is across
the depth-first descent, one `deps_of` and one `fetch_all_parts` per node visited. A
node's edges have to be read before its children can be resolved, so removing it means
restructuring the suspending walk to proceed level by level, which is a scheduler change
rather than a store call swap. Left undone deliberately, and recorded here so the next
reader does not re-derive the wrong fix from the same symptom.

## D53 — Impact defaults to counts, and enumeration is opt-in

`impact` prints a per-recipe summary by default, with `--all` to enumerate, `--limit` to
bound the listing and `--depth` to bound the walk. In the recorded 250k profile one
shared root reaches 255,000 dependents; printing them helps nobody and rendering them to
Graphviz helps less. `--format dot` and `--format mermaid` therefore refuse without
`--all` and say so.

The counting is done in SQL (`counts_by_recipe`), so the default answer never
materializes 255,000 `ArtifactRef` objects to throw away. It takes 0.87 s on that
profile.

## D54 — An `ArtifactGraph` carries the direction it was walked in

`edges` is always `(dependent, dependency)` regardless of which way the walk ran, because
that is what the `dep` table records and storing it reversed would make the two walks
disagree about what an edge means. A renderer therefore cannot tell from the edges alone
which end to descend from, so the graph carries a `direction` of `"inputs"` or
`"dependents"`.

This is what lets one value type and one set of four renderers serve both lineage and an
enumerated impact set. The DOT and Mermaid renderings point their arrows the way data
flows — dependency to dependent — in both directions, because that is the reading a
provenance graph gets from anyone looking at one.

## D55 — The build-order documents are pruned at first release, not published

`PLAN.md` and the two handoff plans under `plans/` described work that is finished. They
were written for whoever was building the thing next, and a reader arriving at a released
library reads them as current intent instead — a plan that says "not started" about
something that shipped is worse than no plan at all.

They are removed rather than moved under `docs/`, because git history keeps them and the
reasoning worth keeping outlived them: the ten rules and their test names are in
`CLAUDE.md`, the measurements from the 250k plan are in `docs/performance.md`, and what
the graph-query plan concluded is in `docs/graph.md` and in D49–D54.

`DECISIONS.md` and `ROADMAP.md` stay at the root and stay public. They are not records of
work in progress: one says why the code is the way it is, the other says what is
deliberately *not* being built, and both answer questions a user of a released library
actually asks.

## D56 — Publishing is tag-driven and credential-free

A `v*` tag runs `ops/workflows/release.yml`: `make check`, a guard that the tag agrees
with `__version__`, `make release-check`, then a trusted-publishing upload and a GitHub
release whose notes are the changelog section for that version.

Trusted publishing over an API token because the alternative is a long-lived credential
with upload rights sitting in repository secrets, for a project with one maintainer and no
rotation process. The cost is that the registration binds to this workflow's *filename*
and its environment name, which is recorded in `RELEASING.md` because renaming either
silently breaks releases.

The tag-versus-`__version__` guard is not ceremony. Hatchling reads the version from
`src/matview/__init__.py`, so a mistyped tag would otherwise publish a file whose name and
metadata disagree, and PyPI accepts a version number exactly once.

`make release-check` exists because the test suite cannot see the distribution: it imports
an editable install, so a missing `py.typed`, an unshipped subpackage, an entry point that
does not resolve or metadata an index would reject are all invisible to it. The target
builds, validates the metadata, then installs the wheel in isolation and runs both an
import and the console script.

The workflow is staged under `ops/workflows/` rather than installed because pushing under
`.github/workflows/` needs a token scope the automation account does not have. Only the
file that does not exist yet is staged; the installed `ci.yml` and `docs.yml` are not
copied there, since two copies of a workflow drift and the repository already refuses that
trade for `SPEC.md`.

## D57 — Published for use, not for collaboration, and the security posture says what it trusts

The README states that pull requests may sit unreviewed and asks for an issue first. The
alternative — the full community scaffolding of a contributing guide, a code of conduct
and issue templates — advertises a review capacity that one maintainer does not have, and
an unanswered pull request costs a contributor more than a stated policy does. Bug reports
are still wanted; what is declined is the expectation of review.

For the same reason there is no `CONTRIBUTING.md`. What a contributor would need from one
is already load-bearing elsewhere: `make check` is what CI runs, `CLAUDE.md` states the
ten rules and that `SPEC.md` and the golden fixtures are not to be edited, and this file
records why each choice is what it is.

`SECURITY.md` documents the trust model rather than only an address to write to, because
three of matview's positions are easy to misread as guarantees. Recipes are code and
registering one runs it. A database is input, and decoding a payload hands third-party
Arrow and `.npy` parsers bytes the reader did not choose — no pickle, ever, but that is
not the same as safety. Digests are unkeyed and detect change, not tampering. Saying so
once, publicly, is worth more than answering it in issues.
