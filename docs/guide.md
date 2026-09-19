# Guide

## Recipes

A recipe is a named, versioned computation, identified by `(name, version)`. It is a
*kind* of computation, not a particular result.

```python
from matview import recipe

@recipe(version=1, codec="json", index=("dataset",))
def distance_matrix(ctx, *, dataset: str, metric: str = "hamming"):
    return compute(dataset, metric)
```

Registration is import-time and global. The first parameter is the context; everything
after it is keyword-only and becomes a key field.

`version` is yours to bump when the computation changes. Bumping it creates a *new*
recipe row: old artifacts stay in place, unreachable but intact, and dropping the
version back finds them again. [`gc()`](#maintenance) is what actually removes them.

## Keys

The key is the parameters of one invocation, as a flat dict of scalars. matview never
interprets a key; it canonicalizes and hashes it.

- Key fields must be `str`, `int`, `float`, `bool` or `None`. No nested structures —
  deliberately, because nested params make canonicalization far harder to get right
  across languages.
- An integral float normalizes to an int, so `window=14.0` and `window=14` are the same
  artifact.
- `NaN` and infinities are rejected, at registration and at call time.
- Declared defaults are filled in **before** hashing, so `sma(symbol="AAPL", window=200)`
  and `sma(symbol="AAPL", window=200, interval="1d")` are the same artifact.

Anything that changes the values while leaving the computation identical belongs in the
key, not in the recipe name. That is how matview avoids a special-cased notion of series
identity.

`index=("dataset",)` promotes those fields to real columns so they can be queried. They
remain ordinary key fields semantically.

!!! warning "Reserved names"
    `on_miss`, `force`, `force_deps`, `deadline` and `part` are policy arguments to
    `get()`. A recipe parameter with one of those names is rejected at registration
    rather than silently swallowed.

## Reading

```python
cache = Cache(store=SqliteStore("cache.db"))

value = cache.get("sma", symbol="AAPL", window=200)        # decoded
raw   = cache.get_bytes("sma", symbol="AAPL", window=200)  # stored bytes
```

The codec is a property of the recipe, so decoding is a lookup and never an inference.
`get_bytes` is the escape hatch, for replication and pass-through: decoding and
re-encoding both wastes work and risks failing to round-trip.

Inside a recipe, `ctx.get` decodes by the same rule, so a recipe body should never
contain a manual decode call.

## The three states

| State | Condition | On read |
| --- | --- | --- |
| absent | no row | must compute |
| stale | row exists, `valid = 0` | verify; may or may not recompute |
| valid | row exists, `valid = 1` | return it |

**Stale is not absent.** Reading a stale artifact resolves its dependencies, recomputes
its fingerprint and compares it to the stored one. If they match, the flag flips back
and the stored bytes are returned without recomputing anything. Nothing is ever deleted
on invalidation: deleting would collapse the first two states and lose early cutoff
permanently.

## Freshness

A recipe may declare `max_age`, in seconds. Once that long has passed since its stored
value was last successfully computed or checked, matview treats the value as needing
recomputation.

```python
@recipe(version=1, codec="arrow", index=("symbol",), max_age=18 * 60 * 60)
def ohlcv(ctx, *, symbol: str, interval: str = "1d"):
    return vendor.bars(symbol, interval)
```

matview knows nothing about trading sessions, business days or cron. It decides whether
a stored value is *old*; you still decide when reads happen.

**Expiry is not invalidation.** It does not clear the stored `valid` bit and it does not
cascade — it is ephemeral scheduler state for one read, not a statement about the
dependency graph. `plan()` nevertheless reports an expired artifact under `stale`,
because it needs work before that read can be current.

**Freshness has its own clock.** Artifacts carry `refreshed_at` alongside `computed_at`,
and only `refreshed_at` advances when a recomputation produces identical bytes. This is
forced by rule 4: `computed_at` is preserved on that path, so using it for freshness
would make an unchanged source refetch on *every* read once it had expired the first
time. `computed_at` keeps its meaning — when the content last changed — and
`refreshed_at` answers when it was last confirmed.

!!! warning "A TTL is not transitive"
    Expiry is checked when the recipe that declares it is resolved. A valid dependent is
    returned from storage without walking its graph to inspect its dependencies' clocks —
    doing so would turn every ordinary cache hit into a graph traversal and make a TTL
    silently transitive.

    So a scheduler should **refresh age-limited source roots first**, then ask for the
    derived values. A changed digest then drives the ordinary invalidation cascade, and
    everything downstream follows for the usual reasons.

An expired dependency that the walk does reach still obeys its `on_miss` ceiling. A
protected one therefore blocks rather than serving its old value, and must be refreshed
explicitly as a root — the same rule as any other protected miss, and the same reason:
a permissive caller must not be able to trigger a paid refetch it never mentioned.

## Policy

```python
cache.get("sma", symbol="AAPL", window=200,
          on_miss="compute",   # compute | enqueue | raise
          force=False,
          force_deps=False,
          deadline=30.0)
```

### `on_miss` is a ceiling

Ordered permissive to strict: `compute` < `enqueue` < `raise`. At every node the walk
*descends* to, the effective policy is the stricter of what the caller asked for and
what the recipe declares. A caller may restrict further; a caller may never loosen.

A recipe declaring `on_miss="raise"` is therefore protected everywhere it appears as a
transitive dependency — a backtester asking for a Sharpe ratio with `on_miss="compute"`
cannot fire a paid vendor request six levels down that it never mentioned.

At the node the caller *names*, the caller's ask governs, which is what makes ingesting
an explicit act:

```python
cache.get("ohlcv", symbol="AAPL", on_miss="compute")   # deliberate, and it works
```

### Misses are reported in batch

matview does not raise on the first protected miss. It walks as much of the graph as it
can and raises once, with every protected node attached:

```python
try:
    cache.get("sharpe", symbol="AAPL")
except MissingArtifacts as missing:
    for node in missing.nodes:
        print(node.describe())
```

How far that reaches is set by the design's central choice, that dependencies are
discovered by execution: a recipe blocked on its first `ctx.get` has no value to
continue with. Batching collects everything wherever the dependency set is knowable
*without* running the recipe — stale verification, `plan()`, and foreign manifests.

### Batches

A single root stops at its first blocked `ctx.get`. Independent roots in one walk do
not stop each other, which is what makes the cold-run story work:

```python
result = cache.get_many([("sharpe", {"symbol": s}) for s in universe])

for index in result.resolved:
    use(result[index])
for node in result.missing:
    print(node.describe())        # every symbol that needs ingesting, in one pass
```

`values` is keyed by request index rather than positional, because `None` is a
legitimate value and "resolved to `None`" must not look like "did not resolve".
`raise_if_missing()` gives back `get`'s all-or-nothing behaviour once you have looked
at what did resolve. An exception raised by a recipe propagates immediately rather than
being collected: a miss is a policy outcome, a traceback is a bug.

Roots resolve in order, sharing one memo, so a dependency common to several of them is
computed once. They are not run in parallel — see `DECISIONS.md` D33.

Each request is a root, so a batch naming a protected recipe directly is an ingest
script:

```python
cache.get_many([("ohlcv", {"symbol": s}) for s in universe], on_miss="compute")
```

`plan_many` is the same walk with nothing computed.

### `force` is local

`force=True` recomputes that node only. If it were transitive, forcing one indicator
during development would refetch every root beneath it. `force_deps=True` is the
transitive one, and the ceiling still applies under it, so protected roots are skipped
rather than refetched.

### Deadlines

`deadline` is wall-clock seconds, converted to an absolute instant at the top-level call
and carried down the recursion; each descent checks the remaining budget first. A
deadline is preferred over a budget in abstract cost units because per-recipe cost
estimates are guesses that go stale, whereas elapsed time is a measurement.

## Planning

```python
report = cache.plan("sharpe", symbol="AAPL")
print(report.describe())
```

`plan()` walks without computing. It reports absent and stale separately, because stale
work is often near-free while absent work at a protected root means you cannot proceed
at all. It also lists recipes declared `deterministic=False`, so nobody spends an
afternoon wondering why one branch always rebuilds.

The walk stops at an absent Python recipe, necessarily — its dependencies are only
knowable by running it. It walks straight *through* a foreign recipe, whose inputs are
declared.

## Codecs

| Name | Encodes | Decodes to |
| --- | --- | --- |
| `json` | JSON-compatible values | `dict`, `list`, scalars |
| `arrow` | polars `DataFrame` or `Series` | polars `DataFrame` |
| `arrow-series` | polars `DataFrame` or `Series` | polars `Series` |
| `arrow-pandas` | pandas `DataFrame` or `Series` | pandas `DataFrame` |
| `arrow-year` | polars, sliced by calendar year | polars `DataFrame` |
| `arrow-year-pandas` | pandas, sliced by calendar year | pandas `DataFrame` |
| `npy` | numpy `ndarray` | `ndarray` |

Because fingerprints key on content digests, **a codec must produce identical bytes for
identical input, on every machine, every time**. A codec that fails this defeats early
cutoff silently: nothing errors, the cascade simply never stops.

!!! danger "Parquet is disqualified"
    It stamps a creation timestamp and writer version into the file, so two encodings of
    the same table differ and every downstream fingerprint changes for no reason.

The three Arrow codecs share **one** canonical encoding and differ only in what they
decode to, rather than inspecting the table to guess — the codec is a property of the
recipe, so decoding stays a lookup. Either can read what another wrote, so a cache is not
partitioned by which dataframe library a recipe happens to use:

```python
cache.get_bytes("bars", symbol="AAPL")   # the same bytes, whichever codec wrote them
```

That holds because the encoding pins what the producing library would otherwise choose
for itself. `string` and `large_string` differ only in offset width, and polars, pandas 2
and pandas 3 do not agree on which to emit; matview normalizes to the wide variant so the
digest depends on the data and not on a dependency's minor version. A dictionary
(categorical) column's index width is *not* normalized, because it varies with cardinality
and so is data rather than encoding — cast it explicitly if you need a
producer-independent digest for one.

### Writing one

```python
from matview.codecs import BaseCodec, register_codec

class YearCodec(BaseCodec):
    name = "years"
    partitions = True

    def encode(self, value): return {year: dump(rows) for year, rows in value.items()}
    def decode(self, data): return load(data)
    def decode_parts(self, parts): return {y: load(d) for y, d in sorted(parts.items())}

register_codec(YearCodec())
```

Returning a dict means partitioned output, with the dict keys becoming `part` labels.
Partitioning is entirely a codec concern; the scheduler treats `part` as an opaque label.

## Partitioned values

```python
whole = cache.get("series", symbol="AAPL")               # every part, merged
one   = cache.get("series", symbol="AAPL", part="2024")  # that part alone
```

A recipe that reads one part depends on **that part alone**, so a write to the current
year does not invalidate anything built from a closed one. Every part of a value is
written in one transaction, and a part a recomputation no longer produces is removed —
otherwise it would be merged into every later whole-value read forever.

### Calendar years

`arrow-year` slices a frame by the calendar year of a column named `date`:

```python
@recipe(version=1, codec="arrow-year", index=("symbol",))
def ohlcv(ctx, *, symbol: str):
    return vendor.bars(symbol)          # -> parts "2023", "2024", "2025", ...
```

The point is write amplification. A whole-series encoding means a daily append rewrites
the entire series; with yearly parts it rewrites one year, and the closed years are not
even touched — their digests and `computed_at` are unchanged, so nothing built on them is
invalidated. That is the whole argument for partitioning, and the test suite asserts it
rather than taking it on trust.

For any other column name, register your own instance:

```python
from matview.codecs import register_codec
from matview.codecs._arrow import ArrowYearCodec

register_codec(ArrowYearCodec(name="bars-year", column="day"))
```

Three details worth knowing:

- **Rows are sorted by the partitioning column** before slicing, so a frame that is merely
  reordered encodes identically rather than churning every fingerprint above it. Arrow's
  sort is stable, so rows sharing a date keep their input order — canonical for one row
  per date, and no stronger claim than that.
- **Timezone-aware timestamps are read in UTC.** The same instant is 2023 in UTC and 2024
  in Asia/Tokyo, and a row near a year boundary must not change part because of a column's
  timezone metadata. A `Date` column is unambiguous.
- **Nulls are refused.** A null date belongs to no year, and bucketing it anywhere would
  lose rows on the next read.

An empty frame has no years, so it stores as one unpartitioned artifact rather than zero
parts: an empty series is a value, not a miss.

## Foreign recipes

A recipe may be written in any language. Unlike Python recipes, foreign recipes declare
their dependencies upfront in a manifest. This is the privileged case, not a degraded
one: a declared graph can be cycle-checked and planned before anything executes.

```toml
name = "phylo_tree"
version = 2
codec = "json"
index = ["dataset"]
command = ["Rscript", "phylo_tree.R"]
source = "phylo_tree.R"

[params]
dataset = { type = "string" }
model   = { type = "string", default = "GTR" }

[[inputs]]
name   = "matrix"
recipe = "distance_matrix"
key    = { dataset = "${dataset}" }
```

```python
from matview.foreign import load_manifests

load_manifests("recipes/", store=cache.store)
```

A value that is exactly `"${name}"` substitutes that parameter with its type intact, so
`window = "${window}"` yields the integer `14`. Anything with surrounding text is string
interpolation and yields a string — and these feed `key_hash`, where `14` and `"14"` are
different artifacts.

The invocation contract is one JSON job file passed as the only argument:

```json
{
  "params":  {"dataset": "ie-ielex", "model": "GTR"},
  "inputs":  {"matrix": "/tmp/matview-8a1f/matrix"},
  "output":  "/tmp/matview-8a1f/output",
  "deadline_s": 120
}
```

- stdout and stderr are logging only. The result is read from `output`, so a stray
  `print` cannot corrupt a value.
- A nonzero exit, a missing output file or an unparseable one is a failure, and nothing
  is cached.
- The job file is left on disk on failure, so a failed job is reproducible by hand.
- The output is decoded and re-encoded canonically, so a foreign process cannot break
  byte-determinism.

## Maintenance

```python
cache.query(recipe="sma", symbol="AAPL")   # ArtifactRefs, filtered on indexed fields
cache.drop("sma", version=2)               # remove one version's artifacts
cache.gc()                                 # drop superseded versions with no dependents
```

## Observability

Every node the scheduler resolves emits one DEBUG event on the `matview.scheduler`
logger, and the same outcomes are counted cumulatively on the cache:

```python
cache.get("sharpe", symbol="AAPL")
print(cache.stats.describe())
# 3 lookups: 1 hit, 1 revalidated, 1 recomputed, 0 blocked (67% hit rate); ...
```

The three outcomes worth telling apart are not visible in a two-way hit/miss count:

| Counter | Meaning |
| --- | --- |
| `hits` | Valid, returned with no work at all |
| `revalidated` | Stale, fingerprint still matched, bytes returned **without** recomputing |
| `recomputed` | The recipe actually ran |
| `blocked` | Could not be produced under the effective policy |
| `expired` | A `max_age` had elapsed — a **reason**, not an outcome |

`revalidated` is early cutoff doing its job. `hit_rate` counts it as a hit, because no
recipe ran and that is the cost that matters.

The first four partition the nodes resolved, and `lookups` is their sum. `expired` sits
alongside them: each expired node is *also* counted under `recomputed` or `blocked`,
depending on what followed. It is there because a rebuild driven by a clock and a rebuild
driven by a changed input are worth telling apart — without it, a graph with short
`max_age` values looks indistinguishable from one with an invalidation bug.

No log handler is installed: a library that configures logging is a library that fights
its host.

## Concurrency

The SQLite connection is per-thread with WAL on, and writers serialize on
`BEGIN IMMEDIATE`. There is no distributed locking: duplicate computation by concurrent
runners is acceptable and expected to be rare, and idempotent writes handle it — a write
whose digest and fingerprint already match does not touch the row.

`Cache` is deliberately not global. The recipe registry is, because registration is
import-time; the cache is not, because a process may hold several.
