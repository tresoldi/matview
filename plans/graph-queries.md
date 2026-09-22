# Plan: lineage, impact and cost

**Status: approved, not started.** Nothing here is blocked; the four design questions were
asked and answered, and the answers are recorded under
[Settled decisions](#settled-decisions). Do not relitigate them.

Written as a handoff. Baseline is commit `39a7262`, where the suite is 324 tests with all
codec extras, 257 without, 51 at the version floor, and `DECISIONS.md` ends at **D48** — so
new entries start at D49.

Read `CLAUDE.md` first, and `plans/250k-scale.md` for context on the store work this builds
on (`_id_set_predicate`, the JSON1 requirement, the recorded baseline).

---

## Why

matview records a complete observed DAG in the `dep` table and offers almost no way to ask
it anything. `deps_of` returns one level. `dependents_of` walks transitively but **downward
only**, and returns bare ids. There is no upward walk at all, so the question "which raw
inputs produced this number?" — the audit question for the financial consumer, and the
reproducibility question for the linguistics one — is currently unanswerable.

`compute_ms` is recorded on every artifact specifically so that decisions like the deferred
lookback window can rest on measurement, and nothing aggregates it along the graph it was
recorded against.

All three features are pure metadata queries. None needs the recipe registry, so all of
them work with nothing imported, consistent with D37.

## A correctness trap, verified before planning around it

**`compute_ms` is recorded once per artifact, and a partitioned value has several
artifacts that each carry the whole cost.** `Cache._persist` sets the same `compute_ms` on
every part, with the comment "Every part of one value shares the cost of producing that
value; the compute happened once."

Measured on `39a7262`, a three-year `arrow-year` value:

```
parts and compute_ms: [('2023', 39), ('2024', 39), ('2025', 39)]
naive sum: 117 ms -- actual compute happened ONCE
```

So any cost feature that does `SUM(compute_ms)` over artifacts overcounts partitioned
values by their part count. **Deduplicate by `(recipe, version, key_hash)`, not by artifact
row.**

This also means `SqliteStore.summary()` is overcounting its global `compute_ms` today. Fix
it in the same pass and note it in `CHANGELOG.md`.

---

## Settled decisions

Record each as a `DECISIONS.md` entry (from D49) as you implement it.

1. **Store walks, a `matview/graph.py` module, and CLI commands.** The store gains only the
   two new SQL walks; graph assembly and rendering live in `matview/graph.py`, which takes a
   store. A store persists — it should not know about Graphviz, and the `Store` protocol
   should not grow methods a remote backend has no business implementing. Library users
   reach it as `matview.graph.lineage(cache.store, artifact_id)`. Rejected: everything on
   `SqliteStore` (puts rendering on the persistence seam), and methods on `Cache` (which
   requires a registry these queries do not need, and which a read-only tool has no reason
   to construct).

2. **`impact` answers all three questions** — a digest change, a version bump, and a drop.
   See Phase 2 for what each means.

3. **`cost` reports all three metrics** — embodied cost over the input closure, a per-recipe
   rollup, and the critical path. See Phase 3.

4. **Four output formats** — indented tree text, structured JSON, Graphviz DOT, Mermaid.

## Decided without asking, and why

- **Impact defaults to a summary.** In the recorded 250k benchmark graph there is one shared
  root whose impact is 255,001 artifacts. Dumping that, still less rendering it to DOT, helps
  nobody. `impact` therefore prints counts by recipe by default, with `--depth`, `--limit`
  and an explicit `--all` to enumerate.
- **`--depth` on every walk.** Closures are small in the benchmark shape (~51 nodes for an
  aggregate) but the library must not assume that.
- **No new registry dependency anywhere in this work.** If a command seems to need
  `--recipes`, stop: it probably means the design drifted.

---

## Phase 0 — the shared foundation

Everything else is a thin layer over this, and it pays for itself on its own.

1. `SqliteStore.inputs_of(artifact_ids, *, depth=None) -> list[int]` — the transitive
   **upward** walk. Mirror `dependents_of`: same recursive-CTE shape, same
   `_id_set_predicate` for the root set, but join the other way. The existing walks read

   ```sql
   SELECT d.artifact_id FROM dep d JOIN affected a ON d.dep_artifact_id = a.id
   ```

   and this one needs

   ```sql
   SELECT d.dep_artifact_id FROM dep d JOIN affected a ON d.artifact_id = a.id
   ```

   Support `depth` by carrying a level column in the CTE. Exclude the roots from the result,
   as `dependents_of` does, and say so in the docstring.

2. `SqliteStore.deps_of_many(artifact_ids) -> dict[int, list[DepEdge]]` — one query for many
   artifacts' edges, replacing repeated `deps_of`.

3. `matview/graph.py` with an `ArtifactGraph` value type: `nodes: Mapping[int, ArtifactRef]`,
   `edges: tuple[tuple[int, int], ...]` (dependent, dependency), plus `roots`. Renderers:
   `to_tree()`, `to_json()`, `to_dot()`, `to_mermaid()`. Keep it a plain value object —
   no store reference inside it, so it can be built once and rendered many ways.

4. **Fold in the `plan_many` N+1.** `src/matview/cache.py` calls `deps_of(row.id)` once per
   node at two places (`_verify` and `_walk_stored_deps`, around lines 606 and 651). The
   recorded baseline puts `plan_many` at ~291 roots/s against `get_many`'s ~24,880, and this
   is why. Switching both to `deps_of_many` is nearly free once it exists. Re-run the
   harness's `plan-many` operation afterwards and update `docs/performance.md` with the new
   figure, noting that the old one is superseded rather than deleting it.

Tests: build a small diamond and a chain; assert `inputs_of` returns the closure and
excludes the roots, that `depth=1` stops after one level, that a cycle cannot occur (the
scheduler forbids them, but the CTE must terminate on a malformed database rather than
hang), and that `deps_of_many` agrees with repeated `deps_of`.

## Phase 1 — `lineage`

`matview.graph.lineage(store, artifact_id, *, depth=None) -> ArtifactGraph`, the input
closure with edges.

CLI: `matview lineage --id 12 [--key-hash …] [--depth N] [--format tree|json|dot|mermaid]`,
addressing an artifact the way `show` does.

The tree rendering should make the useful things visible at a glance: recipe, version, part,
whether the node is valid, and its digest prefix. A stale node in someone's lineage is
exactly what they are looking for.

## Phase 2 — `impact`

Three questions, one command, read-only throughout.

- `matview impact --changed ID` — the transitive dependents that would be marked stale if
  that artifact's bytes changed. The recursive CTE for this already exists inside `_cascade`;
  it is simply unreachable except as a side effect of a write. Reuse `dependents_of`.

- `matview impact --bump RECIPE` — what happens if that recipe's version is bumped. The
  honest answer is counter-intuitive and worth stating plainly in the output: **nothing goes
  stale.** Under rule 8 a dependency's recipe version is not in a dependent's fingerprint,
  so a bump invalidates nothing. What actually happens is that the recipe's existing
  artifacts become unreachable (a new `recipe` row), and on the next read each dependent
  re-resolves the dependency at the new version, finds it absent, recomputes it, and then
  cascades *only if the recomputed bytes differ*. Report the artifacts that become
  unreachable and the dependents that will re-resolve. Getting this wording right is most of
  the value: it is the part of the fingerprint spec that confuses every new reader.

- `matview impact --drop RECIPE [--version N]` — what `cache.drop()` would delete and what
  it would mark stale, per D19. The CLI deliberately does not offer `drop`; being able to
  see what it would do is the useful half.

Defaults to a per-recipe summary. `--all` enumerates; `--depth` and `--limit` bound it.

## Phase 3 — `cost`

- **Embodied cost.** Total `compute_ms` over the input closure of an artifact,
  **deduplicated by `(recipe, version, key_hash)`** per the trap above, so a partitioned
  dependency counts once. Report the value count as well as the total, so the deduplication
  is visible rather than implicit.

- **Per-recipe rollup.** `GROUP BY` over `artifact`, no graph walk: artifacts, distinct
  values, total and mean `compute_ms`, total bytes. Cheapest thing in this plan and probably
  the first question anyone optimizing asks. Extend `recipe_rows()` or add a sibling — it
  currently aggregates `n_bytes` and `invalid` but not `compute_ms`.

- **Critical path.** The longest chain through the input closure by cumulative `compute_ms`.
  A topological walk in Python over the closure is fine: lineage closures are small, and
  this is the input direction, not the 255k-node dependent direction. Report the chain, not
  just its length, since the point is to see which step dominates.

- **Fix `summary()`'s global `compute_ms`**, which overcounts partitioned values today.

CLI: `matview cost --id 12`, `matview cost --by-recipe`, `matview cost --id 12
--critical-path`.

## Phase 4 — documentation

- A `docs/graph.md` page in the mkdocs nav, or a section in `docs/cli.md` — whichever reads
  better once the commands exist. It must explain the `--bump` semantics, because that is
  where a reader will most need help.
- `CHANGELOG.md` for the features and for the `summary()` overcount fix.
- `docs/performance.md` updated with the new `plan_many` figure.

---

## Do not

- Do not add a registry dependency to any of these commands.
- Do not put rendering on `SqliteStore`, or these methods on the `Store` protocol.
- Do not `SUM(compute_ms)` over artifact rows. See the trap above; it is the one thing in
  this plan most likely to produce confidently wrong numbers.
- Do not enumerate or render a full impact set by default.
- Do not touch `SPEC.md`. Nothing here should need to.
- Run `make check` before pushing. Note the `codec-floor` caveat documented in the Makefile:
  run locally it is not a true isolation check, because uv layers the project `.venv` in.
