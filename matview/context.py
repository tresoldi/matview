"""The context handed to a running recipe.

Every `ctx.get` a recipe makes is recorded, and the resulting edges are written to the
`dep` table alongside the artifact in one transaction. The graph is therefore always an
accurate record of what actually happened: cycles are caught by the in-flight stack
rather than by static analysis, and a recipe that takes a different branch on different
parameters records different dependencies, correctly.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from matview.types import OnMiss

if TYPE_CHECKING:
    from matview.cache import Cache, _Walk
    from matview.policy import Policy
    from matview.types import DepEdge

__all__ = ["Context"]


class Context:
    """What a recipe body receives as its first argument."""

    def __init__(self, cache: Cache, walk: _Walk, policy: Policy, collected: list[DepEdge]) -> None:
        self._cache = cache
        self._walk = walk
        self._policy = policy
        self._collected = collected
        #: Seconds spent resolving dependencies, subtracted from this node's compute_ms
        #: so the recorded cost is the recipe body's own. See DECISIONS.md D6.
        self.dep_seconds = 0.0

    def get(
        self,
        recipe: str,
        *,
        part: str | None = None,
        on_miss: OnMiss | None = None,
        **params: Any,
    ) -> Any:
        """Resolve a dependency and return the decoded value.

        Decoding is by the dependency's own declared codec, so a recipe body should
        never contain a manual decode call.
        """
        data, codec = self._fetch(recipe, part, on_miss, params)
        return codec.decode(data) if isinstance(data, bytes) else codec.decode_parts(data)

    def get_bytes(
        self,
        recipe: str,
        *,
        part: str | None = None,
        on_miss: OnMiss | None = None,
        **params: Any,
    ) -> bytes | dict[str, bytes]:
        """The stored bytes, undecoded. For replication and pass-through, not for logic."""
        return self._fetch(recipe, part, on_miss, params)[0]

    @property
    def deadline_remaining(self) -> float | None:
        """Wall-clock seconds left in the budget, or None when there is no deadline."""
        return self._policy.remaining()

    def _fetch(
        self, recipe: str, part: str | None, on_miss: OnMiss | None, params: dict[str, Any]
    ) -> tuple[bytes | dict[str, bytes], Any]:
        started = time.monotonic()
        try:
            rows, codec = self._cache._resolve_dependency(
                self._walk, self._policy, recipe, params, part, on_miss
            )
        finally:
            self.dep_seconds += time.monotonic() - started

        # One edge per part actually read. A recipe that selects a single part of a
        # partitioned dependency therefore depends on that part alone, and a write to
        # another part does not invalidate it. See DECISIONS.md D11.
        self._collected.extend(self._cache._edges_for(rows))

        if part is not None or (len(rows) == 1 and rows[0].part is None):
            return rows[0].data, codec
        return {str(row.part): row.data for row in rows}, codec
