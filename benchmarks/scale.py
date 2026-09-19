#!/usr/bin/env python3
"""Manual scale harness for a matview cache.

Run from the repository with ``uv run --all-extras python benchmarks/scale.py``.
This is deliberately outside the test suite: its default profile writes roughly 250k
artifacts, and the realistic profile needs about 10 GiB of payload storage.
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from matview import Cache, Registry, SqliteStore, recipe
from matview.types import Request

DEFAULT_SYMBOLS = 5_000
DEFAULT_INDICATORS = 50
DEFAULT_BATCH_ROOTS = 1_000
REALISTIC_PAYLOAD_BYTES = 40 * 1024
TINY_PAYLOAD_BYTES = 32

MUTATING_OPERATIONS = {"cold-write", "cascade", "drop", "gc"}
OPERATIONS = (
    "cold-write",
    "warm-read",
    "stale-verify",
    "cascade",
    "query",
    "list-refs",
    "gc",
    "drop",
    "verify",
    "get-many",
    "plan-many",
)


def _symbol(index: int) -> str:
    return f"S{index:05d}"


def _payload(label: str, size: int) -> str:
    return (label + ":" + "x" * size)[:size]


def _registry(payload_bytes: int, indicators: int) -> Registry:
    registry = Registry()

    @recipe(version=1, codec="json", on_miss="raise", registry=registry)
    def market(_ctx: Any) -> dict[str, str]:
        return {"payload": _payload("market", payload_bytes)}

    @recipe(version=1, codec="json", index=("symbol",), registry=registry)
    def indicator(ctx: Any, *, symbol: str, indicator: int) -> dict[str, str | int]:
        # All indicator rows share this protected source, making it a deliberately wide
        # cascade root. Reading it is load-bearing: the dependency must be recorded.
        source = ctx.get("market")
        return {
            "symbol": symbol,
            "indicator": indicator,
            "source_size": len(source["payload"]),
            "payload": _payload(f"{symbol}-{indicator}", payload_bytes),
        }

    @recipe(version=1, codec="json", index=("symbol",), registry=registry)
    def aggregate(ctx: Any, *, symbol: str) -> dict[str, int | str]:
        values = [ctx.get("indicator", symbol=symbol, indicator=n) for n in range(indicators)]
        return {
            "symbol": symbol,
            "indicator_bytes": sum(len(str(value["payload"])) for value in values),
            "payload": _payload(f"aggregate-{symbol}", payload_bytes),
        }

    return registry


def _register_legacy(registry: Registry, version: int, payload_bytes: int) -> None:
    @recipe(version=version, codec="json", index=("symbol",), registry=registry)
    def legacy(_ctx: Any, *, symbol: str) -> dict[str, str]:
        return {"payload": _payload(f"legacy-{version}-{symbol}", payload_bytes)}


def _remove_database(path: Path) -> None:
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        if candidate.exists():
            candidate.unlink()


def _percentile_95(samples: Sequence[float]) -> float:
    return sorted(samples)[max(0, (len(samples) * 95 + 99) // 100 - 1)]


def _measure(name: str, units: str, repeats: int, work: Callable[[], int]) -> dict[str, object]:
    samples: list[float] = []
    count = 0
    for _ in range(repeats):
        started = time.perf_counter()
        count = work()
        samples.append(time.perf_counter() - started)
    median = statistics.median(samples)
    return {
        "operation": name,
        "repeats": repeats,
        "units": units,
        "count": count,
        "median_s": median,
        "p95_s": _percentile_95(samples),
        "units_per_second": count / median if median else None,
        "samples_s": samples,
    }


def _populate(path: Path, symbols: int, indicators: int, realistic: bool) -> dict[str, object]:
    payload_bytes = REALISTIC_PAYLOAD_BYTES if realistic else TINY_PAYLOAD_BYTES
    registry = _registry(payload_bytes, indicators)
    store = SqliteStore(path)
    cache = Cache(store, registry=registry)
    try:

        def write() -> int:
            cache.get("market", on_miss="compute")
            for symbol_index in range(symbols):
                cache.get("aggregate", symbol=_symbol(symbol_index))
            # A small superseded set makes gc() meaningful while its query still scans
            # the full benchmark database.
            _register_legacy(registry, 1, payload_bytes)
            for symbol_index in range(symbols):
                cache.get("legacy", symbol=_symbol(symbol_index))
            _register_legacy(registry, 2, payload_bytes)
            for symbol_index in range(symbols):
                cache.get("legacy", symbol=_symbol(symbol_index))
            return 1 + symbols * (indicators + 3)

        result = _measure("cold-write", "artifacts", 1, write)
        result["database_bytes"] = path.stat().st_size
        result["artifacts"] = store.summary()["artifacts"]
        return result
    finally:
        store.close()


def _existing_cache(path: Path, indicators: int, realistic: bool) -> tuple[SqliteStore, Cache]:
    if not path.exists():
        raise SystemExit(f"{path} does not exist; run --operation cold-write --reset first")
    registry = _registry(
        REALISTIC_PAYLOAD_BYTES if realistic else TINY_PAYLOAD_BYTES,
        indicators,
    )
    store = SqliteStore(path)
    return store, Cache(store, registry=registry)


def _run_existing(args: argparse.Namespace) -> list[dict[str, object]]:
    store, cache = _existing_cache(args.db, args.indicators, args.realistic)
    target = _symbol(0)
    roots = min(args.batch_roots, args.symbols)
    try:
        if args.operation == "warm-read":

            def warm_read() -> int:
                cache.get("aggregate", symbol=target)
                return 1

            results = [
                _measure(
                    "warm-read",
                    "reads",
                    args.repeats,
                    warm_read,
                )
            ]
        elif args.operation == "stale-verify":
            target_id = store.query("aggregate", symbol=target)[0].id

            def verify() -> int:
                store.invalidate([target_id])
                cache.get("aggregate", symbol=target)
                return 1

            results = [_measure("stale-verify", "revalidations", args.repeats, verify)]
        elif args.operation == "cascade":
            root_id = store.query("market")[0].id
            results = [
                _measure("cascade", "invalidated artifacts", 1, lambda: store.invalidate([root_id]))
            ]
        elif args.operation == "query":
            results = [
                _measure("query-unfiltered", "rows", args.repeats, lambda: len(cache.query())),
                _measure(
                    "query-filtered",
                    "rows",
                    args.repeats,
                    lambda: len(cache.query("indicator", symbol=target)),
                ),
            ]
        elif args.operation == "list-refs":
            results = [
                _measure(
                    "list-refs-unfiltered", "rows", args.repeats, lambda: len(store.list_refs())
                ),
                _measure(
                    "list-refs-filtered",
                    "rows",
                    args.repeats,
                    lambda: len(store.list_refs("indicator", fields={"symbol": target})),
                ),
            ]
        elif args.operation == "gc":
            results = [_measure("gc", "artifacts", 1, cache.gc)]
        elif args.operation == "drop":
            results = [_measure("drop", "artifacts", 1, lambda: cache.drop("indicator"))]
        elif args.operation == "verify":
            command = [sys.executable, "-m", "matview.cli", "verify", "--db", str(args.db)]
            results = [
                _measure(
                    "matview-verify",
                    "artifacts",
                    args.repeats,
                    lambda: (
                        subprocess.run(
                            command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
                        ).returncode
                        or store.summary()["artifacts"]
                    ),
                )
            ]
        elif args.operation == "get-many":
            requests = [Request("aggregate", {"symbol": _symbol(n)}) for n in range(roots)]
            results = [
                _measure(
                    "get-many",
                    "roots",
                    args.repeats,
                    lambda: len(cache.get_many(requests).values),
                )
            ]
        elif args.operation == "plan-many":
            requests = [Request("aggregate", {"symbol": _symbol(n)}) for n in range(roots)]

            def plan_many() -> int:
                cache.plan_many(requests)
                return len(requests)

            results = [
                _measure(
                    "plan-many",
                    "roots",
                    args.repeats,
                    plan_many,
                )
            ]
        else:  # pragma: no cover - argparse keeps this unreachable
            raise AssertionError(args.operation)
        for result in results:
            result["database_bytes"] = args.db.stat().st_size
        return results
    finally:
        store.close()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("benchmarks/matview-scale.db"))
    parser.add_argument("--operation", choices=OPERATIONS, required=True)
    parser.add_argument("--symbols", type=int, default=DEFAULT_SYMBOLS)
    parser.add_argument("--indicators", type=int, default=DEFAULT_INDICATORS)
    parser.add_argument("--batch-roots", type=int, default=DEFAULT_BATCH_ROOTS)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--realistic", action="store_true", help="use roughly 40 KiB payloads")
    parser.add_argument("--reset", action="store_true", help="replace --db for cold-write")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    if min(args.symbols, args.indicators, args.batch_roots, args.repeats) < 1:
        raise SystemExit("--symbols, --indicators, --batch-roots, and --repeats must be positive")
    if args.operation in MUTATING_OPERATIONS and args.repeats != 1:
        raise SystemExit(f"{args.operation} changes the database and requires --repeats 1")

    if args.operation == "cold-write":
        if args.db.exists() and not args.reset:
            raise SystemExit(f"{args.db} exists; pass --reset to replace it")
        args.db.parent.mkdir(parents=True, exist_ok=True)
        if args.reset:
            _remove_database(args.db)
        results = [_populate(args.db, args.symbols, args.indicators, args.realistic)]
    else:
        results = _run_existing(args)
    print(
        json.dumps(
            {"profile": vars(args), "results": results}, default=str, indent=2, sort_keys=True
        )
    )


if __name__ == "__main__":
    main()
