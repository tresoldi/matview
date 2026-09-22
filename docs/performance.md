# Performance baseline

This is a recorded manual baseline, not a performance contract or a CI threshold. It
uses the harness in the repository's `benchmarks/` directory, whose operations can be
re-run individually against an existing database.

## Environment

Measured on 2026-09-19 in the development container:

- Fedora Linux 43, x86_64; 246 GiB free disk space at the start.
- Python 3.14.6; SQLite 3.50.2; matview 0.1.0.
- polars 1.44.2, pyarrow 25.0.1, numpy 2.5.3, pandas 3.0.6.
- Default harness profile: one shared protected root, 5,000 symbols × 50 indicators,
  and one aggregate per symbol. It also writes 5,000 superseded legacy rows for the GC
  measurement.

The generated database had 265,001 artifacts, 500,000 dependency edges, 24.8 MB of
payload bytes, and occupied 188 MiB on disk. Payloads in this profile are tiny,
deterministic JSON values; this is deliberately an index/query measurement rather than
a realistic I/O payload test.

## Results

Times are seconds. Median and p95 are over the displayed number of samples. A one-sample
operation necessarily has identical median and p95.

| Operation | Work | Samples | Median | P95 | Throughput |
| --- | ---: | ---: | ---: | ---: | ---: |
| Cold write | 265,001 artifacts | 1 | 53.801 | 53.801 | 4,926 artifacts/s |
| Warm read | 1 aggregate | 5 | 0.000115 | 0.001140 | 8,704 reads/s |
| Stale verification | 1 aggregate | 5 | 0.004631 | 0.007096 | 216 revalidations/s |
| Cascade from root | 255,001 artifacts invalidated | 1 | 1.855 | 1.855 | 137,492 artifacts/s |
| `query()` unfiltered | 265,001 rows | 5 | 2.615 | 3.966 | 101,339 rows/s |
| `query()` filtered | 50 rows | 5 | 0.000316 | 0.000587 | 158,069 rows/s |
| `list_refs()` unfiltered | 265,001 rows | 5 | 2.599 | 2.621 | 101,963 rows/s |
| `list_refs()` filtered | 50 rows | 5 | 0.000329 | 0.000546 | 152,156 rows/s |
| `gc()` | 5,000 artifacts removed | 1 | 0.267 | 0.267 | 18,714 artifacts/s |
| `drop("indicator")` | 250,000 artifacts removed | 1 | 29.486 | 29.486 | 8,479 artifacts/s |
| `matview verify` | 265,001 payloads | 1 | 0.840 | 0.840 | 315,454 artifacts/s |
| `get_many()` | 1,000 aggregate roots | 3 | 0.040 | 0.043 | 24,880 roots/s |
| `plan_many()` | 1,000 aggregate roots | 3 | 3.433 | 5.191 | 291 roots/s (superseded, see below) |

## A second machine, and a superseded `plan_many` figure

The `plan_many` row above is **superseded**. It is kept because the rest of the table is
from the same run on the same machine, and replacing one cell with a figure from another
would silently make the table incomparable with itself.

The graph-query work re-ran `plan_many` while investigating an expected N+1. That
re-run is on a different machine and is roughly three times slower on the same cold
write (155 s against 53.8 s), so only the before/after pair below is meaningful — not
either figure against the table.

Measured 2026-09-22, same 250k profile, five samples per run, two runs each:

| `plan_many`, 1,000 aggregate roots | Median | Throughput |
| --- | ---: | ---: |
| Before: one `deps_of` per node | 3.654 s, 3.637 s | 274, 275 roots/s |
| `deps_of_many`, no single-id fast path | 4.262 s | 235 roots/s |
| `deps_of_many`, with the fast path | 3.857 s, 3.732 s | 259, 268 roots/s |
| **Shipped**: `deps_of` again | 3.724 s | 269 roots/s |

The batching was expected to be a free speedup and is a regression at every size. The
premise was wrong: the loop that looked like an N+1 iterates over the *parts of one
node*, so for an unpartitioned value it runs once and there is nothing to batch across.
Both call sites therefore keep `deps_of`, and carry a comment saying why the
obvious-looking cleanup is not one.

`plan_many` is still genuinely slow against `get_many`, and its N+1 is real — one
`deps_of` and one `fetch_all_parts` per node visited in a depth-first descent. A node's
edges have to be read before its children can be resolved, so removing it means
restructuring the suspending walk level by level. That is a scheduler change, and it has
not been made. See DECISIONS.md D52.

## Graph queries

Measured on the same second machine and the same 250k profile, so these are comparable
with the table above only in shape, not in absolute time. Five samples each, three for
the impact walk.

| Query | Work | Median | P95 |
| --- | ---: | ---: | ---: |
| `lineage()` of an aggregate | 52 nodes | 0.0010 s | 0.0018 s |
| `embodied_cost()` of an aggregate | 52 values | 0.0006 s | 0.0009 s |
| `critical_path()` of an aggregate | 3 steps | 0.0009 s | 0.0011 s |
| `impact --changed` on the shared root | 255,000 dependents, summarized | 0.87 s | 0.89 s |
| `recipe_costs()` | 265,001 artifacts | 1.48 s | 1.53 s |

Input closures are small in this shape, which is what makes the Python topological walk
behind `critical_path` the right tool for it. The impact figure is the *summary* path:
it counts in SQL and never builds 255,000 `ArtifactRef` objects, which is why
enumeration is behind `--all`. `recipe_costs()` is a full grouped scan and pays for the
deduplication that makes its `compute_ms` correct.

## What this covers

The cold-write result includes the harness's deterministic recipe execution, dependency
recording, and normal SQLite writes. The stale-verification result invalidates one
aggregate, then verifies its stored dependency graph without recomputing it. The cascade
uses the harness's one shared root, so it measures a deliberately wide invalidation walk.

The two query and listing rows contrast materializing all metadata with an indexed
`symbol` filter. `matview verify` is the command-line read-only digest walk, not a
decode/encode round-trip. The batch rows resolve already-valid aggregate roots; `plan_many`
also walks their stored dependency graphs, which is why it is materially slower.

`gc()` and `drop()` are destructive and were each run once on a suitable fresh state.
They are not directly comparable to the read-only repeated samples. SQLite does not
necessarily return freed pages to the filesystem immediately, so the database's reported
file size is not a measure of reclaimed space after either operation.

## What this does not cover

The `--realistic` 40 KiB-payload profile was intentionally not run here. It needs about
10 GiB and answers a different question: payload I/O and storage cost, rather than index
and graph behaviour. It should be run deliberately and recorded alongside the machine
that ran it.

These results are one local baseline. They do not cover concurrent writers, filesystem
or network contention, cache-cold operating-system page cache behaviour, application
recipe costs, or another SQLite/Python build. They must not be turned into CI timing
thresholds.
