# matview — fingerprint specification

**fingerprint_spec_version: 1**

This file is versioned independently of the code. The value above is written into
the `meta` table as `fingerprint_spec_version` when a store is created, and a store
whose recorded value differs from this one must not be read as if it were compatible.

The body below is reproduced **verbatim** from `docs/design-spec.md`. It is the
interoperability contract: any implementation in any language that agrees with this
section can share a cache, and any implementation that disagrees will silently miss
on every lookup rather than error. Change it only by bumping the version above.

---

## Canonicalization and fingerprints

This section is a specification, not an implementation detail. It is the layer that determines whether a non-Python implementation can ever share a cache. Two implementations that disagree here will not error; they will silently miss on every lookup and look like an ordinary cold cache. **Write it down before writing the code, and change it only by bumping a format version.**

### Hash function

BLAKE2b, 32-byte digest, no key or salt. Hex-encoded lowercase where stored as text.

### Key canonicalization

Keys serialize as JSON Canonicalization Scheme (RFC 8785): keys sorted by UTF-16 code unit, no insignificant whitespace, UTF-8 output. On top of JCS:

- Key fields must be scalars: string, int, float, bool, or null. No nested structures. This is a restriction, and it is deliberate; nested params make canonicalization far harder to get right across languages.
- A float whose value is integral is normalized to an int. `{"window": 14.0}` and `{"window": 14}` must produce the same hash. Getting this wrong silently splits a cache in two.
- `NaN` and infinities are rejected at registration and at call time.
- Omitted parameters are filled from the recipe's declared defaults **before** hashing, so `sma(symbol="AAPL", window=200)` and `sma(symbol="AAPL", window=200, interval="1d")` are the same artifact when `interval` defaults to `"1d"`.

`key_hash = BLAKE2b(jcs(key))`.

### Digest

`digest = BLAKE2b(artifact_bytes)`, over the encoded payload exactly as stored.

### Fingerprint

```
fingerprint(A) = BLAKE2b(
    jcs({
      "v":       1,
      "recipe":  name(A),
      "version": version(A),
      "key":     key_hash(A),
      "part":    part(A),
      "deps":    [ [d.recipe, d.key_hash, d.part, d.digest]
                   for d in sorted(deps(A)) ]
    })
)
```

Dependencies sort by `(recipe, key_hash, part)`. The crucial asymmetry, which will look like a bug to anyone reading the code: **A's own recipe version is in the fingerprint; its dependencies' recipe versions are not.** Dependencies contribute only their content digests.

This is a deliberate choice and the comment in the code should say so. It buys early cutoff: if a dependency is recomputed under a new version and produces identical bytes, nothing downstream is invalidated. It costs the ability to force a downstream rebuild by bumping an upstream version, which is occasionally wanted during development and is served instead by `force_deps`.

