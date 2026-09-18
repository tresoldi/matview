"""Exceptions raised by matview.

All of them descend from `MatviewError`, so a caller can catch this library without
catching everything.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from matview.types import MissingNode

__all__ = [
    "ArtifactTooLarge",
    "CodecError",
    "CodecUnavailable",
    "CycleError",
    "DeadlineExceeded",
    "ForeignRecipeError",
    "InvalidKey",
    "MatviewError",
    "MissingArtifacts",
    "RecipeNotFound",
    "StoreError",
]


class MatviewError(Exception):
    """Base for everything this library raises."""


class InvalidKey(MatviewError):
    """A key field is not a scalar, is NaN or infinite, or uses a reserved name."""


class RecipeNotFound(MatviewError):
    """No recipe is registered under that name."""


class CycleError(MatviewError):
    """A recipe asked, transitively, for its own output.

    Cycles are detected by the in-flight stack rather than by static analysis, so the
    message carries the full path that closed the loop.
    """

    def __init__(self, path: list[str]) -> None:
        self.path = path
        super().__init__("dependency cycle: " + " -> ".join(path))


class DeadlineExceeded(MatviewError):
    """The wall-clock budget handed to the top-level call is already spent."""

    def __init__(self, node: str, budget_s: float) -> None:
        self.node = node
        self.budget_s = budget_s
        super().__init__(f"deadline of {budget_s:g}s exhausted before resolving {node}")


class MissingArtifacts(MatviewError):
    """Nodes that were needed but could not be computed under the effective policy.

    Carries *every* such node rather than the first, because the point of the
    protection is to tell a cold run exactly what needs ingesting in one pass.
    """

    def __init__(self, nodes: list[MissingNode]) -> None:
        self.nodes = nodes
        lines = "\n".join(f"  - {n.describe()}" for n in nodes)
        super().__init__(f"{len(nodes)} artifact(s) missing under the effective policy:\n{lines}")


class ArtifactTooLarge(MatviewError):
    """A payload exceeded `max_artifact_bytes`.

    Raised loudly on purpose: it surfaces a codec or partitioning problem when it
    appears rather than as mysterious slowness months later.
    """


class CodecError(MatviewError):
    """A codec could not encode or decode a value."""


class CodecUnavailable(CodecError):
    """A codec exists but its optional extra is not installed."""


class ForeignRecipeError(MatviewError):
    """A foreign recipe's manifest, invocation or output was unusable."""


class StoreError(MatviewError):
    """The store could not satisfy a request."""
