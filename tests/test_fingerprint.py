"""Stage 1: canonicalization and fingerprints, per SPEC.md.

These are the tests that stop a future refactor from silently changing a hash. A
changed hash does not error; it misses on every lookup and looks like a cold cache.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from matview.canon import canonicalize_key, jcs, utf16_sort_key
from matview.errors import InvalidKey
from matview.fingerprint import (
    FINGERPRINT_SPEC_VERSION,
    canonical_key_hash,
    digest_bytes,
    fingerprint,
    key_hash,
)
from matview.types import DepEdge

GOLDEN = Path(__file__).with_name("golden_vectors.json")


def _dep(recipe: str, kh: str, part: str | None, digest: str, version: int = 1) -> DepEdge:
    return DepEdge(
        recipe=recipe, version=version, key_hash=kh, part=part, digest=digest, artifact_id=0
    )


# --- key canonicalization --------------------------------------------------


def test_integral_float_normalizes_to_int() -> None:
    assert key_hash({"window": 14.0}) == key_hash({"window": 14})
    assert canonicalize_key({"window": 14.0}) == {"window": 14}
    assert canonicalize_key({"window": -0.0}) == {"window": 0}


def test_non_integral_float_is_left_alone() -> None:
    assert canonicalize_key({"alpha": 0.1}) == {"alpha": 0.1}
    assert key_hash({"alpha": 0.1}) != key_hash({"alpha": 0})


def test_integral_float_outside_the_safe_range_stays_a_float() -> None:
    # 1e300 is a double that no integer rendering can match across languages, so it
    # keeps its ES6 form rather than becoming a 301-digit integer. DECISIONS.md D16.
    assert canonicalize_key({"big": 1e300}) == {"big": 1e300}
    assert jcs(canonicalize_key({"big": 1e300})) == b'{"big":1e+300}'


def test_integers_outside_the_safe_range_are_rejected() -> None:
    with pytest.raises(InvalidKey, match="exact-integer range"):
        canonicalize_key({"big": 2**53 + 1})


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nan_and_infinities_are_rejected(value: float) -> None:
    assert not math.isfinite(value)
    with pytest.raises(InvalidKey, match="NaN and infinities"):
        canonicalize_key({"x": value})


@pytest.mark.parametrize("value", [[1, 2], {"a": 1}, (1,), {1, 2}, object()])
def test_nested_structures_are_rejected(value: object) -> None:
    with pytest.raises(InvalidKey, match="key fields must be"):
        canonicalize_key({"x": value})


def test_bool_is_not_an_int() -> None:
    assert canonicalize_key({"flag": True}) == {"flag": True}
    assert jcs(canonicalize_key({"flag": True})) == b'{"flag":true}'
    assert key_hash({"flag": True}) != key_hash({"flag": 1})


def test_keys_sort_by_utf16_code_unit_and_output_is_utf8() -> None:
    canonical, _ = canonical_key_hash({"b": 1, "a": 2, "Z": 3, "é": 4})
    assert canonical == '{"Z":3,"a":2,"b":1,"é":4}'


def test_key_field_names_must_be_strings() -> None:
    with pytest.raises(InvalidKey, match="must be strings"):
        canonicalize_key({1: "x"})


def test_key_order_does_not_affect_the_hash() -> None:
    assert key_hash({"a": 1, "b": 2}) == key_hash({"b": 2, "a": 1})


# --- fingerprint -----------------------------------------------------------


def test_dep_order_does_not_affect_the_fingerprint() -> None:
    a = _dep("alpha", "1" * 64, None, "d1")
    b = _dep("beta", "2" * 64, None, "d2")
    forwards = fingerprint(recipe="r", version=1, key_hash="k", part=None, deps=(a, b))
    backwards = fingerprint(recipe="r", version=1, key_hash="k", part=None, deps=(b, a))
    assert forwards == backwards


def test_null_part_sorts_before_every_string_part() -> None:
    assert utf16_sort_key(None) < utf16_sort_key("")
    assert utf16_sort_key("") < utf16_sort_key("2026")
    whole = _dep("x", "1" * 64, None, "d0")
    y2026 = _dep("x", "1" * 64, "2026", "d1")
    assert fingerprint(
        recipe="r", version=1, key_hash="k", part=None, deps=(y2026, whole)
    ) == fingerprint(recipe="r", version=1, key_hash="k", part=None, deps=(whole, y2026))


def test_part_is_in_the_fingerprint() -> None:
    assert fingerprint(recipe="r", version=1, key_hash="k", part="2025", deps=()) != fingerprint(
        recipe="r", version=1, key_hash="k", part="2026", deps=()
    )
    assert fingerprint(recipe="r", version=1, key_hash="k", part=None, deps=()) != fingerprint(
        recipe="r", version=1, key_hash="k", part="", deps=()
    )


def test_a_dependency_digest_change_changes_the_fingerprint() -> None:
    before = _dep("x", "1" * 64, None, "d1")
    after = _dep("x", "1" * 64, None, "d2")
    assert fingerprint(
        recipe="r", version=1, key_hash="k", part=None, deps=(before,)
    ) != fingerprint(recipe="r", version=1, key_hash="k", part=None, deps=(after,))


def test_digest_is_over_the_bytes_exactly_as_stored() -> None:
    assert digest_bytes(b"") == digest_bytes(b"")
    assert digest_bytes(b"a") != digest_bytes(b"b")
    assert len(digest_bytes(b"a")) == 64  # 32 bytes, hex, lowercase
    assert digest_bytes(b"a").islower()


# --- golden vectors --------------------------------------------------------


def test_golden_vectors_still_hold() -> None:
    """Fixed inputs, fixed digests, checked into the repo.

    This is the regression guard on the interoperability contract, and the reference a
    second-language implementation validates against. If this fails, either a hash
    changed (every existing cache just became unreadable) or `fingerprint_spec_version`
    needs bumping. It is not a test to update casually.
    """
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert golden["fingerprint_spec_version"] == FINGERPRINT_SPEC_VERSION

    for case in golden["keys"]:
        canonical, kh = canonical_key_hash(case["key"])
        assert canonical == case["key_json"], case
        assert kh == case["key_hash"], case

    for case in golden["digests"]:
        assert digest_bytes(bytes.fromhex(case["bytes_hex"])) == case["digest"], case

    for case in golden["fingerprints"]:
        deps = tuple(
            _dep(d["recipe"], d["key_hash"], d["part"], d["digest"], d["version"])
            for d in case["deps"]
        )
        assert (
            fingerprint(
                recipe=case["recipe"],
                version=case["version"],
                key_hash=case["key_hash"],
                part=case["part"],
                deps=deps,
            )
            == case["fingerprint"]
        ), case
