"""adapters.alpha_vantage: Alpha Vantage historical equity + options data.

STATE: STUB. Phase 1 implementation: fetch_bars (OHLCV daily/intraday).
Phase 2: fetch_options_chain (post-2018 options data).

API docs: https://www.alphavantage.co/documentation/
Free tier: 25 requests/day, 5 requests/min.
Premium tier needed for intraday and options endpoints.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import pandas as pd


AV_API_KEY = os.environ.get("ALPHA_VANTAGE_API_KEY") or os.environ.get("AV_API_KEY")
AV_BASE = "https://www.alphavantage.co/query"


def fetch_bars(symbols: list[str], start: str, end: str, interval: str = "1d") -> pd.DataFrame:
    """Returns OHLCV in same schema as adapters.massive.fetch_bars."""
    raise NotImplementedError(
        "alpha_vantage.fetch_bars: Phase 1 pending "
        "(Stub — even with AV_API_KEY set, fetch logic is not yet implemented.)"
    )


def fetch_options_chain(symbol: str, date: str | None = None) -> pd.DataFrame:
    """Returns option chain snapshot. Columns: expiry, strike, type,
       bid, ask, mark, iv, delta, gamma, theta, vega, open_interest, volume.
    """
    raise NotImplementedError(
        "alpha_vantage.fetch_options_chain: Phase 2 pending "
        "(Stub — even with AV_API_KEY set, fetch logic is not yet implemented.)"
    )


def fetch_earnings_calendar(symbol: str | None = None) -> pd.DataFrame:
    """Earnings dates. Used by adapters.event_calendar.get_earnings_dates."""
    raise NotImplementedError(
        "alpha_vantage.fetch_earnings_calendar: Phase 2 pending "
        "(Stub — even with AV_API_KEY set, fetch logic is not yet implemented.)"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="adapters.alpha_vantage")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_fetch = sub.add_parser("fetch")
    p_fetch.add_argument("--thesis_id", required=True)
    p_fetch.add_argument("--tickers", required=True)
    p_fetch.add_argument("--start", required=True)
    p_fetch.add_argument("--end", required=True)
    p_fetch.add_argument("--datatype", choices=["bars", "options", "earnings"], default="bars")
    args = parser.parse_args(argv)

    if args.cmd == "fetch":
        symbols = [s.strip() for s in args.tickers.split(",") if s.strip()]
        out_dir = Path(f"theses/{args.thesis_id}/data")
        out_dir.mkdir(parents=True, exist_ok=True)
        if args.datatype == "bars":
            df = fetch_bars(symbols, args.start, args.end)
            df.to_parquet(out_dir / "av_bars.parquet")
        elif args.datatype == "options":
            # Per-symbol options pull
            for sym in symbols:
                df = fetch_options_chain(sym)
                df.to_parquet(out_dir / f"av_options_{sym}.parquet")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
