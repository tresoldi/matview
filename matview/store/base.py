"""The storage seam.

v1 ships only `SqliteStore`, but the interface is defined now, because retrofitting a
transaction boundary is far harder than designing for one.

Two constraints make this worth having rather than being ceremony:

**`store()` takes the dep edges.** There is no separate `add_deps` call. Artifact,
edges and the invalidation cascade must land in one transaction, and if the caller made
two calls an HTTP-backed store could never make that atomic. Putting the transaction
boundary inside the interface is precisely what keeps a remote implementation possible.

**Fingerprinting is client-side, always.** The store is handed finished digests and
fingerprints; it never computes them. If a local SQLite store and a future HTTP store
could disagree about a hash, cross-backend lookups would miss silently and look like a
cold cache. `SPEC.md` governs the client; backends only persist what they are given.

The corollary for a future remote backend: `store()` becomes one request carrying
artifact, edges and the cascade instruction, and the server executes it in a
transaction. Do not design an API where the client walks the graph over the wire.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from matview.types import Artifact, ArtifactRef, DepEdge, StoreResult

__all__ = ["Store"]


@runtime_checkable
class Store(Protocol):
    """Persistence for artifacts and the observed DAG."""

    max_artifact_bytes: int

    def fetch(
        self, recipe: str, version: int, key_hash: str, part: str | None
    ) -> Artifact | None: ...

    def fetch_all_parts(self, recipe: str, version: int, key_hash: str) -> list[Artifact]:
        """Every part of one `(recipe, version, key_hash)`, NULL part first.

        Not in the spec's sketch of this protocol, and needed by it: a codec may emit a
        dict of parts, and a whole-value read has to find them all without knowing the
        labels in advance. See DECISIONS.md D11.
        """
        ...

    def store(self, artifact: Artifact, deps: Sequence[DepEdge]) -> StoreResult: ...

    def dependents_of(self, artifact_ids: Sequence[int]) -> list[int]: ...

    def invalidate(self, artifact_ids: Sequence[int]) -> int: ...

    def revalidate(self, artifact_id: int) -> None: ...

    def drop(self, recipe: str, version: int | None = None) -> int: ...

    def query(self, recipe: str | None = None, **indexed_fields: object) -> list[ArtifactRef]: ...
