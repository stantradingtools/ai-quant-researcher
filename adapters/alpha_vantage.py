"""adapters.alpha_vantage: Alpha Vantage equity universe + (future) price/options.

STATE (Phase 1):
  - LISTING_STATUS universe pull  -> IMPLEMENTED (this file).
      Survivorship-free US listing master: active + delisted symbols, with
      ipo/delist dates. Free, tier-independent endpoint (~2 calls total).
  - fetch_bars / fetch_options_chain / fetch_earnings_calendar -> STUBS.

API docs: https://www.alphavantage.co/documentation/
Free tier: 25 requests/day, 5 requests/min. LISTING_STATUS works on the public
'demo' key, so the universe pull is usable with no subscription.

API key resolution (first hit wins):
    ALPHAVANTAGE_API_KEY  ->  AV_API_KEY  ->  ALPHA_VANTAGE_API_KEY
If none are set, the LISTING_STATUS pull falls back to the public 'demo' key
(with a loud warning) because that endpoint accepts it; every other endpoint
needs a real key. Set one in .env to lift the rate limit.

Outputs (all under data/, which is gitignored — commit CODE only, never data):
    data/av/universe_listing.parquet      union of active + delisted listings
    data/av/raw/listing_status_active.csv    raw active CSV (cache)
    data/av/raw/listing_status_delisted.csv  raw delisted CSV (cache)
    data/av/survivorship_compare.csv      per-symbol overlap vs the ORATS panel

CLI:
    python -m adapters.alpha_vantage universe          # pull -> parquet
    python -m adapters.alpha_vantage compare           # overlap vs ORATS panel
    python -m adapters.alpha_vantage pull              # universe + compare
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
from pathlib import Path

import pandas as pd

try:
    import requests
except ImportError:
    requests = None


# Key resolution order matches the task spec (ALPHAVANTAGE_API_KEY first), with
# ALPHA_VANTAGE_API_KEY kept as a third fallback for the older stub convention.
AV_API_KEY = (
    os.environ.get("ALPHAVANTAGE_API_KEY")
    or os.environ.get("AV_API_KEY")
    or os.environ.get("ALPHA_VANTAGE_API_KEY")
)
AV_BASE = "https://www.alphavantage.co/query"

# Data-layer paths. data/av/ is the persistent AV master (NOT per-thesis).
AV_DIR = Path("data/av")
AV_RAW_DIR = AV_DIR / "raw"
AV_UNIVERSE_PARQUET = AV_DIR / "universe_listing.parquet"
AV_COMPARE_CSV = AV_DIR / "survivorship_compare.csv"

# The ORATS signal panel we measure AV's survivorship coverage against.
ORATS_PANEL = Path("data/orats/universe_signal.parquet")

# LISTING_STATUS columns, in AV's documented order.
LISTING_COLUMNS = ["symbol", "name", "exchange", "assetType",
                   "ipoDate", "delistingDate", "status"]

# US listed-equity exchanges (uppercased on compare). assetType=='Stock' filters
# out ETFs/funds. AV currently emits NYSE / NASDAQ / NYSE ARCA / NYSE MKT / BATS;
# the extra aliases future-proof against AV renaming a venue.
US_EQUITY_EXCHANGES = {
    "NYSE", "NASDAQ", "NYSE ARCA", "NYSE MKT", "NYSE AMERICAN",
    "AMEX", "BATS", "CBOE", "BATS Z", "IEXG",
}


# ── Security: never let the API key reach a log or exception ─────────────

def _redact(text) -> str:
    """Strip the API key from any string before it lands in a log/exception.

    AV puts the key in the query string (?apikey=KEY), so a leaked URL leaks the
    key. Mirror adapters.orats: scrub the literal value and any apikey= param.
    """
    if text is None:
        return text
    s = str(text)
    if AV_API_KEY:
        s = s.replace(AV_API_KEY, "***")
    return re.sub(r"(apikey=)[^&\s]+", r"\1***", s)


def _resolve_api_key(allow_demo: bool = True) -> tuple[str, str]:
    """Return (key, source). Env key wins; else the public 'demo' key (loud
    warning) when allow_demo, else a clear error. 'demo' works for
    LISTING_STATUS but for nothing that needs a real subscription.
    """
    if AV_API_KEY:
        return AV_API_KEY, "env"
    if allow_demo:
        print(
            "[alpha_vantage] WARNING: no API key in env "
            "(ALPHAVANTAGE_API_KEY / AV_API_KEY / ALPHA_VANTAGE_API_KEY). "
            "Falling back to the public 'demo' key — fine for LISTING_STATUS "
            "(the free universe endpoint), NOT for bars/options/earnings. "
            "Set a key in .env to lift the rate limit.",
            file=sys.stderr,
        )
        return "demo", "demo"
    raise RuntimeError(
        "Alpha Vantage API key not set. Add ALPHAVANTAGE_API_KEY (or AV_API_KEY) "
        "to your environment/.env."
    )


# ── LISTING_STATUS client ────────────────────────────────────────────────

def _detect_json_note(text: str) -> str | None:
    """LISTING_STATUS returns CSV on success. On rate-limit / bad key / bad
    params AV returns a JSON object instead ({"Note"|"Information"|"Error
    Message": ...}). Return that message if the body is JSON, else None.
    """
    s = text.lstrip()
    if not s.startswith("{"):
        return None
    try:
        obj = json.loads(s)
    except json.JSONDecodeError:
        return s[:300]  # JSON-ish but unparseable — surface a snippet
    for k in ("Error Message", "Note", "Information"):
        if k in obj:
            return f"{k}: {obj[k]}"
    return json.dumps(obj)[:300]


def fetch_listing_status(state: str = "active", *, allow_demo: bool = True,
                         use_cache: bool = True) -> pd.DataFrame:
    """One LISTING_STATUS call. state in {'active','delisted'}.

    Caches the raw CSV under data/av/raw/. Fails loudly (no URL/key in the
    message) on a rate-limit/error JSON note, a missing/invalid key, or a body
    that isn't the expected CSV.
    """
    if state not in ("active", "delisted"):
        raise ValueError("state must be 'active' or 'delisted'")

    raw_path = AV_RAW_DIR / f"listing_status_{state}.csv"
    if use_cache and raw_path.exists():
        text = raw_path.read_text(encoding="utf-8")
    else:
        if requests is None:
            raise RuntimeError(
                "adapters.alpha_vantage requires 'requests' (pip install requests)."
            )
        key, _src = _resolve_api_key(allow_demo=allow_demo)
        params = {"function": "LISTING_STATUS", "apikey": key}
        if state == "delisted":
            params["state"] = "delisted"
        try:
            r = requests.get(AV_BASE, params=params, timeout=60)
        except Exception as e:  # network-level; redact before it propagates
            raise RuntimeError(f"Alpha Vantage request failed: {_redact(e)}") from None
        if r.status_code != 200:
            # Don't use raise_for_status(): its message embeds the full URL+key.
            raise RuntimeError(f"Alpha Vantage HTTP {r.status_code} (request rejected)")
        text = r.text

    note = _detect_json_note(text)
    if note is not None:
        raise RuntimeError(
            "Alpha Vantage LISTING_STATUS returned an error/rate-limit note "
            f"instead of CSV: {_redact(note)}"
        )
    header = text.splitlines()[0] if text.strip() else ""
    if "symbol" not in header.lower() or "exchange" not in header.lower():
        raise RuntimeError(
            "Alpha Vantage LISTING_STATUS response not recognized as CSV. "
            f"First line: {header[:200]!r}"
        )

    # Persist the raw CSV (cache) only after we trust it.
    AV_RAW_DIR.mkdir(parents=True, exist_ok=True)
    if not (use_cache and raw_path.exists()):
        raw_path.write_text(text, encoding="utf-8")

    # keep_default_na=False is REQUIRED: pandas would otherwise turn real tickers
    # like "NA" (Nabors), "NAN", "NULL", "TRUE" into NaN. Read all as strings,
    # then coerce only the date columns ('' / 'null' -> NaT).
    df = pd.read_csv(io.StringIO(text), keep_default_na=False, dtype=str)
    df["symbol"] = df["symbol"].str.strip()
    df = df[df["symbol"] != ""].reset_index(drop=True)  # drop blank/trailing rows
    for col in ("ipoDate", "delistingDate"):
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce")
    df["state"] = state  # provenance alongside AV's own 'status' column
    return df


def fetch_universe_listing(*, allow_demo: bool = True, use_cache: bool = True,
                           require_delisted: bool = True,
                           out: Path | str = AV_UNIVERSE_PARQUET) -> pd.DataFrame:
    """Pull active + delisted, union into one frame, write parquet, return it.

    LISTING_STATUS is a free endpoint, but the public 'demo' key only serves the
    active list (state=delisted returns '{}'); any real key returns delisted too.
    With require_delisted=False, a failed delisted pull degrades to an ACTIVE-ONLY
    universe with a loud warning (NOT survivorship-free) instead of erroring.
    """
    active = fetch_listing_status("active", allow_demo=allow_demo, use_cache=use_cache)
    try:
        delisted = fetch_listing_status("delisted", allow_demo=allow_demo, use_cache=use_cache)
    except RuntimeError as e:
        if require_delisted:
            raise
        print(
            f"[alpha_vantage] WARNING: delisted pull unavailable ({_redact(e)}). "
            "Writing ACTIVE-ONLY universe — this is NOT survivorship-free. "
            "Provide a real AV key (ALPHAVANTAGE_API_KEY) for the delisted list.",
            file=sys.stderr,
        )
        delisted = active.iloc[0:0].copy()  # empty frame, identical columns
    universe = pd.concat([active, delisted], ignore_index=True)
    # Defensive: drop only exact-duplicate rows (a symbol can legitimately appear
    # in both lists if a ticker was reused after a delisting).
    universe = universe.drop_duplicates().reset_index(drop=True)

    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    universe.to_parquet(out)
    print(
        f"universe_listing: {len(active):,} active + {len(delisted):,} delisted "
        f"-> {len(universe):,} rows -> {out}"
    )
    return universe


def filter_us_equities(df: pd.DataFrame) -> pd.DataFrame:
    """US listed common stock only (assetType=='Stock' on a US equity exchange).
    Keeps the full frame intact — returns a filtered copy."""
    exch = df["exchange"].astype(str).str.strip().str.upper()
    allowed = {e.upper() for e in US_EQUITY_EXCHANGES}
    is_stock = df["assetType"].astype(str).str.strip().str.casefold() == "stock"
    return df[is_stock & exch.isin(allowed)].copy()


# ── Survivorship comparison vs the ORATS panel ───────────────────────────

def _orats_panel_symbols(path: Path = ORATS_PANEL) -> set[str]:
    """Distinct, upper-cased symbols from the ORATS signal panel.

    Reads only the ticker column, one row group at a time (the panel is written
    one row group per ticker and is ~hundreds of MB), so peak memory stays tiny.
    """
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    pf = pq.ParquetFile(str(path))
    names = pf.schema_arrow.names
    col = next((c for c in ("ticker", "symbol", "Ticker", "Symbol") if c in names), None)
    if col is None:
        raise RuntimeError(f"ORATS panel {path} has no ticker/symbol column; has {names}")
    syms: set[str] = set()
    for i in range(pf.num_row_groups):
        tbl = pf.read_row_group(i, columns=[col])
        for s in pc.unique(tbl.column(col)).to_pylist():
            if s:
                syms.add(str(s).strip().upper())
    return syms


def compare_survivorship(universe: pd.DataFrame | None = None, *,
                         panel: Path = ORATS_PANEL,
                         out: Path | str = AV_COMPARE_CSV) -> dict:
    """Quantify how much AV's survivorship-free universe adds over the ORATS panel.

    Prints the headline counts and writes a per-symbol detail table to `out`.
    Degrades gracefully (still reports AV-side counts) if the panel is absent.
    """
    if universe is None:
        if not AV_UNIVERSE_PARQUET.exists():
            raise RuntimeError(
                f"{AV_UNIVERSE_PARQUET} not found — run the `universe` command first."
            )
        universe = pd.read_parquet(AV_UNIVERSE_PARQUET)

    sym_u = universe["symbol"].astype(str).str.strip().str.upper()
    status_l = universe["status"].astype(str).str.strip().str.casefold()
    av_active = set(sym_u[status_l == "active"])
    av_delisted = set(sym_u[status_l == "delisted"])

    us = filter_us_equities(universe)
    us_sym_u = us["symbol"].astype(str).str.strip().str.upper()
    av_us = set(us_sym_u)
    av_us_active = av_us & av_active
    av_us_delisted = av_us & av_delisted

    print("── Survivorship comparison (AV LISTING_STATUS vs ORATS panel) ──")
    print(f"AV total symbols          : {len(av_active | av_delisted):,} "
          f"({len(av_active):,} active, {len(av_delisted):,} delisted)")
    print(f"AV US-equity symbols      : {len(av_us):,} "
          f"({len(av_us_active):,} active, {len(av_us_delisted):,} delisted)")

    if not Path(panel).exists():
        print(f"ORATS panel               : NOT FOUND at {panel}")
        print("  -> skipping overlap; run the ORATS universe build first.")
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        rows = [{"symbol": s, "in_orats": False,
                 "in_av_active": s in av_active, "in_av_delisted": s in av_delisted,
                 "in_av_us_equity": s in av_us,
                 "category": "av_us_only" if s in av_us else "av_nonus"}
                for s in sorted(av_active | av_delisted)]
        pd.DataFrame(rows).to_csv(out, index=False)
        print(f"  wrote AV-only detail -> {out}")
        return {"orats_panel_found": False, "av_total": len(av_active | av_delisted),
                "av_us": len(av_us)}

    orats = _orats_panel_symbols(Path(panel))
    in_active = orats & av_active
    in_delisted = (orats & av_delisted) - av_active     # delisted but not (also) active
    missing = orats - av_active - av_delisted
    us_not_in_orats = av_us - orats                     # the survivorship gap
    gap_active = av_us_active - orats
    gap_delisted = av_us_delisted - orats

    print(f"ORATS panel tickers       : {len(orats):,}")
    print(f"  of those, AV-active     : {len(in_active):,}")
    print(f"  of those, AV-delisted   : {len(in_delisted):,}")
    print(f"  of those, AV-missing    : {len(missing):,}")
    print(f"AV US-equity NOT in ORATS : {len(us_not_in_orats):,} "
          f"({len(gap_active):,} active, {len(gap_delisted):,} delisted)  "
          f"<- survivorship coverage AV adds")
    if not av_delisted:
        print("  NOTE: this universe has NO delisted rows (active-only pull) — so "
              "'AV-missing' here conflates truly-unknown tickers with delisted ones. "
              "Pull the delisted list (real AV key) for the true survivorship gap.")

    # Per-symbol detail over the union of both universes.
    meta_cols = ["symbol", "name", "exchange", "assetType", "status"]
    meta = (universe.assign(_su=sym_u)
            .sort_values("status")  # 'Active' < 'Delisted' so active wins on dedupe
            .drop_duplicates("_su", keep="first")
            .set_index("_su")[meta_cols])
    detail = []
    for s in sorted(orats | av_us | av_active | av_delisted):
        in_o = s in orats
        in_a = s in av_active
        in_d = s in av_delisted
        in_us = s in av_us
        if in_o and in_a:
            cat = "orats_and_av_active"
        elif in_o and in_d:
            cat = "orats_and_av_delisted"
        elif in_o:
            cat = "orats_av_missing"
        elif in_us:
            cat = "av_us_only"          # survivorship gap (esp. delisted names)
        else:
            cat = "av_nonus_only"
        m = meta.loc[s] if s in meta.index else None
        detail.append({
            "symbol": s,
            "in_orats": in_o, "in_av_active": in_a, "in_av_delisted": in_d,
            "in_av_us_equity": in_us, "category": cat,
            "av_name": None if m is None else m["name"],
            "av_exchange": None if m is None else m["exchange"],
            "av_assetType": None if m is None else m["assetType"],
            "av_status": None if m is None else m["status"],
        })
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(detail).to_csv(out, index=False)
    print(f"wrote per-symbol detail   -> {out}  ({len(detail):,} rows)")

    return {
        "orats_panel_found": True,
        "orats_tickers": len(orats),
        "orats_av_active": len(in_active),
        "orats_av_delisted": len(in_delisted),
        "orats_av_missing": len(missing),
        "av_us_not_in_orats": len(us_not_in_orats),
        "av_us_not_in_orats_active": len(gap_active),
        "av_us_not_in_orats_delisted": len(gap_delisted),
    }


# ── Stubs (Phase 1/2 price + options — unchanged interface) ──────────────

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


# ── CLI ───────────────────────────────────────────────────────────────────

def _cmd_universe(args) -> int:
    fetch_universe_listing(allow_demo=not args.require_key, use_cache=not args.no_cache,
                           require_delisted=not args.allow_active_only)
    return 0


def _cmd_compare(args) -> int:
    compare_survivorship(panel=Path(args.panel))
    return 0


def _cmd_pull(args) -> int:
    universe = fetch_universe_listing(allow_demo=not args.require_key, use_cache=not args.no_cache,
                                      require_delisted=not args.allow_active_only)
    compare_survivorship(universe=universe, panel=Path(args.panel))
    return 0


def _cmd_fetch(args) -> int:
    """Original per-thesis stub entry point (bars/options/earnings)."""
    symbols = [s.strip() for s in args.tickers.split(",") if s.strip()]
    out_dir = Path(f"theses/{args.thesis_id}/data")
    out_dir.mkdir(parents=True, exist_ok=True)
    if args.datatype == "bars":
        df = fetch_bars(symbols, args.start, args.end)
        df.to_parquet(out_dir / "av_bars.parquet")
    elif args.datatype == "options":
        for sym in symbols:
            df = fetch_options_chain(sym)
            df.to_parquet(out_dir / f"av_options_{sym}.parquet")
    elif args.datatype == "earnings":
        df = fetch_earnings_calendar()
        df.to_parquet(out_dir / "av_earnings.parquet")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="adapters.alpha_vantage")
    sub = parser.add_subparsers(dest="cmd", required=True)

    pu = sub.add_parser("universe", help="Pull LISTING_STATUS -> data/av/universe_listing.parquet")
    pu.add_argument("--require-key", action="store_true",
                    help="Error instead of falling back to the public 'demo' key.")
    pu.add_argument("--no-cache", action="store_true", help="Ignore cached raw CSVs.")
    pu.add_argument("--allow-active-only", action="store_true",
                    help="If the delisted pull fails (e.g. demo key), write active-only "
                         "(NOT survivorship-free) instead of erroring.")

    pc = sub.add_parser("compare", help="Survivorship overlap vs the ORATS panel")
    pc.add_argument("--panel", default=str(ORATS_PANEL))

    pp = sub.add_parser("pull", help="universe + compare in one go")
    pp.add_argument("--require-key", action="store_true")
    pp.add_argument("--no-cache", action="store_true")
    pp.add_argument("--allow-active-only", action="store_true")
    pp.add_argument("--panel", default=str(ORATS_PANEL))

    pf = sub.add_parser("fetch", help="(stub) per-thesis bars/options/earnings")
    pf.add_argument("--thesis_id", required=True)
    pf.add_argument("--tickers", required=True)
    pf.add_argument("--start", required=True)
    pf.add_argument("--end", required=True)
    pf.add_argument("--datatype", choices=["bars", "options", "earnings"], default="bars")

    args = parser.parse_args(argv)
    if args.cmd == "universe":
        return _cmd_universe(args)
    if args.cmd == "compare":
        return _cmd_compare(args)
    if args.cmd == "pull":
        return _cmd_pull(args)
    if args.cmd == "fetch":
        return _cmd_fetch(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
