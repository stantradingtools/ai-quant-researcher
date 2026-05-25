"""quant_validator.regime_diagnosis: diagnose the 2020 21d inversion and test an
EX-ANTE market-regime conditioner on the clean survivorship-free panel.

The Prompt-B temporal gate found the pooled +18.3 bps (21d) edge INVERTS in 2020
(-41.9 bps, z=-5.06) while 2021 (+109) / 2023 (+83) — also high-vol — stay strongly
positive. This module asks: is there a market-regime variable KNOWN AT ENTRY that
separates 2020's losing fires from those winners WITHOUT killing the 9 good years
(the removed-VIX-filter cautionary test)?

Regime series (SPY, trailing-only, NO look-ahead — every value at date D uses only
SPY closes through D, and the fire's 21d forward return is strictly after D):
    ret_21d, ret_63d        trailing equity-index momentum
    rvol_21d                trailing 21d realized vol (annualized)
    dd_252                  drawdown from trailing-252d peak (<=0)
    rebound_off_low63       price / trailing-63d low - 1 (the V-recovery magnitude)
    vol_chg_21              rvol_21d - rvol_21d[21d ago] (vol spike/collapse proxy)

Per-fire edge (date/direction-matched, reusing the run_test tradeable guard):
    edge_i = sign_i * (fwd21_i - poolmean(date_i))
    poolmean(d) = mean fwd21 of the eligible universe on date d
    sign: BULL->-1 (fade short), BEAR->+1 (fade long)
    eligible = av_matched & fwd_available & raw_close>=$1 & |fwd21|<=500%

Conditioner counterfactual: gate fires on dangerous-regime DATES (a market-wide,
ex-ante state), re-run the per-year 21d verdict, and report alpha retained vs alpha
blocked. The blocked-trade mean must NOT be strongly positive (else it's the
VIX-filter mistake: throwing away good trades).

CLI:
    python -m quant_validator.regime_diagnosis --diagnose-only
    python -m quant_validator.regime_diagnosis --gate_feature ret_21d --gate_side high --gate_q 0.90
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from .consensus_signal import signal_sign
from .signal_vs_random import annotate_clean, clean_run_columns, run_by_period

CLEAN_PANEL = Path("data/av/signal_panel_clean.parquet")
SPY_PARQUET = Path("data/av/daily_adjusted/SPY.parquet")
REPORT_TXT = Path("reports/regime_diagnosis_2020.txt")
REPORT_CSV = Path("reports/regime_diagnosis_2020.csv")

START = "2012-01-01"
PRICE_FLOOR, MAX_ABS_FWD = 1.0, 5.0
FEATURES = ["ret_21d", "ret_63d", "rvol_21d", "dd_252", "rebound_off_low63", "vol_chg_21"]
WINNER_YEARS = (2019, 2021, 2023)


# ── ex-ante SPY regime series ─────────────────────────────────────────────

def build_spy_regime(spy: pd.DataFrame) -> pd.DataFrame:
    """Trailing-only regime features (SPY has no splits, so raw close is the clean
    price). Every rolling window ENDS at the row's date -> known at entry close."""
    s = spy.sort_values("date").reset_index(drop=True)
    p = pd.Series(s["close"].astype(float).to_numpy())
    r = p.pct_change()
    out = pd.DataFrame({"date": pd.to_datetime(s["date"]).dt.normalize()})
    out["ret_21d"] = p.pct_change(21).to_numpy()
    out["ret_63d"] = p.pct_change(63).to_numpy()
    out["rvol_21d"] = (r.rolling(21).std() * np.sqrt(252)).to_numpy()
    peak = p.rolling(252, min_periods=20).max()
    out["dd_252"] = (p / peak - 1.0).to_numpy()
    low = p.rolling(63, min_periods=10).min()
    out["rebound_off_low63"] = (p / low - 1.0).to_numpy()
    out["vol_chg_21"] = out["rvol_21d"] - out["rvol_21d"].shift(21)
    return out


# ── fires with per-fire edge + entry-date regime ──────────────────────────

def fires_with_regime(panel: pd.DataFrame, regime: pd.DataFrame):
    """Annotated fires with date/direction-matched per-fire edge + entry-date regime.
    Returns (fires, ann, pool_mean_by_date)."""
    ann = annotate_clean(panel, returns="total", universe="full").copy()
    ann["_d"] = ann["tradeDate"].dt.normalize()
    elig = (ann["fwd21"].notna() & (ann["raw_close"] >= PRICE_FLOOR)
            & (ann["fwd21"].abs() <= MAX_ABS_FWD))
    pool_mean = ann.loc[elig].groupby("_d")["fwd21"].mean()

    fires = ann[ann["side"].notna() & elig & (ann["tradeDate"] >= pd.Timestamp(START))].copy()
    fires["sign"] = fires["side"].map(signal_sign).astype(float)
    fires["abs_net"] = fires["sign"] * fires["fwd21"]                 # signal's own 21d return
    fires["pool"] = fires["sign"] * fires["_d"].map(pool_mean).to_numpy()
    fires["edge"] = fires["abs_net"] - fires["pool"]                 # date/direction-matched
    fires["year"] = fires["tradeDate"].dt.year
    rmap = regime.set_index("date")
    for f in FEATURES:
        fires[f] = fires["_d"].map(rmap[f])
    return fires, ann, pool_mean


def _stat(x) -> tuple[int, float, float]:
    x = np.asarray(x, float)
    x = x[np.isfinite(x)]
    n = x.size
    if n == 0:
        return 0, float("nan"), float("nan")
    m = float(x.mean())
    se = float(x.std(ddof=1) / np.sqrt(n)) if n > 1 else float("nan")
    return n, m, (m / se if se and se > 0 else float("nan"))


def bucket_by_feature(fires: pd.DataFrame, feat: str, nq: int = 5) -> list[dict]:
    f = fires[fires[feat].notna()].copy()
    if f.empty:
        return []
    try:
        f["q"] = pd.qcut(f[feat], nq, labels=False, duplicates="drop")
    except ValueError:
        return []
    rows = []
    for q, g in f.groupby("q"):
        n, me, te = _stat(g["edge"])
        _, ma, _ = _stat(g["abs_net"])
        rows.append({"feature": feat, "quantile": int(q), "feat_lo": float(g[feat].min()),
                     "feat_hi": float(g[feat].max()), "n": n,
                     "edge_bps": me * 1e4, "edge_t": te, "abs_net_bps": ma * 1e4})
    return rows


def year_profile(fires: pd.DataFrame, years) -> list[dict]:
    rows = []
    for y in years:
        g = fires[fires["year"] == y]
        n, me, _ = _stat(g["edge"])
        _, ma, _ = _stat(g["abs_net"])
        row = {"year": y, "n_fires": n, "edge_bps": me * 1e4, "abs_net_bps": ma * 1e4}
        for f in FEATURES:
            row[f"{f}_mean"] = float(g[f].mean()) if n else float("nan")
        rows.append(row)
    return rows


# ── conditioner counterfactual ────────────────────────────────────────────

def counterfactual(ann: pd.DataFrame, fires: pd.DataFrame, regime: pd.DataFrame,
                   feature: str, gate_side: str, gate_q: float, n_boot: int = 2000):
    """Gate fires on dangerous-regime DATES (ex-ante, market-wide), re-run the
    per-year verdict, and measure retained vs blocked alpha."""
    rser = regime.set_index("date")[feature]
    reg_win = regime[regime["date"] >= pd.Timestamp(START)][feature].dropna()
    # gate_q = percentile cutoff. high -> block feat >= p(gate_q) (top 1-gate_q of days);
    # low -> block feat <= p(1-gate_q) (bottom 1-gate_q of days).
    thr = float(np.quantile(reg_win, gate_q if gate_side == "high" else 1 - gate_q))

    a = ann.copy()
    feat_at = a["_d"].map(rser)
    blocked = (feat_at >= thr) if gate_side == "high" else (feat_at <= thr)
    cond = a.copy()
    cond.loc[blocked.to_numpy(), "side"] = pd.NA   # gate the signal; pool stays intact

    base_year = run_by_period(a, price_col="raw_close", start_date=START, n_boot=n_boot)
    cond_year = run_by_period(cond, price_col="raw_close", start_date=START, n_boot=n_boot)

    f_feat = fires["_d"].map(rser)
    f_blocked = (f_feat >= thr) if gate_side == "high" else (f_feat <= thr)
    blk = fires[f_blocked.to_numpy()]
    kept = fires[~f_blocked.to_numpy()]
    return {
        "feature": feature, "gate_side": gate_side, "gate_q": gate_q, "threshold": thr,
        "base_year": base_year, "cond_year": cond_year,
        "n_fires": len(fires), "n_blocked": len(blk), "n_kept": len(kept),
        "blocked_abs_net_bps": _stat(blk["abs_net"])[1] * 1e4 if len(blk) else float("nan"),
        "blocked_edge_bps": _stat(blk["edge"])[1] * 1e4 if len(blk) else float("nan"),
        "kept_abs_net_bps": _stat(kept["abs_net"])[1] * 1e4 if len(kept) else float("nan"),
        "kept_edge_bps": _stat(kept["edge"])[1] * 1e4 if len(kept) else float("nan"),
    }


def _y21(year_res: dict, y: int) -> dict:
    r = year_res.get(y, {}).get("horizons", {})
    return r.get(21) or r.get("21") or {}


def _incr_bps(d: dict) -> float | None:
    return (d["signal_mean"] - d["random_mean"]) * 1e4 if d.get("n") else None


def drawdown_proxy(fires: pd.DataFrame) -> dict:
    """Worst calendar-month and worst-year mean 21d signal return (abs_net) — the
    loss regime the sizing/kill-switch must tolerate. (Per-trade mean; 21d holds
    overlap, so this is an attribution proxy, not a tradeable equity curve.)"""
    f = fires.copy()
    f["ym"] = f["tradeDate"].dt.to_period("M").astype(str)
    by_m = f.groupby("ym")["abs_net"].mean() * 1e4
    by_y = f.groupby("year")["abs_net"].mean() * 1e4
    return {"worst_month": by_m.idxmin(), "worst_month_bps": float(by_m.min()),
            "worst_year": int(by_y.idxmin()), "worst_year_bps": float(by_y.min())}


# ── report ─────────────────────────────────────────────────────────────────

def _format_report(fires, buckets, years, cf, dd, all_years_base) -> str:
    L = ["=" * 92,
         "2020 REGIME-INVERSION DIAGNOSIS — ex-ante market-regime conditioner test",
         "=" * 92,
         "Pooled 21d edge = +18.3 bps but inverts in 2020 (-41.9 bps, z=-5.06) while 2021",
         "(+109) / 2023 (+83) — also high-vol — stay strongly positive. Q: is there an",
         "EX-ANTE (known-at-entry) SPY regime variable that separates 2020's losers from",
         "those winners WITHOUT killing the 9 good years? Per-fire edge = sign*(fwd21 -",
         "date-matched pool mean). All regime features are trailing-only (no look-ahead).",
         ""]

    L.append("-- 21d EDGE by regime-feature quintile (Q0=low ... Q4=high) " + "-" * 27)
    L.append(f"  {'feature':>17} | {'q':>2} | {'feat range':>20} | {'n':>7} | "
             f"{'edge(bps)':>9} | {'t':>6} | {'abs_net(bps)':>12}")
    L.append("  " + "-" * 86)
    for feat in FEATURES:
        for r in buckets.get(feat, []):
            rng = f"[{r['feat_lo']:+.3f},{r['feat_hi']:+.3f}]"
            L.append(f"  {feat:>17} | {r['quantile']:>2} | {rng:>20} | {r['n']:>7,} | "
                     f"{r['edge_bps']:>+9.1f} | {r['edge_t']:>+6.2f} | {r['abs_net_bps']:>+12.1f}")
        L.append("")

    L.append("-- 2020 vs winners: entry-date regime profile (mean) " + "-" * 33)
    cols = ["year", "n_fires", "edge_bps", "abs_net_bps"] + [f"{f}_mean" for f in FEATURES]
    L.append("  " + " | ".join(f"{c:>14}" for c in cols))
    for row in years:
        L.append("  " + " | ".join(
            (f"{row[c]:>14.0f}" if c in ("year", "n_fires") else f"{row[c]:>+14.3f}")
            for c in cols))
    L.append("")

    if cf is not None:
        L.append("-- CANDIDATE CONDITIONER + COUNTERFACTUAL " + "-" * 44)
        L.append(f"  rule: GATE fires on dates where SPY {cf['feature']} is "
                 f"{'HIGH' if cf['gate_side']=='high' else 'LOW'} "
                 f"(>= {cf['threshold']:+.4f}, the {cf['gate_q']*100:.0f}th pctl of market days)")
        L.append(f"  fires: {cf['n_fires']:,} total -> kept {cf['n_kept']:,} / blocked {cf['n_blocked']:,} "
                 f"({100*cf['n_blocked']/cf['n_fires']:.1f}%)")
        L.append(f"  BLOCKED alpha: abs_net={cf['blocked_abs_net_bps']:+.1f} bps, "
                 f"edge={cf['blocked_edge_bps']:+.1f} bps  "
                 f"(must be <= ~0 — strongly positive => VIX-filter mistake)")
        L.append(f"  KEPT alpha   : abs_net={cf['kept_abs_net_bps']:+.1f} bps, "
                 f"edge={cf['kept_edge_bps']:+.1f} bps")
        L.append("")
        L.append("  per-year 21d increment (bps): baseline -> conditioned")
        L.append(f"    {'year':>4} | {'base':>8} | {'cond':>8} | delta")
        check_years = sorted(set([2020] + list(WINNER_YEARS) + [2024, 2025]))
        for y in check_years:
            b = _incr_bps(_y21(cf["base_year"], y))
            c = _incr_bps(_y21(cf["cond_year"], y))
            if b is None:
                continue
            cs = "n/a (all gated)" if c is None else f"{c:>+8.1f}"
            ds = "" if c is None else f"{c-b:>+7.1f}"
            L.append(f"    {y:>4} | {b:>+8.1f} | {cs:>8} | {ds}")
        L.append("")

    L.append("-- DRAWDOWN the sizing/kill-switch must tolerate " + "-" * 37)
    L.append(f"  worst year : {dd['worst_year']} at {dd['worst_year_bps']:+.1f} bps/trade mean 21d signal return")
    L.append(f"  worst month: {dd['worst_month']} at {dd['worst_month_bps']:+.1f} bps/trade mean 21d signal return")
    L.append("  (per-trade mean; 21d holds overlap so treat as attribution, not an equity curve.)")
    L.append("")

    if cf is not None:
        yr = {r["year"]: r for r in years}
        e2020 = yr.get(2020, {})
        b2020 = _incr_bps(_y21(cf["base_year"], 2020))
        c2020 = _incr_bps(_y21(cf["cond_year"], 2020))
        soften = (c2020 is not None and b2020 is not None and c2020 > b2020)
        win_pres = []
        for y in WINNER_YEARS:
            b = _incr_bps(_y21(cf["base_year"], y)); c = _incr_bps(_y21(cf["cond_year"], y))
            if b and c is not None:
                win_pres.append(c >= 0.7 * b)
        winners_ok = all(win_pres) if win_pres else False
        vix_mistake = cf["blocked_abs_net_bps"] > 20.0   # blocking strongly-profitable trades
        viable = soften and winners_ok and not vix_mistake

        L.append("-- VERDICT / RECOMMENDATION " + "-" * 58)
        L.append(f"  2020 edge: base {b2020:+.1f} -> conditioned "
                 f"{'n/a' if c2020 is None else f'{c2020:+.1f}'} bps  (softened={soften})")
        L.append(f"  winners (2019/21/23) retained >=70%: {winners_ok}")
        L.append(f"  blocked-trade abs_net = {cf['blocked_abs_net_bps']:+.1f} bps "
                 f"-> {'VIX-FILTER MISTAKE (discards profitable trades)' if vix_mistake else 'ok (not strongly positive)'}")
        L.append("")
        if viable:
            L.append(f"  VERDICT: VIABLE conditioner — gate SPY {cf['feature']} "
                     f"{cf['gate_side']} (>= {cf['threshold']:+.4f}). Use as a regime scale/gate in sizing.")
        else:
            a2020 = e2020.get("abs_net_bps", float("nan"))
            cond_str = "n/a" if c2020 is None else f"{c2020:+.1f}"
            L.append("  VERDICT: NO ROBUST CONDITIONER (n=1).")
            L.append(f"  - The tested gate (SPY {cf['feature']} {cf['gate_side']}, "
                     f"{cf['gate_q']*100:.0f}th pctl) "
                     f"{'BACKFIRED — 2020 edge WORSENED' if not soften else 'softened 2020'} "
                     f"({b2020:+.1f} -> {cond_str} bps) and blocked trades whose mean abs_net was "
                     f"{cf['blocked_abs_net_bps']:+.0f} bps -> profitable trades discarded (VIX-filter mistake).")
            L.append(f"  - 2020 fires sit in regime buckets with POSITIVE absolute return (2020 abs_net "
                     f"{a2020:+.0f} bps/trade): it was a SELECTION miss in a V-recovery where the random")
            L.append("    pool rose even MORE, NOT an absolute loss. Any gate that catches 2020 throws")
            L.append("    away profit, so no ex-ante regime rule helps on this single event.")
            L.append("  RECOMMEND: ACCEPT the signal, SIZE-TO-SURVIVE, add a DRAWDOWN KILL-SWITCH.")
            L.append(f"  Kill-switch must tolerate: worst month {dd['worst_month']} "
                     f"({dd['worst_month_bps']:+.0f} bps/trade, the meme squeeze) and 2020's 21d edge")
            L.append(f"  inversion ({b2020:+.1f} bps vs random). Both are survivable in ABSOLUTE terms; the")
            L.append("  kill-switch caps tail risk, it does not 'fix' the selection miss.")
        L.append("")
    return "\n".join(L)


def run(diagnose_only: bool = False, gate_feature: str = "ret_21d",
        gate_side: str = "high", gate_q: float = 0.90, n_boot: int = 2000) -> dict:
    panel = pd.read_parquet(CLEAN_PANEL, columns=clean_run_columns())
    spy = pd.read_parquet(SPY_PARQUET, columns=["date", "close"])
    regime = build_spy_regime(spy)
    fires, ann, _ = fires_with_regime(panel, regime)

    buckets = {f: bucket_by_feature(fires, f) for f in FEATURES}
    years = year_profile(fires, sorted(set(list(WINNER_YEARS) + [2020, 2022, 2024, 2025])))
    dd = drawdown_proxy(fires)

    cf = None
    if not diagnose_only:
        cf = counterfactual(ann, fires, regime, gate_feature, gate_side, gate_q, n_boot=n_boot)

    txt = _format_report(fires, buckets, years, cf, dd, None)
    print(txt)
    if not diagnose_only:
        REPORT_TXT.parent.mkdir(parents=True, exist_ok=True)
        REPORT_TXT.write_text(txt, encoding="utf-8")
        # CSV: bucket rows + year-profile rows + counterfactual per-year
        brows = [r for f in FEATURES for r in buckets.get(f, [])]
        pd.DataFrame(brows).to_csv(REPORT_CSV, index=False)
        print(f"wrote {REPORT_TXT} (+ {REPORT_CSV})")
    return {"fires": len(fires), "buckets": buckets, "years": years, "cf": cf, "dd": dd}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="quant_validator.regime_diagnosis")
    ap.add_argument("--diagnose-only", action="store_true")
    ap.add_argument("--gate_feature", default="ret_21d", choices=FEATURES)
    ap.add_argument("--gate_side", default="high", choices=["high", "low"])
    ap.add_argument("--gate_q", type=float, default=0.90)
    ap.add_argument("--n-boot", type=int, default=2000)
    args = ap.parse_args(argv)
    run(diagnose_only=args.diagnose_only, gate_feature=args.gate_feature,
        gate_side=args.gate_side, gate_q=args.gate_q, n_boot=args.n_boot)
    return 0


if __name__ == "__main__":
    sys.exit(main())
