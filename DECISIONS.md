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
