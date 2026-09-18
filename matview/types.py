"""The data the core passes around.

The core knows about opaque keys and opaque bytes, and nothing about time series,
symbols or dates.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from matview.canon import Scalar

__all__ = [
    "Artifact",
    "ArtifactRef",
    "CacheStats",
    "DepEdge",
    "MissingNode",
    "OnMiss",
    "PlanReport",
    "Request",
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
    refreshed_at: int
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
    refreshed_at: int
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
    #: The resolution chain from the root down to and including this node. Knowing that
    #: `ohlcv(AAPL)` is missing is a fact; knowing which indicator asked for it is the
    #: thing that tells you what to do about it.
    path: tuple[str, ...] = ()

    def describe(self) -> str:
        params = ", ".join(f"{k}={v!r}" for k, v in sorted(self.key.items()))
        where = f"{self.recipe}(v{self.version})[{params}]"
        if self.part is not None:
            where += f" part={self.part!r}"
        reason = {"raise": "protected", "enqueue": "enqueued", "compute": "would compute"}[
            self.action
        ]
        text = f"{where} ({reason})"
        if len(self.path) > 1:
            text += "\n      wanted by " + " -> ".join(self.path[:-1])
        return text


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


@dataclass(slots=True)
class CacheStats:
    """Cumulative counters for one `Cache`.

    A cache that cannot tell you its hit rate is hard to trust, and the three outcomes
    worth distinguishing are not obvious from the outside: a plain hit, a *stale*
    artifact whose fingerprint still matched so its bytes were returned without
    recomputing, and a genuine rebuild. The middle one is early cutoff doing its job,
    and it is invisible in a two-way hit/miss count.

    Counts are per node resolved, not per artifact row, so a partitioned value counts
    once. They are incremented without a lock: under concurrent walks they may undercount
    slightly, which is an acceptable trade for telemetry that costs nothing on the hot
    path.
    """

    hits: int = 0
    """Valid artifacts returned with no work at all."""

    revalidated: int = 0
    """Stale artifacts whose recomputed fingerprint matched, returned without recomputing."""

    recomputed: int = 0
    """Nodes whose recipe actually ran."""

    blocked: int = 0
    """Nodes that could not be produced under the effective policy."""

    expired: int = 0
    """Nodes whose `max_age` had elapsed.

    A *reason*, not an outcome, and deliberately not part of `lookups`: each of these is
    also counted under `recomputed` or `blocked` depending on what followed. It answers
    the question the other counters cannot — how much of the work was driven by a clock
    rather than by a changed input. Without it a TTL-heavy graph looks exactly like one
    with an invalidation problem. See DECISIONS.md D35.
    """

    enqueued: int = 0
    """Task rows written by `on_miss="enqueue"`."""

    artifacts_written: int = 0
    bytes_written: int = 0
    compute_ms: int = 0
    """Total recipe-body time, excluding dependency resolution."""

    @property
    def lookups(self) -> int:
        """Outcomes, which partition the nodes resolved. `expired` is not among them."""
        return self.hits + self.revalidated + self.recomputed + self.blocked

    @property
    def hit_rate(self) -> float:
        """The share of lookups answered from storage, revalidation included.

        Revalidation counts as a hit because no recipe ran, which is the cost that
        matters. Returns 0.0 when nothing has been looked up.
        """
        if not self.lookups:
            return 0.0
        return (self.hits + self.revalidated) / self.lookups

    def reset(self) -> None:
        for name in self.__slots__:
            setattr(self, name, 0)

    def describe(self) -> str:
        text = (
            f"{self.lookups} lookups: {self.hits} hit, {self.revalidated} revalidated, "
            f"{self.recomputed} recomputed, {self.blocked} blocked "
            f"({self.hit_rate:.0%} hit rate)"
        )
        if self.expired:
            text += f", {self.expired} of them expired by max_age"
        return (
            f"{text}; {self.artifacts_written} artifacts / {self.bytes_written} bytes "
            f"written, {self.compute_ms} ms computing"
        )


@dataclass(frozen=True, slots=True)
class Request:
    """One root of a batch: a recipe, its parameters, and optionally a single part."""

    recipe: str
    key: Mapping[str, Any]
    part: str | None = None

    @classmethod
    def coerce(cls, value: Request | tuple[Any, ...]) -> Request:
        """Accept a `Request`, or `(recipe, key)`, or `(recipe, key, part)`."""
        if isinstance(value, Request):
            return value
        if isinstance(value, tuple) and len(value) in (2, 3):
            return cls(*value)
        raise TypeError(
            f"a batch request is a Request, or a (recipe, key) or (recipe, key, part) "
            f"tuple; got {value!r}"
        )

    def describe(self) -> str:
        params = ", ".join(f"{k}={v!r}" for k, v in sorted(self.key.items()))
        text = f"{self.recipe}[{params}]"
        return text if self.part is None else f"{text} part={self.part!r}"


@dataclass(frozen=True, slots=True)
class BatchResult:
    """What one batch walk produced.

    `values` is keyed by the request's index rather than being a list, because `None`
    and an empty frame are legitimate values: an artifact that resolved to `None` and a
    request that did not resolve at all must not look the same. A request's index is
    absent from `values` exactly when it could not be produced.
    """

    requests: tuple[Request, ...]
    values: Mapping[int, Any]
    missing: tuple[MissingNode, ...]

    def __len__(self) -> int:
        return len(self.requests)

    def __getitem__(self, index: int) -> Any:
        try:
            return self.values[index]
        except KeyError:
            raise KeyError(
                f"request {index} ({self.requests[index].describe()}) did not resolve; see .missing"
            ) from None

    def get(self, index: int, default: Any = None) -> Any:
        return self.values.get(index, default)

    @property
    def resolved(self) -> tuple[int, ...]:
        return tuple(sorted(self.values))

    @property
    def unresolved(self) -> tuple[int, ...]:
        return tuple(i for i in range(len(self.requests)) if i not in self.values)

    @property
    def complete(self) -> bool:
        return not self.missing

    def raise_if_missing(self) -> None:
        """Raise `MissingArtifacts` if anything could not be produced.

        For callers who want a batch to behave like `get`, once they have had the
        chance to look at what did resolve.
        """
        if self.missing:
            from matview.errors import MissingArtifacts

            raise MissingArtifacts(list(self.missing))

    def describe(self) -> str:
        lines = [f"{len(self.values)}/{len(self.requests)} resolved"]
        for index in self.unresolved:
            lines.append(f"  unresolved  {self.requests[index].describe()}")
        for node in self.missing:
            lines.append(f"  missing     {node.describe()}")
        return "\n".join(lines)
