"""adapters.deribit: Deribit public REST API for crypto vol data (free).

STATE: PARTIALLY IMPLEMENTED. DVOL OHLC fetcher works without API key.
Chain snapshot fetcher works without API key. Historical chain reconstruction
requires Tardis or manual stitching from snapshots.

Public REST base: https://www.deribit.com/api/v2/public

No auth required for market data endpoints. Rate-limited; be polite.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import pandas as pd
import requests


DERIBIT_BASE = "https://www.deribit.com/api/v2/public"
REQUEST_TIMEOUT = 30


def fetch_dvol_history(
    currency: str = "BTC",
    start: str = "2020-01-01",
    end: str | None = None,
    resolution: str = "1D",
) -> pd.DataFrame:
    """DVOL OHLC index. resolution one of: '1', '60', '720', '1D'.

    Returns DataFrame indexed by timestamp with [open, high, low, close].
    DVOL is Deribit's forward-looking 30-day implied volatility index.
    """
    start_ts = int(pd.Timestamp(start, tz="UTC").timestamp() * 1000)
    end_ts = int(pd.Timestamp(end or "now", tz="UTC").timestamp() * 1000)
    r = requests.get(
        f"{DERIBIT_BASE}/get_volatility_index_data",
        params={
            "currency": currency,
            "start_timestamp": start_ts,
            "end_timestamp": end_ts,
            "resolution": resolution,
        },
        timeout=REQUEST_TIMEOUT,
    )
    r.raise_for_status()
    data = r.json().get("result", {}).get("data", [])
    if not data:
        return pd.DataFrame(columns=["open", "high", "low", "close"])
    df = pd.DataFrame(data, columns=["timestamp_ms", "open", "high", "low", "close"])
    df["timestamp"] = pd.to_datetime(df["timestamp_ms"], unit="ms", utc=True)
    return df.set_index("timestamp").drop(columns=["timestamp_ms"]).sort_index()


def fetch_chain_snapshot(currency: str = "BTC") -> pd.DataFrame:
    """Current full option chain. NOT historical — for live/recent use only."""
    r = requests.get(
        f"{DERIBIT_BASE}/get_book_summary_by_currency",
        params={"currency": currency, "kind": "option"},
        timeout=REQUEST_TIMEOUT,
    )
    r.raise_for_status()
    data = r.json().get("result", [])
    return pd.DataFrame(data)


def fetch_index_price(currency: str = "BTC") -> float:
    """Current index price for the currency."""
    r = requests.get(
        f"{DERIBIT_BASE}/get_index_price",
        params={"index_name": f"{currency.lower()}_usd"},
        timeout=REQUEST_TIMEOUT,
    )
    r.raise_for_status()
    return float(r.json()["result"]["index_price"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="adapters.deribit")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_fetch = sub.add_parser("fetch")
    p_fetch.add_argument("--thesis_id", required=True)
    p_fetch.add_argument("--currency", default="BTC", choices=["BTC", "ETH"])
    p_fetch.add_argument("--start", required=True)
    p_fetch.add_argument("--end", default=None)
    p_fetch.add_argument("--datatype", choices=["dvol", "chain"], default="dvol")
    p_fetch.add_argument("--resolution", default="1D")
    args = parser.parse_args(argv)

    if args.cmd == "fetch":
        out_dir = Path(f"theses/{args.thesis_id}/data")
        out_dir.mkdir(parents=True, exist_ok=True)
        if args.datatype == "dvol":
            df = fetch_dvol_history(args.currency, args.start, args.end, args.resolution)
            df.to_parquet(out_dir / f"deribit_dvol_{args.currency}.parquet")
            print(f"Wrote {len(df)} DVOL rows for {args.currency}")
        else:
            df = fetch_chain_snapshot(args.currency)
            df.to_parquet(out_dir / f"deribit_chain_{args.currency}.parquet")
            print(f"Wrote {len(df)} chain rows for {args.currency}")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
