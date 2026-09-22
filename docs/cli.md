# Command line

```bash
matview info   --db cache.db      # what this file is, whatever version wrote it
matview stats  --db cache.db      # totals, and a breakdown per recipe version
matview ls     --db cache.db      # list artifacts, with filters
matview show   --db cache.db --id 12
matview verify --db cache.db      # every payload against its recorded digest
matview cat    --db cache.db --id 12
matview plan   --db cache.db --recipes myapp.recipes sharpe symbol=AAPL

matview lineage --db cache.db --id 12     # what produced this?
matview impact  --db cache.db --changed 12  # what breaks if this changes?
matview cost    --db cache.db --by-recipe   # where the compute time goes
```

`--db` defaults to `$MATVIEW_DB`. Every command takes `--json`.

**Nothing here writes.** The database is opened through SQLite's read-only mode, so a
mistyped path is an error rather than a new empty database, and inspecting a cache does
not switch its journal mode underneath whoever is using it.

## What needs `--recipes`, and what does not

Most of what you want is recorded in the file, so it needs nothing imported: names,
versions, codecs, keys, digests, fingerprints, sizes, timestamps, validity and the
dependency edges.

Two things do need `--recipes MODULE` (a dotted module, or a path to a `.py` file;
repeatable):

- **A key given as parameters.** Declared defaults are filled in *before* a key is
  hashed, so `show sma symbol=AAPL` cannot know what `interval` defaults to and the hash
  will not match. Passing every field works without `--recipes`; a partial key needs it.
  When this is the cause, the error says so rather than reporting the artifact absent.
- **`plan`**, which resolves recipes in order to walk the graph.

`lineage`, `impact` and `cost` do not need it either: they read the dependency graph the
cache recorded as it ran, so nothing has to be imported to walk it. They have their own
page, [Graph queries](graph.md), because `impact --bump` needs more explaining than a
table row allows.

Decoding a value does **not** need it. The codec is recorded on the recipe row, so
`cat --id 12` decodes without importing anything — unless the codec is one your
application registered itself, which `cat` names explicitly when it happens.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Fine |
| 1 | A problem worth acting on: nothing found, a digest mismatch, a database this build cannot read, or `plan` reporting work to do |
| 2 | Usage error |

So `matview verify --db cache.db` is usable in CI, and `matview plan … ; [ $? -eq 0 ]`
answers "is this graph current".

## info

Works on **any** matview database, including one this build refuses to open. It reads
only the `meta` table, the SQLite catalogue and row counts — none of which depend on the
schema version — and says what wrote the file and whether this build can read it.

```
cache.db  (68.0 KiB, wal)
  schema_version            2 (this build implements 2)
  fingerprint_spec_version  1 (this build implements 1)
  readable by this build    yes

  rows
    artifact       4
    ...
```

This is the command to reach for first when something is wrong, because it is the one
that cannot itself be blocked by what is wrong.

## ls

```bash
matview ls --db cache.db --recipe sma --invalid
matview ls --db cache.db --where symbol=AAPL --limit 20
matview ls --db cache.db --json | jq '.[] | select(.n_bytes > 1e6) | .key'
```

`--where FIELD=VALUE` filters on fields the recipe promoted with `index=(...)`;
`--valid` / `--invalid` select current or stale rows; `--version` pins one recipe
version, which is how you find artifacts left behind by a version bump.

## show

Addresses an artifact by `--id`, by `--key-hash`, or by recipe and key fields. Prints
everything stored about it plus its dependencies (what it read, and at which digest) and
its dependents.

Key values are parsed as JSON, then taken as strings if that fails. So `window=2` is the
integer `2`, `flag=true` is a boolean, and a string that looks like a number can be
forced with `window='"2"'`. This matters: `2` and `"2"` canonicalize to different keys
and therefore different artifacts.

## verify

Recomputes BLAKE2b over every stored payload and compares it to the `digest` column.

This is the one corruption the library cannot detect on its own. A payload that no longer
matches its digest does not raise anywhere — it silently invalidates every fingerprint
above it, and the cache goes on looking healthy while never hitting again. Exits 1 if
anything mismatches.

```bash
matview verify --db cache.db --recipe ohlcv
matview verify --db cache.db --json | jq .mismatches
```

## cat

Prints the decoded value, or `--raw` for the stored bytes exactly as they are (which is
also how to pipe a payload into another tool without a decode-and-re-encode round trip).

## plan

The `plan()` walk, rendered. Reports absent, stale and expired separately, lists recipes
declared `deterministic=False`, and gives each absent node the chain of recipes that
wanted it. Needs `--recipes`.

## lineage, impact, cost

Three questions about the recorded dependency graph, each with an indented-tree, JSON,
Graphviz DOT and Mermaid rendering. They have their own page: [Graph queries](graph.md).

```bash
matview lineage --db cache.db --id 12 --format dot | dot -Tsvg -o lineage.svg
matview impact  --db cache.db --bump ohlcv
matview cost    --db cache.db --id 12 --critical-path
```

## Not here yet

`drop` and `gc` mutate, and are deliberately absent. They exist on `Cache`.
`matview impact --drop` shows what a drop would do, which is the useful half.
