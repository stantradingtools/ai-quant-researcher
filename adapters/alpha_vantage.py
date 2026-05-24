"""adapters.alpha_vantage: Alpha Vantage equity universe + adjusted daily prices.

STATE (Phase 1):
  - LISTING_STATUS universe pull           -> IMPLEMENTED.
      Survivorship-free US listing master (active + delisted, ipo/delist dates).
      Free, tier-independent endpoint (~2 calls). 'demo' key serves active only.
  - Symbol resolution (ORATS -> AV)        -> IMPLEMENTED. No API; uses parquets.
  - TIME_SERIES_DAILY_ADJUSTED price pull  -> IMPLEMENTED. Split/dividend-adjusted
      full daily history, one call per symbol. PREMIUM endpoint (the 'demo' key
      and free tier are rejected). Resumable (per-ticker parquet + manifest),
      paced under the 150 RPM premium limit with exponential backoff.
  - fetch_bars / fetch_options_chain / fetch_earnings_calendar -> STUBS.

API docs: https://www.alphavantage.co/documentation/

API key resolution (first hit wins):
    ALPHAVANTAGE_API_KEY  ->  AV_API_KEY  ->  ALPHA_VANTAGE_API_KEY
LISTING_STATUS falls back to the public 'demo' key (active list only) with a
loud warning; the price pull REQUIRES a real key (demo/free tier are rejected).

Outputs (all under data/, which is gitignored — commit CODE only, never data):
    data/av/universe_listing.parquet         union of active + delisted listings
    data/av/raw/listing_status_*.csv         raw LISTING_STATUS CSVs (cache)
    data/av/survivorship_compare.csv         per-symbol overlap vs the ORATS panel
    data/av/symbol_map.csv                   matched ORATS->AV (+ match_type, status)
    data/av/unmatched.csv                    ORATS tickers with no AV match (review)
    data/av/daily_adjusted/{SYMBOL}.parquet  per-ticker full adjusted history
    data/av/pull_manifest.csv                per-symbol pull status (resumable)
    data/av/daily_adjusted_panel.parquet     consolidated long panel
    data/av/quality_probe.csv                zero-close / >100%-move artifact counts

CLI:
    python -m adapters.alpha_vantage universe       # LISTING_STATUS -> parquet
    python -m adapters.alpha_vantage compare        # overlap vs ORATS panel
    python -m adapters.alpha_vantage symbol-map     # resolve ORATS -> AV symbols
    python -m adapters.alpha_vantage daily-pull      # adjusted price pull (needs key)
    python -m adapters.alpha_vantage consolidate    # per-ticker -> one panel
    python -m adapters.alpha_vantage quality-probe  # artifact counts
"""

from __future__ import annotations

import argparse
import collections
import io
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timezone
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

# ── Daily-adjusted price layer ────────────────────────────────────────────
AV_DAILY_DIR = AV_DIR / "daily_adjusted"            # per-ticker parquet files
AV_PANEL_PARQUET = AV_DIR / "daily_adjusted_panel.parquet"
AV_MANIFEST_CSV = AV_DIR / "pull_manifest.csv"
AV_SYMBOL_MAP_CSV = AV_DIR / "symbol_map.csv"
AV_UNMATCHED_CSV = AV_DIR / "unmatched.csv"
AV_QUALITY_CSV = AV_DIR / "quality_probe.csv"

# Premium key = 150 requests/min, no daily cap. Stay safely under with jitter.
AV_RPM = int(os.environ.get("AV_RPM", "145"))
MAX_RETRIES = int(os.environ.get("AV_MAX_RETRIES", "5"))

# AV TIME_SERIES_DAILY_ADJUSTED JSON field -> our column name.
_DAILY_FIELD_MAP = {
    "1. open": "open", "2. high": "high", "3. low": "low", "4. close": "close",
    "5. adjusted close": "adjusted_close", "6. volume": "volume",
    "7. dividend amount": "dividend_amount", "8. split coefficient": "split_coefficient",
}
# Long-panel column order. Raw close + split_coefficient + dividend_amount are
# KEPT so downstream can pick the adjustment basis and audit split/zero artifacts.
DAILY_COLUMNS = ["date", "open", "high", "low", "close", "adjusted_close",
                 "volume", "dividend_amount", "split_coefficient"]
_DAILY_FLOAT_COLS = ["open", "high", "low", "close", "adjusted_close",
                     "dividend_amount", "split_coefficient"]


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


# ── Symbol resolution (ORATS panel ticker -> AV listing symbol) ──────────

def _norm_key(sym: str) -> str:
    """Canonical key collapsing share-class / suffix punctuation so that ORATS
    'BRK.B' / 'BRKB' and AV 'BRK-B' all map to the same key 'BRKB'."""
    return re.sub(r"[^A-Z0-9]", "", str(sym).upper())


def resolve_symbols(panel: Path = ORATS_PANEL, universe: Path | str = AV_UNIVERSE_PARQUET,
                    map_out: Path | str = AV_SYMBOL_MAP_CSV,
                    unmatched_out: Path | str = AV_UNMATCHED_CSV) -> dict:
    """Map distinct ORATS tickers to AV symbols: exact first, then normalized
    (punctuation-insensitive) for share-class/suffix differences. Writes the
    matched map and the unmatched list (never silently dropped)."""
    orats = sorted(_orats_panel_symbols(Path(panel)))
    uni = pd.read_parquet(universe)
    uni_up = uni["symbol"].astype(str).str.strip().str.upper()
    status_l = uni["status"].astype(str).str.strip().str.casefold()

    # Exact lookup, preferring an active listing if a ticker was reused.
    exact_status: dict[str, str] = {}
    for s, st in zip(uni_up, status_l):
        if s and (s not in exact_status or st == "active"):
            exact_status[s] = st
    # Normalized buckets: norm_key -> [(av_symbol, status), ...].
    norm_map: dict[str, list[tuple[str, str]]] = {}
    for s, st in zip(uni_up, status_l):
        nk = _norm_key(s)
        if len(nk) >= 2:
            norm_map.setdefault(nk, []).append((s, st))

    matched, unmatched = [], []
    for t in orats:
        if t in exact_status:
            matched.append((t, t, "exact", exact_status[t]))
            continue
        cands = norm_map.get(_norm_key(t))
        if cands:
            # Prefer active; then the '-' form (AV's class convention); then short/lexical.
            best = sorted(cands, key=lambda c: (c[1] != "active", "-" not in c[0],
                                                len(c[0]), c[0]))[0]
            matched.append((t, best[0], "normalized", best[1]))
        else:
            unmatched.append(t)

    map_df = pd.DataFrame(matched, columns=["orats_ticker", "av_symbol", "match_type", "av_status"])
    um_df = pd.DataFrame({"orats_ticker": unmatched})
    Path(map_out).parent.mkdir(parents=True, exist_ok=True)
    map_df.to_csv(map_out, index=False)
    um_df.to_csv(unmatched_out, index=False)

    n_exact = int((map_df["match_type"] == "exact").sum())
    n_norm = int((map_df["match_type"] == "normalized").sum())
    n_delisted = int((map_df["av_status"] == "delisted").sum())
    print(f"symbol_map: {len(orats):,} ORATS tickers -> {len(matched):,} matched "
          f"({n_exact:,} exact, {n_norm:,} normalized; {n_delisted:,} delisted), "
          f"{len(unmatched):,} unmatched -> {map_out}")
    if n_delisted == 0:
        print("  NOTE: universe_listing.parquet is active-only — delisted ORATS names "
              "can't match. Rebuild the universe with a real key for full coverage.")
    return {"orats": len(orats), "matched": len(matched), "exact": n_exact,
            "normalized": n_norm, "delisted": n_delisted, "unmatched": len(unmatched)}


# ── TIME_SERIES_DAILY_ADJUSTED client (premium endpoint) ─────────────────

class _RateLimiter:
    """Sliding-window limiter: at most `rpm` calls per rolling 60s, plus jitter.
    Premium AV is 150 RPM; we run ~145 to leave headroom."""

    def __init__(self, rpm: int):
        self.rpm = max(1, int(rpm))
        self._calls: collections.deque[float] = collections.deque()

    def wait(self) -> None:
        now = time.monotonic()
        while self._calls and now - self._calls[0] >= 60.0:
            self._calls.popleft()
        if len(self._calls) >= self.rpm:
            sleep_for = 60.0 - (now - self._calls[0]) + random.uniform(0.05, 0.30)
            if sleep_for > 0:
                time.sleep(sleep_for)
            now = time.monotonic()
            while self._calls and now - self._calls[0] >= 60.0:
                self._calls.popleft()
        self._calls.append(time.monotonic())


def _backoff_seconds(attempt: int) -> float:
    return min(60.0, 2.0 ** attempt) + random.uniform(0.1, 0.6)


def _http_get_text(params: dict, timeout: int = 60) -> tuple[int | None, str | None, str | None]:
    """GET AV_BASE. Returns (status_code, text, network_error). Network errors are
    redacted (the URL carries the apikey) and returned, not raised, so the caller
    can apply backoff uniformly."""
    if requests is None:
        raise RuntimeError("adapters.alpha_vantage requires 'requests' (pip install requests).")
    try:
        r = requests.get(AV_BASE, params=params, timeout=timeout)
        return r.status_code, r.text, None
    except Exception as e:  # noqa: BLE001 — network layer; redact before returning
        return None, None, _redact(str(e))


def _classify_daily_payload(text: str):
    """Map a DAILY_ADJUSTED response body to (kind, detail, series).
    kind in {ok, empty, error, ratelimit, premium, truncated, unknown}."""
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return ("truncated", text[:200], None)
    if not isinstance(obj, dict):
        return ("unknown", str(obj)[:200], None)
    ts = obj.get("Time Series (Daily)")
    if isinstance(ts, dict):
        return ("ok", None, ts) if ts else ("empty", "empty Time Series (Daily)", None)
    if "Error Message" in obj:                       # invalid symbol / params
        return ("error", str(obj["Error Message"]), None)
    note = obj.get("Note") or obj.get("Information")
    if note is not None:
        low = str(note).lower()
        if "premium" in low or "demo" in low:        # key not entitled -> fatal
            return ("premium", str(note), None)
        if "rate" in low or "frequency" in low or "thank you" in low or "calls per" in low:
            return ("ratelimit", str(note), None)
        return ("error", str(note), None)
    return ("empty", "no Time Series (Daily) key", None)


def _standardize_daily(df: pd.DataFrame) -> pd.DataFrame:
    """Coerce to the canonical dtypes so every per-ticker parquet shares one schema."""
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    for c in _DAILY_FLOAT_COLS:
        df[c] = pd.to_numeric(df.get(c), errors="coerce").astype("float64")
    df["volume"] = pd.to_numeric(df.get("volume"), errors="coerce").astype("Int64")
    return df[DAILY_COLUMNS]


def _parse_daily_series(ts: dict) -> pd.DataFrame:
    """AV 'Time Series (Daily)' dict -> tidy ascending-by-date DataFrame."""
    rows = []
    for d, fields in ts.items():
        row = {"date": d}
        for av_k, col in _DAILY_FIELD_MAP.items():
            row[col] = fields.get(av_k)
        rows.append(row)
    df = _standardize_daily(pd.DataFrame(rows, columns=["date"] + list(_DAILY_FIELD_MAP.values())))
    return df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)


def fetch_daily_adjusted(av_symbol: str, *, key: str, limiter: _RateLimiter | None = None,
                         outputsize: str = "full"):
    """Pull one symbol's adjusted history. Returns (df_or_None, status, detail) with
    status in {ok, empty, error}. Retries 429/5xx/network/ratelimit/truncated with
    backoff; raises on a premium/entitlement note (the whole run can't proceed)."""
    params = {"function": "TIME_SERIES_DAILY_ADJUSTED", "symbol": av_symbol,
              "outputsize": outputsize, "datatype": "json", "apikey": key}
    last = None
    for attempt in range(MAX_RETRIES):
        if limiter is not None:
            limiter.wait()
        status_code, text, neterr = _http_get_text(params)
        if neterr is not None:
            last = f"network: {neterr}"; time.sleep(_backoff_seconds(attempt)); continue
        if status_code == 429 or (status_code is not None and status_code >= 500):
            last = f"HTTP {status_code}"; time.sleep(_backoff_seconds(attempt)); continue
        if status_code != 200:
            return None, "error", f"HTTP {status_code}"
        kind, detail, ts = _classify_daily_payload(text)
        if kind == "ok":
            return _parse_daily_series(ts), "ok", None
        if kind == "empty":
            return None, "empty", detail
        if kind == "premium":
            raise RuntimeError(
                "AV rejected TIME_SERIES_DAILY_ADJUSTED as premium/not-entitled "
                f"(key lacks access): {_redact(detail)}"
            )
        if kind in ("ratelimit", "truncated"):
            last = f"{kind}: {_redact(str(detail))[:120]}"
            time.sleep(_backoff_seconds(attempt)); continue
        return None, "error", _redact(str(detail))[:200]   # error / unknown
    return None, "error", f"exhausted {MAX_RETRIES} retries: {last}"


# ── Resumable per-ticker pull + manifest ─────────────────────────────────

_MANIFEST_COLS = ["symbol", "rows", "first_date", "last_date", "status", "detail", "fetched_at"]


def _atomic_write_parquet(df: pd.DataFrame, path: Path) -> None:
    """Write to a temp file then os.replace, so a crash mid-write never leaves a
    half-written parquet that the resume logic would mistake for complete."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    df.to_parquet(tmp)
    os.replace(tmp, path)


def _load_manifest(path: Path) -> dict[str, dict]:
    if not Path(path).exists():
        return {}
    m = pd.read_csv(path, dtype={"symbol": str}, keep_default_na=False)
    return {str(r["symbol"]): dict(r) for _, r in m.iterrows()}


def _flush_manifest(manifest: dict[str, dict], path: Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(list(manifest.values()), columns=_MANIFEST_COLS)
    df.sort_values("symbol").to_csv(path, index=False)


def _manifest_entry_from_disk(sym: str, path: Path) -> dict:
    """Rebuild a manifest row from an existing per-ticker parquet (self-healing
    resume if the manifest CSV was lost)."""
    try:
        d = pd.read_parquet(path, columns=["date"])
        rows = len(d)
        fd = str(pd.to_datetime(d["date"]).min().date()) if rows else None
        ld = str(pd.to_datetime(d["date"]).max().date()) if rows else None
        return {"symbol": sym, "rows": rows, "first_date": fd, "last_date": ld,
                "status": "ok" if rows else "empty", "detail": "resumed-from-disk",
                "fetched_at": ""}
    except Exception as e:  # noqa: BLE001
        return {"symbol": sym, "rows": 0, "first_date": None, "last_date": None,
                "status": "error", "detail": f"unreadable: {_redact(str(e))[:80]}",
                "fetched_at": ""}


def pull_daily_adjusted(symbols: list[str], *, allow_demo: bool = False, rpm: int = AV_RPM,
                        outputsize: str = "full", out_dir: Path | str = AV_DAILY_DIR,
                        manifest_path: Path | str = AV_MANIFEST_CSV,
                        limit: int | None = None) -> dict:
    """Pull adjusted daily history for each symbol, one call apiece. Resumable:
    skips symbols already on disk; writes the manifest as it goes. The price
    endpoint needs a real key (demo/free rejected), so allow_demo defaults False."""
    key, _src = _resolve_api_key(allow_demo=allow_demo)
    out_dir, manifest_path = Path(out_dir), Path(manifest_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = _load_manifest(manifest_path)
    limiter = _RateLimiter(rpm)

    todo = symbols if limit is None else symbols[:limit]
    n = len(todo)
    counts = {"ok": 0, "empty": 0, "error": 0, "skip": 0}
    print(f"daily pull: {n:,} symbols (rpm<={rpm}, outputsize={outputsize}) -> {out_dir}")
    try:
        for i, sym in enumerate(todo):
            pq_path = out_dir / f"{sym}.parquet"
            if pq_path.exists():                       # resume: trust on-disk data
                if sym not in manifest:
                    manifest[sym] = _manifest_entry_from_disk(sym, pq_path)
                counts["skip"] += 1
                continue
            df, status, detail = fetch_daily_adjusted(sym, key=key, limiter=limiter,
                                                       outputsize=outputsize)
            if status == "ok" and df is not None and len(df):
                _atomic_write_parquet(df, pq_path)
                counts["ok"] += 1
                entry = {"symbol": sym, "rows": len(df),
                         "first_date": str(df["date"].min().date()),
                         "last_date": str(df["date"].max().date()),
                         "status": "ok", "detail": ""}
            else:
                counts[status if status in counts else "error"] += 1
                entry = {"symbol": sym, "rows": 0, "first_date": None, "last_date": None,
                         "status": status, "detail": (detail or "")[:200]}
            entry["fetched_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            manifest[sym] = entry
            if (i + 1) % 25 == 0:
                _flush_manifest(manifest, manifest_path)
                print(f"  [{i+1}/{n}] ok={counts['ok']} empty={counts['empty']} "
                      f"error={counts['error']} skip={counts['skip']}")
    finally:
        _flush_manifest(manifest, manifest_path)
    print(f"daily pull done: ok={counts['ok']} empty={counts['empty']} "
          f"error={counts['error']} skipped={counts['skip']} -> manifest {manifest_path}")
    return {"manifest_path": str(manifest_path), **counts, "n": n}


# ── Consolidate + quality probe ──────────────────────────────────────────

def consolidate_daily_adjusted(in_dir: Path | str = AV_DAILY_DIR,
                               out: Path | str = AV_PANEL_PARQUET) -> dict:
    """Stream every per-ticker parquet into one long panel (+symbol col), one
    ticker at a time so the full panel is never held in memory at once."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    files = sorted(Path(in_dir).glob("*.parquet"))
    if not files:
        raise RuntimeError(f"No per-ticker parquet in {in_dir} — run the pull first.")
    Path(out).parent.mkdir(parents=True, exist_ok=True)

    writer = None
    schema = None
    total = nsym = 0
    min_d = max_d = None
    for f in files:
        df = _standardize_daily(pd.read_parquet(f))
        if df.empty:
            continue
        df.insert(0, "symbol", f.stem)
        table = pa.Table.from_pandas(df[["symbol"] + DAILY_COLUMNS], preserve_index=False)
        if writer is None:
            schema = table.schema
            writer = pq.ParquetWriter(str(out), schema)
        elif table.schema != schema:
            table = table.cast(schema)
        writer.write_table(table)
        total += len(df); nsym += 1
        mn, mx = df["date"].min(), df["date"].max()
        min_d = mn if min_d is None else min(min_d, mn)
        max_d = mx if max_d is None else max(max_d, mx)
    if writer is not None:
        writer.close()
    span = (f"{min_d.date()}..{max_d.date()}" if min_d is not None else "n/a")
    print(f"panel: {nsym:,} symbols, {total:,} rows, {span} -> {out}")
    return {"symbols": nsym, "rows": total, "first_date": str(min_d) if min_d is not None else None,
            "last_date": str(max_d) if max_d is not None else None, "out": str(out)}


def quality_probe(in_dir: Path | str = AV_DAILY_DIR,
                  out: Path | str = AV_QUALITY_CSV) -> dict:
    """Per-symbol artifact counts: exact-zero adjusted_close, exact-zero raw close,
    and |1-day adjusted return| > 100% (residual split/data artifacts). Streams the
    per-ticker files. Tells us how much split/div adjustment cleans up the panel."""
    files = sorted(Path(in_dir).glob("*.parquet"))
    if not files:
        raise RuntimeError(f"No per-ticker parquet in {in_dir} — run the pull first.")

    rows = []
    tot = {"rows": 0, "zero_adj": 0, "zero_raw": 0, "big_move": 0}
    for f in files:
        df = _standardize_daily(pd.read_parquet(f)).sort_values("date")
        if df.empty:
            continue
        adj = df["adjusted_close"]
        n_zero_adj = int((adj == 0).sum())
        n_zero_raw = int((df["close"] == 0).sum())
        prev = adj.shift(1)
        # Only meaningful where the prior close is positive (else ret is inf/NaN).
        ret = (adj - prev) / prev.where(prev > 0)
        n_big = int((ret.abs() > 1.0).sum())
        rows.append({"symbol": f.stem, "rows": len(df), "zero_adj_close": n_zero_adj,
                     "zero_raw_close": n_zero_raw, "big_moves_gt100pct": n_big})
        tot["rows"] += len(df); tot["zero_adj"] += n_zero_adj
        tot["zero_raw"] += n_zero_raw; tot["big_move"] += n_big

    Path(out).parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).sort_values("symbol").to_csv(out, index=False)
    print(f"quality probe: {len(rows):,} symbols, {tot['rows']:,} rows | "
          f"zero adjusted_close={tot['zero_adj']:,}, zero raw close={tot['zero_raw']:,}, "
          f"|1d adj return|>100%={tot['big_move']:,} -> {out}")
    print("  (compare zero-raw-close vs the ORATS panel's known 505 zero-closes; "
          "adjustment should leave far fewer residual artifacts.)")
    return {"symbols": len(rows), **tot, "out": str(out)}


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


def _cmd_symbol_map(args) -> int:
    resolve_symbols(panel=Path(args.panel), universe=Path(args.universe))
    return 0


def _cmd_daily_pull(args) -> int:
    if not Path(args.symbol_map).exists():
        raise RuntimeError(f"{args.symbol_map} not found — run `symbol-map` first.")
    smap = pd.read_csv(args.symbol_map, dtype=str, keep_default_na=False)
    symbols = smap["av_symbol"].dropna().astype(str).str.strip()
    symbols = sorted({s for s in symbols if s})
    pull_daily_adjusted(symbols, allow_demo=args.allow_demo, rpm=args.rpm,
                        outputsize=args.outputsize, limit=args.limit)
    return 0


def _cmd_consolidate(args) -> int:
    consolidate_daily_adjusted(in_dir=Path(args.in_dir), out=Path(args.out))
    return 0


def _cmd_quality_probe(args) -> int:
    quality_probe(in_dir=Path(args.in_dir), out=Path(args.out))
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

    psm = sub.add_parser("symbol-map", help="Resolve ORATS panel tickers -> AV symbols")
    psm.add_argument("--panel", default=str(ORATS_PANEL))
    psm.add_argument("--universe", default=str(AV_UNIVERSE_PARQUET))

    pdp = sub.add_parser("daily-pull", help="Pull TIME_SERIES_DAILY_ADJUSTED (needs real key)")
    pdp.add_argument("--symbol-map", default=str(AV_SYMBOL_MAP_CSV))
    pdp.add_argument("--rpm", type=int, default=AV_RPM)
    pdp.add_argument("--outputsize", choices=["full", "compact"], default="full")
    pdp.add_argument("--limit", type=int, default=None, help="Only pull the first N symbols.")
    pdp.add_argument("--allow-demo", action="store_true",
                     help="(Testing) allow the demo key — AV rejects it for this endpoint.")

    pcon = sub.add_parser("consolidate", help="Per-ticker parquet -> one long panel")
    pcon.add_argument("--in-dir", default=str(AV_DAILY_DIR))
    pcon.add_argument("--out", default=str(AV_PANEL_PARQUET))

    pqp = sub.add_parser("quality-probe", help="Zero-close / >100%-move artifact counts")
    pqp.add_argument("--in-dir", default=str(AV_DAILY_DIR))
    pqp.add_argument("--out", default=str(AV_QUALITY_CSV))

    pf = sub.add_parser("fetch", help="(stub) per-thesis bars/options/earnings")
    pf.add_argument("--thesis_id", required=True)
    pf.add_argument("--tickers", required=True)
    pf.add_argument("--start", required=True)
    pf.add_argument("--end", required=True)
    pf.add_argument("--datatype", choices=["bars", "options", "earnings"], default="bars")

    args = parser.parse_args(argv)
    dispatch = {
        "universe": _cmd_universe, "compare": _cmd_compare, "pull": _cmd_pull,
        "symbol-map": _cmd_symbol_map, "daily-pull": _cmd_daily_pull,
        "consolidate": _cmd_consolidate, "quality-probe": _cmd_quality_probe,
        "fetch": _cmd_fetch,
    }
    handler = dispatch.get(args.cmd)
    return handler(args) if handler else 1


if __name__ == "__main__":
    sys.exit(main())
