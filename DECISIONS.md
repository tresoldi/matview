# Decisions

The design spec says: *"Where this document specifies behaviour, implement it as
specified rather than improving it. Where it is silent, use your judgement and record
each such choice in a `DECISIONS.md` at the repo root."*

This is that file. Each entry states the gap, the choice and the reason. Entries
marked **provisional** are ones where the choice is cheap to reverse now and expensive
later, so they are flagged for confirmation rather than settled quietly.

---

## D1 — Python floor is 3.11 *(provisional)*

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

## D4 — `deterministic=False` is advisory in v1

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
