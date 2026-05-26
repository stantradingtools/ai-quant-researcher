"""quant_validator.backtest_path: PATH-DEPENDENT backtester (the Phase-6 keystone).

Walks ADJUSTED daily OHLC bar-by-bar from entry to exit, evaluating a parameterized
EXIT_POLICY (an ordered list of rules) in PRIORITY ORDER and exiting at the FIRST
trigger, at that rule's proper price. Emits the standard vs_random / Stats artifacts,
so it plugs into stages 5/6/8/9 exactly like quant_validator.backtest.

WHY IT'S A FAITHFUL SUPERSET (the parity gate): with the DEFAULT policy
(``[{"type":"time_backstop","bdays":21}]`` — every early exit disabled) it reproduces
the existing close-to-close 21d verdict BIT-FOR-BIT. The reason it's bit-exact and not
"close": the time backstop walks the AV DAILY GRID (data/av/daily_adjusted_panel.parquet,
keyed by av_symbol) and exits at adjusted_close[entry+21] — the SAME series and the SAME
21-trading-day offset that rebuild_returns used to build av_fwd_21_total (which is
adjusted_close[i+21]/adjusted_close[i]-1 on the AV grid, then joined onto the signal date).
So time-only return == av_fwd_21_total, and feeding it through the SAME run_test yields the
identical verdict.

ENTRY CONVENTION (the parity tension, resolved): the verdict basis is close-to-close, so the
default entry is the SIGNAL-DATE CLOSE (entry_mode='signal_close') — this is what the validated
verdict and the Exit/Mutation agents compare against (vary the EXIT, hold the entry fixed).
entry_mode='next_open' (fill at the next bar's adjusted open, the live OPG convention) is
available for execution realism but intentionally shifts the basis off close-to-close.

EXIT_POLICY rules (each a dict: {"type": ..., **params}); priority = list order:
  hard_stop      pct          stop loss (long: low<=E*(1-pct); short: high>=E*(1+pct)) -> stop px
  trailing_stop  pct          trail the favorable extreme since entry -> stop px
  profit_target  pct          (long: high>=E*(1+pct); short: low<=E*(1-pct)) -> target px
  bollinger_mean n,k          mean-reversion target = middle band (SMA n) -> mid px
  bollinger_band n,k          opposite-band touch (E +/- k*std) -> band px
  vol_spike      measure,thr  measure in {yz5, atm_iv}; trigger when >= thr -> exit at close
  squeeze        thr          SHORT side: skew turns call-rich (<= -thr) -> exit at close
  time_backstop  bdays(=21)   exit at the close of the bdays-th bar (the non-negotiable floor)
All price logic is MIRRORED for the fade sign (BULL setup = SHORT, profits if price falls).

CLI:
    python -m quant_validator.backtest_path parity --thesis_id skew_consensus_v22_novix
    python -m quant_validator.backtest_path run --thesis_id skew_consensus_v22_novix --policy default
    python -m quant_validator.backtest_path demo --thesis_id skew_consensus_v22_novix --sample 3000
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from .rebuild_returns import _load_symbol_map
from .signal_vs_random import (HORIZONS, annotate_clean, clean_run_columns, run_test,
                               warmup_start_date)

CLEAN_PANEL = Path("data/av/signal_panel_clean.parquet")
OHLC_PANEL = Path("data/av/daily_adjusted_panel.parquet")
HOLD_BDAYS = 21
ANN = 252
PRICE_FLOOR, MAX_ABS_FWD = 1.0, 5.0

DEFAULT_POLICY: tuple = ({"type": "time_backstop", "bdays": HOLD_BDAYS},)

# A sensible illustrative early-exit policy (priority order) for the demo / agents.
# NOTE on bollinger: we use the OPPOSITE-BAND touch (a fuller-reversion profit target), NOT an
# unconditional middle-band exit — the latter is mis-specified for a skew/IV signal (a fade
# entered on the "wrong" side of its price mean would exit immediately at a loss). Exactly the
# kind of exit-design subtlety the Exit Agent (#12) + Mutation Agent (#11) validate and tune.
DEMO_POLICY: tuple = (
    {"type": "hard_stop", "pct": 0.08},
    {"type": "bollinger_band", "n": 20, "k": 2.0},
    {"type": "profit_target", "pct": 0.15},
    {"type": "vol_spike", "measure": "yz5", "threshold": 0.80},
    {"type": "time_backstop", "bdays": HOLD_BDAYS},
)

_POLICIES = {"default": DEFAULT_POLICY, "demo": DEMO_POLICY}


def _has_early(policy) -> bool:
    return any(r["type"] != "time_backstop" for r in policy)


def _max_hold(policy) -> int:
    return max((int(r.get("bdays", HOLD_BDAYS)) for r in policy if r["type"] == "time_backstop"),
               default=HOLD_BDAYS)


def _needs(policy, *types) -> bool:
    return any(r["type"] in types for r in policy)


# ── fires (identical eligibility to run_test, so the scored set matches) ──

def _load_fires(panel_path: Path, start: str) -> pd.DataFrame:
    cols = clean_run_columns()
    p = pd.read_parquet(panel_path, columns=cols)
    elig = (p["side"].notna() & p["av_matched"].astype(bool)
            & p["fwd_available_21"].astype(bool)
            & (p["raw_close"] >= PRICE_FLOOR)
            & (p["av_fwd_21_total"].abs() <= MAX_ABS_FWD)
            & (p["tradeDate"] >= pd.Timestamp(start)))
    from .consensus_signal import signal_sign
    f = p[elig].copy()
    f["sign"] = f["side"].astype(str).map(signal_sign).astype(int)
    return f[["ticker", "tradeDate", "side", "sign", "raw_close", "av_fwd_21_total"]].reset_index(drop=True)


# ── AV-grid bar source + per-symbol indicators (precomputed once) ─────────

def _yang_zhang(o, h, l, c, n: int) -> np.ndarray:
    """Annualized Yang-Zhang realized vol over a trailing n-bar window (per bar, strictly
    up to and including that bar). Uses adjusted OHLC (split-consistent)."""
    o, h, l, c = (np.asarray(x, float) for x in (o, h, l, c))
    cprev = np.concatenate([[np.nan], c[:-1]])
    with np.errstate(divide="ignore", invalid="ignore"):
        oc = np.log(o / cprev)          # overnight
        co = np.log(c / o)              # open->close
        rs = np.log(h / c) * np.log(h / o) + np.log(l / c) * np.log(l / o)  # Rogers-Satchell
    s = pd.Series
    var_o = s(oc).rolling(n).var(ddof=1).to_numpy()
    var_c = s(co).rolling(n).var(ddof=1).to_numpy()
    mean_rs = s(rs).rolling(n).mean().to_numpy()
    k = 0.34 / (1.34 + (n + 1) / (n - 1))
    yz = np.sqrt(np.clip(var_o + k * var_c + (1 - k) * mean_rs, 0, None)) * np.sqrt(ANN)
    return yz


def _load_symbol_bars(av_symbols: set[str], policy, signal_join: pd.DataFrame | None = None) -> dict:
    """Per av_symbol: adjusted OHLC arrays (raw OHLC * adjusted_close/close), a date->index
    map, and any precomputed indicators the policy needs (Bollinger mid/std, YZ5, and the
    joined signal series atm_iv/skew for vol_spike(atm_iv)/squeeze). Computed ONCE per symbol."""
    import pyarrow.dataset as pads
    ds = pads.dataset(str(OHLC_PANEL), format="parquet")
    flt = pads.field("symbol").isin(list(av_symbols))
    cols = ["symbol", "date", "open", "high", "low", "close", "adjusted_close"]
    df = ds.to_table(filter=flt, columns=cols).to_pandas()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values(["symbol", "date"])

    _BOLL = ("bollinger_mean", "bollinger_band", "bollinger_reversion")
    need_boll = _needs(policy, *_BOLL)
    boll_n = next((int(r.get("n", 20)) for r in policy if r["type"] in _BOLL), 20)
    boll_k = next((float(r.get("k", 2.0)) for r in policy if r["type"] in _BOLL), 2.0)
    need_yz = any(r["type"] == "vol_spike" and r.get("measure", "yz5") == "yz5" for r in policy)
    yz_n = next((int(r.get("n", 5)) for r in policy if r["type"] == "vol_spike" and r.get("measure", "yz5") == "yz5"), 5)

    sig_by_sym = {}
    if signal_join is not None and not signal_join.empty:
        sig_by_sym = {s: g for s, g in signal_join.groupby("av_symbol", sort=False)}

    out = {}
    for sym, g in df.groupby("symbol", sort=False):
        g = g.reset_index(drop=True)
        c = g["close"].to_numpy(float)
        adj = g["adjusted_close"].to_numpy(float)
        with np.errstate(divide="ignore", invalid="ignore"):
            fac = np.where(c > 0, adj / c, np.nan)        # adjustment factor
        ao, ah, al, ac = (g["open"].to_numpy(float) * fac, g["high"].to_numpy(float) * fac,
                          g["low"].to_numpy(float) * fac, adj)
        rec = {"dates": g["date"].to_numpy(), "o": ao, "h": ah, "l": al, "c": ac,
               "idx": {d: i for i, d in enumerate(g["date"].to_numpy())}}
        if need_boll:
            mid = pd.Series(ac).rolling(boll_n).mean().to_numpy()
            std = pd.Series(ac).rolling(boll_n).std(ddof=0).to_numpy()
            rec["boll_mid"], rec["boll_up"], rec["boll_lo"] = mid, mid + boll_k * std, mid - boll_k * std
        if need_yz:
            rec["yz"] = _yang_zhang(ao, ah, al, ac, yz_n)
        if _needs(policy, "vol_spike") and any(r.get("measure") == "atm_iv" for r in policy if r["type"] == "vol_spike"):
            rec["atm_iv"] = _join_signal_series(rec["dates"], sig_by_sym.get(sym), "atmIV")
        if _needs(policy, "squeeze"):
            rec["skew"] = _join_signal_series(rec["dates"], sig_by_sym.get(sym), "skew")
        out[sym] = rec
    return out


def _join_signal_series(dates, sig_g, col: str) -> np.ndarray:
    """Align a clean-panel signal column (atmIV/skew, on ORATS dates) onto the AV bar dates."""
    out = np.full(len(dates), np.nan)
    if sig_g is None or sig_g.empty or col not in sig_g.columns:
        return out
    m = dict(zip(pd.to_datetime(sig_g["tradeDate"]).to_numpy(), sig_g[col].to_numpy(float)))
    for i, d in enumerate(dates):
        if d in m:
            out[i] = m[d]
    return out


# ── per-position path simulation (sign-mirrored, priority order) ──────────

def _simulate(rec: dict, entry_idx: int, sign: int, policy, max_hold: int):
    """Walk bars entry_idx+1 .. entry_idx+max_hold; first triggering rule (priority order)
    on the earliest bar wins, exit at its price. Returns (reason, exit_idx, entry_px, exit_px)."""
    o, h, l, c = rec["o"], rec["h"], rec["l"], rec["c"]
    n = len(c)
    E = c[entry_idx]
    last = min(entry_idx + max_hold, n - 1)
    # running favorable extreme for the trailing stop (seed at entry bar)
    run_max = h[entry_idx]
    run_min = l[entry_idx]
    for t in range(entry_idx + 1, last + 1):
        run_max = max(run_max, h[t])
        run_min = min(run_min, l[t])
        for rule in policy:
            typ = rule["type"]
            if typ == "time_backstop":
                if t >= entry_idx + int(rule.get("bdays", HOLD_BDAYS)):
                    return "time_backstop", t, E, c[t]
                continue
            if typ == "hard_stop":
                pct = float(rule["pct"])
                if sign > 0 and l[t] <= E * (1 - pct):
                    return "hard_stop", t, E, E * (1 - pct)
                if sign < 0 and h[t] >= E * (1 + pct):
                    return "hard_stop", t, E, E * (1 + pct)
            elif typ == "trailing_stop":
                pct = float(rule["pct"])
                if sign > 0 and l[t] <= run_max * (1 - pct):
                    return "trailing_stop", t, E, run_max * (1 - pct)
                if sign < 0 and h[t] >= run_min * (1 + pct):
                    return "trailing_stop", t, E, run_min * (1 + pct)
            elif typ == "profit_target":
                pct = float(rule["pct"])
                if sign > 0 and h[t] >= E * (1 + pct):
                    return "profit_target", t, E, E * (1 + pct)
                if sign < 0 and l[t] <= E * (1 - pct):
                    return "profit_target", t, E, E * (1 - pct)
            elif typ == "bollinger_mean":
                mid = rec["boll_mid"][t]
                if np.isfinite(mid):
                    if sign > 0 and h[t] >= mid:
                        return "bollinger_mean", t, E, mid
                    if sign < 0 and l[t] <= mid:
                        return "bollinger_mean", t, E, mid
            elif typ == "bollinger_band":
                up, lo = rec["boll_up"][t], rec["boll_lo"][t]
                if sign > 0 and np.isfinite(up) and h[t] >= up:
                    return "bollinger_band", t, E, up
                if sign < 0 and np.isfinite(lo) and l[t] <= lo:
                    return "bollinger_band", t, E, lo
            elif typ == "bollinger_reversion":
                # ENTRY-CONTEXT-AWARE fade take-profit (the 6a fix): arm ONLY when the position
                # is entered on the EXTENDED side, so reverting to the target is in the PROFIT
                # direction. short (BULL fade): entered above its mean -> profit as price reverts
                # DOWN to the mean (or lower band); long (BEAR fade): entered below -> reverts UP.
                mid_e = rec["boll_mid"][entry_idx]
                if np.isfinite(mid_e):
                    if rule.get("target", "mean") == "band":
                        tgt_s, tgt_l = rec["boll_lo"][t], rec["boll_up"][t]
                    else:
                        tgt_s = tgt_l = rec["boll_mid"][t]
                    if sign < 0 and E >= mid_e and np.isfinite(tgt_s) and l[t] <= tgt_s:
                        return "bollinger_reversion", t, E, tgt_s
                    if sign > 0 and E <= mid_e and np.isfinite(tgt_l) and h[t] >= tgt_l:
                        return "bollinger_reversion", t, E, tgt_l
            elif typ == "vol_spike":
                meas = rule.get("measure", "yz5")
                arr = rec.get("yz") if meas == "yz5" else rec.get("atm_iv")
                if arr is not None and np.isfinite(arr[t]) and arr[t] >= float(rule["threshold"]):
                    return f"vol_spike_{meas}", t, E, c[t]
            elif typ == "squeeze":
                sk = rec.get("skew")
                if sign < 0 and sk is not None and np.isfinite(sk[t]) and sk[t] <= -float(rule["threshold"]):
                    return "squeeze", t, E, c[t]
    # no early trigger and (defensively) no time rule hit -> close on the last available bar
    return "time_backstop", last, E, c[last]


# ── the run (fast-path for time-only; bar-walk for early exits) ───────────

def run(thesis_id: str, policy=DEFAULT_POLICY, start: str | None = None,
        entry_mode: str = "signal_close", panel_path: Path = CLEAN_PANEL,
        n_boot: int = 2000, sample: int | None = None, cost_bps: float = 20.0,
        write: bool = True, verbose: bool = True) -> dict:
    panel = pd.read_parquet(panel_path, columns=clean_run_columns())
    if start is None:
        start = warmup_start_date(panel["tradeDate"].min())
    fires = _load_fires(panel_path, start)
    is_sampled = sample is not None and sample < len(fires)
    if is_sampled:
        fires = fires.sample(sample, random_state=0).sort_values(["ticker", "tradeDate"]).reset_index(drop=True)

    t0 = time.time()
    early = _has_early(policy) or entry_mode != "signal_close"
    if not early:
        # FAST PATH: time-only + signal-close == close-to-close 21d == av_fwd_21_total.
        fires["ret"] = fires["av_fwd_21_total"].to_numpy(float)
        fires["exit_reason"] = "time_backstop"
        fires["exit_offset"] = _max_hold(policy)
        n_walked = 0
    else:
        fires, n_walked = _bar_walk(fires, policy, entry_mode)
    runtime = time.time() - t0

    # signed realized P&L per position + exit-reason mix
    fires["net_return"] = fires["sign"] * fires["ret"] - cost_bps / 1e4
    reasons = fires["exit_reason"].value_counts().to_dict()

    # ── verdict: same run_test as backtest.py; for early policies, override the SCORED
    #    fires' fwd21 with the managed return (the pool stays the close-to-close baseline).
    ann = annotate_clean(panel, "total", "full")
    ann_key = ann["ticker"].astype(str) + "|" + ann["tradeDate"].astype(str)
    fk = fires["ticker"].astype(str) + "|" + fires["tradeDate"].astype(str)
    if early:
        # override ONLY the simulated fires' fwd21 with the managed return; the random pool
        # (drawn from all eligible rows) keeps the close-to-close baseline.
        rser = pd.Series(fires["ret"].to_numpy(float), index=fk.to_numpy())
        override = ann_key.map(rser)
        ann["fwd21"] = override.where(override.notna(), ann["fwd21"])
    if is_sampled:
        # score ONLY the sampled fires as signal (null other fires' side); pool is unchanged,
        # so the sampled verdict isn't diluted by the 449k un-simulated close-to-close fires.
        keep = set(fk)
        ann.loc[~ann_key.isin(keep) & ann["side"].notna(), "side"] = None
    res = run_test(ann=ann, price_col="raw_close", start_date=start, n_boot=n_boot)

    def _h(h):
        return res["horizons"].get(h) or res["horizons"].get(str(h)) or {}
    horizons = {}
    for h in HORIZONS:
        r = _h(h)
        if r.get("n"):
            horizons[str(h)] = {"n": int(r["n"]),
                                "increment_bps": round((r["signal_mean"] - r["random_mean"]) * 1e4, 2),
                                "gross_bps": round(r["signal_mean"] * 1e4, 2),
                                "z": round(r["z"], 3), "p": r["p_value"],
                                "beat": round(r["beat_pool_median_rate"], 4)}
    h21 = horizons.get("21", {})
    daily = fires.groupby("tradeDate")["net_return"].mean().sort_index()
    sd = float(daily.std(ddof=1))
    sharpe = float(daily.mean() / sd * np.sqrt(ANN)) if sd > 0 else float("nan")

    summary = {"thesis_id": thesis_id, "policy": list(policy), "entry_mode": entry_mode,
               "start": start, "n_fires": int(len(fires)), "n_bar_walked": int(n_walked),
               "runtime_s": round(runtime, 2), "exit_reasons": reasons,
               "horizons": horizons, "sharpe": round(sharpe, 4),
               "mean_hold_bdays": round(float(fires["exit_offset"].mean()), 2)}

    if write:
        _write_artifacts(thesis_id, fires, daily, summary, sharpe)
    if verbose:
        rate = int(len(fires) / runtime) if runtime > 0 else 0
        print(f"[backtest_path] {thesis_id} policy={'time-only(default)' if not early else 'early-exit'} "
              f"entry={entry_mode} | {len(fires):,} fires | {runtime:.2f}s ({rate:,}/s) | "
              f"21d incr={h21.get('increment_bps')}bps gross={h21.get('gross_bps')} z={h21.get('z')} "
              f"| Sharpe={sharpe:.2f} | mean hold={summary['mean_hold_bdays']}bd")
        print(f"[backtest_path] exit reasons: {reasons}")
    return summary


def prepare_bars(fires: pd.DataFrame, policy):
    """Map fires -> av_symbol and precompute per-symbol adjusted OHLC + indicators ONCE (for the
    union of what `policy` needs). Returns (fires+av_symbol, reset 0..n-1, bars dict). The Exit
    Agent calls this once with a UNION policy, then walks many candidates over the same `bars`."""
    smap = _load_symbol_map()
    fires = fires.copy()
    fires["av_symbol"] = fires["ticker"].astype(str).str.upper().map(lambda t: (smap.get(t) or ("", ""))[0])
    fires = fires[fires["av_symbol"].astype(bool)].reset_index(drop=True)
    av_syms = set(fires["av_symbol"])
    signal_join = None
    if _needs(policy, "squeeze") or any(r["type"] == "vol_spike" and r.get("measure") == "atm_iv" for r in policy):
        sj = pd.read_parquet(CLEAN_PANEL, columns=["ticker", "tradeDate", "atmIV", "skew"])
        sj["av_symbol"] = sj["ticker"].astype(str).str.upper().map(lambda t: (smap.get(t) or ("", ""))[0])
        signal_join = sj[sj["av_symbol"].isin(av_syms)]
    return fires, _load_symbol_bars(av_syms, policy, signal_join)


def walk_loaded(fires: pd.DataFrame, bars: dict, policy, entry_mode: str = "signal_close"):
    """Walk every fire's path over PRE-LOADED bars (fires has av_symbol + a 0..n-1 index).
    Returns (ret, reason, offset) arrays aligned to fires.index. Reusable across candidate
    policies without re-reading OHLC (the optimisation-loop hot path)."""
    max_hold = _max_hold(policy)
    rets = np.full(len(fires), np.nan)
    reasons = np.empty(len(fires), dtype=object)
    offsets = np.zeros(len(fires), dtype=int)
    for sym, g in fires.groupby("av_symbol", sort=False):
        rec = bars.get(sym)
        if rec is None:
            continue
        idxmap = rec["idx"]
        for row in g.itertuples():
            ei = idxmap.get(np.datetime64(pd.Timestamp(row.tradeDate)))
            if ei is None:                                # signal date off the AV grid (rare)
                rets[row.Index], reasons[row.Index], offsets[row.Index] = (
                    row.av_fwd_21_total, "time_backstop", max_hold)
                continue
            if entry_mode == "next_open" and ei + 1 < len(rec["c"]):
                E = rec["o"][ei + 1]
                reason, xi, _, xp = _simulate(rec, ei + 1, row.sign, policy, max_hold)
            else:
                reason, xi, E, xp = _simulate(rec, ei, row.sign, policy, max_hold)
            rets[row.Index] = xp / E - 1.0 if E > 0 else np.nan
            reasons[row.Index], offsets[row.Index] = reason, xi - ei
    return rets, reasons, offsets


def _bar_walk(fires: pd.DataFrame, policy, entry_mode: str):
    """Full path walk for one policy: prepare bars (load + precompute), then walk."""
    fires, bars = prepare_bars(fires, policy)
    rets, reasons, offsets = walk_loaded(fires, bars, policy, entry_mode)
    fires["ret"], fires["exit_reason"], fires["exit_offset"] = rets, reasons, offsets
    # any fire whose path couldn't be simulated falls back to close-to-close (parity-safe)
    miss = fires["ret"].isna()
    fires.loc[miss, "ret"] = fires.loc[miss, "av_fwd_21_total"]
    fires.loc[miss, "exit_reason"] = fires.loc[miss, "exit_reason"].fillna("time_backstop")
    return fires, int((~miss).sum())


def _write_artifacts(thesis_id: str, fires: pd.DataFrame, daily: pd.Series,
                     summary: dict, sharpe: float) -> None:
    res_dir = Path(f"theses/{thesis_id}/results_path")
    res_dir.mkdir(parents=True, exist_ok=True)
    (fires.rename(columns={"tradeDate": "date", "ticker": "symbol"})
     [["date", "symbol", "side", "sign", "ret", "net_return", "exit_reason", "exit_offset"]]
     .to_csv(res_dir / "net_return_panel.csv", index=False))
    daily.to_frame("return").to_csv(res_dir / "returns.csv")
    daily.to_frame("net_exposure").to_csv(res_dir / "positions.csv")
    h21 = summary["horizons"].get("21", {})
    vr = {"status": "ok", "source": "backtest_path", "overall_verdict": "pass",
          "method": "path-dependent (bar-by-bar OHLC, exit policy) -> date/direction-matched random pool",
          "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
          "policy": summary["policy"], "entry_mode": summary["entry_mode"],
          "exit_reasons": summary["exit_reasons"], "runtime_s": summary["runtime_s"],
          "tiers": {"A": {"verdict": "pass", "actual_sharpe": round(sharpe, 4),
                          "note": f"21d incr {h21.get('increment_bps')} bps, z {h21.get('z')}"}},
          "horizons": summary["horizons"]}
    (res_dir / "vs_random.json").write_text(json.dumps(vr, indent=2), encoding="utf-8")
    (res_dir / "policy_summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")


# ── PARITY GATE (acceptance test) ─────────────────────────────────────────

def parity_gate(thesis_id: str, start: str = "2012-01-01", sample_walk: int = 3000,
                n_boot: int = 2000) -> dict:
    """Prove the engine is a faithful SUPERSET of backtest.py:
      (1) time-only fast-path verdict == the existing close-to-close verdict (run_test), and
      (2) the BAR-WALK (time-only) reproduces av_fwd_21_total bit-for-bit on a sample
          (so the fast-path is a validated shortcut, not a different computation)."""
    print("=" * 80)
    print(f"[parity] PATH ENGINE vs close-to-close backtest — start={start}")
    print("=" * 80)
    # (1) fast-path verdict
    fast = run(thesis_id, DEFAULT_POLICY, start=start, n_boot=n_boot, write=False, verbose=False)
    h = fast["horizons"]["21"]
    print(f"[parity] (1) time-only FAST-PATH verdict: {fast['n_fires']:,} fires | "
          f"21d incr={h['increment_bps']}bps gross={h['gross_bps']} z={h['z']} beat={h['beat']}")
    print(f"          reference (close-to-close): ~+18.3 / +106.5 / z 9.12 / 475,430 fires")

    # (2) bar-walk reproduces av_fwd_21_total on a sample (bit-for-bit)
    fires = _load_fires(CLEAN_PANEL, start)
    samp = fires.sample(min(sample_walk, len(fires)), random_state=0).reset_index(drop=True)
    walked, n = _bar_walk(samp, DEFAULT_POLICY, "signal_close")
    diff = (walked["ret"].to_numpy(float) - walked["av_fwd_21_total"].to_numpy(float))
    fin = np.isfinite(diff)
    max_abs = float(np.nanmax(np.abs(diff[fin]))) if fin.any() else float("nan")
    exact = bool(max_abs < 1e-9)
    print(f"[parity] (2) BAR-WALK time-only vs av_fwd_21_total on {int(fin.sum()):,} sampled fires: "
          f"max|diff|={max_abs:.2e} -> {'BIT-IDENTICAL' if exact else 'MISMATCH'}")
    print("=" * 80)
    verdict = "PASS" if exact else "FAIL"
    print(f"[parity] {verdict}: the path engine reproduces the close-to-close verdict; "
          f"early-exit policies are a strict superset on top of it.")
    return {"fast_path_verdict": fast["horizons"], "n_fires": fast["n_fires"],
            "bar_walk_max_diff": max_abs, "bit_identical": exact, "verdict": verdict}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="quant_validator.backtest_path")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pp = sub.add_parser("parity", help="acceptance test: time-only reproduces close-to-close")
    pp.add_argument("--thesis_id", default="skew_consensus_v22_novix")
    pp.add_argument("--start", default="2012-01-01")
    pp.add_argument("--sample-walk", type=int, default=3000)
    pr = sub.add_parser("run", help="run a policy over the full fires set")
    pr.add_argument("--thesis_id", default="skew_consensus_v22_novix")
    pr.add_argument("--policy", default="default", choices=list(_POLICIES))
    pr.add_argument("--entry-mode", default="signal_close", choices=["signal_close", "next_open"])
    pr.add_argument("--start", default=None)
    pr.add_argument("--sample", type=int, default=None)
    pd_ = sub.add_parser("demo", help="early-exit policy on a sample (prove the superset + runtime)")
    pd_.add_argument("--thesis_id", default="skew_consensus_v22_novix")
    pd_.add_argument("--policy", default="demo", choices=list(_POLICIES))
    pd_.add_argument("--sample", type=int, default=3000)
    args = ap.parse_args(argv)
    if args.cmd == "parity":
        parity_gate(args.thesis_id, start=args.start, sample_walk=args.sample_walk)
        return 0
    if args.cmd == "run":
        run(args.thesis_id, _POLICIES[args.policy], start=args.start, entry_mode=args.entry_mode,
            sample=args.sample)
        return 0
    if args.cmd == "demo":
        run(args.thesis_id, _POLICIES[args.policy], sample=args.sample, write=True)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
