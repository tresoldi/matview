"""Regenerate tests/golden_vectors.json.

Run this only when `fingerprint_spec_version` is deliberately bumped. Regenerating it
to make a failing test pass would defeat the entire point of having it: the file is
what proves a hash did not change by accident.

    uv run python tools/gen_golden_vectors.py
"""

from __future__ import annotations

import json
from pathlib import Path

from matview.fingerprint import (
    FINGERPRINT_SPEC_VERSION,
    canonical_key_hash,
    digest_bytes,
    fingerprint,
)
from matview.types import DepEdge

OUT = Path(__file__).resolve().parents[1] / "tests" / "golden_vectors.json"

KEYS: list[dict[str, object]] = [
    {},
    {"symbol": "AAPL", "window": 200},
    {"window": 14.0},
    {"window": 14},
    {"alpha": 0.1},
    {"big": 1e300},
    {"tiny": 1e-7},
    {"flag": True, "missing": None},
    {"b": 1, "a": 2, "Z": 3, "é": 4},
    {"unicode": "日本語 ü \U0001f600"},
    {"symbol": "AAPL", "interval": "1d", "adjust": "split"},
    {"negzero": -0.0},
    {"edge": 2**53 - 1},
    {"edge_negative": -(2**53 - 1)},
]

DIGEST_INPUTS: list[bytes] = [
    b"",
    b"a",
    b'{"a":1}',
    bytes(range(256)),
    "日本語".encode(),
]

DEPS = [
    DepEdge(
        recipe="ohlcv", version=1, key_hash="b" * 64, part=None, digest="c" * 64, artifact_id=1
    ),
    DepEdge(
        recipe="ohlcv", version=7, key_hash="b" * 64, part="2026", digest="d" * 64, artifact_id=2
    ),
    DepEdge(
        recipe="calendar", version=2, key_hash="e" * 64, part=None, digest="f" * 64, artifact_id=3
    ),
]

FINGERPRINTS: list[dict[str, object]] = [
    {"recipe": "ohlcv", "version": 1, "key_hash": "a" * 64, "part": None, "deps": []},
    {"recipe": "sma", "version": 3, "key_hash": "a" * 64, "part": None, "deps": DEPS[:1]},
    {"recipe": "sma", "version": 3, "key_hash": "a" * 64, "part": "2026", "deps": DEPS},
    {"recipe": "phylo_tree", "version": 2, "key_hash": "0" * 64, "part": "", "deps": DEPS[1:]},
]


def main() -> None:
    doc = {
        "_comment": (
            "Golden vectors for SPEC.md. Fixed inputs, fixed digests. A second "
            "implementation in any language must reproduce every value here exactly. "
            "Regenerate only when fingerprint_spec_version is deliberately bumped."
        ),
        "fingerprint_spec_version": FINGERPRINT_SPEC_VERSION,
        "hash": "BLAKE2b, 32-byte digest, no key or salt, hex-encoded lowercase",
        "keys": [],
        "digests": [],
        "fingerprints": [],
    }

    for key in KEYS:
        key_json, key_hash = canonical_key_hash(key)
        doc["keys"].append({"key": key, "key_json": key_json, "key_hash": key_hash})

    for data in DIGEST_INPUTS:
        doc["digests"].append({"bytes_hex": data.hex(), "digest": digest_bytes(data)})

    for case in FINGERPRINTS:
        deps = case["deps"]
        doc["fingerprints"].append(
            {
                "recipe": case["recipe"],
                "version": case["version"],
                "key_hash": case["key_hash"],
                "part": case["part"],
                "deps": [
                    {
                        "recipe": d.recipe,
                        "version": d.version,
                        "key_hash": d.key_hash,
                        "part": d.part,
                        "digest": d.digest,
                    }
                    for d in deps
                ],
                "fingerprint": fingerprint(
                    recipe=case["recipe"],
                    version=case["version"],
                    key_hash=case["key_hash"],
                    part=case["part"],
                    deps=deps,
                ),
            }
        )

    OUT.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
