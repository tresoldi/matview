"""Regenerate tests/golden_codecs.json.

Fixed values, fixed bytes. If a codec's output changes — because a library changed how
it writes, or because someone "improved" an encoder — every artifact already stored
under that codec becomes unreachable: nothing errors, the digests simply stop matching
and the cache looks cold forever.

So regenerating this file is a deliberate act with a migration behind it, not a way to
make a red test green.

    uv run --all-extras python tools/gen_golden_codecs.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from matview.codecs import get_codec

OUT = Path(__file__).resolve().parents[1] / "tests" / "golden_codecs.json"


def samples() -> dict[str, Any]:
    import datetime as dt

    import numpy as np
    import pandas as pd
    import polars as pl

    DATES = [
        dt.date(2023, 12, 31),
        dt.date(2024, 1, 1),
        dt.date(2024, 7, 1),
        dt.date(2025, 2, 2),
    ]

    frame = pl.DataFrame(
        {
            "close": [1.0, 2.5, 3.0, -0.5],
            "volume": [10, 20, 30, 40],
            "symbol": ["AAPL", "AAPL", "AAPL", "AAPL"],
            "flag": [True, False, True, False],
        }
    )
    return {
        "json": {"b": 1, "a": [1, 2.5, None, True], "z": "ü", "nested": {"y": 1, "x": 2}},
        "arrow": frame,
        "arrow-series": frame["close"],
        # The same logical table. Its bytes must equal the `arrow` entry above, which is
        # what pins the offset-width normalization.
        "arrow-pandas": pd.DataFrame(frame.to_dict(as_series=False)),
        "npy": np.arange(12, dtype="int64").reshape(3, 4),
        # Spans three calendar years, so the fixture pins the part labels as well as the
        # bytes, over the same columns as the unpartitioned entries above. The pandas
        # entry must match it part for part.
        "arrow-year": pl.DataFrame({"date": DATES}).hstack(frame),
        "arrow-year-pandas": pd.DataFrame({"date": DATES, **frame.to_dict(as_series=False)}),
    }


def main() -> None:
    import numpy as np
    import pandas as pd
    import polars as pl

    doc: dict[str, Any] = {
        "_comment": (
            "Golden codec bytes. Identical input, identical bytes, on every machine and "
            "every version. A failure here means every artifact stored under that codec "
            "has become unreachable. Regenerate only with a migration behind it."
        ),
        "generated_with": {
            "polars": pl.__version__,
            "pandas": pd.__version__,
            "numpy": np.__version__,
        },
        "codecs": {},
    }
    for name, value in samples().items():
        encoded = get_codec(name).encode(value)
        # Uniform shape: `parts` is keyed by part label, with "" for an unpartitioned
        # codec, so the fixture and the test that reads it have one code path.
        parts = {"": encoded} if isinstance(encoded, bytes) else encoded
        doc["codecs"][name] = {
            "parts": {label: data.hex() for label, data in sorted(parts.items())},
            "n_bytes": sum(len(data) for data in parts.values()),
        }

    OUT.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
