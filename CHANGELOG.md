# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The one thing that is **not** versioned by this file is the fingerprint specification.
It lives in [`SPEC.md`](SPEC.md) with its own `fingerprint_spec_version`, recorded in
every database's `meta` table. Changing it invalidates every existing cache, so it
changes only by bumping that number — never as part of an ordinary release.

## [Unreleased]

First working version, not yet published. Will be released as 0.1.0.

### Added

- Content-addressed compute cache over a dependency graph discovered by execution: a
  suspending scheduler with constructive traces. Every `ctx.get` is recorded, and the
  artifact, its dep edges and the invalidation cascade land in one transaction.
- Three artifact states kept distinct — absent, stale, valid. A stale artifact is
  verified by recomputing its fingerprint from its stored edges, and returned without
  recomputing when it still matches. Invalidation never deletes.
- `SqliteStore`, WAL, connection per thread, writers serialized on `BEGIN IMMEDIATE`,
  with a configurable `max_artifact_bytes` (64 MiB) that raises rather than writing.
- Policy: an `on_miss` ceiling resolved against each recipe's own declaration, local
  `force` and transitive `force_deps`, a propagated wall-clock `deadline`, and batched
  reporting of every protected miss the walk could reach.
- `plan()`, which walks without computing and reports absent and stale separately.
- Codecs `json`, `arrow`, `arrow-series` and `npy`, each byte-deterministic, with
  partitioned output supported through `decode_parts`. The arrow codecs encode through
  pyarrow with pinned IPC options rather than through polars' writer, whose output is
  not stable across polars releases.
- Foreign recipes: a TOML manifest plus an executable, with inputs declared upfront so
  the graph can be cycle-checked and planned before anything runs. One JSON job file as
  the only argument; results read from a file, never from stdout.
- `SPEC.md` as the cross-language interoperability contract, with golden vectors for
  hashes and for codec bytes checked into the test suite.
- Two examples, one financial and one not, both exercised by the test suite.

### Notes

- Every choice made where the design document was silent is recorded, with its
  reasoning, in [`DECISIONS.md`](DECISIONS.md).
