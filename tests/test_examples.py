"""Stage 8: the two examples, one financial and one not.

Running them here is what keeps them honest. An example that has quietly stopped
working is worse than no example, and these two carry the claims the README makes.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def run(script: Path) -> str:
    completed = subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True, timeout=300, check=False
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return completed.stdout


@pytest.fixture(autouse=True)
def _clean_example_databases() -> None:
    for db in EXAMPLES.rglob("*.db*"):
        db.unlink(missing_ok=True)


def test_the_financial_example_demonstrates_early_cutoff() -> None:
    pytest.importorskip("polars")
    output = run(EXAMPLES / "finance" / "pipeline.py")

    assert "(protected)" in output, "a permissive caller still cannot reach a protected root"
    assert output.count("[fetch] ohlcv") == 3, (
        "once to ingest, once for the revised old bar, once for the appended day"
    )

    # The claim is about what happens *after* the old bar is revised, so read that
    # section rather than counting across the whole run.
    after_revision = output.split("Reading again.")[1]
    assert "[compute] sma" in after_revision, "the SMA is stale and its fingerprint changed"
    assert "[compute] sharpe" not in after_revision, (
        "early cutoff: the SMA's bytes did not change, so nothing above it reruns"
    )

    sharpes = [line for line in output.splitlines() if "'sharpe':" in line]
    assert len(sharpes) == 3
    assert len(set(sharpes)) == 1, "and the value is unchanged, which is why it was skipped"

    # The year codec's own claim: appending a day leaves the closed years alone.
    years = output.split("Appending one day")[1]
    assert "2023  untouched" in years
    assert "2024  untouched" in years
    assert "2025  rewritten" in years


def test_the_linguistics_example_plans_through_a_foreign_recipe() -> None:
    pytest.importorskip("numpy")
    output = run(EXAMPLES / "linguistics" / "pipeline.py")

    assert "absent: 2" in output, "a declared graph is walkable before anything executes"
    assert output.count("[compute] distance_matrix") == 1, "the second read is a hit"
    assert "'model': 'GTR'" in output
    assert "'model': 'JC69'" in output, "a different key is a different artifact"
