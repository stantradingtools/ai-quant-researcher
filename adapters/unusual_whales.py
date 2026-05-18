"""adapters.unusual_whales: Unusual Whales API adapter.

STATE: SCAFFOLD ONLY. The full adapter is structured but inert until
UW_API_KEY is set in .env. All fetch_* methods raise UnusualWhalesNotSubscribed
when called without the key.

To activate:
1. Subscribe at unusualwhales.com
2. Add UW_API_KEY=your_key_here to .env
3. (Optional) Run: claude mcp add unusual-whales https://unusualwhales.com/public-api/mcp
   to enable native MCP tool access in the hypothesis-refiner subagent

Endpoints scaffolded (see api.unusualwhales.com/docs for current schemas):
- fetch_dark_pool_trades(ticker, date, as_of=None)
- fetch_flow_alerts(ticker, start, end, min_premium=100_000)
- fetch_gex_history(ticker, start, end)
- fetch_options_volume_history(ticker, start, end)
- fetch_congressional_trades(ticker, start, end)

Point-in-time discipline note:
UW reports trades AFTER they print, with reporting lag. For backtests,
the as_of parameter MUST be set to filter to trades whose reported_at
<= as_of. Otherwise look-ahead bias.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import pandas as pd


UW_KEY = os.environ.get("UW_API_KEY")
UW_BASE = "https://api.unusualwhales.com/api"


class UnusualWhalesNotSubscribed(RuntimeError):
    """Raised when UW adapter is called without UW_API_KEY in environment."""

    def __init__(self, message: str | None = None) -> None:
        super().__init__(
            message or (
                "UW_API_KEY not set in .env. UW adapter is scaffolded but inactive. "
                "Subscribe at unusualwhales.com and add UW_API_KEY=... to .env to enable."
            )
        )


def _require_key() -> None:
    if not UW_KEY:
        raise UnusualWhalesNotSubscribed()


# ═══════════════════════════════════════════════════════════════
# Public functions (all currently raise NotSubscribed without UW_API_KEY)

def fetch_dark_pool_trades(
    ticker: str,
    date: str,
    as_of: pd.Timestamp | None = None,
) -> pd.DataFrame:
    """Off-exchange prints for one ticker on one date.

    Returns DataFrame: [timestamp, size, price, premium, market_center, reported_at]
    If as_of is set, filters to trades where reported_at <= as_of.
    If None, returns settled view (use ONLY for non-backtest analysis).
    """
    _require_key()
    # TODO: implement once subscription active
    # endpoint: GET /api/darkpool/{ticker}?date={date}
    raise NotImplementedError("UW dark pool fetch not yet implemented")


def fetch_flow_alerts(
    ticker: str,
    start: str,
    end: str,
    min_premium: float = 100_000.0,
) -> pd.DataFrame:
    """Unusual options activity alerts above premium threshold."""
    _require_key()
    raise NotImplementedError("UW flow alerts fetch not yet implemented")


def fetch_gex_history(ticker: str, start: str, end: str) -> pd.DataFrame:
    """Daily GEX time series. Cross-validates Flash Alpha exposure data."""
    _require_key()
    raise NotImplementedError("UW GEX history fetch not yet implemented")


def fetch_options_volume_history(ticker: str, start: str, end: str) -> pd.DataFrame:
    """Daily options volume by ticker."""
    _require_key()
    raise NotImplementedError("UW options volume fetch not yet implemented")


def fetch_congressional_trades(
    ticker: str | None,
    start: str,
    end: str,
) -> pd.DataFrame:
    """Congressional and senate trading disclosures."""
    _require_key()
    raise NotImplementedError("UW congressional trades fetch not yet implemented")


# ═══════════════════════════════════════════════════════════════
# CLI (consistent with other adapters; reports inert state if no key)

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="adapters.unusual_whales")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_fetch = sub.add_parser("fetch")
    p_fetch.add_argument("--thesis_id", required=True)
    p_fetch.add_argument("--start", required=True)
    p_fetch.add_argument("--end", required=True)
    p_fetch.add_argument("--tickers", required=True)
    p_fetch.add_argument("--datatype", choices=["dark_pool", "flow", "gex", "volume"],
                        default="flow")

    sub.add_parser("status")

    args = parser.parse_args(argv)

    if args.cmd == "status":
        if UW_KEY:
            print("Unusual Whales adapter: ACTIVE (UW_API_KEY set)")
        else:
            print("Unusual Whales adapter: INACTIVE (UW_API_KEY not set in .env)")
            print("To activate: subscribe at unusualwhales.com and set UW_API_KEY")
        return 0

    if args.cmd == "fetch":
        try:
            _require_key()
        except UnusualWhalesNotSubscribed as e:
            print(f"Cannot fetch: {e}", file=sys.stderr)
            return 2
        # Will be implemented in Phase 3 (post-subscription)
        print("Fetch not yet implemented; subscribe first then implement.", file=sys.stderr)
        return 2

    return 1


if __name__ == "__main__":
    sys.exit(main())
