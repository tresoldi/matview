"""Fixtures for the matview test suite.

The recipe registry is global and import-time by design, which is fine for a program
and poison for a test suite, so every test gets an isolated `Registry` and passes it
explicitly. That is also the reason `Cache` must not be global: see the spec's "Three
behaviours to fix from the start".

Imports are deliberately inside the fixtures. The suite is built stage by stage, and
a module-level import of the whole public API would make every earlier stage's tests
uncollectable until the last stage landed.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture
def registry() -> Any:
    from matview import Registry

    return Registry()


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Any]:
    from matview import SqliteStore

    s = SqliteStore(tmp_path / "test.db")
    yield s
    s.close()


@pytest.fixture
def cache(store: Any, registry: Any) -> Any:
    from matview import Cache

    return Cache(store=store, registry=registry)


def make_artifact(
    *,
    recipe: str = "r",
    version: int = 1,
    codec: str = "json",
    key: dict[str, object] | None = None,
    part: str | None = None,
    data: bytes = b"{}",
    deps: tuple = (),
    index_fields: dict[str, str] | None = None,
) -> Any:
    """Build an Artifact the way the scheduler would, for tests that poke the store directly."""
    from matview.fingerprint import canonical_key_hash, digest_bytes, fingerprint
    from matview.types import Artifact

    key = {} if key is None else key
    key_json, key_hash = canonical_key_hash(key)
    dg = digest_bytes(data)
    return Artifact(
        recipe=recipe,
        version=version,
        codec=codec,
        key_hash=key_hash,
        key_json=key_json,
        part=part,
        digest=dg,
        fingerprint=fingerprint(
            recipe=recipe, version=version, key_hash=key_hash, part=part, deps=deps
        ),
        n_bytes=len(data),
        data=data,
        computed_at=int(time.time()),
        refreshed_at=int(time.time()),
        compute_ms=1,
        index_fields=index_fields or {},
    )
