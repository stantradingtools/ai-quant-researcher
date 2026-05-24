"""quant_validator.signal_vs_random: the signal-GENERATION-vs-random test.

The verdict Phase 1 exists for. Question: does *generating* a consensus signal
on (ticker, date, direction) identify points with better forward P&L than a
random (ticker, date) drawn from the same universe on the same day, holding the
direction fixed?

Design (locked with the user):
  1. Fixed horizons: 5 / 10 / 21 trading-day forward returns (apples-to-apples
     vs random; the tool's variable direction-change hold is a separate ref).
  2. Date-matched random baseline: for each real signal on date D, the random
     comparison draws a random eligible ticker ON DATE D — controls for market
     regime / beta so the signal isn't merely credited for trading on big-move days.
  3. Direction-matched signed P&L: the random draw inherits the signal's fade
     side (BULL->short->-r, BEAR->long->+r), so we test ENTRY SELECTION, not a
     directional bet.

Reports per horizon: signal mean signed return, bootstrap distribution of the
date/direction-matched random mean, one-sided empirical p-value, z effect size,
hit rates, and signal Sharpe.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .consensus_signal import ConsensusOpts, compute_consensus, signal_sign

HORIZONS = (5, 10, 21)


def _pit_pctl_vec(x, lookback: int = 252, min_obs: int = 126):
    """Strictly-trailing mid-rank percentile (same methodology as putP/callP): rank x[i]
    within the lookback values STRICTLY before i. Used by pit_ivp to rebuild ivP from
    local atmIV with zero look-ahead, instead of trusting ORATS ivPct1y."""
    from numpy.lib.stride_tricks import sliding_window_view
    x = np.asarray(x, float); n = len(x); out = np.full(n, np.nan)
    if n == 0: return out
    pad = np.concatenate([np.full(lookback, np.nan), x])
    W = sliding_window_view(pad, lookback)[:n]; cur = x[:, None]
    finite = np.isfinite(W); cnt = finite.sum(1); Wm = np.where(finite, W, np.nan)
    with np.errstate(invalid="ignore"):
        lt = np.nansum(Wm < cur, axis=1); eq = np.nansum(Wm == cur, axis=1)
    valid = (cnt >= min_obs) & np.isfinite(x)
    with np.errstate(invalid="ignore", divide="ignore"):
        pct = (lt + eq / 2.0) / cnt * 100.0
    out[valid] = pct[valid]; return out


def _per_ticker_signal_and_fwd(uni: pd.DataFrame, horizons=HORIZONS,
                               opts: ConsensusOpts = ConsensusOpts(), pit_ivp: bool = False) -> pd.DataFrame:
    """Annotate the universe with consensus side + forward returns per ticker.
    `uni` must have columns: ticker, tradeDate, clsPx + the 6 signal columns."""
    if pit_ivp and "atmIV" not in uni.columns:
        raise ValueError("pit_ivp=True needs an 'atmIV' column in the panel")
    out = []
    for tk, g in uni.sort_values(["ticker", "tradeDate"]).groupby("ticker", sort=False):
        if pit_ivp:
            g = g.copy(); g["ivP"] = _pit_pctl_vec(g["atmIV"].to_numpy(float))
        g = compute_consensus(g, opts)
        px = g["clsPx"].astype(float).to_numpy()
        px = np.where(px > 0, px, np.nan)   # guard non-positive prices: fwd -> NaN, not inf
        for h in horizons:
            fwd = np.full(len(px), np.nan)
            if len(px) > h:
                fwd[:-h] = px[h:] / px[:-h] - 1.0
            g[f"fwd{h}"] = fwd
        out.append(g)
    return pd.concat(out, ignore_index=True)


def run_test(uni: pd.DataFrame, horizons=HORIZONS, opts: ConsensusOpts = ConsensusOpts(),
             n_boot: int = 2000, seed: int = 0, start_date: str = None,
             price_floor: float = 1.0, max_abs_fwd: float = 5.0,
             pit_ivp: bool = False, match_high_iv: bool = False,
             iv_match_threshold: float = 75.0) -> dict:
    """Run the date/direction-matched signal-vs-random test. Returns a dict
    keyed by horizon with the verdict statistics.

    start_date (e.g. '2012-01-01'): score only fires on/after this date. Consensus
    and forward returns are still computed over the FULL panel (so rolling lookback
    is preserved at the boundary); only the SCORED signals are restricted. Use it to
    exclude the 2011 warmup year, whose percentiles the port can't match the tool on.
    """
    rng = np.random.default_rng(seed)
    ann = _per_ticker_signal_and_fwd(uni, horizons, opts, pit_ivp=pit_ivp)
    fires = ann[ann["side"].notna()].copy()
    fires["sign"] = fires["side"].map(signal_sign)
    n_raw = int(len(fires))
    if start_date is not None:
        fires = fires[fires["tradeDate"] >= pd.Timestamp(start_date)].copy()

    results = {"n_signals_raw": n_raw,
               "n_signals_scored": int(len(fires)),
               "start_date": start_date,
               "scored_date_range": ([str(pd.Timestamp(fires["tradeDate"].min()).date()),
                                      str(pd.Timestamp(fires["tradeDate"].max()).date())]
                                     if len(fires) else None),
               "variant": {"pit_ivp": bool(pit_ivp), "match_high_iv": bool(match_high_iv),
                           "iv_match_threshold": iv_match_threshold if match_high_iv else None,
                           "price_floor": price_floor, "max_abs_fwd": max_abs_fwd},
               "horizons": {}}
    if fires.empty:
        return results

    for h in horizons:
        col = f"fwd{h}"
        # eligible pool: finite fwd, entry px >= floor, |fwd| <= cap. isfinite alone
        # lets FINITE extremes from sub-$floor prices ($0.01->$5 = +49900%) poison the
        # random mean; the price floor + return cap remove them. Counts are reported.
        fwd_all = ann[col].to_numpy(float); px_all = ann["clsPx"].to_numpy(float)
        finite = np.isfinite(fwd_all)
        elig = finite & (px_all >= price_floor) & (np.abs(fwd_all) <= max_abs_fwd)
        if match_high_iv:
            elig = elig & (ann["ivP"].to_numpy(float) >= iv_match_threshold)
        n_drop_price = int((finite & (px_all < price_floor)).sum())
        n_drop_ret = int((finite & (px_all >= price_floor) & (np.abs(fwd_all) > max_abs_fwd)).sum())
        pool = ann[elig][["tradeDate", "ticker", col]]
        pool_by_date = {d: grp[col].to_numpy() for d, grp in pool.groupby("tradeDate", sort=False)}
        pool_median_by_date = {d: float(np.median(v)) for d, v in pool_by_date.items()}
        fcol = fires[col].to_numpy(float); fpx = fires["clsPx"].to_numpy(float)
        f = fires[np.isfinite(fcol) & (fpx >= price_floor) & (np.abs(fcol) <= max_abs_fwd)].copy()
        if f.empty:
            results["horizons"][h] = {"n": 0}
            continue
        sgn = f["sign"].to_numpy(float)
        sig_pnl = sgn * f[col].to_numpy(float)          # signed signal P&L
        signal_mean = float(np.mean(sig_pnl))
        signal_median = float(np.median(sig_pnl))
        dates = f["tradeDate"].to_numpy()
        pmed = np.array([pool_median_by_date.get(d, np.nan) for d in dates]); okm = np.isfinite(pmed)
        beat_pool_median_rate = float(np.mean((sgn[okm]*f[col].to_numpy(float)[okm]) > (sgn[okm]*pmed[okm]))) if okm.any() else float("nan")

        # Date-grouped VECTORIZED bootstrap (replaces a per-fire Python double-loop
        # that was O(n_boot * n_fires) Python-level rng calls -> ~90 min). For each
        # scored date, draw a (k_fires x n_boot) matrix of random picks from that
        # date's eligible pool, apply each fire's sign, sum across fires, accumulate
        # across dates. Same date/direction-matched quantity, numpy-fast (seconds).
        order = np.argsort(dates, kind="stable")
        sd = dates[order]
        ss = sgn[order].astype(float)
        uniq, starts = np.unique(sd, return_index=True)
        bounds = np.append(starts, len(sd))
        boot_sums = np.zeros(n_boot, dtype=float)
        n_used = 0
        for di in range(len(uniq)):
            P = pool_by_date.get(uniq[di])
            if P is None or len(P) == 0:
                continue
            s_blk = ss[bounds[di]:bounds[di + 1]]            # (k,)
            R = rng.integers(len(P), size=(len(s_blk), n_boot))  # (k, n_boot)
            boot_sums += (s_blk[:, None] * P[R]).sum(axis=0)     # (n_boot,)
            n_used += len(s_blk)
        if n_used == 0:
            results["horizons"][h] = {"n": int(len(f)), "note": "no date-matched pool"}
            continue
        boot_means = boot_sums / n_used

        rand_mean = float(boot_means.mean())
        rand_std = float(boot_means.std(ddof=1))
        # one-sided: P(random >= signal) — small => signal beats random
        p_value = float((np.sum(boot_means >= signal_mean) + 1) / (n_boot + 1))
        z = float((signal_mean - rand_mean) / rand_std) if rand_std > 0 else np.nan

        results["horizons"][h] = {
            "n": int(len(f)),
            "signal_mean": signal_mean,
            "random_mean": rand_mean,
            "random_std": rand_std,
            "p_value": p_value,
            "z": z,
            "signal_hit_rate": float(np.mean(sig_pnl > 0)),
            "signal_sharpe": float(signal_mean / np.std(sig_pnl)) if np.std(sig_pnl) > 0 else np.nan,
            "signal_median": signal_median,
            "beat_pool_median_rate": beat_pool_median_rate,
            "pool_dropped_below_price_floor": n_drop_price,
            "pool_dropped_extreme_return": n_drop_ret,
        }
    return results


def validate_against_csv(uni: pd.DataFrame, csv_path: str,
                         opts: ConsensusOpts = ConsensusOpts(),
                         warmup: int = 252) -> dict:
    """Warmup-aware parity check of the ported gate vs the tool's ACCEPTED trades.

    Splits trades into COLD (the ticker had < `warmup` panel days before the
    entry — percentiles can't match the tool, which had earlier history; a
    data-window artifact, NOT a port-fidelity question) and WARM (>= warmup days
    — the real fidelity test). For each WARM miss it records the port vs CSV
    percentiles, the port's stage flags (which gate diverged), and a `near_corner`
    tag (a deciding percentile within 0.6 of the 25/75 threshold => benign
    tie-noise, not a logic bug).
    """
    csv = pd.read_csv(csv_path)
    csv = csv[csv["Status"] == "ACCEPTED"].copy()

    def _parse(s):
        d = {}
        for kv in str(s).split(";"):
            if "=" in kv:
                k, v = kv.split("=", 1)
                d[k.strip()] = v.strip()
        return d

    feat = csv["ExtraInputs"].apply(_parse).apply(pd.Series)
    csv = pd.concat([csv.reset_index(drop=True), feat.reset_index(drop=True)], axis=1)
    csv["Entry"] = pd.to_datetime(csv["Entry"])
    for c in ("put", "call", "iv", "rr"):
        if c in csv.columns:
            csv[c] = pd.to_numeric(csv[c], errors="coerce")

    # Only the tickers that actually appear in the accepted trades need the
    # consensus computed (~hundreds, not all ~8,500) — makes this run in seconds
    # with minimal memory instead of grinding the whole panel.
    keep = set(csv["Ticker"].astype(str).unique())
    uni_f = uni[uni["ticker"].astype(str).isin(keep)]
    if uni_f.empty:
        return {"error": "none of the CSV tickers are present in the universe panel",
                "accepted_trades": int(len(csv))}
    ann = pd.concat(
        [compute_consensus(g, opts) for _, g in
         uni_f.sort_values(["ticker", "tradeDate"]).groupby("ticker", sort=False)],
        ignore_index=True).set_index(["ticker", "tradeDate"])
    panel_dates = {tk: np.sort(sub.index.get_level_values("tradeDate").values)
                   for tk, sub in ann.groupby(level="ticker")}

    def _near_corner(r) -> bool:
        for k in ("putP", "callP", "ivP", "rrP"):
            v = r.get(k)
            if pd.notna(v) and (abs(v - 25) < 0.6 or abs(v - 75) < 0.6):
                return True
        return False

    def _blank():
        return dict(matched=0, fired=0, side_match=0, stall_match=0, div_match=0, nonfire=0)
    warm, cold = _blank(), _blank()
    warm_misses = []
    missing = 0

    for _, t in csv.iterrows():
        tk = t["Ticker"]
        entry = pd.Timestamp(t["Entry"])
        key = (tk, entry)
        if key not in ann.index:
            missing += 1
            continue
        r = ann.loc[key]
        if isinstance(r, pd.DataFrame):
            r = r.iloc[0]
        dts = panel_dates.get(tk)
        prior = int(np.searchsorted(dts, np.datetime64(entry), side="left")) if dts is not None else 0
        b = warm if prior >= warmup else cold
        is_warm = b is warm
        b["matched"] += 1

        if pd.isna(r["side"]):
            b["nonfire"] += 1
            if is_warm:
                warm_misses.append({
                    "key": str(key), "reason": "NON_FIRE", "csv_m1": t.get("m1"),
                    "port": {k: (round(float(r[k]), 1) if pd.notna(r[k]) else None)
                             for k in ("putP", "callP", "ivP", "rrP")},
                    "csv": {"put": t.get("put"), "call": t.get("call"),
                            "iv": t.get("iv"), "rr": t.get("rr")},
                    "port_stages": {s: bool(r[s]) for s in
                                    ("m1_recent_bull", "m1_recent_bear", "m2_recent_bull",
                                     "m2_recent_bear", "m3_bull", "m3_bear")},
                    "near_corner": _near_corner(r)})
            continue

        b["fired"] += 1
        # The trade's actual consensus side is the top-level `Side` column (BEAR/BULL);
        # `m1` is blank for trades that fired via M2 or a recent-window corner, so
        # comparing against m1 spuriously flags them. Fall back to m1 only if Side absent.
        csv_side = t.get("Side")
        if csv_side not in ("BULL", "BEAR"):
            csv_side = t.get("m1")
        side_ok = csv_side in ("BULL", "BEAR") and r["side"] == csv_side
        b["side_match"] += int(side_ok)
        sd = "bull" if r["side"] == "BULL" else "bear"
        b["stall_match"] += int((str(t.get("stall", "")).upper() == "Y") == bool(r[f"m3_stall_{sd}"]))
        b["div_match"] += int((str(t.get("divergence", "")).upper() == "Y") == bool(r[f"m3_div_{sd}"]))
        if is_warm and not side_ok:
            warm_misses.append({
                "key": str(key), "reason": "SIDE", "port_side": r["side"], "csv_side": csv_side,
                "port": {k: (round(float(r[k]), 1) if pd.notna(r[k]) else None)
                         for k in ("putP", "callP", "ivP", "rrP")},
                "csv": {"put": t.get("put"), "call": t.get("call"),
                        "iv": t.get("iv"), "rr": t.get("rr")},
                "near_corner": _near_corner(r)})

    return {
        "accepted_trades": int(len(csv)),
        "matched_in_universe": warm["matched"] + cold["matched"],
        "missing_from_universe": missing,
        "warm": warm,
        "cold": cold,
        "warm_misses_near_corner": sum(1 for m in warm_misses if m["near_corner"]),
        "warm_misses_material": sum(1 for m in warm_misses if not m["near_corner"]),
        "warm_miss_sample": warm_misses[:25],
    }


def summarize(results: dict) -> str:
    lines = [f"Signal-vs-random: {results.get('n_signals_raw', 0)} raw consensus signals"]
    for h, r in results.get("horizons", {}).items():
        if r.get("n", 0) == 0:
            lines.append(f"  {h}d: no usable signals")
            continue
        lines.append(
            f"  {h}d: n={r['n']}  signal={r['signal_mean']*100:+.2f}%  "
            f"random={r['random_mean']*100:+.2f}%  z={r['z']:+.2f}  "
            f"p={r['p_value']:.4f}  hit={r['signal_hit_rate']*100:.1f}%  "
            f"sharpe={r['signal_sharpe']:.3f}")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys, json
    _pos = [a for a in sys.argv[1:] if not a.startswith("--")]
    _flags = {a for a in sys.argv[1:] if a.startswith("--")}
    if not _pos:
        print("usage: python -m quant_validator.signal_vs_random <panel.parquet> "
              "[start_date] [--pit-ivp] [--match-high-iv] [--iv-threshold=75]")
        sys.exit(1)
    _panel, _start = _pos[0], (_pos[1] if len(_pos) > 1 else None)
    _iv_thr = next((float(f.split("=", 1)[1]) for f in _flags
                    if f.startswith("--iv-threshold=")), 75.0)
    _res = run_test(pd.read_parquet(_panel), start_date=_start,
                    pit_ivp=("--pit-ivp" in _flags),
                    match_high_iv=("--match-high-iv" in _flags),
                    iv_match_threshold=_iv_thr)
    print(json.dumps(_res, indent=2, default=str))
    print()
    print(summarize(_res))
