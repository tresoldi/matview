"""Stage 4: the concurrency assumption, exercised rather than assumed.

The spec excludes distributed locking and work leases, on the grounds that duplicate
computation by concurrent runners is acceptable and expected to be rare, and that
idempotent writes handle it. The implementation backs that with WAL, per-thread
connections, `BEGIN IMMEDIATE` and the rule 4 no-op write.

Those are claims, and until this file existed nothing checked them.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest

from matview import Cache, Registry, SqliteStore, recipe

WORKERS = 8


def _run_concurrently(target, workers: int = WORKERS) -> list:
    """Start `workers` threads on a barrier, so they collide rather than queue."""
    barrier = threading.Barrier(workers)
    results: list = [None] * workers
    errors: list[BaseException] = []

    def wrapped(index: int) -> None:
        try:
            barrier.wait(timeout=30)
            results[index] = target()
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=wrapped, args=(i,)) for i in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not errors, errors
    assert not any(t.is_alive() for t in threads), "a worker did not finish"
    return results


def test_concurrent_readers_of_the_same_node_agree_and_write_one_row(
    cache: Cache, registry: Registry
) -> None:
    computed = []
    lock = threading.Lock()

    @recipe(version=1, codec="json", registry=registry)
    def slow(ctx, *, n: int = 1):
        with lock:
            computed.append(1)
        return {"v": n * 7}

    results = _run_concurrently(lambda: cache.get("slow"))

    assert results == [{"v": 7}] * WORKERS, "every worker saw the same value"
    assert len(cache.query(recipe="slow")) == 1, "one artifact, however many racers"
    # Duplicate work is explicitly acceptable; silently wrong answers are not.
    assert 1 <= len(computed) <= WORKERS


def test_an_identical_concurrent_rewrite_does_not_cascade(cache: Cache, registry: Registry) -> None:
    """Rule 4's actual purpose, under the conditions it was written for.

    Redundant work from a duplicate runner must not rewrite a good row, because
    equivalent-but-different bytes would flip every downstream fingerprint for no
    reason. Here the bytes are identical, so nothing downstream may move.
    """

    @recipe(version=1, codec="json", registry=registry)
    def root(ctx, *, n: int = 1):
        return {"v": n}

    @recipe(version=1, codec="json", registry=registry)
    def leaf(ctx, *, n: int = 1):
        return {"v": ctx.get("root", n=n)["v"] + 1}

    cache.get("leaf")
    before = cache.query(recipe="leaf")[0]

    _run_concurrently(lambda: cache.get("root", force=True))

    after = cache.query(recipe="leaf")[0]
    assert after.valid is True, "an identical rewrite must not invalidate a dependent"
    assert after.digest == before.digest
    assert after.computed_at == before.computed_at, "and must not touch the row at all"
    assert len(cache.query(recipe="root")) == 1


def test_concurrent_writers_of_different_nodes_do_not_block_each_other_into_failure(
    cache: Cache, registry: Registry
) -> None:
    @recipe(version=1, codec="json", index=("symbol",), registry=registry)
    def bars(ctx, *, symbol: str):
        return {"s": symbol}

    symbols = [f"SYM{i}" for i in range(WORKERS)]
    order = iter(symbols)
    lock = threading.Lock()

    def one() -> object:
        with lock:
            symbol = next(order)
        return cache.get("bars", symbol=symbol)

    results = _run_concurrently(one)
    assert sorted(r["s"] for r in results) == sorted(symbols)
    assert len(cache.query(recipe="bars")) == WORKERS


def test_each_thread_uses_its_own_connection(store: SqliteStore) -> None:
    seen: set[int] = set()
    lock = threading.Lock()

    def record() -> None:
        with lock:
            seen.add(id(store._connect()))

    _run_concurrently(record)
    assert len(seen) == WORKERS, "one connection per thread, as WAL requires"


# --- across processes ------------------------------------------------------

WORKER_SCRIPT = """
import sys
from matview import Cache, Registry, SqliteStore, recipe

registry = Registry()

@recipe(version=1, codec="json", index=("symbol",), registry=registry)
def bars(ctx, *, symbol: str):
    return {"s": symbol, "n": 41 + 1}

cache = Cache(store=SqliteStore(sys.argv[1]), registry=registry)
for _ in range(20):
    value = cache.get("bars", symbol=sys.argv[2])
    assert value == {"s": sys.argv[2], "n": 42}, value
print("ok")
"""


@pytest.mark.parametrize("symbol_per_process", [False, True])
def test_two_processes_share_one_database_file(tmp_path: Path, symbol_per_process: bool) -> None:
    """The deployment the spec describes is local runs, CI and hosted jobs.

    Threads share a process and the GIL; separate processes share only the file, which
    is where WAL and BEGIN IMMEDIATE actually have to work.
    """
    script = tmp_path / "worker.py"
    script.write_text(textwrap.dedent(WORKER_SCRIPT), encoding="utf-8")
    db = tmp_path / "shared.db"

    processes = [
        subprocess.Popen(
            [sys.executable, str(script), str(db), f"SYM{i}" if symbol_per_process else "SAME"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for i in range(4)
    ]
    outcomes = [(p.wait(timeout=180), *p.communicate()) for p in processes]

    for code, out, err in outcomes:
        assert code == 0, err
        assert out.strip() == "ok"

    store = SqliteStore(db)
    rows = store.query(recipe="bars")
    assert len(rows) == (4 if symbol_per_process else 1)
    assert all(row.valid for row in rows)
    store.close()
