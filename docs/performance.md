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
| `plan_many()` | 1,000 aggregate roots | 3 | 3.433 | 5.191 | 291 roots/s |

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
