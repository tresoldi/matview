# Plan: behaviour at 250k artifacts

**Status: approved, not started.** One decision is still open and blocks Phase 1 — see
[Open decision](#open-decision-blocks-phase-1). Everything else below is settled; do not
relitigate it.

Written as a handoff. Baseline is commit `25e8094`, where the suite is 316 tests with all
codec extras, 249 without, 51 at the version floor, and `DECISIONS.md` ends at **D45** —
so new entries start at D46.

Read `CLAUDE.md` first. It is not optional here: this work touches the store's write paths,
which is where the spec's hard rules live.

---

## Why

`ROADMAP.md` carries this as a tier-2 item. The design spec cites roughly 5,000 symbols and
50 indicators — about **250,000 artifact rows** — and nothing in the library has ever been
measured or even exercised at that size. The investigation below found one outright defect
and one quadratic path, so this is not only a benchmarking exercise.

## Findings

All measured on `25e8094` in the development container (SQLite 3.45.1, Python 3.11.15).
Each is reproducible with the command given, so re-verify rather than trusting this file.

### 1. Seven unbounded `IN` clauses — a hard failure on other SQLite builds

`src/matview/store/sqlite.py` builds one placeholder per artifact id in five methods:

| Function | What it does |
| --- | --- |
| `dependents_of` | two `IN` lists, one of them inside the recursive CTE |
| `_prune_vanished_parts` | invalidate dependents, delete dep edges, delete artifacts |
| `invalidate` | one `IN` list over ids plus their transitive dependents |
| `drop` | invalidate dependents, delete edges, delete artifacts |
| `gc` | delete edges, delete artifacts |

Measured limits:

```bash
uv run --extra dev python -c "
import sqlite3
c = sqlite3.connect(':memory:'); c.execute('CREATE TABLE t (id INTEGER)')
print(c.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER))
for n in (250000, 250001):
    try:
        c.execute('SELECT 1 FROM t WHERE id IN (%s)' % ','.join(['?']*n), list(range(n)))
        print(n, 'ok')
    except Exception as e: print(n, type(e).__name__, e)
"
```

This build allows exactly **250,000** and fails at 250,001 with
`OperationalError: too many SQL variables`. That is the very scale in question, and we pass
here by luck: SQLite's compiled default has been **32,766** since 3.32 and **999** before
that, and Python's bundled SQLite varies by build and platform. So on a plausible
interpreter, `cache.drop("ohlcv")` across 33,000 artifacts raises rather than working.

This is a correctness bug, not a slowdown, and it does not need a large fixture to test —
see the `setlimit` trick under Phase 1.

### 2. `iter_payloads` paginates with `OFFSET`

`src/matview/store/sqlite.py`, `iter_payloads`, walks the whole `artifact` table with
`ORDER BY a.id LIMIT ? OFFSET ?`. SQLite reaches an offset by scanning and discarding, so
`matview verify` over 250k artifacts is quadratic in the number of batches. Keyed
pagination (`WHERE a.id > ? ORDER BY a.id LIMIT ?`) is strictly better and no harder.

`list_refs` also takes `OFFSET`, but there it is a caller-supplied argument for paging a
listing rather than a full walk. Leave it.

### 3. Not yet measured, and worth measuring

- `_cascade`'s recursive CTE against a wide fan-out root — it rides `dep_reverse`, which
  should be enough, but nobody has checked.
- `gc()` loops to a fixed point, each pass a full scan with `NOT IN (SELECT dep_artifact_id
  FROM dep)`.
- `dependents_of` materializes every transitive dependent into a Python list and feeds it
  straight back into an `IN` clause — the two problems compound.
- `query`/`list_refs` building 250k dataclasses.
- Blobs in-row: the artifact table holds the payloads, so a metadata-only scan may read
  through pages carrying them. SQLite keeps large blobs on overflow pages, which probably
  saves us — measure it rather than assuming either way.

Disk in the container: 29 GB free. 250k rows is ~50 MB with tiny payloads, ~10 GB at the
spec's realistic 40 KB per artifact.

---

## Settled decisions

Asked and answered. Record each as a `DECISIONS.md` entry (from D46) as you implement it.

1. **Fix the `IN` clauses with `json_each`**, passing the ids as one JSON array:
   `WHERE id IN (SELECT value FROM json_each(?))`. No variable limit, no chunking logic,
   and it works inside the recursive CTE too. Verified that the planner still uses the
   index rather than degrading to a scan:

   ```
   SEARCH t USING INTEGER PRIMARY KEY (rowid=?)
   LIST SUBQUERY 1
   SCAN json_each VIRTUAL TABLE INDEX 1:
   ```

   Chunking into batches below 900 was considered and rejected as more plumbing at seven
   call sites, with the cascade CTE needing separate handling anyway. A json_each path with
   a chunking fallback was also rejected: two code paths where only one is ever exercised
   on a given machine is the same hazard already declined for the CLI's version-tolerant
   read path (D36).

2. **Scale correctness belongs in the ordinary test suite; timings do not gate CI.** The
   variable-limit tests are deterministic and fast, so they run like any other test.
   Timings live in a manual harness and their numbers get written up. No timing thresholds
   in CI: they are flaky on shared runners, and a benchmark that cries wolf is ignored
   exactly when it matters.

3. **Two harness profiles.** Default: 250k rows with tiny payloads (~50 MB, minutes), which
   measures index and query behaviour. `--realistic`: 40 KB payloads (~10 GB), run
   deliberately and recorded once. Keeping them separate stops two different measurements
   being conflated.

4. **Fix defects, report slowness.** The variable-limit overflow and the quadratic
   pagination are defects: fix them. Anything merely slower than hoped gets measured and
   written up with options, for the repository owner to prioritize — the answer depends on
   how argos actually loads it.

## Open decision (blocks Phase 1)

`json_each` needs SQLite with JSON1. It has been compiled in by default since SQLite
**3.38** (March 2022); this container has 3.45.1. matview supports Python 3.11+, whose
bundled SQLite varies by build, so JSON1 is not guaranteed everywhere.

What should happen when it is absent? This was not settled.

- **(a) Check at open and refuse**, with a message naming the SQLite version, and document
  JSON1 as a requirement. Coherent with rejecting the fallback above, and it fails loudly
  at open rather than mysteriously inside `drop()`. Cost: matview stops opening on an
  unusually old or minimal build. This is the recommendation on the table.
- **(b) Chunk everywhere instead** (settled decision 1's rejected option 2), raising no
  requirement at the cost of more plumbing.

**Ask the repository owner before writing code for Phase 1.** Do not quietly pick one.

---

## Phase 1 — scale correctness

Everything here is in `src/matview/store/sqlite.py` and `tests/`.

1. Add a helper for the id-set predicate — one place that renders
   `IN (SELECT value FROM json_each(?))` and the matching parameter — and use it in
   `dependents_of` (both clauses, including inside the CTE), `_prune_vanished_parts`,
   `invalidate`, `drop` and `gc`.
2. If decision (a) above is taken, add the JSON1 capability check to `_check_versions` (or
   beside it), so it is enforced on every open, read-only opens included.
3. Change `iter_payloads` to keyed pagination.
4. Tests, in the ordinary suite. The point is that **no large fixture is needed**: lower the
   limit on the connection instead.

   ```python
   store._connect().setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 10)
   ```

   Verified: with the limit at 10, a 20-placeholder `IN` raises `OperationalError`. So build
   a small graph, lower the limit, and assert `invalidate`, `drop`, `gc`,
   `dependents_of` and a part-pruning write all still work. Each of those tests fails
   today and passes after the fix, which is the property that makes them worth having.
5. Also cover `iter_payloads` walking more rows than one batch, so the pagination change is
   pinned by behaviour rather than only by inspection.

Run `make check` before pushing. Note the caveat in the `codec-floor` target: run locally
from the repository it is not a true isolation check, because uv layers the project `.venv`
in. CI is the authority for that one (D45).

## Phase 2 — the harness

A `benchmarks/` directory, not part of the test suite.

- A generator that builds a cache of configurable shape — symbols × indicators, with a
  dependency graph resembling the finance example (protected root, indicators over it, an
  aggregate over those) — with `--scale` and `--realistic`.
- Timed operations, reporting median and p95 per operation plus rows/second and the
  resulting database size: cold write of N artifacts; warm read of a valid artifact; stale
  verification that revalidates without recomputing; a cascade from a root with wide
  fan-out; `query` and `list_refs` filtered and unfiltered; `gc()`; `drop()` of one recipe;
  `matview verify`; `get_many` and `plan_many` over many roots.
- Deterministic data, so two runs are comparable. It must be possible to run one operation
  alone against an existing database, or iterating on a fix means regenerating 250k rows
  each time.

## Phase 3 — record

`docs/performance.md`: the numbers, the machine and library versions that produced them,
and what each measurement does and does not cover. Add it to the mkdocs nav.

Defects fixed in Phase 1 get a `CHANGELOG.md` entry. Anything merely slow gets written up
with options and left for the owner.

---

## Do not

- Do not add timing thresholds to CI (settled decision 2).
- Do not touch `SPEC.md`. Nothing here should need to; if it looks like it does, stop and say so.
- Do not optimize paths the harness has not measured. "Fix defects, report slowness" is the
  agreement, and speculative optimization of a path argos never touches is cost without
  benefit.
- Do not change `list_refs`' `OFFSET`; it is a paging argument, not a full walk.
