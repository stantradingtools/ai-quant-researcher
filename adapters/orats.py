"""adapters.orats: ORATS historical options data (pre-2018 specialty).

STATE: STUB. Phase 1.

Endpoints (commonly used):
- hist/cores              (EOD core option metrics per ticker)
- hist/strikes            (per-strike daily snapshots)
- hist/dailies            (daily summary aggregates)
- hist/iv                 (historical IV)
- hist/volatility         (RV measures)

API: https://docs.orats.io
Requires ORATS_API_TOKEN in .env.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import pandas as pd


ORATS_KEY = os.environ.get("ORATS_API_TOKEN") or os.environ.get("ORATS_API_KEY")
ORATS_BASE = "https://api.orats.io/datav2"


def fetch_cores(symbol: str, start: str, end: str) -> pd.DataFrame:
    """EOD core option metrics: IVs by tenor, atm_skew, ivol_pct_rank etc."""
    if not ORATS_KEY:
        raise RuntimeError("ORATS_API_TOKEN not set in .env")
    raise NotImplementedError("orats.fetch_cores: Phase 1 pending")


def fetch_strikes(symbol: str, start: str, end: str) -> pd.DataFrame:
    """Per-strike daily option snapshots."""
    if not ORATS_KEY:
        raise RuntimeError("ORATS_API_TOKEN not set in .env")
    raise NotImplementedError("orats.fetch_strikes: Phase 1 pending")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="adapters.orats")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_fetch = sub.add_parser("fetch")
    p_fetch.add_argument("--thesis_id", required=True)
    p_fetch.add_argument("--tickers", required=True)
    p_fetch.add_argument("--start", required=True)
    p_fetch.add_argument("--end", required=True)
    p_fetch.add_argument("--datatype", choices=["cores", "strikes"], default="cores")
    args = parser.parse_args(argv)
    if args.cmd == "fetch":
        symbols = [s.strip() for s in args.tickers.split(",")]
        out_dir = Path(f"theses/{args.thesis_id}/data")
        out_dir.mkdir(parents=True, exist_ok=True)
        for sym in symbols:
            if args.datatype == "cores":
                df = fetch_cores(sym, args.start, args.end)
            else:
                df = fetch_strikes(sym, args.start, args.end)
            df.to_parquet(out_dir / f"orats_{args.datatype}_{sym}.parquet")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
