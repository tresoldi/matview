"""The data the core passes around.

The core knows about opaque keys and opaque bytes, and nothing about time series,
symbols or dates.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from matview.canon import Scalar

__all__ = [
    "Artifact",
    "ArtifactRef",
    "DepEdge",
    "MissingNode",
    "OnMiss",
    "PlanReport",
    "StoreResult",
    "stricter",
]

#: Ordered permissive to strict. A caller may restrict further; a caller may never loosen.
OnMiss = Literal["compute", "enqueue", "raise"]

_ON_MISS_RANK: dict[str, int] = {"compute": 0, "enqueue": 1, "raise": 2}


def stricter(a: OnMiss, b: OnMiss) -> OnMiss:
    """The less permissive of two policies. See rule 6: never the caller's alone."""
    return a if _ON_MISS_RANK[a] >= _ON_MISS_RANK[b] else b


@dataclass(frozen=True, slots=True)
class DepEdge:
    """One observed edge: a dependent read this artifact, at this digest, at compute time.

    `version` is here because the store needs to resolve the row, and `artifact_id`
    because the cascade walks on it. Neither reaches the fingerprint — see rule 8 and
    the note in `matview.fingerprint.fingerprint`.
    """

    recipe: str
    version: int
    key_hash: str
    part: str | None
    digest: str
    artifact_id: int
    #: The dependency's canonical key. Carried so a stale verification can re-resolve
    #: the dependency at its *current* registered version, which may be a different
    #: artifact row. Not fingerprinted, for the same reason `version` is not.
    key_json: str = ""


@dataclass(frozen=True, slots=True)
class Artifact:
    """One stored result: `(recipe, key_hash, part)` -> bytes."""

    recipe: str
    version: int
    codec: str
    key_hash: str
    key_json: str
    part: str | None
    digest: str
    fingerprint: str
    n_bytes: int
    data: bytes
    computed_at: int
    compute_ms: int | None = None
    valid: bool = True
    id: int | None = None
    source_hash: str | None = None
    index_fields: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    """An artifact without its payload, for querying and planning."""

    id: int
    recipe: str
    version: int
    key_hash: str
    key_json: str
    part: str | None
    digest: str
    fingerprint: str
    valid: bool
    n_bytes: int
    computed_at: int
    compute_ms: int | None


@dataclass(frozen=True, slots=True)
class StoreResult:
    """What a write actually did.

    `written=False` is the rule 4 path: a row with the same digest and fingerprint was
    already there and its payload was left alone.
    """

    artifact_id: int
    written: bool
    digest_changed: bool
    invalidated: int


@dataclass(frozen=True, slots=True)
class MissingNode:
    """A node that was needed but could not be computed under the effective policy."""

    recipe: str
    version: int
    key: Mapping[str, Scalar]
    part: str | None
    #: What the effective policy did here. "compute" appears only in a plan, where
    #: nothing runs, and means this node would be computed.
    action: Literal["compute", "enqueue", "raise"]

    def describe(self) -> str:
        params = ", ".join(f"{k}={v!r}" for k, v in sorted(self.key.items()))
        where = f"{self.recipe}(v{self.version})[{params}]"
        if self.part is not None:
            where += f" part={self.part!r}"
        reason = {"raise": "protected", "enqueue": "enqueued", "compute": "would compute"}[
            self.action
        ]
        return f"{where} ({reason})"


@dataclass(frozen=True, slots=True)
class PlanReport:
    """What a walk found without computing anything.

    Absent and stale are reported separately because stale work is often near-free
    while absent work at a protected root means you cannot proceed at all.
    """

    absent: tuple[MissingNode, ...]
    stale: tuple[ArtifactRef, ...]
    valid: tuple[ArtifactRef, ...]
    nondeterministic: tuple[str, ...]

    @property
    def is_current(self) -> bool:
        return not self.absent and not self.stale

    def describe(self) -> str:
        lines = [
            f"absent: {len(self.absent)}",
            f"stale: {len(self.stale)}",
            f"valid: {len(self.valid)}",
        ]
        if self.nondeterministic:
            lines.append("non-deterministic (always cascades): " + ", ".join(self.nondeterministic))
        for node in self.absent:
            lines.append(f"  absent  {node.describe()}")
        for ref in self.stale:
            lines.append(f"  stale   {ref.recipe}(v{ref.version}) {ref.key_json}")
        return "\n".join(lines)
