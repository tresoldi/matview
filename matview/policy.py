"""Call policy: the `on_miss` ceiling, the two kinds of force, and the deadline."""

from __future__ import annotations

import time
from dataclasses import dataclass, replace

from matview.errors import DeadlineExceeded
from matview.types import OnMiss, stricter

__all__ = ["Policy"]


@dataclass(frozen=True, slots=True)
class Policy:
    """What a caller asked for, carried down the recursion.

    `on_miss` here is the *caller's* ask. It is combined with each recipe's own
    declaration at each node, and the stricter of the two wins. A recipe's declaration
    protects that recipe; it does not propagate to the recipe's own dependencies.
    """

    on_miss: OnMiss = "compute"
    force: bool = False
    force_deps: bool = False
    deadline_at: float | None = None

    @classmethod
    def from_call(
        cls,
        *,
        on_miss: OnMiss = "compute",
        force: bool = False,
        force_deps: bool = False,
        deadline: float | None = None,
    ) -> Policy:
        """Convert a wall-clock budget to an absolute monotonic instant, once, at the top.

        A deadline is deliberately preferred over a compute budget in abstract cost
        units, because per-recipe cost estimates are guesses that go stale, whereas
        elapsed time is a measurement.
        """
        if deadline is not None and deadline <= 0:
            raise DeadlineExceeded("(top level)", deadline)
        return cls(
            on_miss=on_miss,
            force=force,
            force_deps=force_deps,
            deadline_at=None if deadline is None else time.monotonic() + deadline,
        )

    def narrow(self, on_miss: OnMiss | None) -> Policy:
        """Restrict further. A caller may never loosen, so this only ever tightens."""
        if on_miss is None:
            return self
        return replace(self, on_miss=stricter(self.on_miss, on_miss))

    def remaining(self) -> float | None:
        if self.deadline_at is None:
            return None
        return self.deadline_at - time.monotonic()

    def check(self, node: str) -> None:
        """Raise if the budget is already exhausted, before descending any further."""
        left = self.remaining()
        if left is not None and left <= 0:
            raise DeadlineExceeded(node, 0.0)
