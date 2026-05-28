"""quant_validator.mutation_agent: the Mutation Agent (#11).

A drawdown-constrained, multiple-testing-disciplined alpha optimiser. Closed loop:
  propose a variant (entry / filter / exit / universe)
    -> backtest_path (6a path engine)
    -> walk-forward Sharpe AND maxDD (the 6b basis)
    -> Risk Agent's drawdown lens (hard maxDD bound)
    -> cumulative MT/DSR penalty over EVERY candidate ever tried (incl. 6b's 12)
    -> feedback (keep best-so-far, perturb around it) -> propose next.
Promote ONLY a walk-forward improvement that beats the v22+exits baseline AND survives the
multiple-testing penalty AND respects the DD bound. An honest "no validated improvement" is a
SUCCESS (v22 is near a local optimum), not a failure.

THESIS-LOCKED: the core skew/RR/IV fade (M1 corner ∧ M2 IV×RR ∧ M3 stall/divergence, fade
direction) is NEVER mutated. Only the KNOBS around it are tuned:
  * entry   — 75/25 thresholds, freshness, sigma_threshold (RE-FIRES the consensus; EXPENSIVE)
  * filters — liquidity tier / universe subset (a fire filter; cheap)
  * exit    — the 6b validated set + combinations (reuse fires + loaded OHLC via walk_loaded; CHEAP)
  * universe— full vs liquid subset
Cost-aware: search cheap exit variants widely, expensive entry variants sparingly.

Sampling is TICKER-based (a random ticker subset, ALL their fires) so the baseline, exit, universe
AND re-fired entry variants are all scored on the SAME universe and the SAME walk-forward fold
edges — apples-to-apples. Start from a strong baseline: v22 + the 6b survivors
(hard_stop_8 + boll_reversion_band).

CLI:
    python -m quant_validator.mutation_agent optimise --thesis_id skew_consensus_v22_novix \
        --tickers 600 --n-folds 5 --dd-bound -0.25
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

from . import backtest_path as bt
from . import exit_agent as ea
from .backtest import OOS_END, SPLIT_CUTOFF
from .consensus_signal import ConsensusOpts, compute_consensus, signal_sign
from .rebuild_returns import _load_symbol_map
from .signal_vs_random import warmup_start_date
from .stats import deflated_sharpe

REPORTS = Path("reports")
MUT_REPORT = REPORTS / "mutation_agent.txt"
MUT_JSON = REPORTS / "mutation_agent.json"

# Strong starting point: v22 (default consensus) + the 6b validated exits.
BASELINE_EXIT = (ea.r_hard_stop(0.08), ea.r_bollinger_reversion(target="band"), ea.r_time())
PRIOR_TRIALS = 12          # 6b logged 12 candidates — the MT count carries forward (cumulative)
DSR_PROB_BAR = 0.95        # the best variant's deflated-Sharpe prob-real must clear this at n_trials
EDGE_KEEP = 0.5            # don't surrender more than half the baseline mean edge

_FEAT = ["ticker", "tradeDate", "putP", "callP", "ivP", "rrP", "sigma", "skewDelta",
         "side", "av_matched", "raw_close", "fwd_available_21", "av_fwd_21_total"]


# ── thesis-locked candidate surface (cost-tagged) ─────────────────────────

def exit_candidates() -> list[tuple]:
    """CHEAP: reuse the SAME fires + loaded OHLC; just re-walk a different exit policy."""
    R = ea
    return [
        ("exit_hardstop_only_8",     (R.r_hard_stop(0.08), R.r_time())),
        ("exit_hardstop_6",          (R.r_hard_stop(0.06), R.r_bollinger_reversion(target="band"), R.r_time())),
        ("exit_hardstop_10",         (R.r_hard_stop(0.10), R.r_bollinger_reversion(target="band"), R.r_time())),
        ("exit_boll_band_only",      (R.r_bollinger_reversion(target="band"), R.r_time())),
        ("exit_hardstop_boll_mean",  (R.r_hard_stop(0.08), R.r_bollinger_reversion(target="mean"), R.r_time())),
        ("exit_trailing_boll",       (R.r_trailing_stop(0.10), R.r_bollinger_reversion(target="band"), R.r_time())),
        ("exit_base_plus_vol",       (R.r_hard_stop(0.08), R.r_bollinger_reversion(target="band"),
                                      R.r_vol_spike("yz5", 0.80), R.r_time())),
        ("exit_base_plus_squeeze",   (R.r_hard_stop(0.08), R.r_bollinger_reversion(target="band"),
                                      R.r_squeeze(2.0), R.r_time())),
        ("exit_base_plus_tail",      (R.r_hard_stop(0.08), R.r_bollinger_reversion(target="band"),
                                      R.r_vol_spike("yz5", 0.80), R.r_squeeze(2.0), R.r_time())),
        ("exit_hardstop_profit",     (R.r_hard_stop(0.08), R.r_profit_target(0.15), R.r_time())),
        ("exit_hardstop_5_tight",    (R.r_hard_stop(0.05), R.r_bollinger_reversion(target="band"), R.r_time())),
    ]


def universe_candidates() -> list[tuple]:
    """CHEAP: a fire filter (no re-fire). Liquidity tiers; the shortable gate is applied LIVE in
    fills (so here we use a price-liquidity proxy as the standing universe lever)."""
    return [
        ("universe_liquid_5",  lambda f: f[f["raw_close"] >= 5.0]),
        ("universe_liquid_10", lambda f: f[f["raw_close"] >= 10.0]),
    ]


def entry_candidates() -> list[tuple]:
    """EXPENSIVE: re-fire the consensus with perturbed KNOBS (structure thesis-locked). Sparse."""
    return [
        ("entry_corner_70_30",  ConsensusOpts(hi=70.0, lo=30.0)),
        ("entry_corner_80_20",  ConsensusOpts(hi=80.0, lo=20.0)),
        ("entry_freshness_5",   ConsensusOpts(freshness=5)),
        ("entry_sigma_1_5",     ConsensusOpts(sigma_threshold=1.5)),
    ]


# ── sample + (re-)fire ─────────────────────────────────────────────────────

def _sample_panel(n_tickers: int, seed: int = 0) -> pd.DataFrame:
    """All clean-panel rows (features + fwd + flags) for a random ticker subset — serves both the
    baseline fires (side as-is) and the entry re-fire (recompute side with perturbed opts)."""
    import pyarrow.parquet as pq
    allt = pd.read_parquet(bt.CLEAN_PANEL, columns=["ticker"])["ticker"].unique()
    rng = np.random.default_rng(seed)
    keep = set(rng.choice(allt, size=min(n_tickers, len(allt)), replace=False))
    cols = [c for c in _FEAT if c in pq.read_metadata(bt.CLEAN_PANEL).schema.names]
    df = pd.read_parquet(bt.CLEAN_PANEL, columns=cols,
                         filters=[("ticker", "in", list(keep))])
    return df.sort_values(["ticker", "tradeDate"]).reset_index(drop=True)


def _eligible(rows: pd.DataFrame, start: str, end: str | None = None) -> pd.DataFrame:
    elig = (rows["side"].notna() & rows["av_matched"].astype(bool)
            & rows["fwd_available_21"].astype(bool) & (rows["raw_close"] >= 1.0)
            & (rows["av_fwd_21_total"].abs() <= 5.0) & (rows["tradeDate"] >= pd.Timestamp(start)))
    if end is not None:   # OOS upper bound (pre-2018 leak-guard window)
        elig &= (rows["tradeDate"] <= pd.Timestamp(end))
    f = rows[elig].copy()
    f["sign"] = f["side"].astype(str).map(signal_sign).astype(int)
    return f[["ticker", "tradeDate", "side", "sign", "raw_close", "av_fwd_21_total"]].reset_index(drop=True)


def _refire(rows: pd.DataFrame, opts: ConsensusOpts, start: str) -> pd.DataFrame:
    """Recompute the consensus side per ticker with perturbed opts (thesis-locked structure),
    then take the eligible fires. The features (putP/.../skewDelta) are already in `rows`."""
    out = []
    for _, g in rows.groupby("ticker", sort=False):
        g = compute_consensus(g.sort_values("tradeDate"), opts)
        out.append(g)
    refired = pd.concat(out, ignore_index=True)
    return _eligible(refired, start)


# ── scoring: walk-forward Sharpe + maxDD on FIXED fold edges ──────────────

def _map_avsym(fires: pd.DataFrame, smap: dict) -> pd.DataFrame:
    f = fires.copy()
    f["av_symbol"] = f["ticker"].astype(str).str.upper().map(lambda t: (smap.get(t) or ("", ""))[0])
    return f[f["av_symbol"].astype(bool)].reset_index(drop=True)


def _wf_score(fires: pd.DataFrame, bars: dict, exit_policy, fold_edges, cost_bps: float) -> dict:
    """Walk `fires` over pre-loaded `bars`, bucket by the FIXED fold_edges, score each fold's
    monthly book (Sharpe + maxDD) + pooled + the 2021-01 tail. Returns the standard score dict."""
    rets, reasons, offs = bt.walk_loaded(fires, bars, exit_policy)
    net = fires["sign"].to_numpy(float) * rets - cost_bps / 1e4
    dates = fires["tradeDate"]
    fid = np.searchsorted(fold_edges, dates.values, side="right")
    per_fold = [ea._book_metrics(dates[fid == i], net[fid == i]) for i in range(1, len(fold_edges))]
    return {"pooled": ea._book_metrics(dates, net), "per_fold": per_fold,
            "jan2021_bps": round(ea._month_return(dates, net, "2021-01") * 1e4, 1),
            "n_fires": int(len(fires)), "mean_hold_bd": round(float(np.mean(offs)), 1),
            "net": net, "dates": dates}


def _beats(score: dict, base: dict, dd_bound: float, n_folds: int) -> tuple[bool, int]:
    """Walk-forward promotion test vs the baseline: beats baseline Sharpe on a MAJORITY of folds,
    within the DD bound, keeping >=EDGE_KEEP of the baseline mean edge."""
    folds_beat = sum(1 for a, b in zip(score["per_fold"], base["per_fold"])
                     if np.isfinite(a["sharpe"]) and np.isfinite(b["sharpe"]) and a["sharpe"] >= b["sharpe"])
    maj = (n_folds + 1) // 2
    within_dd = np.isfinite(score["pooled"]["maxdd"]) and score["pooled"]["maxdd"] >= dd_bound
    edge_kept = (score["pooled"]["mean_bps"] >= EDGE_KEEP * base["pooled"]["mean_bps"]
                 if base["pooled"]["mean_bps"] > 0 else True)
    beats_pooled = score["pooled"]["sharpe"] > base["pooled"]["sharpe"]
    return (folds_beat >= maj and within_dd and edge_kept and beats_pooled), folds_beat


def _oos_holdout(best: dict, rows: pd.DataFrame, smap: dict, oos_start: str, oos_end: str,
                 cost_bps: float) -> dict:
    """PRE-2018 LEAK-GUARD: re-score the CHOSEN variant on the held-out OOS window the search
    never touched (warm-up floor .. oos_end). Reconstructs the variant's fires in that window
    (entry re-fire / universe filter / plain) and walks its exit policy. Confirmation only."""
    kind = best["kind"]
    if kind == "entry":
        of = _refire(rows, best["opts"], oos_start)
        of = _map_avsym(of[of["tradeDate"] <= pd.Timestamp(oos_end)].reset_index(drop=True), smap)
    else:
        of = _map_avsym(_eligible(rows, oos_start, oos_end), smap)
        if kind == "universe":
            of = best["filt"](of).reset_index(drop=True)
    if len(of) < 50:
        return {"status": "not_available", "window": [str(oos_start), str(oos_end)],
                "n_fires": int(len(of)), "reason": "too few OOS fires"}
    of, obars = bt.prepare_bars(of, ea._UNION)
    rets, _, _ = bt.walk_loaded(of, obars, best["exit"])
    net = of["sign"].to_numpy(float) * rets - cost_bps / 1e4
    m = ea._book_metrics(of["tradeDate"], net)
    return {"status": "ok", "window": [str(oos_start), str(oos_end)], "n_fires": int(len(of)),
            "oos_sharpe": m["sharpe"], "oos_mean_bps": round(m["mean_bps"], 1), "oos_maxdd": m["maxdd"]}


# ── the closed loop ────────────────────────────────────────────────────────

def optimise(thesis_id: str = "skew_consensus_v22_novix", n_tickers: int = 600, n_folds: int = 5,
             start: str = SPLIT_CUTOFF, dd_bound: float = -0.25, cost_bps: float = 20.0,
             do_entry: bool = True, write: bool = True) -> dict:
    t0 = time.time()
    smap = _load_symbol_map()
    rows = _sample_panel(n_tickers)
    base_fires = _map_avsym(_eligible(rows, start), smap)
    base_fires, bars = bt.prepare_bars(base_fires, ea._UNION)   # load OHLC + indicators ONCE
    # FIXED fold edges from the baseline fires' date quantiles (shared by every candidate)
    qs = np.linspace(0, 1, n_folds + 1)
    fold_edges = np.quantile(base_fires["tradeDate"].astype("int64"), qs).astype("datetime64[ns]")
    fold_edges[0] = base_fires["tradeDate"].min().to_datetime64()
    fold_edges[-1] = base_fires["tradeDate"].max().to_datetime64() + np.timedelta64(1, "D")

    log = []                                   # EVERY candidate (the cumulative MT search record)

    def evaluate(fires, exit_policy, name, kind):
        sc = _wf_score(fires, bars, exit_policy, fold_edges, cost_bps)
        log.append({"name": name, "kind": kind, "n_fires": sc["n_fires"],
                    "pooled_sharpe": sc["pooled"]["sharpe"], "pooled_maxdd": sc["pooled"]["maxdd"],
                    "pooled_mean_bps": round(sc["pooled"]["mean_bps"], 1), "jan2021_bps": sc["jan2021_bps"],
                    "mean_hold_bd": sc["mean_hold_bd"]})
        return sc

    base = evaluate(base_fires, BASELINE_EXIT, "baseline_v22+6b_exits", "baseline")
    best = {"name": "baseline_v22+6b_exits", "kind": "baseline", "score": base,
            "fires": base_fires, "exit": BASELINE_EXIT}

    # ── stage 1: EXIT variants (cheap, wide) — reuse baseline fires + bars ──
    for name, exitp in exit_candidates():
        sc = evaluate(base_fires, exitp, name, "exit")
        ok, _ = _beats(sc, base, dd_bound, n_folds)
        if ok and sc["pooled"]["sharpe"] > best["score"]["pooled"]["sharpe"]:
            best = {"name": name, "kind": "exit", "score": sc, "fires": base_fires, "exit": exitp}

    # ── stage 2: UNIVERSE filters (cheap) — on the best-so-far exit ──
    for name, filt in universe_candidates():
        fsub = filt(base_fires).reset_index(drop=True)
        if len(fsub) < 200:
            continue
        sc = evaluate(fsub, best["exit"], name, "universe")
        ok, _ = _beats(sc, base, dd_bound, n_folds)
        if ok and sc["pooled"]["sharpe"] > best["score"]["pooled"]["sharpe"]:
            best = {"name": name, "kind": "universe", "score": sc, "fires": fsub,
                    "exit": best["exit"], "filt": filt}

    # ── stage 3: ENTRY re-fires (expensive, sparse) — on the best-so-far exit ──
    if do_entry:
        for name, opts in entry_candidates():
            fre = _map_avsym(_refire(rows, opts, start), smap)
            if len(fre) < 200:
                continue
            sc = evaluate(fre, best["exit"], name, "entry")
            ok, _ = _beats(sc, base, dd_bound, n_folds)
            if ok and sc["pooled"]["sharpe"] > best["score"]["pooled"]["sharpe"]:
                best = {"name": name, "kind": "entry", "score": sc, "fires": fre,
                        "exit": best["exit"], "opts": opts}

    runtime = round(time.time() - t0, 1)

    # ── MT / DSR penalty: cumulative trials = 6b's 12 + everything tried here ──
    n_trials = PRIOR_TRIALS + len(log)
    bsc = best["score"]
    daily = (pd.DataFrame({"d": pd.to_datetime(np.asarray(bsc["dates"])),
                           "net": np.asarray(bsc["net"], float)})
             .dropna().groupby("d")["net"].mean().sort_index())
    dsr = deflated_sharpe(daily, n_trials=n_trials)
    prob_real = float(dsr.get("dsr_probability_real", 0.0))
    improved = best["name"] != "baseline_v22+6b_exits"
    beats_base, folds_beat = _beats(bsc, base, dd_bound, n_folds) if improved else (False, 0)
    mt_survives = prob_real >= DSR_PROB_BAR

    # ── PRE-2018 LEAK-GUARD: confirm the chosen variant on the held-out OOS (search never saw it) ──
    oos_start = warmup_start_date(rows["tradeDate"].min())
    holdout = _oos_holdout(best, rows, smap, oos_start, OOS_END, cost_bps)
    holdout["primary_sharpe"] = bsc["pooled"]["sharpe"]
    holdout["primary_mean_bps"] = round(bsc["pooled"]["mean_bps"], 1)
    holdout["same_sign_as_primary"] = bool(
        holdout.get("status") == "ok"
        and (holdout["oos_mean_bps"] > 0) == (bsc["pooled"]["mean_bps"] > 0))
    holdout["confirms"] = bool(holdout.get("status") == "ok" and holdout.get("oos_mean_bps", 0) > 0
                               and holdout["same_sign_as_primary"] and holdout.get("oos_sharpe", 0) > 0)

    promoted = bool(improved and beats_base and mt_survives and holdout["confirms"])
    verdict = ("PROMOTE (survives MT + pre-2018 leak-guard)" if promoted else
               "REJECTED BY PRE-2018 LEAK-GUARD (held on PRIMARY, failed the pre-2018 OOS)"
               if (improved and beats_base and mt_survives and not holdout["confirms"]) else
               "NO VALIDATED IMPROVEMENT — v22+exits is near a local optimum")

    result = {
        "thesis_id": thesis_id, "n_tickers": n_tickers, "n_fires_baseline": int(len(base_fires)),
        "start": start, "n_folds": n_folds, "dd_bound": dd_bound, "cost_bps": cost_bps,
        "search_space": {"exit": len(exit_candidates()), "universe": len(universe_candidates()),
                         "entry": len(entry_candidates()) if do_entry else 0},
        "n_candidates_this_run": len(log), "prior_trials_6b": PRIOR_TRIALS,
        "cumulative_trials": n_trials,
        "baseline": {"sharpe": base["pooled"]["sharpe"], "maxdd": base["pooled"]["maxdd"],
                     "mean_bps": round(base["pooled"]["mean_bps"], 1), "jan2021_bps": base["jan2021_bps"]},
        "best": {"name": best["name"], "kind": best["kind"], "exit": list(best["exit"]),
                 "sharpe": bsc["pooled"]["sharpe"], "maxdd": bsc["pooled"]["maxdd"],
                 "mean_bps": round(bsc["pooled"]["mean_bps"], 1), "jan2021_bps": bsc["jan2021_bps"],
                 "folds_beat_baseline": folds_beat,
                 "delta_sharpe": round(bsc["pooled"]["sharpe"] - base["pooled"]["sharpe"], 3),
                 "delta_maxdd": round(bsc["pooled"]["maxdd"] - base["pooled"]["maxdd"], 4)},
        "mt_penalty": {"cumulative_trials": n_trials, "deflated_prob_real": round(prob_real, 4),
                       "deflated_pvalue": dsr.get("dsr_pvalue"), "annualized_sharpe": dsr.get("annualized_sharpe"),
                       "expected_max_sharpe_under_null": dsr.get("expected_max_sharpe_under_null"),
                       "bar": DSR_PROB_BAR, "survives": mt_survives},
        "search_window": {"start": str(start), "end": None, "name": "PRIMARY (>=2018)"},
        "holdout": holdout,
        "promoted": promoted, "verdict": verdict, "log": log, "runtime_s": runtime,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    if write:
        _write_report(result)
        _write_report_html(thesis_id, result)
    _print(result)
    return result


# ── reporting ──────────────────────────────────────────────────────────────

def _holdout_txt(h: dict) -> str:
    if h.get("status") != "ok":
        return (f"  pre-2018 OOS: {h.get('status')} ({h.get('reason', '')}) "
                f"— n_fires {h.get('n_fires', 0):,}")
    return (f"  pre-2018 OOS ({h['window'][0]}..{h['window'][1]}): n_fires {h['n_fires']:,} | "
            f"Sharpe {h['oos_sharpe']} | mean {h['oos_mean_bps']}bps | maxDD {h['oos_maxdd']} | "
            f"same-sign {h['same_sign_as_primary']} | CONFIRMS {h['confirms']}")


def _write_report(r: dict) -> None:
    REPORTS.mkdir(parents=True, exist_ok=True)
    MUT_JSON.write_text(json.dumps(r, indent=2, default=str), encoding="utf-8")
    b, bs = r["baseline"], r["best"]
    L = ["=" * 96,
         "MUTATION AGENT (#11) — drawdown-constrained, MT-disciplined alpha optimiser (thesis-locked)",
         "=" * 96,
         f"sample {r['n_tickers']} tickers ({r['n_fires_baseline']:,} baseline fires) | {r['n_folds']} "
         f"walk-forward folds from {r['start']} | DD bound {r['dd_bound']} | cost {r['cost_bps']:.0f}bps "
         f"| {r['runtime_s']}s",
         f"search space: {r['search_space']['exit']} exit (cheap) + {r['search_space']['universe']} universe "
         f"+ {r['search_space']['entry']} entry (expensive) = {r['n_candidates_this_run']} candidates this run",
         f"cumulative trials (MT count): {r['prior_trials_6b']} (6b) + {r['n_candidates_this_run']} = "
         f"{r['cumulative_trials']}",
         "",
         f"baseline (v22 + 6b exits): Sharpe {b['sharpe']} | maxDD {b['maxdd']} | mean {b['mean_bps']}bps "
         f"| 2021-01 {b['jan2021_bps']}bps",
         f"best variant: {bs['name']} ({bs['kind']})",
         f"  Sharpe {bs['sharpe']} (Δ{bs['delta_sharpe']:+}) | maxDD {bs['maxdd']} (Δ{bs['delta_maxdd']:+}) "
         f"| mean {bs['mean_bps']}bps | folds beating baseline {bs['folds_beat_baseline']}/{r['n_folds']}",
         "",
         "-- MULTIPLE-TESTING / DSR PENALTY " + "-" * 60,
         f"  cumulative trials n={r['mt_penalty']['cumulative_trials']} -> expected max Sharpe under null "
         f"{r['mt_penalty']['expected_max_sharpe_under_null']}",
         f"  best variant annualized Sharpe {r['mt_penalty']['annualized_sharpe']} -> deflated prob-real "
         f"{r['mt_penalty']['deflated_prob_real']} (bar {r['mt_penalty']['bar']}) | "
         f"survives MT: {r['mt_penalty']['survives']}",
         "",
         "-- PRE-2018 LEAK-GUARD (search held out the pre-2018 OOS) " + "-" * 37,
         _holdout_txt(r["holdout"]),
         "",
         f"VERDICT: {r['verdict']}",
         "",
         "-- ALL CANDIDATES (logged; feeds the cumulative MT count) " + "-" * 36,
         f"  {'candidate':<28}|{'kind':>9}|{'fires':>7}|{'Sharpe':>7}|{'maxDD':>8}|{'mean bps':>9}|{'2021-01':>9}"]
    for c in r["log"]:
        L.append(f"  {c['name']:<28}|{c['kind']:>9}|{c['n_fires']:>7,}|{c['pooled_sharpe']:>7}|"
                 f"{c['pooled_maxdd']:>8}|{c['pooled_mean_bps']:>9}|{c['jan2021_bps']:>9}")
    L += ["",
          "READOUT:",
          "  - Promote ONLY a walk-forward improvement (beats baseline Sharpe on a majority of folds,",
          "    within the DD bound, keeping >=50% of edge) that also SURVIVES the cumulative MT/DSR penalty.",
          "  - 'No validated improvement' = v22+exits is near a local optimum; an HONEST and valid outcome.",
          "  - Thesis-locked: the core skew/RR/IV fade is never mutated; only its knobs + exits + universe.",
          ""]
    MUT_REPORT.write_text("\n".join(L), encoding="utf-8")


def _write_report_html(thesis_id: str, r: dict) -> None:
    """Write a 'mutations' block into report.json and re-render the living report."""
    jpath = REPORTS / thesis_id / "report.json"
    if not jpath.exists():
        return
    rep = json.loads(jpath.read_text(encoding="utf-8"))
    rep["mutations"] = {k: r[k] for k in ("search_space", "n_candidates_this_run", "prior_trials_6b",
                                          "cumulative_trials", "baseline", "best", "mt_penalty",
                                          "holdout", "search_window", "promoted", "verdict", "updated_at")}
    rep["mutations"]["top_candidates"] = sorted(
        r["log"], key=lambda c: (c["pooled_sharpe"] if c["pooled_sharpe"] == c["pooled_sharpe"] else -9))[-8:][::-1]
    rep["updated_at"] = r["updated_at"]
    jpath.write_text(json.dumps(rep, indent=2), encoding="utf-8")
    try:
        from . import reporter
        reporter.render(thesis_id)
    except Exception as e:  # noqa: BLE001
        print(f"[mutation] render skipped: {type(e).__name__}")


def _print(r: dict) -> None:
    print(MUT_REPORT.read_text(encoding="utf-8") if MUT_REPORT.exists() else "")
    print(f"wrote {MUT_REPORT} (+ {MUT_JSON}); mutations section -> report.html")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="quant_validator.mutation_agent")
    sub = ap.add_subparsers(dest="cmd", required=True)
    po = sub.add_parser("optimise", help="closed-loop DD-constrained, MT-disciplined search")
    po.add_argument("--thesis_id", default="skew_consensus_v22_novix")
    po.add_argument("--tickers", type=int, default=600)
    po.add_argument("--n-folds", type=int, default=5)
    po.add_argument("--start", default=SPLIT_CUTOFF)
    po.add_argument("--dd-bound", type=float, default=-0.25)
    po.add_argument("--no-entry", action="store_true", help="skip the expensive entry re-fires")
    args = ap.parse_args(argv)
    if args.cmd == "optimise":
        optimise(args.thesis_id, n_tickers=args.tickers, n_folds=args.n_folds, start=args.start,
                 dd_bound=args.dd_bound, do_entry=not args.no_entry)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
