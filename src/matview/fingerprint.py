"""Hashes, per `SPEC.md`.

The other half of the interoperability contract. `SPEC.md` is authoritative; this
module implements it and nothing more.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Final

from matview.canon import canonicalize_key, jcs, utf16_sort_key

if TYPE_CHECKING:
    from matview.types import DepEdge

__all__ = [
    "FINGERPRINT_SPEC_VERSION",
    "canonical_key_hash",
    "digest_bytes",
    "fingerprint",
    "key_hash",
    "source_hash",
]

#: Bumping this invalidates every existing cache. See SPEC.md.
FINGERPRINT_SPEC_VERSION: Final = 1

#: BLAKE2b, 32-byte digest, no key or salt. Hex-encoded lowercase where stored as text.
_DIGEST_SIZE: Final = 32


def _blake2b(data: bytes) -> str:
    return hashlib.blake2b(data, digest_size=_DIGEST_SIZE).hexdigest()


def key_hash(key: Mapping[str, Any]) -> str:
    """`key_hash = BLAKE2b(jcs(key))`."""
    return _blake2b(jcs(canonicalize_key(key)))


def canonical_key_hash(key: Mapping[str, Any]) -> tuple[str, str]:
    """Return `(key_json, key_hash)`, so callers hash and store the *same* canonical form."""
    canonical = jcs(canonicalize_key(key))
    return canonical.decode("utf-8"), _blake2b(canonical)


def digest_bytes(data: bytes) -> str:
    """`digest = BLAKE2b(artifact_bytes)`, over the encoded payload exactly as stored."""
    return _blake2b(data)


def source_hash(data: bytes) -> str:
    """Advisory only. Recorded on the recipe row, never fingerprinted."""
    return _blake2b(data)


def fingerprint(
    *,
    recipe: str,
    version: int,
    key_hash: str,
    part: str | None,
    deps: Sequence[DepEdge],
) -> str:
    """The hash of the inputs that produced an artifact.

    An artifact is current when its recomputed fingerprint matches the stored one.

    Note the asymmetry, which will look like a bug to anyone reading this fresh: **A's
    own recipe version is in the fingerprint; its dependencies' recipe versions are
    not.** Dependencies contribute only their content digests, which is why `DepEdge`
    carries a `version` that this function pointedly ignores.

    This is deliberate. It buys early cutoff: if a dependency is recomputed under a new
    version and produces identical bytes, nothing downstream is invalidated. It costs
    the ability to force a downstream rebuild by bumping an upstream version, which is
    occasionally wanted during development and is served instead by `force_deps`.

    Do not "fix" it. See SPEC.md, and rule 8 in the spec's hard rules.
    """
    ordered = sorted(
        deps,
        key=lambda d: (
            utf16_sort_key(d.recipe),
            utf16_sort_key(d.key_hash),
            utf16_sort_key(d.part),
        ),
    )
    payload = {
        "v": FINGERPRINT_SPEC_VERSION,
        "recipe": recipe,
        "version": version,
        "key": key_hash,
        "part": part,
        "deps": [[d.recipe, d.key_hash, d.part, d.digest] for d in ordered],
    }
    return _blake2b(jcs(payload))
