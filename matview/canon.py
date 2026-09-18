"""Key canonicalization, per `SPEC.md`.

This is half of the interoperability contract. Two implementations that disagree here
do not error; they miss on every lookup and look like an ordinary cold cache. Change
it only by bumping `fingerprint_spec_version`.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any, Final

import rfc8785

from matview.errors import InvalidKey

__all__ = [
    "RESERVED_KEY_FIELDS",
    "Scalar",
    "canonicalize_key",
    "index_value",
    "jcs",
    "utf16_sort_key",
]

Scalar = str | int | float | bool | None

#: ECMAScript's MAX_SAFE_INTEGER. JCS numbers are doubles, so this is the range in
#: which an int and an integral float are unambiguously the same number and render
#: identically. RFC 8785 serializers reject integers beyond it, so matview does too,
#: rather than letting the rejection surface from inside a dependency.
SAFE_INTEGER: Final = 2**53 - 1

#: Policy arguments to `get`, which the spec's own API mixes with key fields in one
#: `**kwargs`. A recipe parameter with one of these names would be silently swallowed
#: by the policy layer, so it is rejected at registration instead. See DECISIONS.md D18.
RESERVED_KEY_FIELDS: Final = frozenset({"on_miss", "force", "force_deps", "deadline", "part"})


def normalize_scalar(field: str, value: Any) -> Scalar:
    """Validate and normalize one key field.

    Key fields must be scalars. This is a restriction and it is deliberate: nested
    params make canonicalization far harder to get right across languages.
    """
    if value is None or isinstance(value, (str, bool)):
        return value  # bool first: it is a subclass of int, and JCS renders it `true`.
    if isinstance(value, int):
        if abs(value) > SAFE_INTEGER:
            raise InvalidKey(
                f"key field {field!r} is {value}, outside the exact-integer range of a "
                f"double (±(2^53 - 1)); no JCS implementation can render it identically"
            )
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise InvalidKey(f"key field {field!r} is {value!r}; NaN and infinities are rejected")
        # An integral float within the safe range *is* the corresponding int, and the
        # two must hash the same. Getting this wrong silently splits a cache in two.
        if value.is_integer() and abs(value) <= SAFE_INTEGER:
            return int(value)
        return value
    raise InvalidKey(
        f"key field {field!r} has type {type(value).__name__}; key fields must be "
        f"string, int, float, bool or null, with no nested structures"
    )


def canonicalize_key(key: Mapping[str, Any]) -> dict[str, Scalar]:
    """Normalize a key dict. Defaults must already have been filled in by the caller."""
    out: dict[str, Scalar] = {}
    for field, value in key.items():
        if not isinstance(field, str):
            raise InvalidKey(f"key field names must be strings, got {field!r}")
        out[field] = normalize_scalar(field, value)
    return out


def jcs(value: Any) -> bytes:
    """Serialize with the JSON Canonicalization Scheme (RFC 8785).

    Object keys sorted by UTF-16 code unit, no insignificant whitespace, UTF-8 output,
    ES6 number rendering. Delegated rather than hand-rolled: the hard part is number
    formatting, and getting that wrong shows up as a cold cache, not an exception.
    See DECISIONS.md D3.
    """
    try:
        return rfc8785.dumps(value)
    except Exception as exc:  # pragma: no cover - rfc8785 raises several types
        raise InvalidKey(f"value is not JCS-serializable: {exc}") from exc


def utf16_sort_key(value: str | None) -> tuple[int, bytes]:
    """Order strings by UTF-16 code unit, matching JCS, with `None` sorting first.

    Comparing UTF-16BE encodings byte-wise is the same ordering as comparing the code
    unit sequences numerically, surrogate pairs included. The leading int is what puts
    a NULL part before every string part. See DECISIONS.md D15.
    """
    if value is None:
        return (0, b"")
    return (1, value.encode("utf-16-be"))


def index_value(value: Scalar) -> str:
    """Render a key field for promotion into `artifact_key`.

    The column is `TEXT NOT NULL`, but indexed fields may be ints, floats or bools, so
    they are stored as their canonical JCS scalar rendering: `200` not `"200"`, `true`
    not `"True"`. That keeps `query()` consistent with `key_json` and the promotion
    lossless. See DECISIONS.md D8.
    """
    if isinstance(value, str):
        return value
    return jcs(value).decode("utf-8")
