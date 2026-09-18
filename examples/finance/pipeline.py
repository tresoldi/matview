"""A financial pipeline: OHLCV bars, a moving average, a Sharpe ratio.

Run it twice to see the cache work, and watch what a revision to an old bar does *not*
invalidate:

    uv run --extra arrow python examples/finance/pipeline.py

The vendor here is synthetic so the example runs anywhere, but it is marked
`deterministic=False` and `on_miss="raise"` exactly as a paid one would be: nothing can
reach it by accident from six levels up, and ingesting is an explicit act.
"""

from __future__ import annotations

import math
from pathlib import Path

import polars as pl

from matview import Cache, MissingArtifacts, SqliteStore, recipe

DB = Path(__file__).with_name("finance.db")

#: Stands in for a vendor API. Editing a value here and re-running is the interesting part.
CLOSES: dict[str, list[float]] = {
    "AAPL": [100 + 10 * math.sin(i / 7) + i * 0.4 for i in range(400)],
    "MSFT": [200 + 8 * math.cos(i / 5) + i * 0.3 for i in range(400)],
}


@recipe(version=1, codec="arrow", index=("symbol",), on_miss="raise", deterministic=False)
def ohlcv(ctx, *, symbol: str, interval: str = "1d", adjust: str = "split"):
    """A protected root. A network fetch is non-deterministic and usually costs money."""
    print(f"  [fetch] ohlcv {symbol} {interval} {adjust}")
    closes = CLOSES[symbol]
    return pl.DataFrame(
        {
            "day": list(range(len(closes))),
            "close": closes,
            "volume": [1_000 + i for i in range(len(closes))],
        }
    )


WINDOW = 200

#: How much of the tail the indicators actually report. This is what makes early cutoff
#: visible: a revision older than `window + REPORTED` bars cannot change the output.
REPORTED = 120


@recipe(version=3, codec="arrow-series", index=("symbol",))
def sma(ctx, *, symbol: str, window: int, interval: str = "1d"):
    """`interval` is an ordinary key field, not part of the recipe's identity.

    Anything that changes the values while leaving the computation identical belongs in
    the key. That is how this design avoids a special-cased notion of series identity.
    """
    print(f"  [compute] sma {symbol} window={window}")
    bars = ctx.get("ohlcv", symbol=symbol, interval=interval)
    return bars["close"].rolling_mean(window).tail(REPORTED).alias("sma")


@recipe(version=1, codec="json", index=("symbol",))
def sharpe(ctx, *, symbol: str, window: int = WINDOW):
    print(f"  [compute] sharpe {symbol}")
    series = ctx.get("sma", symbol=symbol, window=window).drop_nulls()
    returns = series.pct_change().drop_nulls()
    mean, std = returns.mean(), returns.std()
    return {"symbol": symbol, "sharpe": 0.0 if not std else float(mean) / float(std) * 252**0.5}


def main() -> None:
    DB.unlink(missing_ok=True)
    cache = Cache(store=SqliteStore(DB))

    print("A cold read of a protected graph refuses, and says what needs ingesting.")
    print("The caller asked for the most permissive policy and still cannot reach a")
    print("recipe that declares itself protected, six levels down:")
    try:
        cache.get("sharpe", symbol="AAPL", on_miss="compute")
    except MissingArtifacts as missing:
        for node in missing.nodes:
            print(f"    {node.describe()}")
    print("  (the bodies above it did start, and nothing they produced was cached:")
    print("   a failed compute is never stored, in any form)")

    print("\nIngesting is an explicit top-level act, and that one works:")
    cache.get("ohlcv", symbol="AAPL", on_miss="compute")

    print("\nFirst read of the derived values:")
    print("  ->", cache.get("sharpe", symbol="AAPL"))

    print("\nSecond read: everything is current, so nothing runs.")
    print("  ->", cache.get("sharpe", symbol="AAPL"))

    print("\nThe vendor revises one bar from the distant past, and we force a refetch:")
    CLOSES["AAPL"][0] = 0.0
    cache.get("ohlcv", symbol="AAPL", force=True)
    print(f"  marked stale: {', '.join(sorted(r.recipe for r in cache.query() if not r.valid))}")

    print("\nReading again. The SMA is stale so it reruns, but it reports only the last")
    print(f"{REPORTED} days, which a {WINDOW}-day window never traces back to that bar. Its bytes")
    print("are unchanged, so the Sharpe ratio above it is not recomputed at all:")
    print("  ->", cache.get("sharpe", symbol="AAPL"))

    print("\nEverything is current again:")
    print("  " + cache.plan("sharpe", symbol="AAPL").describe().replace("\n", "\n  "))

    print("\nWhat is stored:")
    for ref in cache.query():
        part = "" if ref.part is None else f" part={ref.part}"
        print(f"  {ref.recipe:<8} v{ref.version}{part:<10} {ref.n_bytes:>7} bytes  {ref.key_json}")


if __name__ == "__main__":
    main()
