# matview — build plan

Source of truth: `docs/design-spec.md`. Interop contract: `SPEC.md`. Judgement calls
where the spec is silent: `DECISIONS.md`.

The spec fixes the build order and requires that the ten hard rules get tests
*before* the behaviour they constrain exists. Stage 0 is therefore a failing test
suite, not scaffolding.

## Stage 0 — invariant tests (write first, expect red)

One test per hard rule, named after it, so a violation names itself in CI output.

| # | Rule | Test |
| --- | --- | --- |
| 1 | Never delete an artifact on invalidation; clear `valid`, keep bytes | `test_rule01_invalidation_keeps_bytes` |
| 2 | Never cache a failed compute, in any form | `test_rule02_failed_compute_not_cached` |
| 3 | Artifact + dep edges + cascade land in one transaction or not at all | `test_rule03_store_is_atomic` |
| 4 | Never rewrite a row whose digest and fingerprint already match | `test_rule04_idempotent_write_is_noop` |
| 5 | `force` is local; only `force_deps` is transitive | `test_rule05_force_is_local` |
| 6 | `on_miss` resolves to the strictest of caller and recipe | `test_rule06_on_miss_is_a_ceiling` |
| 7 | Digests come from codec output; codecs are byte-deterministic | `test_rule07_codec_byte_determinism` |
| 8 | A dependency's recipe version never enters a dependent's fingerprint | `test_rule08_dep_version_not_in_fingerprint` |
| 9 | Empty or `None` results are values, not misses | `test_rule09_empty_is_a_value` |
| 10 | Never use pickle | `test_rule10_no_pickle_anywhere` (import + source scan) |

## Stage 1 — canonicalization and fingerprints

`matview/_canon.py`, `matview/_hash.py`. No I/O, no SQLite, pure functions.

- JCS serialization of a scalar-only flat dict.
- Integral-float → int normalization; `NaN`/`±inf` rejected.
- Defaults filled from the recipe signature **before** hashing.
- `key_hash`, `digest`, `fingerprint` exactly as `SPEC.md` states — including the
  asymmetry in rule 8, with the comment the spec asks for.

Done when: property tests over scalar dicts, the float-normalization test, and
golden vectors (fixed inputs → fixed hex digests, checked into the repo so a future
refactor or a second-language port can be validated against them).

## Stage 2 — schema and `SqliteStore`

`matview/store/sqlite.py` plus the `Store` protocol in `matview/store/base.py`.

- DDL verbatim from the spec; `meta` seeded with `schema_version` and
  `fingerprint_spec_version`.
- WAL, `foreign_keys=ON`, connection per thread.
- `store()` owns the cascade and the whole transaction: digest compare → no-op,
  else upsert + replace dep edges + recursive `dep_reverse` walk setting `valid=0`.
- `max_artifact_bytes` (default 64 MiB) raises rather than writing.
- `fetch`, `dependents_of`, `invalidate`, `revalidate`, `drop`, `query`.

Done when: rules 1, 3 and 4 pass, including a simulated mid-write crash.

## Stage 3 — registry and context

`matview/registry.py`, `matview/context.py`.

- `@recipe(version=, codec=, index=(), on_miss=, deterministic=)`, import-time and
  global; signature defaults captured at registration.
- `Cache` is **not** global, one per store, connection per thread.
- `ctx.get` records each resolved dependency into the context's collected set.

## Stage 4 — scheduler

`matview/scheduler.py`. Plain recursion, explicit in-flight stack of
`(recipe, key_hash, part)`; re-entry raises with the full path.

- Three states kept distinct: absent / stale / valid.
- Stale read path: resolve deps, recompute fingerprint, compare; match → flip
  `valid=1` and return stored bytes **without recomputing**.
- Failures propagate and are never stored.

Done when: cycle detection, the worked early-cutoff example asserted end to end,
and stale-but-valid revalidation-without-recompute all pass.

## Stage 5 — policy

`matview/policy.py`.

- `on_miss` ceiling: `compute < enqueue < raise`, effective = stricter of the two.
- `force` local, `force_deps` transitive, ceiling still applies under both.
- `deadline` → absolute monotonic instant at the top call, checked before each descend.
- Batched miss reporting: walk as far as possible, collect **every** protected miss,
  raise once with the full set attached. `plan()` is the same walk with computing off,
  reporting absent and stale separately.

## Stage 6 — codecs

`matview/codecs/`: `arrow` (Arrow IPC ↔ polars), `npy` (↔ numpy), `json` (↔ dict).
Each an optional extra; the core installs with none of them.

- Byte-canonical: compression off, fixed field ordering, no custom metadata, no
  timestamp/hostname/PID/library version anywhere in the payload.
- Per codec: `encode(decode(encode(x))) == encode(x)` byte-for-byte, plus a
  cross-version check in CI.
- A dict return means partitioned output; the scheduler treats `part` as opaque.
- `decode_parts(dict[str, bytes])` merges a partitioned value for a whole read;
  `get(..., part=...)` selects one and records a dep edge on that part alone.
  See `DECISIONS.md` D11, which closes a gap the spec left open.

## Stage 7 — foreign recipes

`matview/foreign.py`. TOML manifest discovered by directory scan, registered as if a
decorator had; dependencies declared upfront, so the graph is cycle-checkable before
anything runs. One JSON job file as the only argument; result read from `output`,
never stdout; nonzero exit / missing / unparseable output is a failure and is not
cached; the job file survives a failure for hand-reproduction; `deadline_s` enforced
by killing the process.

## Stage 8 — deliverables

- Two examples, one financial (OHLCV → SMA → Sharpe) and one not (distance matrix →
  tree), to keep domain assumptions out of the core.
- `DECISIONS.md` complete.
- CI: lint, typecheck, test matrix, codec cross-version determinism check.

## Explicitly not built

Distributed locking or leases; remote backends; a queue consumer for `task` rows;
incremental/append-only computation; point-in-time correctness; web UI, daemon or
server; a lookback window. Not as a hidden fast path and not as an unused code path.
