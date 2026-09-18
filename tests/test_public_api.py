"""The public surface is what it says it is.

`__all__` listing a name the module does not actually import is a bug that no other
test notices: everything inside the package keeps working, and only a user writing
`from matview import ThatName` finds out. It happened once here already.
"""

from __future__ import annotations

import importlib

import pytest

import matview

MODULES = [
    "matview",
    "matview.cache",
    "matview.canon",
    "matview.codecs",
    "matview.context",
    "matview.errors",
    "matview.fingerprint",
    "matview.foreign",
    "matview.policy",
    "matview.registry",
    "matview.store",
    "matview.store.base",
    "matview.store.sqlite",
    "matview.types",
]


@pytest.mark.parametrize("module_name", MODULES)
def test_everything_in_dunder_all_actually_exists(module_name: str) -> None:
    module = importlib.import_module(module_name)
    declared = getattr(module, "__all__", None)
    if declared is None:
        pytest.skip(f"{module_name} declares no __all__")
    missing = [name for name in declared if not hasattr(module, name)]
    assert missing == [], f"{module_name}.__all__ names nothing: {missing}"


@pytest.mark.parametrize("module_name", MODULES)
def test_dunder_all_has_no_duplicates(module_name: str) -> None:
    """Ordering is ruff's job (RUF022, which sorts SCREAMING_CASE first); duplicates are not."""
    module = importlib.import_module(module_name)
    declared = getattr(module, "__all__", None)
    if declared is None:
        pytest.skip(f"{module_name} declares no __all__")
    assert len(set(declared)) == len(declared), "duplicate entries"


def test_the_documented_entry_points_import_from_the_package_root() -> None:
    from matview import (
        Cache,
        CacheStats,
        MissingArtifacts,
        Registry,
        Request,
        SqliteStore,
        recipe,
    )

    assert all(
        obj is not None
        for obj in (Cache, CacheStats, MissingArtifacts, Registry, Request, SqliteStore, recipe)
    )


def test_the_package_is_typed() -> None:
    from pathlib import Path

    assert (Path(matview.__file__).parent / "py.typed").exists()
