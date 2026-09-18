"""A computational linguistics pipeline: a distance matrix and a tree fitted from it.

The tree is a *foreign* recipe: a script in another language, declared in a manifest
rather than registered by a decorator. The script here is Python so the example runs
anywhere, but nothing about the contract is Python-specific — the host writes a JSON
job file, the script reads it and writes its result to the output path.

    uv run --extra npy python examples/linguistics/pipeline.py

Note what the declared graph buys: `plan()` walks *through* the foreign recipe without
running it, and a cycle among manifests is caught when they load. A Python recipe's
dependencies are only knowable by running it.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from matview import Cache, SqliteStore, recipe
from matview.foreign import load_manifests

HERE = Path(__file__).parent
DB = HERE / "linguistics.db"

#: Stands in for a cognate-coded wordlist.
DATASETS: dict[str, list[str]] = {
    "ie-ielex": ["latin", "greek", "sanskrit", "gothic", "old_irish"],
    "tupi": ["tupinamba", "guarani", "siriono"],
}


@recipe(version=1, codec="npy", index=("dataset",))
def distance_matrix(ctx, *, dataset: str, metric: str = "hamming"):
    """`metric` is a key field: the same computation over different parameters."""
    print(f"  [compute] distance_matrix {dataset} metric={metric}")
    languages = DATASETS[dataset]
    size = len(languages)
    matrix = np.zeros((size, size), dtype="float64")
    for i, left in enumerate(languages):
        for j, right in enumerate(languages):
            shared = len(set(left) & set(right))
            matrix[i, j] = 0.0 if i == j else 1.0 - shared / max(len(left), len(right))
    return matrix


def main() -> None:
    DB.unlink(missing_ok=True)
    cache = Cache(store=SqliteStore(DB))
    load_manifests(HERE / "recipes", store=cache.store)

    print("The declared graph can be planned before anything executes:")
    print("  " + cache.plan("phylo_tree", dataset="ie-ielex").describe().replace("\n", "\n  "))

    print("\nFirst read: the matrix is computed, written to a file, and handed to the script.")
    print("  ->", cache.get("phylo_tree", dataset="ie-ielex"))

    print("\nSecond read: nothing runs.")
    print("  ->", cache.get("phylo_tree", dataset="ie-ielex"))

    print("\nA different model is a different key, so it is a different artifact:")
    print("  ->", cache.get("phylo_tree", dataset="ie-ielex", model="JC69"))

    print("\nWhat is stored:")
    for ref in cache.query():
        print(f"  {ref.recipe:<16} v{ref.version} {ref.n_bytes:>6} bytes  {ref.key_json}")


if __name__ == "__main__":
    main()
