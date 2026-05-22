"""adapters.flash_alpha: Flash Alpha exposure aggregates.

STATE: STUB. Phase 1 implementation.

Endpoints:
- /exposure_summary       (net GEX/DEX/VEX/CHEX, gamma regime, hedging estimate)
- /gex_by_strike
- /dex_by_strike
- /vex_by_strike
- /chex_by_strike
- /levels                 (gamma flip, call/put walls, max OI strike, 0DTE pivot)
- /surface                (50x50 IV surface grid)
- /volatility             (ATM IV, realized vol, VRP, skew, term structure)
- /vrp / /vrp_history
- /zero_dte
- /option_quote, /stock_quote, /option_chain
- /historical_*           (replay each metric at any minute since Apr 2018)

Free tier limits unknown — confirm at runtime via /account endpoint.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import pandas as pd


FLASH_API_KEY = os.environ.get("FLASH_ALPHA_API_KEY") or os.environ.get("FLASH_API_KEY")
FLASH_BASE = "https://lab.flashalpha.com/api"  # confirm


def fetch_exposure_summary(symbol: str, start: str, end: str) -> pd.DataFrame:
    """Daily net GEX/DEX/VEX/CHEX + gamma regime."""
    raise NotImplementedError(
        "flash_alpha.fetch_exposure_summary: Phase 1 pending "
        "(Stub — even with FLASH_API_KEY set, fetch logic is not yet implemented.)"
    )


def fetch_levels(symbol: str, start: str, end: str) -> pd.DataFrame:
    """Key options levels per day: gamma flip, call/put walls, max-OI, 0DTE pivot."""
    raise NotImplementedError(
        "flash_alpha.fetch_levels: Phase 1 pending "
        "(Stub — even with FLASH_API_KEY set, fetch logic is not yet implemented.)"
    )


def fetch_surface_snapshot(symbol: str, date: str) -> pd.DataFrame:
    """Single-day 50x50 IV surface grid."""
    raise NotImplementedError(
        "flash_alpha.fetch_surface_snapshot: Phase 1 pending "
        "(Stub — even with FLASH_API_KEY set, fetch logic is not yet implemented.)"
    )


def fetch_volatility(symbol: str, start: str, end: str) -> pd.DataFrame:
    """ATM IV, RV (5/10/20/30d), VRP, skew, term structure."""
    raise NotImplementedError(
        "flash_alpha.fetch_volatility: Phase 1 pending "
        "(Stub — even with FLASH_API_KEY set, fetch logic is not yet implemented.)"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="adapters.flash_alpha")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_fetch = sub.add_parser("fetch")
    p_fetch.add_argument("--thesis_id", required=True)
    p_fetch.add_argument("--tickers", required=True)
    p_fetch.add_argument("--start", required=True)
    p_fetch.add_argument("--end", required=True)
    p_fetch.add_argument("--datatype", choices=["exposure", "levels", "vol", "surface"],
                        default="exposure")
    args = parser.parse_args(argv)
    if args.cmd == "fetch":
        symbols = [s.strip() for s in args.tickers.split(",")]
        out_dir = Path(f"theses/{args.thesis_id}/data")
        out_dir.mkdir(parents=True, exist_ok=True)
        # dispatch per datatype — currently raises NotImplementedError
        for sym in symbols:
            if args.datatype == "exposure":
                df = fetch_exposure_summary(sym, args.start, args.end)
            elif args.datatype == "levels":
                df = fetch_levels(sym, args.start, args.end)
            elif args.datatype == "vol":
                df = fetch_volatility(sym, args.start, args.end)
            df.to_parquet(out_dir / f"flash_{args.datatype}_{sym}.parquet")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
