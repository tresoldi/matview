# Lineage, impact and cost

The cache records a complete dependency graph as it runs. Every `ctx.get` becomes an
edge, so the file already knows what produced what. These three commands ask it.

```bash
matview lineage --db cache.db --id 12              # what produced this?
matview impact  --db cache.db --changed 12         # what breaks if this changes?
matview cost    --db cache.db --id 12              # what did this cost to compute?
```

All three are **metadata queries**. They read the recorded graph rather than resolving
recipes, so they need no `--recipes` and work against a database whose recipes this
machine has never seen. All three are read-only; `impact --drop` shows what a drop would
do without doing it.

The same queries are available to a library caller, which is what the command line is a
thin layer over:

```python
from matview import graph

g = graph.lineage(cache.store, artifact_id=12)
print(g.to_dot())
```

## lineage

The input closure of one artifact: everything that went into producing it, transitively,
from what was actually read at compute time rather than from what a recipe declares it
might read.

```
$ matview lineage --db cache.db --id 4
[4] report v1 {"symbol":"AAPL"} valid 60d148afb87b
├─ [2] sma v2 {"symbol":"AAPL","window":2} valid a4732d52b9b7
│  └─ [1] ohlcv v1 {"symbol":"AAPL"} valid 7fd687cdcdff
└─ [3] sma v2 {"symbol":"AAPL","window":3} valid 47935dc7b12f
   └─ [1] ohlcv v1 {"symbol":"AAPL"} valid 7fd687cdcdff  (shown above)
```

Each line carries the row id, recipe, version, part, key, whether the node is valid, and
a digest prefix. A stale node in a lineage is very often what someone opened the command
to find.

The graph is a DAG, not a tree, so a node reachable by more than one path is printed
again and marked rather than expanded twice — which both bounds the output on a diamond
and makes the sharing visible.

Address the artifact the way `show` does: `--id`, `--key-hash`, or a recipe name and key
fields (a partial key needs `--recipes`, for the reason [the CLI page](cli.md) gives).
`--depth N` stops the walk after N levels.

## impact

Three questions, one command, read-only throughout.

### `--changed ID`

The transitive dependents that would be marked stale if that artifact's bytes changed.
This is the cascade the store already runs inside a write, asked as a question.

Artifacts that are *already* stale are counted separately, because an already-stale
dependent is not additional damage and the cascade skips it.

### `--bump RECIPE`

The counter-intuitive one, and the reason this command exists.

**Bumping a recipe's version marks nothing stale.**

A dependency's recipe version is not part of a dependent's fingerprint. That is rule 8 of
the design spec, and it looks like a bug to everyone who meets it fresh. It is not. What
a bump actually does:

1. It writes a **new** `recipe` row. The existing artifacts stay in the database, intact,
   with nothing resolving to them any more. They are unreachable, not stale.
2. On its next read, each dependent re-resolves the dependency **at the new version**,
   finds it absent, and recomputes it.
3. Only then, and only if the recomputed bytes **differ** from the digest the edge
   recorded, does the cascade run.

So if the new version produces identical bytes, nothing above it is recomputed at all.
That is early cutoff, and it is exactly what rule 8 buys. The cost is that you cannot
force a downstream rebuild by bumping an upstream version; `force_deps` is what does
that.

`impact --bump` reports the artifacts that become unreachable, the direct dependents that
will re-resolve, and how many artifacts earlier bumps have already left behind — those
are what `cache.gc()` removes, once nothing depends on them.

The dependents listed are **direct** ones only. The transitive set is conditional on the
bytes differing, so counting it would overstate the answer.

### `--drop RECIPE [--version N]`

What `cache.drop()` would delete and what it would mark stale.

Dropping is the one place in the library where bytes go away: invalidation keeps them and
drop does not. The dependents keep their own bytes, but they can no longer be revalidated
without recomputing, because the inputs their fingerprints refer to are gone.

`drop` is deliberately not a command-line operation. Being able to see what it would do is
the useful half.

### Bounding the answer

`impact` prints **per-recipe counts by default**. In the recorded 250k benchmark profile
one shared root reaches 255,000 dependents; listing those helps nobody, and rendering
them to Graphviz helps less.

| Flag | Effect |
| --- | --- |
| `--all` | enumerate the affected artifacts instead of only counting them |
| `--limit N` | with `--all`, list at most N per group; the count stays honest |
| `--depth N` | with `--changed`, stop the walk after N levels |

`--format dot` and `--format mermaid` draw the affected set, so they require `--all`.

## cost

`compute_ms` is recorded on every artifact. `cost` aggregates it along the graph it was
recorded against.

```bash
matview cost --db cache.db --id 12                    # embodied cost of one value
matview cost --db cache.db --id 12 --critical-path    # and which step dominates
matview cost --db cache.db --by-recipe                # where the time goes overall
```

**Embodied cost** totals `compute_ms` over an artifact's input closure, reporting its own
share separately. **The critical path** is the most expensive chain through that closure,
reported as the chain rather than as a number, since the point of asking is to see which
step dominates. **`--by-recipe`** is a `GROUP BY` with no graph walk at all, and is
usually the first question worth asking before optimizing anything.

!!! warning "Cost is summed per value, not per artifact"

    A partitioned value is several artifact rows, and each one records the cost of the
    single compute that produced them all. Summing `compute_ms` over artifact rows
    multiplies such a value by its part count — a three-year `arrow-year` value reports
    117 ms for 39 ms of work.

    Every total here is deduplicated by `(recipe, version, key_hash)`. That is why the
    reports print a value count next to the artifact count: so the deduplication is
    visible rather than something you have to take on trust.

A value whose `compute_ms` was never recorded contributes nothing and is counted under
`unmeasured_values`. The total is a floor, not an estimate, and the report says so.

## Output formats

`lineage` and `impact` take `--format tree|json|dot|mermaid`; `--json` is the same as
`--format json`.

```bash
matview lineage --db cache.db --id 12 --format dot | dot -Tsvg -o lineage.svg
matview lineage --db cache.db --id 12 --format mermaid
matview impact  --db cache.db --changed 12 --all --format dot
```

In DOT and Mermaid the arrows point the way data flows — from dependency to dependent —
in both walk directions. Stale nodes are dashed; the artifact you asked about is drawn
with a heavier border.

The JSON rendering gives `nodes`, `edges` as `{"dependent": …, "dependency": …}` pairs,
`roots`, and the `direction` the walk ran in. A node's shape is the same one `matview ls`
and `matview show` use.

## What these cost to run

Measured on the 250k benchmark profile described in [Performance](performance.md);
see that page for the machine.

| Query | Work | Median |
| --- | ---: | ---: |
| `lineage` of an aggregate | 52 nodes | 0.0010 s |
| `cost` of an aggregate | 52 values | 0.0006 s |
| `critical_path` of an aggregate | 3 steps | 0.0009 s |
| `impact --changed` on the shared root | 255,000 dependents, summarized | 0.87 s |
| `cost --by-recipe` | 265,001 artifacts | 1.48 s |

The impact figure is the summary path, which counts in SQL and never materializes the
255,000 rows. Enumerating them with `--all` does materialize them, which is why it is
opt-in.
