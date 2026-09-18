"""The scaffolding is importable and the spec version is pinned somewhere findable.

This is the placeholder that keeps CI meaningful before stage 0 lands; it is
replaced, not extended, once the invariant tests exist.
"""

from pathlib import Path

import matview


def test_package_imports() -> None:
    assert matview.__version__


def test_fingerprint_spec_version_is_declared() -> None:
    spec = Path(__file__).resolve().parents[1] / "SPEC.md"
    assert "fingerprint_spec_version: 1" in spec.read_text(encoding="utf-8")
