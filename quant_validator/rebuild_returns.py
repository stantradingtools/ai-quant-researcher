"""quant_validator.rebuild_returns: rebuild the signal panel's forward-return
measurement from clean Alpha Vantage prices, leaving the consensus SIGNAL intact.

The consensus side + M1/M2/M3 flags are produced by the unchanged, parity-verified
``consensus_signal.compute_consensus`` on the ORATS inputs. ONLY the forward
returns are swapped to an AV basis. We never recompute or alter the signal.

Forward-return convention (matched to quant_validator.signal_vs_random):
    fwd_h[i] = price[i+h] / price[i] - 1     # close-to-close, h TRADING-DAY offset
    computed within each symbol's OWN sorted series; NaN for the last h rows
    (window past the series end / delisting -> you couldn't have held); no fill.
    Non-positive prices -> NaN (never inf).

Two AV bases per symbol (built FROM the AV panel):
    split_only_close   (HEADLINE) = AV raw close / product of split_coefficient for
                       all split events STRICTLY AFTER that row's date. Split-adjusted,
                       NOT dividend-adjusted.
    total_return_close (secondary) = AV adjusted_close (split + dividend).

FINDING (validated on AAPL's 2020-08-31 4:1 split; see the report): the ORATS
``clsPx`` is ALREADY split+dividend adjusted (continuous across the split, ~= AV
adjusted_close) — so the ORATS forward returns are NOT split-contaminated. AV's
RAW close is the split-contaminated series; split_only_close corrects it. The real
ORATS artifact is the penny/zero-close tail (the 505 clsPx==0 rows -> huge moves),
which AV prices remove. The headline split_only basis also drops the dividend
adjustment that ORATS clsPx carried — a deliberate basis change, total kept as a check.

Outputs (under data/, gitignored):
    data/av/signal_panel_clean.parquet   ORATS signal cols + av_fwd_*_{split,total}
                                          + raw_close + flags (av_matched, fwd_available_*)
    data/av/rebuild_report.txt           coverage / bias / cleanup narrative
    data/av/rebuild_report.csv           per-fire detail behind the report

CLI:
    python -m quant_validator.rebuild_returns [--limit-tickers N]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from .consensus_signal import ConsensusOpts, compute_consensus, signal_sign

HORIZONS = (5, 10, 21)

ORATS_PANEL = Path("data/orats/universe_signal.parquet")
AV_DIR = Path("data/av")
AV_DAILY_DIR = AV_DIR / "daily_adjusted"
SYMBOL_MAP_CSV = AV_DIR / "symbol_map.csv"
UNIVERSE_PARQUET = AV_DIR / "universe_listing.parquet"
OUT_PANEL = AV_DIR / "signal_panel_clean.parquet"
REPORT_TXT = AV_DIR / "rebuild_report.txt"
REPORT_CSV = AV_DIR / "rebuild_report.csv"

# Columns carried unchanged from the ORATS panel (signal inputs).
_ORATS_CARRY = ["ticker", "tradeDate", "clsPx", "atmIV", "callRaw", "putRaw", "skew",
                "rr", "skewDelta", "putP", "callP", "rrP", "ivP", "ivP_source", "sigma"]
# Columns added by compute_consensus (the signal — carried through untouched).
_CONSENSUS_COLS = ["m1_side", "m2_side", "m1_recent_bull", "m1_recent_bear",
                   "m2_recent_bull", "m2_recent_bear", "m3_stall_bull", "m3_stall_bear",
                   "m3_div_bull", "m3_div_bear", "m3_bull", "m3_bear", "side", "direction"]
_STRING_COLS = ("ticker", "ivP_source", "m1_side", "m2_side", "side", "direction")
_BOOL_COLS = ("m1_recent_bull", "m1_recent_bear", "m2_recent_bull", "m2_recent_bear",
              "m3_stall_bull", "m3_stall_bear", "m3_div_bull", "m3_div_bear",
              "m3_bull", "m3_bear", "av_matched") + tuple(f"fwd_available_{h}" for h in HORIZONS)


# ── price math ────────────────────────────────────────────────────────────

def _fwd(arr: np.ndarray, h: int) -> np.ndarray:
    """fwd[i] = arr[i+h]/arr[i]-1; NaN for last h rows; non-positive prices -> NaN."""
    arr = np.asarray(arr, dtype=float)
    arr = np.where(arr > 0, arr, np.nan)
    out = np.full(len(arr), np.nan)
    if len(arr) > h:
        out[:-h] = arr[h:] / arr[:-h] - 1.0
    return out


def _ret1(arr: np.ndarray) -> np.ndarray:
    """1-day return, non-positive prices -> NaN (for the artifact probe)."""
    arr = np.asarray(arr, dtype=float)
    arr = np.where(arr > 0, arr, np.nan)
    out = np.full(len(arr), np.nan)
    if len(arr) > 1:
        out[1:] = arr[1:] / arr[:-1] - 1.0
    return out


def _split_only_close(close: np.ndarray, split_coef: np.ndarray) -> np.ndarray:
    """Back-adjust raw close for splits ONLY: divide each row by the product of all
    split_coefficient values dated STRICTLY AFTER it. The most recent row is left
    unadjusted; a row before an N:1 split is divided by N."""
    close = np.asarray(close, dtype=float)
    sc = np.asarray(split_coef, dtype=float)
    sc = np.where(np.isfinite(sc) & (sc > 0), sc, 1.0)   # guard bad/missing factors
    rev_cumprod = np.cumprod(sc[::-1])[::-1]             # rev_cumprod[i] = prod(sc[i:])
    suffix_after = rev_cumprod / sc                      # prod(sc[i+1:]) = strictly after i
    return close / suffix_after


def build_clean_av(av: pd.DataFrame) -> pd.DataFrame:
    """From an AV per-ticker frame, build the split-only + total-return closes and
    their forward returns. Returns a date-keyed frame for joining onto signal rows."""
    av = av.sort_values("date").drop_duplicates("date").reset_index(drop=True)
    close = av["close"].to_numpy(float)
    adj = av["adjusted_close"].to_numpy(float)
    split_close = _split_only_close(close, av["split_coefficient"].to_numpy(float))
    out = pd.DataFrame({
        "_d": pd.to_datetime(av["date"]).dt.normalize(),
        "raw_close": close,
        "split_only_close": split_close,
        "total_return_close": adj,
    })
    for h in HORIZONS:
        out[f"av_fwd_{h}_split"] = _fwd(split_close, h)
        out[f"av_fwd_{h}_total"] = _fwd(adj, h)
    return out


# ── lookups ────────────────────────────────────────────────────────────────

def _load_symbol_map(path: Path = SYMBOL_MAP_CSV) -> dict[str, tuple[str, str]]:
    m = pd.read_csv(path, dtype=str, keep_default_na=False)
    return {str(r["orats_ticker"]).strip().upper():
            (str(r["av_symbol"]).strip(), str(r.get("av_status", "")).strip())
            for _, r in m.iterrows()}


def _load_exchange_map(path: Path = UNIVERSE_PARQUET) -> dict[str, str]:
    if not Path(path).exists():
        return {}
    u = pd.read_parquet(path, columns=["symbol", "exchange", "status"])
    u["_su"] = u["symbol"].astype(str).str.strip().str.upper()
    u = u.sort_values("status").drop_duplicates("_su", keep="first")   # active wins
    return dict(zip(u["_su"], u["exchange"].astype(str)))


def _price_tier(px: float) -> str:
    if px is None or not np.isfinite(px):
        return "unknown"
    if px < 1.0:
        return "sub_$1"
    if px < 5.0:
        return "$1-5"
    return ">$5"


# ── main rebuild ─────────────────────────────────────────────────────────

def _standardize_out(df: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    """Force a single schema across per-ticker row groups (object/null-typed columns
    otherwise make the ParquetWriter schema drift between tickers)."""
    df = df.reindex(columns=columns)
    df["tradeDate"] = pd.to_datetime(df["tradeDate"])
    for c in columns:
        if c in _STRING_COLS:
            df[c] = df[c].astype("string")
        elif c in _BOOL_COLS:
            df[c] = df[c].fillna(False).astype(bool)
        elif c != "tradeDate":
            df[c] = pd.to_numeric(df[c], errors="coerce").astype("float64")
    return df


def rebuild(panel: Path = ORATS_PANEL, av_dir: Path = AV_DAILY_DIR,
            symbol_map: Path = SYMBOL_MAP_CSV, universe: Path = UNIVERSE_PARQUET,
            out_panel: Path = OUT_PANEL, report_txt: Path = REPORT_TXT,
            report_csv: Path = REPORT_CSV, opts: ConsensusOpts = ConsensusOpts(),
            limit_tickers: int | None = None) -> dict:
    import pyarrow as pa
    import pyarrow.parquet as pq

    smap = _load_symbol_map(symbol_map)
    exch = _load_exchange_map(universe)
    av_dir = Path(av_dir)

    out_cols = (["ticker", "tradeDate"]
                + [c for c in _ORATS_CARRY if c not in ("ticker", "tradeDate")]
                + _CONSENSUS_COLS
                + ["raw_close", "split_only_close", "total_return_close"]
                + [f"av_fwd_{h}_split" for h in HORIZONS]
                + [f"av_fwd_{h}_total" for h in HORIZONS]
                + ["av_matched"] + [f"fwd_available_{h}" for h in HORIZONS])

    pf = pq.ParquetFile(str(panel))
    Path(out_panel).parent.mkdir(parents=True, exist_ok=True)
    writer = None
    schema = None

    fires: list[dict] = []
    agg = {"rows": 0, "orats_zero": 0, "orats_big1d": 0,
           "av_rows": 0, "av_split_zero": 0, "av_split_big1d": 0, "av_total_big1d": 0,
           "n_tickers": 0, "n_matched_tickers": 0}

    n_rg = pf.num_row_groups
    for i in range(n_rg):
        df_rg = pf.read_row_group(i).to_pandas()
        for tk, g in df_rg.groupby("ticker", sort=False):
            if limit_tickers is not None and agg["n_tickers"] >= limit_tickers:
                break
            agg["n_tickers"] += 1
            g = g.sort_values("tradeDate").reset_index(drop=True)
            g = compute_consensus(g, opts)   # SIGNAL — unchanged, parity-verified

            cls = g["clsPx"].to_numpy(float)
            orats_fwd = {h: _fwd(cls, h) for h in HORIZONS}
            agg["rows"] += len(g)
            agg["orats_zero"] += int((cls == 0).sum())
            agg["orats_big1d"] += int(np.nansum(np.abs(_ret1(cls)) > 1.0))

            tk_u = str(tk).strip().upper()
            av_symbol, av_status = smap.get(tk_u, (None, ""))
            matched = False
            if av_symbol:
                p = av_dir / f"{av_symbol}.parquet"
                if p.exists():
                    av = pd.read_parquet(p, columns=["date", "close", "adjusted_close",
                                                     "split_coefficient"])
                    if len(av):
                        clean = build_clean_av(av)
                        matched = True
                        agg["n_matched_tickers"] += 1
                        agg["av_rows"] += len(clean)
                        agg["av_split_zero"] += int((clean["split_only_close"] == 0).sum())
                        agg["av_split_big1d"] += int(np.nansum(
                            np.abs(_ret1(clean["split_only_close"].to_numpy())) > 1.0))
                        agg["av_total_big1d"] += int(np.nansum(
                            np.abs(_ret1(clean["total_return_close"].to_numpy())) > 1.0))
                        g["_d"] = g["tradeDate"].dt.normalize()
                        g = g.merge(clean, on="_d", how="left").drop(columns="_d")

            if not matched:
                for c in (["raw_close", "split_only_close", "total_return_close"]
                          + [f"av_fwd_{h}_split" for h in HORIZONS]
                          + [f"av_fwd_{h}_total" for h in HORIZONS]):
                    g[c] = np.nan
            g["av_matched"] = matched
            for h in HORIZONS:
                g[f"fwd_available_{h}"] = g[f"av_fwd_{h}_split"].notna() if matched else False

            # Per-fire diagnostics (rows where the consensus fired).
            fire_mask = g["side"].notna().to_numpy()
            if fire_mask.any():
                gf = g[fire_mask]
                rc = gf["raw_close"].to_numpy(float)
                clsf = gf["clsPx"].to_numpy(float)
                px_tier = np.where(np.isfinite(rc), rc, clsf)   # AV price; fall back to ORATS
                idx = np.flatnonzero(fire_mask)
                for j, (_, fr) in enumerate(gf.iterrows()):
                    pos = idx[j]
                    rec = {"ticker": tk_u, "av_symbol": av_symbol or "",
                           "date": str(pd.Timestamp(fr["tradeDate"]).date()),
                           "side": fr["side"], "sign": signal_sign(fr["side"]),
                           "av_matched": matched, "av_status": av_status or ("" if matched else "unmatched"),
                           "exchange": exch.get(av_symbol.upper(), "") if av_symbol else "",
                           "raw_close": fr["raw_close"], "clsPx": fr["clsPx"],
                           "price_tier": _price_tier(px_tier[j])}
                    for h in HORIZONS:
                        rec[f"av_fwd_{h}_split"] = fr[f"av_fwd_{h}_split"]
                        rec[f"av_fwd_{h}_total"] = fr[f"av_fwd_{h}_total"]
                        rec[f"orats_fwd_{h}"] = orats_fwd[h][pos]
                        rec[f"fwd_available_{h}"] = bool(fr[f"fwd_available_{h}"])
                    fires.append(rec)

            out_df = _standardize_out(g, out_cols)
            table = pa.Table.from_pandas(out_df, preserve_index=False)
            if writer is None:
                schema = table.schema
                writer = pq.ParquetWriter(str(out_panel), schema)
            elif table.schema != schema:
                table = table.cast(schema)
            writer.write_table(table)
        if limit_tickers is not None and agg["n_tickers"] >= limit_tickers:
            break
    if writer is not None:
        writer.close()

    diag = _diagnostics(fires, agg)
    fires_df = pd.DataFrame(fires)
    Path(report_csv).parent.mkdir(parents=True, exist_ok=True)
    fires_df.to_csv(report_csv, index=False)
    _write_report(report_txt, diag, agg, out_panel)
    print(f"clean panel: {agg['rows']:,} rows, {agg['n_tickers']:,} tickers "
          f"({agg['n_matched_tickers']:,} AV-matched) -> {out_panel}")
    print(f"fires: {diag['n_fires']:,} | with AV split return (any horizon): "
          f"{diag['n_fires_with_av']:,} ({diag['pct_with_av']:.1f}%) | dropped: {diag['n_dropped']:,}")
    print(f"report -> {report_txt} (+ per-fire {report_csv})")
    return diag


def _signed_stats(vals: np.ndarray) -> dict:
    v = vals[np.isfinite(vals)]
    return {"n": int(v.size), "mean": float(np.mean(v)) if v.size else float("nan"),
            "std": float(np.std(v, ddof=1)) if v.size > 1 else float("nan")}


def _diagnostics(fires: list[dict], agg: dict) -> dict:
    f = pd.DataFrame(fires)
    n = len(f)
    out = {"n_fires": n, "agg": agg}
    if n == 0:
        out.update({"n_fires_with_av": 0, "pct_with_av": 0.0, "n_dropped": 0})
        return out

    avail_any = f[[f"fwd_available_{h}" for h in HORIZONS]].any(axis=1)
    out["n_fires_with_av"] = int(avail_any.sum())
    out["pct_with_av"] = float(100.0 * avail_any.mean())
    out["n_dropped"] = int((~avail_any).sum())

    def _reason(r):
        if not r["av_matched"] and not r["av_symbol"]:
            return "unmatched"
        if not r["av_matched"]:
            return "no_av_data"
        if not np.isfinite(r["raw_close"]):
            return "date_missing_in_av"
        if not r[f"fwd_available_{HORIZONS[0]}"]:
            return "window_past_series_end"
        return "kept"
    f["_reason"] = f.apply(_reason, axis=1)
    out["drop_reasons"] = f.loc[~avail_any, "_reason"].value_counts().to_dict()

    # Kept-vs-dropped bias by status / exchange / price tier.
    f["_kept"] = avail_any.values
    out["bias"] = {}
    for dim in ("av_status", "price_tier", "exchange"):
        tab = f.groupby([dim, "_kept"]).size().unstack(fill_value=0)
        tab.columns = ["dropped" if c is False else "kept" for c in tab.columns]
        for col in ("kept", "dropped"):
            if col not in tab:
                tab[col] = 0
        tab["pct_kept"] = (100.0 * tab["kept"] / (tab["kept"] + tab["dropped"]).replace(0, np.nan))
        out["bias"][dim] = tab.sort_values("kept", ascending=False).head(15)

    # Per-horizon signed return distribution: ORATS vs AV split vs AV total.
    out["dist"] = {}
    sgn = f["sign"].to_numpy(float)
    for h in HORIZONS:
        out["dist"][h] = {
            "orats": _signed_stats(sgn * f[f"orats_fwd_{h}"].to_numpy(float)),
            "av_split": _signed_stats(sgn * f[f"av_fwd_{h}_split"].to_numpy(float)),
            "av_total": _signed_stats(sgn * f[f"av_fwd_{h}_total"].to_numpy(float)),
        }
    return out


def _write_report(path: Path, diag: dict, agg: dict, out_panel: Path) -> None:
    L = []
    L.append("=" * 78)
    L.append("SIGNAL-PANEL FORWARD-RETURN REBUILD (clean AV prices; signal unchanged)")
    L.append("=" * 78)
    L.append("")
    L.append("FINDING: ORATS clsPx is already split+dividend adjusted (validated on")
    L.append("AAPL 2020-08-31 4:1 — clsPx is continuous, ~= AV adjusted_close). So the")
    L.append("ORATS forward returns are NOT split-contaminated; the real artifact is the")
    L.append("penny/zero-close tail. AV raw close IS split-contaminated and is corrected")
    L.append("by split_only_close. Headline = split_only (drops the dividend adjustment")
    L.append("ORATS carried); total_return kept as the apples-to-apples check vs ORATS.")
    L.append("")
    L.append(f"Clean panel: {agg['rows']:,} rows, {agg['n_tickers']:,} tickers "
             f"({agg['n_matched_tickers']:,} AV-matched) -> {out_panel}")
    L.append("")
    L.append("-- COVERAGE (over consensus fires) " + "-" * 44)
    L.append(f"  fires total                  : {diag['n_fires']:,}")
    L.append(f"  with AV split return (any h) : {diag['n_fires_with_av']:,} "
             f"({diag['pct_with_av']:.1f}%)")
    L.append(f"  dropped                      : {diag['n_dropped']:,}")
    if diag.get("drop_reasons"):
        for k, v in diag["drop_reasons"].items():
            L.append(f"      {k:<26}: {v:,}")
    L.append("")
    if diag.get("bias"):
        L.append("-- KEPT vs DROPPED bias " + "-" * 54)
        for dim, tab in diag["bias"].items():
            L.append(f"  by {dim}:")
            L.append("    " + tab.to_string().replace("\n", "\n    "))
            L.append("")
    if diag.get("dist"):
        L.append("-- CLEANUP EVIDENCE: signed fwd-return mean/std per horizon " + "-" * 18)
        L.append(f"  {'h':>3} | {'ORATS mean/std':>22} | {'AV split mean/std':>22} | "
                 f"{'AV total mean/std':>22}")
        for h in HORIZONS:
            d = diag["dist"][h]
            def fmt(s):
                return f"{s['mean']*100:+.3f}% / {s['std']*100:.2f}% (n={s['n']})"
            L.append(f"  {h:>3} | {fmt(d['orats']):>22} | {fmt(d['av_split']):>22} | "
                     f"{fmt(d['av_total']):>22}")
        L.append("")
    L.append("-- PRICE-SERIES ARTIFACTS (panel-wide) " + "-" * 38)
    L.append(f"  ORATS clsPx     : zero-closes={agg['orats_zero']:,}  "
             f"|1d move|>100%={agg['orats_big1d']:,}  (rows={agg['rows']:,})")
    L.append(f"  AV split_only   : zero-closes={agg['av_split_zero']:,}  "
             f"|1d move|>100%={agg['av_split_big1d']:,}  (rows={agg['av_rows']:,})")
    L.append(f"  AV total_return : |1d move|>100%={agg['av_total_big1d']:,}")
    L.append("  (ORATS ~505 zero-closes is the known artifact; AV should show ~0.)")
    L.append("")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text("\n".join(L), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="quant_validator.rebuild_returns")
    ap.add_argument("--panel", default=str(ORATS_PANEL))
    ap.add_argument("--av-dir", default=str(AV_DAILY_DIR))
    ap.add_argument("--symbol-map", default=str(SYMBOL_MAP_CSV))
    ap.add_argument("--universe", default=str(UNIVERSE_PARQUET))
    ap.add_argument("--out", default=str(OUT_PANEL))
    ap.add_argument("--limit-tickers", type=int, default=None,
                    help="Process only the first N tickers (smoke test).")
    args = ap.parse_args(argv)
    rebuild(panel=Path(args.panel), av_dir=Path(args.av_dir), symbol_map=Path(args.symbol_map),
            universe=Path(args.universe), out_panel=Path(args.out),
            limit_tickers=args.limit_tickers)
    return 0


if __name__ == "__main__":
    sys.exit(main())
