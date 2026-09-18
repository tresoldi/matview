"""matview — materialized views for derived values.

Declare recipes: functions that compute a value from parameters and from other
recipes' outputs. Ask the cache for a value; it returns the stored one if it is
current and computes it if not, recursively resolving whatever that computation
turns out to need. Results are memoized in SQLite and reused across processes,
machines and runs.

A suspending scheduler with constructive traces, in the sense of *Build Systems
a la Carte*: dependencies are discovered by execution rather than declared, and
staleness is decided by content digests rather than timestamps.

The interoperability contract lives in ``SPEC.md`` and is versioned independently
of this package.
"""

from matview.cache import Cache
from matview.codecs import Codec, get_codec, register_codec
from matview.errors import (
    ArtifactTooLarge,
    CodecError,
    CycleError,
    DeadlineExceeded,
    ForeignRecipeError,
    InvalidKey,
    MatviewError,
    MissingArtifacts,
    RecipeNotFound,
)
from matview.registry import REGISTRY, Recipe, Registry, recipe
from matview.store import SqliteStore, Store
from matview.types import (
    Artifact,
    ArtifactRef,
    BatchResult,
    CacheStats,
    DepEdge,
    MissingNode,
    OnMiss,
    PlanReport,
    Request,
    StoreResult,
)

__all__ = [
    "REGISTRY",
    "Artifact",
    "ArtifactRef",
    "ArtifactTooLarge",
    "BatchResult",
    "Cache",
    "CacheStats",
    "Codec",
    "CodecError",
    "CycleError",
    "DeadlineExceeded",
    "DepEdge",
    "ForeignRecipeError",
    "InvalidKey",
    "MatviewError",
    "MissingArtifacts",
    "MissingNode",
    "OnMiss",
    "PlanReport",
    "Recipe",
    "RecipeNotFound",
    "Registry",
    "Request",
    "SqliteStore",
    "Store",
    "StoreResult",
    "__version__",
    "get_codec",
    "recipe",
    "register_codec",
]

__version__ = "0.1.0"
