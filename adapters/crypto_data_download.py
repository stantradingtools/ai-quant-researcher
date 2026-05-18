"""adapters.crypto_data_download: free DVOL OHLC CSV mirror.

STATE: STUB. Phase 2.

Source: cryptodatadownload.com publishes BTC and ETH DVOL OHLC daily CSV.
Fully free, no API key. Used as a backstop / cross-check for Deribit DVOL.

Typical URL pattern (subject to change):
  https://www.cryptodatadownload.com/cdd/Deribit_BTCDVOL_d.csv
  https://www.cryptodatadownload.com/cdd/Deribit_ETHDVOL_d.csv
"""

from __future__ import annotations

import argparse
import sys
from io import StringIO
from pathlib import Path

import pandas as pd
import requests


BASE = "https://www.cryptodatadownload.com/cdd"


def fetch_dvol_csv(currency: str = "BTC") -> pd.DataFrame:
    """Download the published DVOL OHLC CSV mirror. Returns OHLC DataFrame."""
    url = f"{BASE}/Deribit_{currency.upper()}DVOL_d.csv"
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    # cdd CSVs typically have a comment header row; try both with and without skiprows
    try:
        df = pd.read_csv(StringIO(r.text), skiprows=1)
    except Exception:
        df = pd.read_csv(StringIO(r.text))
    # Normalize column names if present
    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date").sort_index()
    return df


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="adapters.crypto_data_download")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_fetch = sub.add_parser("fetch")
    p_fetch.add_argument("--thesis_id", required=True)
    p_fetch.add_argument("--currency", default="BTC", choices=["BTC", "ETH"])
    args = parser.parse_args(argv)
    if args.cmd == "fetch":
        df = fetch_dvol_csv(args.currency)
        out_dir = Path(f"theses/{args.thesis_id}/data")
        out_dir.mkdir(parents=True, exist_ok=True)
        df.to_parquet(out_dir / f"cdd_dvol_{args.currency}.parquet")
        print(f"Wrote {len(df)} DVOL rows for {args.currency}")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
