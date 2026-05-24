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

# Representative liquid-name equity round-trip (half-spread each way + impact +
# commission). Horizons whose breakeven (gross signal bps) falls below this are
# flagged as dying under realistic cost. Adjustable; the breakeven itself is the
# basis-independent headline.
REALISTIC_RT_BPS = 20.0


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


def run_test(uni: pd.DataFrame = None, horizons=HORIZONS, opts: ConsensusOpts = ConsensusOpts(),
             n_boot: int = 2000, seed: int = 0, start_date: str = None,
             price_floor: float = 1.0, max_abs_fwd: float = 5.0,
             pit_ivp: bool = False, match_high_iv: bool = False,
             iv_match_threshold: float = 75.0,
             ann: pd.DataFrame = None, price_col: str = "clsPx") -> dict:
    """Run the date/direction-matched signal-vs-random test. Returns a dict
    keyed by horizon with the verdict statistics.

    start_date (e.g. '2012-01-01'): score only fires on/after this date. Consensus
    and forward returns are still computed over the FULL panel (so rolling lookback
    is preserved at the boundary); only the SCORED signals are restricted. Use it to
    exclude the 2011 warmup year, whose percentiles the port can't match the tool on.

    ann: pre-annotated frame (side + fwd{h} + price_col already present) for the
    CLEAN-PANEL path; when None, the ORATS path computes it via compute_consensus +
    clsPx forward returns (UNCHANGED). price_col: the as-traded price for the $1 floor
    ('clsPx' for ORATS; 'raw_close' for the clean panel). The eligible random POOL is
    drawn from `ann` itself, so a survivorship-free signal is matched to a
    survivorship-free pool (and an active-only signal to an active-only pool).
    """
    rng = np.random.default_rng(seed)
    if ann is None:
        if uni is None:
            raise ValueError("run_test needs either `uni` (ORATS path) or `ann` (clean path)")
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
        fwd_all = ann[col].to_numpy(float); px_all = ann[price_col].to_numpy(float)
        finite = np.isfinite(fwd_all)
        elig = finite & (px_all >= price_floor) & (np.abs(fwd_all) <= max_abs_fwd)
        if match_high_iv:
            elig = elig & (ann["ivP"].to_numpy(float) >= iv_match_threshold)
        n_drop_price = int((finite & (px_all < price_floor)).sum())
        n_drop_ret = int((finite & (px_all >= price_floor) & (np.abs(fwd_all) > max_abs_fwd)).sum())
        pool = ann[elig][["tradeDate", "ticker", col]]
        pool_by_date = {d: grp[col].to_numpy() for d, grp in pool.groupby("tradeDate", sort=False)}
        pool_median_by_date = {d: float(np.median(v)) for d, v in pool_by_date.items()}
        fcol = fires[col].to_numpy(float); fpx = fires[price_col].to_numpy(float)
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


def survivor_tickers_from_map(symbol_map_path: str = "data/av/symbol_map.csv") -> set[str]:
    """ORATS tickers whose AV listing is still active (status='active' in symbol_map).
    Used by the survivorship-bias diagnostic (--universe active)."""
    m = pd.read_csv(symbol_map_path, dtype=str, keep_default_na=False)
    act = m[m["av_status"].astype(str).str.strip().str.casefold() == "active"]
    return set(act["orats_ticker"].astype(str).str.strip().str.upper())


def annotate_clean(panel: pd.DataFrame, returns: str = "total", universe: str = "full",
                   survivor_tickers: set[str] | None = None, horizons=HORIZONS) -> pd.DataFrame:
    """Build the run_test `ann` frame from data/av/signal_panel_clean.parquet.

    The consensus `side` is taken AS-IS from the clean panel (materialized earlier
    from the UNCHANGED ORATS signal) — we never recompute it here. Forward returns
    come from av_fwd_{h}_{returns}, kept only where fwd_available_{h}. Rows are
    restricted to av_matched; universe='active' further restricts to survivor tickers
    so the date-matched random pool is drawn from the same (survivors-only) universe.
    """
    if returns not in ("total", "split"):
        raise ValueError("returns must be 'total' or 'split'")
    if universe not in ("full", "active"):
        raise ValueError("universe must be 'full' or 'active'")
    df = panel[panel["av_matched"].astype(bool)].copy()
    if universe == "active":
        if not survivor_tickers:
            raise ValueError("universe='active' needs survivor_tickers")
        df = df[df["ticker"].astype(str).str.upper().isin(survivor_tickers)].copy()
    for h in horizons:
        avail = df[f"fwd_available_{h}"].astype(bool)
        df[f"fwd{h}"] = df[f"av_fwd_{h}_{returns}"].where(avail)
    return df


def clean_run_columns(horizons=HORIZONS) -> list[str]:
    """The subset of signal_panel_clean.parquet columns run_test actually needs
    (side + both fwd bases + raw_close floor + ivP for the confound). Reading only
    these keeps the 1.9 GB panel's memory footprint down for the verdict/by-period."""
    return (["ticker", "tradeDate", "side", "raw_close", "ivP", "av_matched"]
            + [f"fwd_available_{h}" for h in horizons]
            + [f"av_fwd_{h}_total" for h in horizons]
            + [f"av_fwd_{h}_split" for h in horizons])


# ── Temporal-stability gate (per-period verdict) ─────────────────────────

def run_by_period(ann: pd.DataFrame, *, price_col: str = "clsPx", period_freq: str = "year",
                  start_date: str | None = None, end_date: str | None = None,
                  n_boot: int = 2000, seed: int = 0, match_high_iv: bool = False,
                  iv_match_threshold: float = 75.0, horizons=HORIZONS) -> dict:
    """Run the SAME vs-random verdict (run_test) independently per period.

    Because the bootstrap is date/direction-matched, slicing `ann` to one period
    restricts BOTH the signal fires AND the random pool to that period — so each
    bucket reuses the pooled date-grouped bootstrap + tradeable guard verbatim
    (the stats are NOT forked). Returns {period_label: run_test result}.
    """
    if period_freq == "regime":
        raise NotImplementedError(
            "--period_freq regime is not implemented yet (only 'year'). Regime "
            "bucketing (e.g. VIX / market-state windows) is a planned extension.")
    if period_freq != "year":
        raise ValueError(f"unknown period_freq: {period_freq!r}")
    a = ann
    if start_date is not None:
        a = a[a["tradeDate"] >= pd.Timestamp(start_date)]
    if end_date is not None:
        a = a[a["tradeDate"] <= pd.Timestamp(end_date)]
    out: dict[int, dict] = {}
    for y in sorted(pd.to_datetime(a["tradeDate"]).dt.year.unique()):
        ann_y = a[pd.to_datetime(a["tradeDate"]).dt.year == y]
        out[int(y)] = run_test(ann=ann_y, price_col=price_col, n_boot=n_boot, seed=seed,
                               match_high_iv=match_high_iv, iv_match_threshold=iv_match_threshold)
    return out


def _h21(res: dict) -> dict:
    return res.get("horizons", {}).get(21) or res.get("horizons", {}).get("21") or {}


def summarize_by_period(results: dict, horizons=HORIZONS) -> dict:
    """Flatten per-period results and flag instability. A period is FLAGGED if, at
    21d, the increment flips sign (<0), loses significance (p>0.05), or
    beat_pool_median < 50%. Verdict = 'stable' unless any period is flagged."""
    rows, flagged, reasons_by = [], [], {}
    for y in sorted(results):
        r21 = _h21(results[y])
        reasons = []
        if r21.get("n"):
            inc21 = r21["signal_mean"] - r21["random_mean"]
            if inc21 < 0:
                reasons.append("21d increment<0")
            if (r21.get("p_value") or 1.0) > 0.05:
                reasons.append("21d p>0.05")
            if (r21.get("beat_pool_median_rate") or 0.0) < 0.5:
                reasons.append("21d beat<50%")
        else:
            reasons.append("no 21d fires")
        if reasons:
            flagged.append(y)
            reasons_by[y] = ";".join(reasons)
        for h in horizons:
            r = results[y].get("horizons", {}).get(h) or results[y].get("horizons", {}).get(str(h)) or {}
            inc = (r["signal_mean"] - r["random_mean"]) if r.get("n") else None
            rows.append({"year": y, "horizon": h, "n_fires": int(r.get("n", 0)),
                         "increment": inc, "increment_bps": (inc * 1e4) if inc is not None else None,
                         "z": r.get("z"), "p_value": r.get("p_value"),
                         "beat_pool_median": r.get("beat_pool_median_rate"),
                         "year_flagged": y in flagged,
                         "flag_reason": reasons_by.get(y, "")})
    verdict = "stable" if not flagged else f"regime-concentrated — years {flagged}"
    return {"rows": rows, "flagged_years": flagged, "verdict": verdict, "reasons": reasons_by}


def _format_temporal_txt(results: dict, summ: dict, horizons=HORIZONS) -> str:
    L = ["=" * 90,
         "TEMPORAL-STABILITY GATE — per-year vs-random verdict (clean survivorship-free panel)",
         "=" * 90,
         "Buckets the verdict by calendar year, reusing the pooled date-grouped bootstrap +",
         "tradeable guard per year (stats NOT forked). Clean matters: the 1,946 delisted names",
         "cluster in stress years (2008-09 pre-window, 2020, 2022), so survivorship-free pricing",
         "is essential to a fair per-year read. A year is FLAGGED on 21d if the increment flips",
         "sign, loses significance (p>0.05), or beat_pool_median < 50%.",
         ""]
    hdr = (f"  {'year':>4} | {'h':>3} | {'n_fires':>8} | {'incr(bps)':>9} | {'z':>6} | "
           f"{'p':>7} | {'beat_med':>8} | flag")
    L += [hdr, "  " + "-" * (len(hdr) - 2)]
    rowmap = {(r["year"], r["horizon"]): r for r in summ["rows"]}
    for y in sorted(results):
        for h in horizons:
            r = rowmap[(y, h)]
            inc = f"{r['increment_bps']:+8.1f}" if r["increment_bps"] is not None else "     n/a"
            z = f"{r['z']:+.2f}" if r["z"] is not None else "   n/a"
            p = f"{r['p_value']:.4f}" if r["p_value"] is not None else "    n/a"
            bm = f"{r['beat_pool_median']*100:.1f}%" if r["beat_pool_median"] is not None else "  n/a"
            flag = "FLAG" if (h == 21 and r["year_flagged"]) else ""
            L.append(f"  {y:>4} | {h:>3} | {r['n_fires']:>8,} | {inc} | {z:>6} | {p:>7} | {bm:>8} | {flag}")
        L.append("")
    L += ["-- VERDICT " + "-" * 78, f"  {summ['verdict']}"]
    for y in summ["flagged_years"]:
        L.append(f"    {y}: {summ['reasons'][y]}")
    L.append("")
    return "\n".join(L)


# ── Cost-survival gate (equity round-trip cost) ──────────────────────────

def cost_survival(gross: dict, costs: list[float], horizons=HORIZONS,
                  realistic_rt_bps: float = REALISTIC_RT_BPS) -> dict:
    """Derive net-of-cost rows from ONE (gross) run_test result.

    v22 trades the underlying equity, so a round-trip cost is a clean subtraction
    in return space. Subtracting it from BOTH signal and pool means it CANCELS in
    the edge (signal-pool) and leaves the date-grouped bootstrap's z/p/beat
    unchanged — so the bootstrap is reused once, not re-run per cost. The binding
    constraint is the ABSOLUTE net signal return; breakeven = gross signal mean.
    """
    h = gross.get("horizons", {})
    rows, per_h = [], {}
    for hz in horizons:
        r = h.get(hz) or h.get(str(hz)) or {}
        if not r.get("n"):
            per_h[hz] = None
            continue
        gsig, gpool = r["signal_mean"], r["random_mean"]
        edge = gsig - gpool
        be_bps = gsig * 1e4
        per_h[hz] = {"gross_signal_bps": gsig * 1e4, "gross_pool_bps": gpool * 1e4,
                     "edge_bps": edge * 1e4, "z": r.get("z"), "p": r.get("p_value"),
                     "beat": r.get("beat_pool_median_rate"), "n": int(r["n"]),
                     "breakeven_bps": be_bps, "dies_realistic": be_bps < realistic_rt_bps}
        for c in costs:
            cf = c / 1e4
            net_sig = gsig - cf
            rows.append({"cost_bps": c, "horizon": hz, "n_fires": int(r["n"]),
                         "net_signal_bps": net_sig * 1e4, "net_pool_bps": (gpool - cf) * 1e4,
                         "net_edge_bps": edge * 1e4, "z": r.get("z"), "p_value": r.get("p_value"),
                         "beat_pool_median": r.get("beat_pool_median_rate"),
                         "dead": bool(net_sig <= 0), "breakeven_bps": be_bps,
                         "dies_realistic": bool(be_bps < realistic_rt_bps)})
    return {"rows": rows, "per_horizon": per_h, "realistic_rt_bps": realistic_rt_bps}


def _cost_sanity(gross: dict, ref_csv: str = "reports/step2_verdict_rerun.csv",
                 horizons=HORIZONS) -> dict | None:
    """cost=0 must reproduce Prompt A's headline (pass 1, total/full). Compares the
    gross run's per-trade + edge (bps) to the committed verdict CSV."""
    import os
    if not os.path.exists(ref_csv):
        return None
    ref = pd.read_csv(ref_csv)
    ref = ref[ref["pass"] == "1_HEADLINE_total_full"]
    h, out = gross.get("horizons", {}), {}
    for hz in horizons:
        r = h.get(hz) or h.get(str(hz)) or {}
        rr = ref[ref["horizon"] == hz]
        if not r.get("n") or rr.empty:
            continue
        this_g, this_e = r["signal_mean"] * 1e4, (r["signal_mean"] - r["random_mean"]) * 1e4
        pa_g, pa_e = float(rr["gross_per_trade_bps"].iloc[0]), float(rr["increment_bps"].iloc[0])
        out[hz] = {"this_gross_bps": this_g, "pa_gross_bps": pa_g, "this_edge_bps": this_e,
                   "pa_edge_bps": pa_e, "match": abs(this_g - pa_g) < 0.5 and abs(this_e - pa_e) < 0.5}
    return out


def _format_cost_txt(cres: dict, start: str | None, sanity: dict | None,
                     horizons=HORIZONS) -> str:
    L = ["=" * 90,
         "COST-SURVIVAL GATE — equity round-trip cost on the clean survivorship-free panel",
         "=" * 90,
         "v22 trades the underlying EQUITY (options pick direction only), so cost is a clean",
         "equity round-trip in return space. A flat round-trip cost is subtracted from BOTH the",
         "signal and the random pool; because it hits both sides equally it CANCELS in the edge",
         "(signal-pool), so net_edge and its z/p/beat are cost-INVARIANT (the date-grouped",
         "bootstrap is reused once). The binding constraint is the ABSOLUTE net signal return:",
         "net_signal = gross - cost; BREAKEVEN round-trip cost = gross signal mean (bps).",
         f"Window from {start}; basis = total-return; universe = full (survivorship-free).",
         ""]
    if sanity:
        L.append("-- SANITY: cost=0 must reproduce Prompt A headline (pass 1 total/full) " + "-" * 17)
        allok = all(v["match"] for v in sanity.values())
        for hz in horizons:
            v = sanity.get(hz)
            if not v:
                continue
            L.append(f"  {hz:>2}d: gross={v['this_gross_bps']:+.1f}bps (A:{v['pa_gross_bps']:+.1f}) "
                     f"edge={v['this_edge_bps']:+.1f}bps (A:{v['pa_edge_bps']:+.1f})  "
                     f"{'MATCH' if v['match'] else 'MISMATCH'}")
        L.append(f"  -> {'PASS — costed rows trustworthy' if allok else 'FAIL — do NOT trust costed rows'}")
        L.append("")

    L.append("-- COST SWEEP (net of round-trip cost; net_edge/z/p/beat are cost-invariant) " + "-" * 9)
    hdr = (f"  {'cost':>4} | {'h':>3} | {'net_sig(bps)':>12} | {'net_pool(bps)':>13} | "
           f"{'net_edge(bps)':>13} | {'z':>6} | {'p':>7} | {'beat':>6} | dead")
    L += [hdr, "  " + "-" * (len(hdr) - 2)]
    for r in cres["rows"]:
        z = f"{r['z']:+.2f}" if r["z"] is not None else "  n/a"
        p = f"{r['p_value']:.4f}" if r["p_value"] is not None else "  n/a"
        bm = f"{r['beat_pool_median']*100:.1f}%" if r["beat_pool_median"] is not None else " n/a"
        L.append(f"  {r['cost_bps']:>4.0f} | {r['horizon']:>3} | {r['net_signal_bps']:>+12.1f} | "
                 f"{r['net_pool_bps']:>+13.1f} | {r['net_edge_bps']:>+13.1f} | {z:>6} | {p:>7} | "
                 f"{bm:>6} | {'DEAD' if r['dead'] else ''}")
    L.append("")

    L.append(f"-- BREAKEVEN round-trip cost (HEADLINE) + realistic flag (< {cres['realistic_rt_bps']:.0f} bps) "
             + "-" * 12)
    for hz in horizons:
        ph = cres["per_horizon"].get(hz)
        if not ph:
            L.append(f"  {hz:>2}d: (no fires)")
            continue
        verdict = ("DIES under realistic cost" if ph["dies_realistic"]
                   else "survives" + (" (big cushion)" if ph["breakeven_bps"] > 75 else ""))
        L.append(f"  {hz:>2}d: breakeven = {ph['breakeven_bps']:6.1f} bps  (gross edge "
                 f"{ph['edge_bps']:+.1f} bps, z={ph['z']:+.2f}) -> {verdict}")
    L.append("")
    L.append("-- LIQUIDITY FOLLOW-ON (noted, not built) " + "-" * 35)
    L.append("  AV now provides per-name volume, so a dollar-volume-tiered cost pass is unblocked:")
    L.append("  scale the round-trip cost by each name's ADV tier instead of a flat bps. The")
    L.append("  liquid (high-$-volume) subset likely carries a different — probably smaller-")
    L.append("  breakeven — edge than the illiquid tail, which here inflates the pooled breakeven.")
    L.append("")
    return "\n".join(L)


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


def _cli(argv: list[str] | None = None) -> dict:
    import argparse
    import json
    ap = argparse.ArgumentParser(prog="quant_validator.signal_vs_random")
    ap.add_argument("panel_path",
                    help="universe_signal.parquet (orats) | signal_panel_clean.parquet (clean)")
    ap.add_argument("start_date", nargs="?", default=None)
    ap.add_argument("--panel", choices=["orats", "clean"], default="orats",
                    help="orats (default, back-compat): recompute side + clsPx fwd; "
                         "clean: take side as-is + use av_fwd_* returns")
    ap.add_argument("--returns", choices=["total", "split"], default="total",
                    help="clean only: av_fwd_*_total (default) vs av_fwd_*_split")
    ap.add_argument("--universe", choices=["full", "active"], default="full",
                    help="clean only: full=all av_matched (survivorship-free); "
                         "active=survivor tickers only (survivorship-bias diagnostic)")
    ap.add_argument("--pit-ivp", action="store_true")
    ap.add_argument("--match-high-iv", action="store_true")
    ap.add_argument("--iv-threshold", type=float, default=75.0)
    ap.add_argument("--symbol-map", default="data/av/symbol_map.csv")
    ap.add_argument("--by_period", action="store_true",
                    help="run the verdict per period instead of pooled (temporal-stability gate)")
    ap.add_argument("--period_freq", choices=["year", "regime"], default="year",
                    help="year (default); 'regime' is a NotImplementedError stub")
    ap.add_argument("--end_date", default=None, help="by_period only: last date to bucket")
    ap.add_argument("--cost_bps", type=float, default=None,
                    help="single round-trip equity cost (bps), subtracted from signal AND pool")
    ap.add_argument("--cost_sweep", default=None,
                    help='comma list of round-trip costs in bps, e.g. "0,5,10,15,20,30,50"')
    args = ap.parse_args(argv)

    if args.panel == "clean":
        panel = pd.read_parquet(args.panel_path, columns=clean_run_columns())
    else:
        panel = pd.read_parquet(args.panel_path)

    if args.by_period:
        from pathlib import Path
        if args.panel == "clean":
            surv = survivor_tickers_from_map(args.symbol_map) if args.universe == "active" else None
            ann = annotate_clean(panel, returns=args.returns, universe=args.universe,
                                 survivor_tickers=surv)
            price_col = "raw_close"
        else:
            ann = _per_ticker_signal_and_fwd(panel, opts=ConsensusOpts(), pit_ivp=args.pit_ivp)
            price_col = "clsPx"
        results = run_by_period(ann, price_col=price_col, period_freq=args.period_freq,
                                start_date=args.start_date, end_date=args.end_date,
                                match_high_iv=args.match_high_iv, iv_match_threshold=args.iv_threshold)
        summ = summarize_by_period(results)
        txt = _format_temporal_txt(results, summ)
        Path("reports").mkdir(parents=True, exist_ok=True)
        Path("reports/gate_temporal_stability.txt").write_text(txt, encoding="utf-8")
        pd.DataFrame(summ["rows"]).to_csv("reports/gate_temporal_stability.csv", index=False)
        print(txt)
        print("wrote reports/gate_temporal_stability.{txt,csv}")
        return results

    if args.cost_sweep or args.cost_bps is not None:
        from pathlib import Path
        if args.panel == "clean":
            surv = survivor_tickers_from_map(args.symbol_map) if args.universe == "active" else None
            ann = annotate_clean(panel, returns=args.returns, universe=args.universe,
                                 survivor_tickers=surv)
            price_col = "raw_close"
        else:
            ann = _per_ticker_signal_and_fwd(panel, opts=ConsensusOpts(), pit_ivp=args.pit_ivp)
            price_col = "clsPx"
        gross = run_test(ann=ann, price_col=price_col, start_date=args.start_date,
                         match_high_iv=args.match_high_iv, iv_match_threshold=args.iv_threshold)
        if args.cost_sweep:
            costs = [float(x) for x in args.cost_sweep.split(",") if x.strip() != ""]
        else:
            costs = [float(args.cost_bps)]
        cres = cost_survival(gross, costs)
        sanity = _cost_sanity(gross)
        txt = _format_cost_txt(cres, args.start_date, sanity)
        Path("reports").mkdir(parents=True, exist_ok=True)
        Path("reports/gate_cost_survival.txt").write_text(txt, encoding="utf-8")
        pd.DataFrame(cres["rows"]).to_csv("reports/gate_cost_survival.csv", index=False)
        print(txt)
        print("wrote reports/gate_cost_survival.{txt,csv}")
        return cres

    if args.panel == "orats":
        res = run_test(uni=panel, start_date=args.start_date, pit_ivp=args.pit_ivp,
                       match_high_iv=args.match_high_iv, iv_match_threshold=args.iv_threshold)
    else:
        surv = survivor_tickers_from_map(args.symbol_map) if args.universe == "active" else None
        ann = annotate_clean(panel, returns=args.returns, universe=args.universe,
                             survivor_tickers=surv)
        res = run_test(ann=ann, price_col="raw_close", start_date=args.start_date,
                       match_high_iv=args.match_high_iv, iv_match_threshold=args.iv_threshold)
    print(json.dumps(res, indent=2, default=str))
    print()
    print(summarize(res))
    return res


if __name__ == "__main__":
    import sys
    _cli()
    sys.exit(0)
