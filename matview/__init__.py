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

__all__ = ["__version__"]

__version__ = "0.0.0"
