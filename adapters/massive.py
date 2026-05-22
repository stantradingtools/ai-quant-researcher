"""adapters.massive: Massive.com live equity quote snapshots.

STATE: STUB. Interface defined; fetch logic to be implemented in Phase 1.

Massive is the primary live-snapshot source for US equity prices. The
adapter's job is to produce a standardized DataFrame for the FeaturePipeline.

Usage:
    python -m adapters.massive fetch \
      --thesis_id <id> --tickers AAPL,SPY --start 2024-01-01 --end 2024-12-31
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import pandas as pd


MASSIVE_API_KEY = os.environ.get("MASSIVE_API_KEY")
MASSIVE_BASE = "https://api.massive.com"  # TODO confirm actual endpoint


def fetch_bars(
    symbols: list[str],
    start: str,
    end: str,
    interval: str = "1d",
) -> pd.DataFrame:
    """Returns long-format DataFrame:
       [timestamp, symbol, open, high, low, close, volume, adj_close]
       MultiIndex on [timestamp, symbol].
       Point-in-time guaranteed: no later restatements applied.
    """
    raise NotImplementedError(
        "massive.fetch_bars: Phase 1 implementation pending "
        "(Stub — even with MASSIVE_API_KEY set, fetch logic is not yet implemented.)"
    )


def fetch_quote_snapshot(symbols: list[str]) -> pd.DataFrame:
    """Current bid/ask/last snapshot for live trading context."""
    raise NotImplementedError(
        "massive.fetch_quote_snapshot: Phase 1 pending "
        "(Stub — even with MASSIVE_API_KEY set, fetch logic is not yet implemented.)"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="adapters.massive")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_fetch = sub.add_parser("fetch")
    p_fetch.add_argument("--thesis_id", required=True)
    p_fetch.add_argument("--tickers", required=True)
    p_fetch.add_argument("--start", required=True)
    p_fetch.add_argument("--end", required=True)
    p_fetch.add_argument("--interval", default="1d")
    args = parser.parse_args(argv)

    if args.cmd == "fetch":
        symbols = [s.strip() for s in args.tickers.split(",") if s.strip()]
        df = fetch_bars(symbols, args.start, args.end, args.interval)
        out_dir = Path(f"theses/{args.thesis_id}/data")
        out_dir.mkdir(parents=True, exist_ok=True)
        df.to_parquet(out_dir / "massive_bars.parquet")
        print(f"Wrote {len(df)} rows to {out_dir}/massive_bars.parquet")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
