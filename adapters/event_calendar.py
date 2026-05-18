"""adapters.event_calendar: unified event calendar for backtest validation.

Produces a single DataFrame of relevant event dates with columns:
  [date, time_et, event_type, ticker, details]

event_type one of:
  - triple_witching         (deterministic: 3rd Fri Mar/Jun/Sep/Dec)
  - monthly_opex            (deterministic: 3rd Fri every month)
  - vix_expiration          (deterministic: Wed before 3rd Fri every month)
  - nfp                     (deterministic: 1st Fri of every month)
  - jpm_collar_roll         (deterministic: last business day Mar/Jun/Sep/Dec)
  - jpm_collar_active       (data-fetch or manual: monthly snapshot of active strikes)
  - fomc                    (hard-coded CSV: config/fomc_dates.csv)
  - cpi                     (hard-coded CSV: config/cpi_dates.csv)
  - market_holiday_full     (hard-coded CSV: config/market_holidays.csv)
  - market_holiday_half     (hard-coded CSV: config/market_holidays.csv)
  - earnings                (data-fetch: AV or FMP per-ticker earnings dates)

Usage:
    python -m adapters.event_calendar fetch \
      --thesis_id <id> --start 2020-01-01 --end 2026-12-31 \
      --tickers AAPL,SPY,QQQ
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd


# ═══════════════════════════════════════════════════════════════
# Deterministic date helpers

def _third_friday(year: int, month: int) -> date:
    """The 3rd Friday of (year, month). Used for monthly OPEX and triple witching."""
    d = date(year, month, 1)
    # weekday(): Monday=0, Friday=4
    first_friday = d + timedelta(days=(4 - d.weekday()) % 7)
    return first_friday + timedelta(weeks=2)


def _first_friday(year: int, month: int) -> date:
    """The 1st Friday of (year, month). Used for NFP releases."""
    d = date(year, month, 1)
    return d + timedelta(days=(4 - d.weekday()) % 7)


def _wednesday_before(d: date) -> date:
    """The Wednesday preceding the given Friday."""
    return d - timedelta(days=2)


def _last_business_day(year: int, month: int) -> date:
    """Last weekday of (year, month). Approximation: doesn't account for holidays."""
    next_month_first = (date(year, month, 1) + pd.offsets.MonthBegin(1)).date() \
        if month < 12 else date(year + 1, 1, 1)
    d = next_month_first - timedelta(days=1)
    while d.weekday() >= 5:  # Saturday=5, Sunday=6
        d -= timedelta(days=1)
    return d


def _months_in_range(start: date, end: date):
    """Yield (year, month) tuples covering all months in [start, end] inclusive."""
    y, m = start.year, start.month
    while date(y, m, 1) <= end:
        yield (y, m)
        m += 1
        if m > 12:
            y += 1
            m = 1


# ═══════════════════════════════════════════════════════════════
# Deterministic event generators

def get_triple_witching_dates(start: str, end: str) -> pd.DataFrame:
    s, e = pd.Timestamp(start).date(), pd.Timestamp(end).date()
    rows = []
    for y, m in _months_in_range(s, e):
        if m not in (3, 6, 9, 12):
            continue
        d = _third_friday(y, m)
        if s <= d <= e:
            rows.append({"date": pd.Timestamp(d), "time_et": "16:00",
                         "event_type": "triple_witching", "ticker": None,
                         "details": "Stock options + index options + index futures expire"})
    return pd.DataFrame(rows)


def get_monthly_opex_dates(start: str, end: str) -> pd.DataFrame:
    s, e = pd.Timestamp(start).date(), pd.Timestamp(end).date()
    rows = []
    for y, m in _months_in_range(s, e):
        d = _third_friday(y, m)
        if s <= d <= e:
            rows.append({"date": pd.Timestamp(d), "time_et": "16:00",
                         "event_type": "monthly_opex", "ticker": None,
                         "details": "Equity monthly options expire"})
    return pd.DataFrame(rows)


def get_vix_expiration_dates(start: str, end: str) -> pd.DataFrame:
    s, e = pd.Timestamp(start).date(), pd.Timestamp(end).date()
    rows = []
    for y, m in _months_in_range(s, e):
        friday = _third_friday(y, m)
        wed = _wednesday_before(friday)
        if s <= wed <= e:
            rows.append({"date": pd.Timestamp(wed), "time_et": "09:00",
                         "event_type": "vix_expiration", "ticker": None,
                         "details": "VIX futures and options settle (AM)"})
    return pd.DataFrame(rows)


def get_nfp_dates(start: str, end: str) -> pd.DataFrame:
    s, e = pd.Timestamp(start).date(), pd.Timestamp(end).date()
    rows = []
    for y, m in _months_in_range(s, e):
        d = _first_friday(y, m)
        if s <= d <= e:
            rows.append({"date": pd.Timestamp(d), "time_et": "08:30",
                         "event_type": "nfp", "ticker": None,
                         "details": "Non-Farm Payrolls (BLS)"})
    return pd.DataFrame(rows)


def get_jpm_collar_roll_dates(start: str, end: str) -> pd.DataFrame:
    """JHEQX (JPM Hedged Equity Fund) quarterly roll: last business day of Mar/Jun/Sep/Dec.

    NOTE: This fund is QUARTERLY, not monthly. The roll dates here capture
    when strikes change. The active strikes between rolls are queried separately
    via get_jpm_collar_strikes_for_month() — see config/jpm_collar_history.csv.
    """
    s, e = pd.Timestamp(start).date(), pd.Timestamp(end).date()
    rows = []
    for y, m in _months_in_range(s, e):
        if m not in (3, 6, 9, 12):
            continue
        d = _last_business_day(y, m)
        if s <= d <= e:
            rows.append({"date": pd.Timestamp(d), "time_et": "15:30",
                         "event_type": "jpm_collar_roll", "ticker": "SPX",
                         "details": "JHEQX quarterly collar roll (JPMorgan Hedged Equity Fund)"})
    return pd.DataFrame(rows)


# ═══════════════════════════════════════════════════════════════
# Hard-coded CSV loaders

def _load_csv_dates(csv_path: Path, event_type: str, start: str, end: str) -> pd.DataFrame:
    if not csv_path.exists():
        return pd.DataFrame(columns=["date", "time_et", "event_type", "ticker", "details"])
    df = pd.read_csv(csv_path, parse_dates=["date"])
    s, e = pd.Timestamp(start), pd.Timestamp(end)
    df = df[(df["date"] >= s) & (df["date"] <= e)].copy()
    if "event_type" not in df.columns:
        df["event_type"] = event_type
    if "ticker" not in df.columns:
        df["ticker"] = None
    if "time_et" not in df.columns:
        df["time_et"] = None
    if "details" not in df.columns:
        df["details"] = None
    return df[["date", "time_et", "event_type", "ticker", "details"]]


def get_fomc_dates(start: str, end: str) -> pd.DataFrame:
    return _load_csv_dates(Path("config/fomc_dates.csv"), "fomc", start, end)


def get_cpi_release_dates(start: str, end: str) -> pd.DataFrame:
    return _load_csv_dates(Path("config/cpi_dates.csv"), "cpi", start, end)


def get_market_holidays(start: str, end: str) -> pd.DataFrame:
    df = _load_csv_dates(Path("config/market_holidays.csv"), "market_holiday", start, end)
    # status column should distinguish full vs half; expect a 'type' field
    if "type" in df.columns:
        df["event_type"] = df["type"].map({
            "full_close": "market_holiday_full",
            "early_close_1pm": "market_holiday_half",
        }).fillna("market_holiday_full")
    return df


# ═══════════════════════════════════════════════════════════════
# JPM collar active strikes (data-fetch or manual)

def get_jpm_collar_strikes_for_month(month_yyyy_mm: str) -> dict | None:
    """Active JHEQX collar strikes for a given month.

    Reads config/jpm_collar_history.csv if present. Returns None if no data.
    The CSV has columns:
      [effective_date, expiration_date, long_put_strike, short_put_strike,
       short_call_strike, notional_usd, source]
    """
    csv_path = Path("config/jpm_collar_history.csv")
    if not csv_path.exists():
        return None
    df = pd.read_csv(csv_path, parse_dates=["effective_date", "expiration_date"])
    if df.empty:
        return None

    target = pd.Timestamp(f"{month_yyyy_mm}-01")
    # Active = most recent effective_date <= target
    active = df[df["effective_date"] <= target].sort_values("effective_date")
    if active.empty:
        return None
    row = active.iloc[-1]
    return {
        "effective_date": str(row["effective_date"].date()),
        "expiration_date": str(row["expiration_date"].date()),
        "long_put_strike": float(row["long_put_strike"]),
        "short_put_strike": float(row["short_put_strike"]),
        "short_call_strike": float(row["short_call_strike"]),
        "notional_usd": float(row["notional_usd"]) if not pd.isna(row.get("notional_usd")) else None,
        "source": row.get("source", "manual"),
    }


# ═══════════════════════════════════════════════════════════════
# Earnings (per-ticker, data-fetch required — stub for now)

def get_earnings_dates(tickers: list[str], start: str, end: str) -> pd.DataFrame:
    """Per-ticker earnings dates. Currently a stub.

    TODO Phase 2: integrate with Alpha Vantage EARNINGS_CALENDAR endpoint
    or FMP earnings_calendar. For now returns empty DataFrame.
    """
    return pd.DataFrame(columns=["date", "time_et", "event_type", "ticker", "details"])


# ═══════════════════════════════════════════════════════════════
# Master function

def get_event_calendar(
    start: str,
    end: str,
    tickers: list[str] | None = None,
    include_earnings: bool = True,
    include_macros: bool = True,
    include_opex: bool = True,
    include_holidays: bool = True,
    include_jpm_collar: bool = True,
) -> pd.DataFrame:
    """Unified event calendar across all sources."""
    frames = []
    if include_opex:
        frames.append(get_triple_witching_dates(start, end))
        frames.append(get_monthly_opex_dates(start, end))
        frames.append(get_vix_expiration_dates(start, end))
    if include_macros:
        frames.append(get_nfp_dates(start, end))
        frames.append(get_fomc_dates(start, end))
        frames.append(get_cpi_release_dates(start, end))
    if include_holidays:
        frames.append(get_market_holidays(start, end))
    if include_jpm_collar:
        frames.append(get_jpm_collar_roll_dates(start, end))
    if include_earnings and tickers:
        frames.append(get_earnings_dates(tickers, start, end))

    if not frames:
        return pd.DataFrame(columns=["date", "time_et", "event_type", "ticker", "details"])

    combined = pd.concat([f for f in frames if not f.empty], ignore_index=True)
    if combined.empty:
        return combined
    return combined.sort_values("date").reset_index(drop=True)


# ═══════════════════════════════════════════════════════════════
# CLI

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="adapters.event_calendar")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_fetch = sub.add_parser("fetch")
    p_fetch.add_argument("--thesis_id", required=True)
    p_fetch.add_argument("--start", required=True)
    p_fetch.add_argument("--end", required=True)
    p_fetch.add_argument("--tickers", default="", help="Comma-separated tickers for earnings")

    args = parser.parse_args(argv)

    if args.cmd == "fetch":
        tickers = [t.strip() for t in args.tickers.split(",") if t.strip()]
        cal = get_event_calendar(args.start, args.end, tickers=tickers)
        out_dir = Path(f"theses/{args.thesis_id}/data")
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / "event_calendar.csv"
        cal.to_csv(out_path, index=False)
        print(f"Wrote {len(cal)} events to {out_path}")
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
