# Scale harness

This is a manual measurement harness, not part of the test suite. The default profile
creates one protected `market` root, 5,000 symbols × 50 indicators, and one aggregate
per symbol: roughly 255,000 artifacts, with tiny deterministic JSON payloads. The
`--realistic` profile uses roughly 40 KiB per payload and needs about 10 GiB.

Create a database, then run any operation against it independently:

```bash
uv run --all-extras python benchmarks/scale.py --operation cold-write --reset
uv run --all-extras python benchmarks/scale.py --operation warm-read
uv run --all-extras python benchmarks/scale.py --operation query --repeats 5
```

The operations are `warm-read`, `stale-verify`, `cascade`, `query`, `list-refs`, `gc`,
`drop`, `verify`, `get-many`, and `plan-many`; results report samples, median, p95,
throughput, and database size. `cascade`, `drop`, and `gc` mutate the database, so each
requires `--repeats 1`; regenerate before measuring a different destructive operation.

Use `--symbols`, `--indicators`, and `--batch-roots` to change the shape. The default
`--repeats 1` keeps the 250k profile a deliberate one-pass measurement; increase it for
repeatable read-only operations when you want a more representative p95.
