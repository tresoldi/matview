# Working on matview

Read `docs/design-spec.md` before changing behaviour, `PLAN.md` for what stage the
work is in, and `DECISIONS.md` for what has already been settled where the spec was
silent. Add an entry to `DECISIONS.md` for every new judgement call; the spec requires it.

## Rules that are not style preferences

These are from the spec's "Rules the implementation must not violate". Each one is
enough on its own to make the library silently wrong rather than loudly broken, so a
plausible-looking simplification that touches one of them is a bug, not a cleanup.

1. Never delete an artifact on invalidation. Clear `valid`, keep the bytes.
2. Never cache a failed compute, in any form.
3. Artifact, dep edges and cascade land in one transaction or not at all.
4. Never rewrite a row whose digest and fingerprint already match.
5. `force` is local; only `force_deps` is transitive.
6. `on_miss` resolves to the strictest of caller and recipe, never the caller's alone.
7. Digests come from codec output; codecs must be byte-deterministic.
8. A dependency's recipe version never enters a dependent's fingerprint.
9. Empty or `None` results are values, not misses.
10. Never use pickle.

Rule 8 in particular reads like a bug to anyone encountering it fresh. It is
deliberate: it is what buys early cutoff. The comment in the code says so; leave it there.

## Other standing constraints

- `SPEC.md` is the cross-language interoperability contract. Changing it invalidates
  every existing cache, so it changes only by bumping `fingerprint_spec_version`. If it
  looks wrong, say so rather than editing it.
- Anything under the spec's "Not in v1" must not exist, including as a hidden fast path
  or an unused code path.
- The recipe registry is global and import-time. `Cache` must not be. SQLite connections
  are per-thread with WAL on.
- Stale is not absent. Three states, kept distinct everywhere: absent, stale, valid.

## Conventions

- Python 3.11+, `uv` for environments, `ruff` for lint and format, `mypy --strict`.
- Run before pushing: `uv run --all-extras ruff check . && uv run --all-extras ruff format --check . && uv run --extra dev mypy matview && uv run --all-extras pytest`.
- The test suite must also pass with **no** codec extras installed; the core has to work
  without them.
