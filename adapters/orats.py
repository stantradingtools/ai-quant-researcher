"""adapters.orats: ORATS Data API v2 client (Phase 1).

Mirrors the EXACT data sources the Skew_backtest_orats tool uses so the
recomputed signal is identical to the backtest.

Historical backtest path (the tool's PATCH-25, ORATS-only):
    /hist/cores     -> skew/IV signal inputs (iv30d, dlt25Iv30d, dlt75Iv30d, ...)
    /hist/dailies   -> price bars (sigma momentum, forward returns, hv)
    /hist/earnings  -> earnings dates (blackout filter)

Signal reconstruction (verified against the tool; ORATS /hist/cores returns
IV in PERCENTAGE POINTS, e.g. 22.9, so NO blanket *100 — see _vp normalizer):
    atmIV   = iv30d
    c25IV   = dlt25Iv30d                       # call-side 25-delta IV
    p25IV   = dlt75Iv30d                       # put-side 25-delta (call .75 = put -.25)
    callRaw = c25IV - atmIV                     # already vol points
    putRaw  = p25IV - atmIV
    skew    = putRaw - callRaw                  # >0 = put-side richer = bearish
    rr      = callRaw - putRaw                  # risk reversal
    putP/callP/rrP = 252d mid-rank rolling percentile (0-100)
    ivP     = ORATS ivPct1y (/hist/ivrank), local 252d pctl fallback
    skewDelta = skew[t] - skew[t-5]
    sigma   = (ac[t]-ac[t-5]) / (ac[t]*hv20*sqrt(5/252))

Auth: ORATS_API_TOKEN (or ORATS_API_KEY) in .env.
Base: https://api.orats.io/datav2  (JSON default; append .csv for CSV)

CLI:
    python -m adapters.orats fetch --tickers AAPL,MSFT --start 2011-01-01 --end 2026-05-01 --datatype cores
    python -m adapters.orats signal --ticker AAPL --start 2011-01-01 --end 2026-05-01
    python -m adapters.orats validate --ticker AAPL --date 2024-01-16
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

try:
    import requests
except ImportError:
    requests = None

ORATS_KEY = os.environ.get("ORATS_API_TOKEN") or os.environ.get("ORATS_API_KEY")
ORATS_BASE = "https://api.orats.io/datav2"
CACHE_DIR = Path(os.environ.get("ORATS_CACHE_DIR", "data/orats_cache"))

# Conservative client-side pacing; backoff handles bursts. 100k/month budget.
MIN_INTERVAL_S = float(os.environ.get("ORATS_MIN_INTERVAL_S", "0.12"))
MAX_RETRIES = 4

# Field sets matching the tool's historical sources
CORES_FIELDS = "ticker,tradeDate,iv30d,dlt25Iv30d,dlt75Iv30d,rSlp30,rDrv30,contango,rVol30"
DAILIES_FIELDS = "ticker,tradeDate,clsPx,hiPx,loPx,open,stockVolume"

_last_call = [0.0]


def _require_key():
    if not ORATS_KEY:
        raise NotImplementedError(
            "adapters.orats: ORATS_API_TOKEN not set in environment/.env. "
            "Add it (rotate first if it was ever shared) before fetching."
        )
    if requests is None:
        raise RuntimeError("adapters.orats requires the 'requests' package (pip install requests).")


def _throttle():
    dt = time.time() - _last_call[0]
    if dt < MIN_INTERVAL_S:
        time.sleep(MIN_INTERVAL_S - dt)
    _last_call[0] = time.time()


def _cache_path(endpoint: str, key: str) -> Path:
    safe = endpoint.replace("/", "_")
    d = CACHE_DIR / safe
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{key}.parquet"


def _redact(text) -> str:
    """Strip the API token from any string before it reaches a log or exception.
    Removes both the literal token value and any `token=...` query parameter.
    """
    if text is None:
        return text
    s = str(text)
    if ORATS_KEY:
        s = s.replace(ORATS_KEY, "***")
    return re.sub(r"(token=)[^&\s]+", r"\1***", s)


def _safe_to_parquet(df: pd.DataFrame, path) -> None:
    """Write to parquet, surviving ORATS out-of-range values.

    Occasionally ORATS returns a garbage integer (e.g. a 'contango' value beyond
    int64/uint64) that pyarrow cannot serialize, raising OverflowError. On
    failure we coerce the mostly-numeric columns to float64 (errors -> NaN) and
    null clearly-garbage magnitudes (|x| > 1e15, safe for legit fields including
    volume), then retry. Genuine non-numeric columns (<50% coercible) are kept.
    """
    try:
        df.to_parquet(path)
        return
    except Exception:
        safe = df.copy()
        for c in safe.columns:
            if c in ("ticker", "tradeDate"):
                continue
            co = pd.to_numeric(safe[c], errors="coerce")
            if co.notna().mean() >= 0.5:
                safe[c] = co.mask(co.abs() > 1e15).astype("float64")
        safe.to_parquet(path)


def _get(endpoint: str, params: dict, cache_key: str | None = None,
         use_cache: bool = True) -> pd.DataFrame:
    """Core GET against /datav2/<endpoint> with caching + backoff. Returns the
    'data' array as a DataFrame (empty frame if no data).

    SECURITY: never calls raise_for_status() (its message embeds the full URL
    incl. token) and never puts the URL/token into any error string. 404 is
    treated as 'no data' (e.g. market holiday) -> empty frame, no retry.
    """
    if cache_key and use_cache:
        cp = _cache_path(endpoint, cache_key)
        if cp.exists():
            return pd.read_parquet(cp)

    _require_key()
    url = f"{ORATS_BASE}/{endpoint}"
    p = dict(params)
    p["token"] = ORATS_KEY

    def _cache_empty() -> pd.DataFrame:
        df = pd.DataFrame()
        if cache_key:
            _cache_path(endpoint, cache_key).parent.mkdir(parents=True, exist_ok=True)
            df.to_parquet(_cache_path(endpoint, cache_key))
        return df

    last_err = None
    for attempt in range(MAX_RETRIES):
        _throttle()
        try:
            r = requests.get(url, params=p, timeout=60)
        except Exception as e:  # network-level; redact before storing
            last_err = _redact(e)
            time.sleep(2 ** attempt)
            continue

        if r.status_code == 404:
            return _cache_empty()  # no data for this ticker/date (holiday) — no retry
        if r.status_code == 429 or r.status_code >= 500:
            last_err = f"HTTP {r.status_code}"  # transient — retry (no URL/token)
            time.sleep(2 ** attempt)
            continue
        if r.status_code >= 400:
            # other client error — do not retry, raise WITHOUT the URL/token
            raise RuntimeError(f"ORATS {endpoint}: HTTP {r.status_code} (request rejected)")

        try:
            payload = r.json()
        except Exception as e:
            last_err = _redact(e)
            time.sleep(2 ** attempt)
            continue
        data = payload.get("data", []) if isinstance(payload, dict) else []
        df = pd.DataFrame(data)
        if cache_key:
            _cache_path(endpoint, cache_key).parent.mkdir(parents=True, exist_ok=True)
            _safe_to_parquet(df, _cache_path(endpoint, cache_key))
        return df
    raise RuntimeError(f"ORATS {endpoint} failed after {MAX_RETRIES} retries: {last_err}")


# ── Historical endpoints (the tool's backtest sources) ──────────────────

def hist_cores(ticker: str | None = None, trade_date: str | None = None,
               fields: str = CORES_FIELDS, use_cache: bool = True) -> pd.DataFrame:
    """/hist/cores — EOD core metrics (skew/IV signal inputs).

    Provide ticker (full history for that name) OR trade_date (all covered
    tickers that day). The universe-wide backfill iterates by trade_date.
    """
    params = {"fields": fields}
    key_parts = []
    if ticker:
        params["ticker"] = ticker
        key_parts.append(ticker)
    if trade_date:
        params["tradeDate"] = trade_date
        key_parts.append(trade_date)
    if not key_parts:
        raise ValueError("hist_cores needs ticker or trade_date")
    return _get("hist/cores", params, cache_key="_".join(key_parts), use_cache=use_cache)


def hist_dailies(ticker: str | None = None, trade_date: str | None = None,
                 fields: str = DAILIES_FIELDS, use_cache: bool = True) -> pd.DataFrame:
    """/hist/dailies — stock OHLCV bars."""
    params = {"fields": fields}
    key_parts = []
    if ticker:
        params["ticker"] = ticker
        key_parts.append(ticker)
    if trade_date:
        params["tradeDate"] = trade_date
        key_parts.append(trade_date)
    if not key_parts:
        raise ValueError("hist_dailies needs ticker or trade_date")
    return _get("hist/dailies", params, cache_key="_".join(key_parts), use_cache=use_cache)


def hist_earnings(ticker: str, use_cache: bool = True) -> pd.DataFrame:
    """/hist/earnings — earnings dates for the blackout filter."""
    return _get("hist/earnings", {"ticker": ticker}, cache_key=ticker, use_cache=use_cache)


IVRANK_FIELDS = "ticker,tradeDate,iv,ivRank1y,ivPct1y"


def hist_ivrank(ticker: str | None = None, trade_date: str | None = None,
                fields: str = IVRANK_FIELDS, use_cache: bool = True) -> pd.DataFrame:
    """/hist/ivrank — ORATS server-side IV rank/percentile history.

    The tool's CSV 'iv' column is this endpoint's `ivPct1y` (1-year IV
    percentile, 0-100). Supports by-ticker (full history) or by-date (all
    tickers) like the other hist endpoints.
    """
    params = {"fields": fields}
    key_parts = []
    if ticker:
        params["ticker"] = ticker
        key_parts.append(ticker)
    if trade_date:
        params["tradeDate"] = trade_date
        key_parts.append(trade_date)
    if not key_parts:
        raise ValueError("hist_ivrank needs ticker or trade_date")
    return _get("hist/ivrank", params, cache_key="_".join(key_parts), use_cache=use_cache)


def hist_strikes(ticker: str, trade_date: str, fields: str | None = None,
                 use_cache: bool = True) -> pd.DataFrame:
    """/hist/strikes — per-strike EOD chain (for options-leg pricing / custom skew)."""
    params = {"ticker": ticker, "tradeDate": trade_date}
    if fields:
        params["fields"] = fields
    return _get("hist/strikes", params, cache_key=f"{ticker}_{trade_date}", use_cache=use_cache)


# ── Percentile engine ───────────────────────────────────────────────────

def roll_pct_vec(vals, lb: int):
    """Vectorized rolling mid-rank percentile, PROVEN bit-identical to the
    per-bar loop in build_signal_frame (200-trial equivalence test incl. ties
    and NaN gaps). For bar i, window = the `lb` values strictly before i; drop
    nulls; require >= lb/2; pct = (#<v + #==v/2)/n*100, mid-rank, 0-100, 1dp.
    Used by build_universe_signal for the full ~5,800-name cross-section.
    """
    from numpy.lib.stride_tricks import sliding_window_view
    vals = np.asarray(vals, float)
    n = len(vals)
    out = np.full(n, np.nan)
    if n <= lb:
        return out
    W = sliding_window_view(vals, lb)[:-1]      # W[k] = vals[k:k+lb] (the lb values before index k+lb)
    cur = vals[lb:]
    valid_cur = ~np.isnan(cur)
    cnt = (~np.isnan(W)).sum(axis=1)            # non-null per window
    lt = (W < cur[:, None]).sum(axis=1)         # NaN<x -> False (excluded)
    eq = (W == cur[:, None]).sum(axis=1)
    ok = valid_cur & (cnt >= (lb // 2))
    with np.errstate(invalid="ignore", divide="ignore"):
        pct = (lt + eq / 2) / cnt * 100.0
    out[lb:] = np.where(ok, np.round(pct, 1), np.nan)
    return out


# ── Signal reconstruction (matches the tool exactly) ────────────────────

def build_signal_frame(ticker: str, lookback: int = 252) -> pd.DataFrame:
    """Pull /hist/cores + /hist/dailies for a ticker and compute the consensus
    signal inputs exactly as the backtest tool does."""
    cores = hist_cores(ticker=ticker)
    dailies = hist_dailies(ticker=ticker)
    if cores.empty or dailies.empty:
        return pd.DataFrame()

    for df in (cores, dailies):
        df["tradeDate"] = pd.to_datetime(df["tradeDate"])
    m = cores.merge(dailies, on=["ticker", "tradeDate"], how="inner").sort_values("tradeDate")
    m = m.set_index("tradeDate")

    # ORATS /hist/cores returns IV in PERCENTAGE POINTS (e.g. 22.9 = 22.9%),
    # confirmed by parity check 2024-01-16. Some endpoints/older data may return
    # decimals (0.229). Normalize to vol points: if |x|<=3 treat as decimal -> *100.
    def _vp(s):
        return s.where(s.abs() > 3.0, s * 100.0)

    atm = _vp(m["iv30d"].astype(float))
    c25 = _vp(m["dlt25Iv30d"].astype(float))   # call-side
    p25 = _vp(m["dlt75Iv30d"].astype(float))   # put-side
    m["atmIV"] = atm
    m["callRaw"] = c25 - atm                    # already vol points; no *100
    m["putRaw"] = p25 - atm
    m["skew"] = m["putRaw"] - m["callRaw"]
    m["rr"] = m["callRaw"] - m["putRaw"]
    m["skewDelta"] = m["skew"] - m["skew"].shift(5)

    # Rolling percentile matching the tool's rollingPercentile() EXACTLY:
    #   for bar i, window = the `lookback` values STRICTLY BEFORE i (not incl. i),
    #   drop nulls, require >= lookback/2 remaining,
    #   pct = (#(<v) + #(==v)/2) / n * 100   (mid-rank tie handling, 0-100, 1 dp).
    # Pure-Python loop is fine per-ticker; vectorize for universe-scale backfill.
    def roll_pct(series, lb):
        vals = series.to_numpy(dtype=float)
        out = np.full(len(vals), np.nan)
        for i in range(lb, len(vals)):
            v = vals[i]
            if np.isnan(v):
                continue
            w = vals[i - lb:i]
            w = w[~np.isnan(w)]
            if len(w) < lb // 2:
                continue
            lt = int(np.sum(w < v))
            eq = int(np.sum(w == v))
            out[i] = round((lt + eq / 2) / len(w) * 100, 1)
        return out

    m["putP"] = roll_pct(m["putRaw"], lookback)    # CSV 'put'  (putSkewPctl)
    m["callP"] = roll_pct(m["callRaw"], lookback)   # CSV 'call' (callSkewPctl)
    m["rrP"] = roll_pct(m["rr"], lookback)          # CSV 'rr'

    # CSV 'iv' = ORATS server-side ivPct1y from /hist/ivrank (PATCH-24 behavior),
    # preferred over a local rolling pctl of iv30d. Fall back to local only where
    # ivPct1y is unavailable for a date. (m is indexed by tradeDate.)
    ivP_local = pd.Series(roll_pct(atm, lookback), index=m.index)
    m["ivP"] = ivP_local
    try:
        ivr = hist_ivrank(ticker)
        if not ivr.empty and "ivPct1y" in ivr.columns:
            ivr["tradeDate"] = pd.to_datetime(ivr["tradeDate"])
            ivmap = pd.to_numeric(
                ivr.set_index("tradeDate")["ivPct1y"], errors="coerce")
            orats_iv = pd.to_numeric(pd.Series(m.index.map(ivmap), index=m.index),
                                     errors="coerce")
            m["ivP"] = orats_iv.where(orats_iv.notna(), ivP_local)
            m["ivP_source"] = np.where(orats_iv.notna(), "orats_ivPct1y", "local_pctl")
    except Exception:
        m["ivP_source"] = "local_pctl"

    # sigma: 5-day move normalized by 20d historical vol scaled to 5d horizon
    ac = m["clsPx"].astype(float)
    logret = np.log(ac / ac.shift(1))
    hv20 = logret.rolling(20).std() * np.sqrt(252)
    m["hv20"] = hv20
    m["sigma"] = (ac - ac.shift(5)) / (ac * hv20 * np.sqrt(5 / 252))

    return m.reset_index()


def build_universe_signal(cache_dir: Path | None = None, lookback: int = 252,
                          out: str = "data/orats/universe_signal.parquet",
                          self_check: bool = True) -> pd.DataFrame:
    """Assemble the validated consensus signal across the FULL cached universe.

    Reads the per-date backfill cache (hist_cores/hist_dailies/hist_ivrank),
    pivots to per-ticker series, and computes the exact signal features using
    the vectorized percentile (proven identical to the validated loop). Output
    is one parquet keyed by (ticker, tradeDate) with the same columns the
    consensus engine fires on. This is the input to the signal-vs-random test.
    """
    cache_dir = Path(cache_dir) if cache_dir else CACHE_DIR

    def _load(name: str, cols: list[str]) -> pd.DataFrame:
        d = cache_dir / name
        files = sorted(d.glob("*.parquet"))
        if not files:
            raise FileNotFoundError(
                f"No cached files in {d} — run `backfill` first (or pass cache_dir).")
        frames = []
        for f in files:
            df = pd.read_parquet(f)
            keep = [c for c in cols if c in df.columns]
            if df.empty or "ticker" not in df.columns:
                continue
            frames.append(df[keep])
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=cols)

    cores = _load("hist_cores", ["ticker", "tradeDate", "iv30d", "dlt25Iv30d", "dlt75Iv30d"])
    dailies = _load("hist_dailies", ["ticker", "tradeDate", "clsPx"])
    ivrank = _load("hist_ivrank", ["ticker", "tradeDate", "ivPct1y"])
    if cores.empty or dailies.empty:
        raise RuntimeError("cores/dailies cache empty — backfill incomplete.")

    for df in (cores, dailies, ivrank):
        if not df.empty:
            df["tradeDate"] = pd.to_datetime(df["tradeDate"])
            df["ticker"] = df["ticker"].astype(str)

    m = cores.merge(dailies, on=["ticker", "tradeDate"], how="inner")
    if not ivrank.empty:
        m = m.merge(ivrank, on=["ticker", "tradeDate"], how="left")
    else:
        m["ivPct1y"] = np.nan
    m = m.sort_values(["ticker", "tradeDate"]).reset_index(drop=True)

    def _vp(s):
        return s.where(s.abs() > 3.0, s * 100.0)

    rows_per_ticker = []
    n_tk = m["ticker"].nunique()
    for j, (tk, g) in enumerate(m.groupby("ticker", sort=False)):
        if len(g) < lookback // 2:
            continue  # too short to ever produce a percentile
        atm = _vp(g["iv30d"].astype(float)).to_numpy()
        c25 = _vp(g["dlt25Iv30d"].astype(float)).to_numpy()
        p25 = _vp(g["dlt75Iv30d"].astype(float)).to_numpy()
        callRaw = c25 - atm
        putRaw = p25 - atm
        rr = callRaw - putRaw
        skew = putRaw - callRaw
        ac = g["clsPx"].astype(float).to_numpy()
        logret = np.diff(np.log(ac), prepend=np.nan)
        hv20 = pd.Series(logret).rolling(20).std().to_numpy() * np.sqrt(252)
        with np.errstate(invalid="ignore", divide="ignore"):
            sigma = (ac - np.concatenate([[np.nan] * 5, ac[:-5]])) / (ac * hv20 * np.sqrt(5 / 252))
        sigma[~np.isfinite(sigma)] = np.nan  # hv20==0 (no vol) -> inf -> NaN
        orats_iv = pd.to_numeric(g["ivPct1y"], errors="coerce").to_numpy() if "ivPct1y" in g else np.full(len(g), np.nan)
        ivP_local = roll_pct_vec(atm, lookback)
        ivP = np.where(~np.isnan(orats_iv), orats_iv, ivP_local)
        rows_per_ticker.append(pd.DataFrame({
            "ticker": tk, "tradeDate": g["tradeDate"].to_numpy(),
            "clsPx": ac, "atmIV": atm, "callRaw": callRaw, "putRaw": putRaw,
            "skew": skew, "rr": rr,
            "skewDelta": skew - np.concatenate([[np.nan] * 5, skew[:-5]]),
            "putP": roll_pct_vec(putRaw, lookback),
            "callP": roll_pct_vec(callRaw, lookback),
            "rrP": roll_pct_vec(rr, lookback),
            "ivP": ivP,
            "ivP_source": np.where(~np.isnan(orats_iv), "orats_ivPct1y", "local_pctl"),
            "sigma": sigma,
        }))
        if (j + 1) % 500 == 0:
            print(f"  signal: {j+1}/{n_tk} tickers")

    sig = pd.concat(rows_per_ticker, ignore_index=True)
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    _safe_to_parquet(sig, out)
    print(f"universe signal: {len(sig):,} (ticker,date) rows, "
          f"{sig['ticker'].nunique():,} tickers -> {out}")

    if self_check:
        chk = sig[(sig["ticker"] == "AAPL") &
                  (sig["tradeDate"] == pd.Timestamp("2015-08-05"))]
        if not chk.empty:
            r = chk.iloc[0]
            print("  [self-check] AAPL 2015-08-05 via universe fast-path "
                  f"(tool: put=98.8 iv=78.97 rr=1.6): "
                  f"putP={r['putP']} ivP={r['ivP']} rrP={r['rrP']} "
                  f"callP={r['callP']} src={r['ivP_source']}")
        else:
            print("  [self-check] AAPL 2015-08-05 not in cache window — skipped.")
    return sig


# ── CLI ─────────────────────────────────────────────────────────────────

def _cmd_fetch(args) -> int:
    out_dir = Path(args.out or "data/orats")
    out_dir.mkdir(parents=True, exist_ok=True)
    tickers = [t.strip() for t in args.tickers.split(",")]
    for sym in tickers:
        if args.datatype == "cores":
            df = hist_cores(ticker=sym)
        elif args.datatype == "dailies":
            df = hist_dailies(ticker=sym)
        elif args.datatype == "earnings":
            df = hist_earnings(sym)
        else:
            raise ValueError(args.datatype)
        df.to_parquet(out_dir / f"orats_{args.datatype}_{sym}.parquet")
        print(f"{sym}: {len(df)} rows -> {out_dir}/orats_{args.datatype}_{sym}.parquet")
    return 0


def _cmd_signal(args) -> int:
    df = build_signal_frame(args.ticker)
    if args.start:
        df = df[df["tradeDate"] >= pd.to_datetime(args.start)]
    if args.end:
        df = df[df["tradeDate"] <= pd.to_datetime(args.end)]
    out = Path(args.out or f"data/orats/signal_{args.ticker}.parquet")
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out)
    print(f"{args.ticker}: {len(df)} rows; columns: {list(df.columns)}")
    cols = ["tradeDate", "skew", "rr", "putP", "callP", "ivP", "rrP", "sigma"]
    print("Signal-series tail (putP/callP/ivP/rrP are 0-100 pctl, match CSV put/call/iv/rr):")
    print(df[cols].tail(3).to_string())
    return 0


def _cmd_validate(args) -> int:
    """Pull one ticker-date of cores and print the reconstructed skew/rr so you
    can eyeball it against the tool's known value for that date."""
    df = hist_cores(ticker=args.ticker, trade_date=args.date, use_cache=False)
    if df.empty:
        print(f"No cores data for {args.ticker} on {args.date}")
        return 1
    r = df.iloc[0]
    raw_atm, raw_c25, raw_p25 = float(r["iv30d"]), float(r["dlt25Iv30d"]), float(r["dlt75Iv30d"])
    # Normalize to vol points (percentage). |x|<=3 => decimal, scale up.
    def _vp(x):
        return x if abs(x) > 3.0 else x * 100.0
    atm, c25, p25 = _vp(raw_atm), _vp(raw_c25), _vp(raw_p25)
    callRaw = round(c25 - atm, 2)
    putRaw = round(p25 - atm, 2)
    print(json.dumps({
        "ticker": args.ticker, "date": args.date,
        "raw_iv30d": raw_atm, "raw_dlt25Iv30d": raw_c25, "raw_dlt75Iv30d": raw_p25,
        "atmIV": atm, "callRaw": callRaw, "putRaw": putRaw,
        "skew": round(putRaw - callRaw, 2), "rr": round(callRaw - putRaw, 2),
    }, indent=2))
    return 0


def _cmd_backfill(args) -> int:
    """Per-date, FULL-UNIVERSE backfill (no ticker -> all ~5,805 tickers/day).

    Loops business days [start, end], fetching cores/dailies/ivrank by date.
    Caching is handled in _get(), so re-runs skip already-cached dates (resumable).
    Empty cores => non-trading day, skipped. Prints a first-date per-endpoint
    diagnostic so you can confirm by-date mode works for all three endpoints
    BEFORE committing to the full loop.
    """
    endpoints = [e.strip() for e in args.endpoints.split(",") if e.strip()]
    fetchers = {"cores": hist_cores, "dailies": hist_dailies, "ivrank": hist_ivrank}
    for e in endpoints:
        if e not in fetchers:
            print(f"Unknown endpoint '{e}' (choose from {list(fetchers)})")
            return 1

    dates = pd.bdate_range(pd.Timestamp(args.start), pd.Timestamp(args.end))
    n = len(dates)
    print(f"Backfill {endpoints} over {n} business days "
          f"{dates[0].date()}..{dates[-1].date()} (per-date, full universe)")

    trading_days = 0
    holidays = 0
    tickers_seen: set[str] = set()
    diag_done = False

    for i, d in enumerate(dates):
        ds = d.strftime("%Y-%m-%d")
        counts = {}
        any_rows = False

        # Gate on cores first: if it's empty, it's a non-trading day -> skip the
        # other endpoints for this date (saves calls on ~100 holidays/year).
        ordered = (["cores"] + [e for e in endpoints if e != "cores"]
                   if "cores" in endpoints else endpoints)
        for ep in ordered:
            try:
                df = fetchers[ep](trade_date=ds)
                counts[ep] = len(df)
                if not df.empty:
                    any_rows = True
                    if "ticker" in df.columns:
                        tickers_seen.update(df["ticker"].astype(str).tolist())
            except Exception as ex:  # noqa: BLE001
                counts[ep] = f"ERR:{type(ex).__name__}"
                print(f"  {ds} {ep}: {_redact(ex)}")
            if ep == "cores" and counts.get("cores") == 0:
                break  # non-trading day; don't spend calls on dailies/ivrank

        if any_rows:
            trading_days += 1
        else:
            holidays += 1

        # First non-empty date: print a per-endpoint diagnostic and pause-point.
        if not diag_done and any_rows:
            diag_done = True
            print(f"  [first-date diagnostic] {ds} rows-per-endpoint: {counts}")
            print("  (each should be ~5,000+; if dailies/ivrank are 0 or 1, "
                  "by-date mode isn't supported there -> tell me and I'll switch "
                  "those to per-ticker.)")

        if (i + 1) % 50 == 0 or i == n - 1:
            print(f"  [{i+1}/{n}] {ds}  trading_days={trading_days} "
                  f"non_trading={holidays} unique_tickers={len(tickers_seen)}")

    print(f"DONE: {trading_days} trading days cached, {holidays} non-trading skipped, "
          f"{len(tickers_seen)} unique tickers seen. Cache: {CACHE_DIR}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="adapters.orats")
    sub = parser.add_subparsers(dest="cmd", required=True)

    pf = sub.add_parser("fetch")
    pf.add_argument("--tickers", required=True)
    pf.add_argument("--start"); pf.add_argument("--end")
    pf.add_argument("--datatype", choices=["cores", "dailies", "earnings"], default="cores")
    pf.add_argument("--out")

    ps = sub.add_parser("signal")
    ps.add_argument("--ticker", required=True)
    ps.add_argument("--start"); ps.add_argument("--end"); ps.add_argument("--out")

    pv = sub.add_parser("validate")
    pv.add_argument("--ticker", required=True)
    pv.add_argument("--date", required=True)

    pb = sub.add_parser("backfill")
    pb.add_argument("--start", required=True)
    pb.add_argument("--end", required=True)
    pb.add_argument("--endpoints", default="cores,dailies,ivrank")

    pu = sub.add_parser("build-universe")
    pu.add_argument("--cache-dir", default=None)
    pu.add_argument("--out", default="data/orats/universe_signal.parquet")
    pu.add_argument("--lookback", type=int, default=252)

    args = parser.parse_args(argv)
    if args.cmd == "fetch":
        return _cmd_fetch(args)
    if args.cmd == "signal":
        return _cmd_signal(args)
    if args.cmd == "validate":
        return _cmd_validate(args)
    if args.cmd == "backfill":
        return _cmd_backfill(args)
    if args.cmd == "build-universe":
        build_universe_signal(cache_dir=args.cache_dir, lookback=args.lookback, out=args.out)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
