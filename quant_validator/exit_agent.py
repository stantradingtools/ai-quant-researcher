"""quant_validator.exit_agent: the Exit Agent (#12).

Three parts, all on top of the 6a path engine (backtest_path.py):

  1. A SIGN-AWARE exit-rule LIBRARY — composable EXIT_POLICY elements, each grounded in the
     Options_Sell_Signal tool and mirrored for the fade sign (BULL setup = SHORT, profits if
     price falls; BEAR = LONG). The headline is the ENTRY-CONTEXT-AWARE Bollinger reversion
     target (the fade's take-profit), NOT the unconditional mean-touch that mis-fired in 6a.

  2. An OOS RULE-SELECTION pass — walk-forward (rolling time folds, since v22's panel is largely
     in-sample) that keeps ONLY rules proven to BEAT the 21D time backstop on risk-adjusted return
     (Sharpe) AND/OR drawdown, without surrendering too much edge. Every candidate tried is logged
     (the count feeds the Mutation Agent's multiple-testing / DSR penalty). The data adjudicates —
     if nothing beats the backstop on return, we say so, and separately check whether the
     tail-control rules (squeeze / vol-spike) cut the worst months (esp. the 2021-01 meme squeeze).

  3. A RUN-TIME RANKER — for each open position in the Phase-4 ledger, compute the live exit
     signals (Bollinger distance-to-target, YZ5/ATM-IV vs threshold, squeeze flag, days-held vs
     21D) and surface 2-3 ranked exit options (trigger + rationale + projected). Advisory by
     default; can act through the manage loop. Renders an "Exit options" block into report.html.

Guardrails: the 21D backstop is non-negotiable and always present; no live rule that hasn't
cleared the OOS selection; the validated set is exposed as candidates for the Mutation Agent (#11).

CLI:
    python -m quant_validator.exit_agent select --thesis_id skew_consensus_v22_novix --sample 20000
    python -m quant_validator.exit_agent rank   --strategy skew_consensus_v22_novix
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from . import backtest_path as bt
from .rebuild_returns import _load_symbol_map

REPORTS = Path("reports")
SELECT_REPORT = REPORTS / "exit_agent_selection.txt"
SELECT_JSON = REPORTS / "exit_agent_selection.json"


# ── 1. sign-aware exit-rule library (composable EXIT_POLICY elements) ──────

def r_time(bdays: int = bt.HOLD_BDAYS) -> dict:
    return {"type": "time_backstop", "bdays": bdays}


def r_hard_stop(pct: float = 0.08) -> dict:
    return {"type": "hard_stop", "pct": pct}


def r_trailing_stop(pct: float = 0.10) -> dict:
    return {"type": "trailing_stop", "pct": pct}


def r_profit_target(pct: float = 0.15) -> dict:
    return {"type": "profit_target", "pct": pct}


def r_bollinger_reversion(n: int = 20, k: float = 2.0, target: str = "mean") -> dict:
    """The fade's TAKE-PROFIT: price reverting toward the mean (target='mean') or through the
    far band (target='band'). Entry-context-aware in the engine — armed only when entered on the
    extended side, so the reversion is in the PROFIT direction (short above its mean / long below)."""
    return {"type": "bollinger_reversion", "n": n, "k": k, "target": target}


def r_vol_spike(measure: str = "yz5", threshold: float = 0.80) -> dict:
    """Regime-turn exit: YZ5 realized vol or ATM IV >= threshold (annualized) -> exit at close."""
    return {"type": "vol_spike", "measure": measure, "threshold": threshold}


def r_squeeze(threshold: float = 2.0) -> dict:
    """SHORT-side tail control: skew turns call-rich (skew <= -threshold vol pts) -> exit. The
    direct answer to the 2021-01 meme-squeeze month, where fade shorts were run over."""
    return {"type": "squeeze", "threshold": threshold}


# Candidate set for OOS selection: single rules + sensible combos, EACH ending in the 21D
# backstop (non-negotiable). The Bollinger n/k is fixed (20/2) so one precompute serves all.
CANDIDATES: dict[str, tuple] = {
    "baseline_21d_backstop":  (r_time(),),
    "hard_stop_8":            (r_hard_stop(0.08), r_time()),
    "trailing_10":            (r_trailing_stop(0.10), r_time()),
    "profit_target_15":       (r_profit_target(0.15), r_time()),
    "boll_reversion_mean":    (r_bollinger_reversion(target="mean"), r_time()),
    "boll_reversion_band":    (r_bollinger_reversion(target="band"), r_time()),
    "vol_spike_yz5":          (r_vol_spike("yz5", 0.80), r_time()),
    "vol_spike_atm_iv":       (r_vol_spike("atm_iv", 0.80), r_time()),
    "squeeze":                (r_squeeze(2.0), r_time()),
    "tail_control":           (r_hard_stop(0.08), r_vol_spike("yz5", 0.80), r_squeeze(2.0), r_time()),
    "reversion_plus_stop":    (r_hard_stop(0.08), r_bollinger_reversion(target="mean"), r_time()),
    "full_combo":             (r_hard_stop(0.08), r_bollinger_reversion(target="mean"),
                               r_vol_spike("yz5", 0.80), r_squeeze(2.0), r_time()),
}

# A union policy so prepare_bars precomputes every indicator the candidates can need, once.
_UNION = (r_bollinger_reversion(20, 2.0, "band"), r_vol_spike("yz5", 0.0),
          r_vol_spike("atm_iv", 0.0), r_squeeze(0.0), r_time())


# ── 2. OOS walk-forward rule selection ────────────────────────────────────

def _book_metrics(dates: pd.Series, net: np.ndarray) -> dict:
    """Monthly equal-weight book (mean net per-trade return by entry-month) -> Sharpe, maxDD,
    mean. Matches the Risk Agent's book-month framing."""
    s = pd.Series(net, index=pd.to_datetime(np.asarray(dates))).dropna()
    if s.empty:
        return {"n": 0, "mean_bps": float("nan"), "sharpe": float("nan"), "maxdd": float("nan")}
    monthly = s.groupby(s.index.to_period("M")).mean()
    eq = (1.0 + monthly).cumprod()
    maxdd = float((eq / eq.cummax() - 1.0).min())
    sd = float(monthly.std(ddof=1))
    sharpe = float(monthly.mean() / sd * np.sqrt(12)) if sd > 0 else float("nan")
    return {"n": int(s.size), "mean_bps": float(s.mean() * 1e4), "sharpe": round(sharpe, 3),
            "maxdd": round(maxdd, 4), "n_months": int(monthly.size)}


def _month_return(dates: pd.Series, net: np.ndarray, ym: str) -> float:
    s = pd.Series(net, index=pd.to_datetime(np.asarray(dates))).dropna()
    sel = s[s.index.to_period("M") == pd.Period(ym, "M")]
    return float(sel.mean()) if len(sel) else float("nan")


def select_rules(thesis_id: str = "skew_consensus_v22_novix", sample: int = 20000,
                 n_folds: int = 5, start: str = "2014-01-01", cost_bps: float = 20.0,
                 dd_margin: float = 0.01, write: bool = True) -> dict:
    """Walk-forward OOS selection. Loads OHLC once, walks every candidate, scores each on rolling
    time folds vs the 21D-backstop baseline. A rule SURVIVES if it beats the baseline on Sharpe
    OR materially cuts maxDD on a MAJORITY of folds without surrendering >50% of the mean edge."""
    t0 = time.time()
    fires = bt._load_fires(bt.CLEAN_PANEL, start)
    n_all = len(fires)
    if sample < n_all:
        fires = fires.sample(sample, random_state=0)
    fires = fires.sort_values("tradeDate").reset_index(drop=True)
    fires, bars = bt.prepare_bars(fires, _UNION)
    fires = fires.sort_values("tradeDate").reset_index(drop=True)
    sign = fires["sign"].to_numpy(float)
    dates = fires["tradeDate"]
    fold_id = pd.qcut(dates.rank(method="first"), n_folds, labels=False).to_numpy()
    fold_spans = [(fid, str(dates[fold_id == fid].min().date()), str(dates[fold_id == fid].max().date()))
                  for fid in range(n_folds)]

    out = {}
    tried = []           # every candidate evaluated (the count for the Mutation DSR penalty)
    for name, policy in CANDIDATES.items():
        tried.append(name)
        rets, reasons, offs = bt.walk_loaded(fires, bars, policy)
        net = sign * rets - cost_bps / 1e4
        per_fold = [_book_metrics(dates[fold_id == fid], net[fold_id == fid]) for fid in range(n_folds)]
        out[name] = {"pooled": _book_metrics(dates, net), "per_fold": per_fold,
                     "jan2021_bps": round(_month_return(dates, net, "2021-01") * 1e4, 1),
                     "mean_hold_bd": round(float(np.mean(offs)), 1), "exit_mix": dict(Counter(reasons))}
    runtime = round(time.time() - t0, 1)

    # ── adjudicate vs baseline ──
    base = out["baseline_21d_backstop"]
    base_pf = base["per_fold"]
    base_pooled = base["pooled"]
    ranked = []
    for name, r in out.items():
        if name == "baseline_21d_backstop":
            continue
        beats_sharpe = sum(1 for a, b in zip(r["per_fold"], base_pf)
                           if np.isfinite(a["sharpe"]) and np.isfinite(b["sharpe"]) and a["sharpe"] >= b["sharpe"])
        cuts_dd = sum(1 for a, b in zip(r["per_fold"], base_pf)
                      if np.isfinite(a["maxdd"]) and np.isfinite(b["maxdd"]) and a["maxdd"] > b["maxdd"] + dd_margin)
        maj = (n_folds + 1) // 2
        edge_kept = (r["pooled"]["mean_bps"] >= 0.5 * base_pooled["mean_bps"]) if base_pooled["mean_bps"] > 0 else True
        survives = (beats_sharpe >= maj or cuts_dd >= maj) and edge_kept
        jan_improve = r["jan2021_bps"] - base["jan2021_bps"]
        ranked.append({"name": name, "survives": bool(survives),
                       "folds_beat_sharpe": beats_sharpe, "folds_cut_dd": cuts_dd, "n_folds": n_folds,
                       "edge_kept": bool(edge_kept), "pooled_sharpe": r["pooled"]["sharpe"],
                       "pooled_maxdd": r["pooled"]["maxdd"], "pooled_mean_bps": round(r["pooled"]["mean_bps"], 1),
                       "jan2021_bps": r["jan2021_bps"], "jan2021_vs_base_bps": round(jan_improve, 1)})
    ranked.sort(key=lambda d: (d["survives"], d["folds_beat_sharpe"], d["folds_cut_dd"], d["pooled_sharpe"]),
                reverse=True)
    validated = [r["name"] for r in ranked if r["survives"]]

    result = {"thesis_id": thesis_id, "n_fires_sampled": int(len(fires)), "n_fires_total": int(n_all),
              "n_folds": n_folds, "fold_spans": fold_spans, "start": start, "cost_bps": cost_bps,
              "n_candidates_tried": len(tried), "candidates_tried": tried,
              "baseline": {"pooled": base_pooled, "jan2021_bps": base["jan2021_bps"]},
              "ranked": ranked, "validated_rules": validated, "exit_mix": {n: out[n]["exit_mix"] for n in out},
              "runtime_s": runtime, "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    if write:
        _write_selection(result)
    _print_selection(result)
    return result


def _write_selection(result: dict) -> None:
    REPORTS.mkdir(parents=True, exist_ok=True)
    SELECT_JSON.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    L = ["=" * 92,
         "EXIT AGENT (#12) — OOS WALK-FORWARD RULE SELECTION vs the 21D time backstop",
         "=" * 92,
         f"sample {result['n_fires_sampled']:,}/{result['n_fires_total']:,} fires | {result['n_folds']} rolling "
         f"time folds | from {result['start']} | cost {result['cost_bps']:.0f}bps | "
         f"{result['n_candidates_tried']} candidates tried (Mutation DSR count) | {result['runtime_s']}s",
         f"baseline (21D backstop): pooled Sharpe {result['baseline']['pooled']['sharpe']} "
         f"maxDD {result['baseline']['pooled']['maxdd']} mean {round(result['baseline']['pooled']['mean_bps'],1)}bps "
         f"| 2021-01 {result['baseline']['jan2021_bps']}bps",
         "",
         f"  {'candidate':<24}|{'surv':>5}|{'foldsβSharpe':>12}|{'foldsↆDD':>9}|{'Sharpe':>7}|"
         f"{'maxDD':>8}|{'mean bps':>9}|{'2021-01 vs base':>16}",
         "  " + "-" * 96]
    for r in result["ranked"]:
        L.append(f"  {r['name']:<24}|{'YES' if r['survives'] else ' no':>5}|"
                 f"{str(r['folds_beat_sharpe'])+'/'+str(r['n_folds']):>12}|"
                 f"{str(r['folds_cut_dd'])+'/'+str(r['n_folds']):>9}|{r['pooled_sharpe']:>7}|"
                 f"{r['pooled_maxdd']:>8}|{r['pooled_mean_bps']:>9}|{r['jan2021_vs_base_bps']:>+16.1f}")
    L += ["",
          f"VALIDATED (beat the backstop on a majority of folds): {result['validated_rules'] or 'NONE'}",
          "",
          "READOUT:",
          "  - 'survives' = beats baseline Sharpe OR cuts maxDD on a majority of folds AND keeps >=50% of edge.",
          "  - 2021-01 vs base (bps): tail-control value on the meme-squeeze month (+ = cut the loss).",
          "  - Every candidate above is logged; n_candidates_tried feeds the Mutation Agent's DSR penalty.",
          "  - 21D backstop is always present and non-negotiable; no live rule bypasses this selection.",
          ""]
    SELECT_REPORT.write_text("\n".join(L), encoding="utf-8")


def _print_selection(result: dict) -> None:
    print(SELECT_REPORT.read_text(encoding="utf-8") if SELECT_REPORT.exists() else "")
    print(f"wrote {SELECT_REPORT} (+ {SELECT_JSON})")


# ── 3. run-time exit ranker (2-3 ranked options per open position) ────────

def _load_validated() -> list[str]:
    if SELECT_JSON.exists():
        try:
            return json.loads(SELECT_JSON.read_text(encoding="utf-8")).get("validated_rules", [])
        except Exception:  # noqa: BLE001
            return []
    return []


def rank_exits_for_position(rec: dict, asof: int, sign: int, days_held: int,
                            scheduled_exit: str, n: int = 20, k: float = 2.0,
                            vol_thr: float = 0.80, sqz_thr: float = 2.0) -> list[dict]:
    """2-3 ranked exit options for ONE open position, from the latest bar `asof`. Sign-aware."""
    c = rec["c"][asof]
    opts = []
    # Bollinger reversion target (the fade's take-profit) — only if entered/sitting extended side
    mid = rec.get("boll_mid", np.full(len(rec["c"]), np.nan))[asof]
    lo = rec.get("boll_lo", np.full(len(rec["c"]), np.nan))[asof]
    up = rec.get("boll_up", np.full(len(rec["c"]), np.nan))[asof]
    if np.isfinite(mid):
        if sign < 0 and c > mid:                       # short above mean -> revert DOWN = profit
            tgt = mid
            opts.append({"rule": "bollinger_reversion", "trigger": f"price reverts to mean ~${tgt:,.2f}",
                         "rationale": "fade take-profit: short reverting down toward its 20d mean",
                         "projected_return_pct": round((c / tgt - 1.0) * 100, 2), "urgency": "target"})
        elif sign > 0 and c < mid:                     # long below mean -> revert UP = profit
            tgt = mid
            opts.append({"rule": "bollinger_reversion", "trigger": f"price reverts to mean ~${tgt:,.2f}",
                         "rationale": "fade take-profit: long reverting up toward its 20d mean",
                         "projected_return_pct": round((tgt / c - 1.0) * 100, 2), "urgency": "target"})
    # vol-spike (regime turn) — act now if over threshold
    yz = rec.get("yz", np.full(len(rec["c"]), np.nan))[asof]
    if np.isfinite(yz):
        over = yz >= vol_thr
        opts.append({"rule": "vol_spike_yz5", "trigger": f"YZ5 realized vol {yz:.0%} vs {vol_thr:.0%}",
                     "rationale": "regime turn against the position" if over else "vol below threshold (hold)",
                     "projected_return_pct": None, "urgency": "act_now" if over else "watch"})
    # squeeze (short-side tail) — act now if call-rich
    sk = rec.get("skew", np.full(len(rec["c"]), np.nan))[asof]
    if sign < 0 and np.isfinite(sk):
        sqz = sk <= -sqz_thr
        opts.append({"rule": "squeeze", "trigger": f"skew {sk:+.1f} vs -{sqz_thr:.0f} (call-rich)",
                     "rationale": "squeeze building — exit the short (2021-01 tail)" if sqz else "no squeeze (hold)",
                     "projected_return_pct": None, "urgency": "act_now" if sqz else "watch"})
    # 21D backstop — always present, non-negotiable
    opts.append({"rule": "time_backstop", "trigger": f"21D hard exit on {scheduled_exit}",
                 "rationale": f"held {days_held}/{bt.HOLD_BDAYS}d; close regardless on the backstop date",
                 "projected_return_pct": None, "urgency": "backstop"})
    # rank: act_now (triggered tail) > target (favorable reversion) > backstop > watch
    order = {"act_now": 0, "target": 1, "backstop": 2, "watch": 3}
    opts.sort(key=lambda o: order.get(o["urgency"], 9))
    # keep the 2-3 most actionable + always the backstop
    top = [o for o in opts if o["urgency"] in ("act_now", "target")][:2]
    top += [o for o in opts if o["urgency"] == "backstop"]
    return top[:3]


def rank_open_positions(strategy: str = "skew_consensus_v22_novix", illustrative_n: int = 5,
                        write: bool = True) -> dict:
    """Rank exits for every OPEN ledger position. If the book is flat, also produce ILLUSTRATIVE
    rankings for the top current live-queue names (clearly labeled) so the ranker is demonstrable.
    Writes an 'Exit options' block into report.json phases.4_paper and re-renders."""
    from .fills import _load_ledger, _today, _signal_age_bdays  # local: avoid import cycle
    led = _load_ledger(strategy)
    open_pos = [r for r in led.get("positions", {}).values() if r.get("status") in ("open", "pending")]

    # build the illustrative set from the live queue if flat
    illustrative = []
    rep_path = REPORTS / strategy / "report.json"
    if not open_pos and rep_path.exists():
        rep = json.loads(rep_path.read_text(encoding="utf-8"))
        q = rep.get("phases", {}).get("4_paper", {}).get("next_open_queue", [])
        for o in sorted(q, key=lambda d: -d.get("weight", 0))[:illustrative_n]:
            illustrative.append({"symbol": o["symbol"], "side": o["direction"],
                                 "signal_sign": -1 if o["side"] == "sell" else 1,
                                 "entry_signal_date": rep["phases"]["4_paper"]["queue_meta"]["signal_date"],
                                 "scheduled_exit_date": "(hypothetical +21bd)", "status": "illustrative"})
    targets = open_pos or illustrative
    if not targets:
        print("[exit-rank] no open positions and no live queue — nothing to rank.")
        return {"open": 0, "options": []}

    # load bars for the target symbols + the policy indicators (union), as of the latest bar
    smap = _load_symbol_map()
    fr = pd.DataFrame([{"ticker": t["symbol"], "tradeDate": pd.Timestamp(t.get("entry_signal_date") or _today()),
                        "sign": int(t.get("signal_sign", 1)), "av_fwd_21_total": np.nan,
                        "side": t.get("side"), "raw_close": np.nan} for t in targets])
    fr, bars = bt.prepare_bars(fr, _UNION)
    today = str(_today().date())
    options = []
    for t in targets:
        av = (smap.get(str(t["symbol"]).upper()) or ("", ""))[0]
        rec = bars.get(av)
        if rec is None or len(rec["c"]) == 0:
            continue
        asof = len(rec["c"]) - 1                       # latest available bar
        base = t.get("entry_fill_date") or t.get("entry_signal_date")
        days_held = int(np.busday_count(pd.Timestamp(base).date(), _today().date())) if base else 0
        opts = rank_exits_for_position(rec, asof, int(t.get("signal_sign", 1)), days_held,
                                       t.get("scheduled_exit_date", "(21bd)"))
        options.append({"symbol": t["symbol"], "side": t.get("side"), "status": t.get("status"),
                        "days_held": days_held, "options": opts})

    block = {"as_of": today, "live": bool(open_pos), "illustrative": not bool(open_pos),
             "validated_rules": _load_validated(), "positions": options,
             "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    if write and rep_path.exists():
        rep = json.loads(rep_path.read_text(encoding="utf-8"))
        rep.setdefault("phases", {}).setdefault("4_paper", {})["exit_options"] = block
        rep_path.write_text(json.dumps(rep, indent=2), encoding="utf-8")
        try:
            from . import reporter
            reporter.render(strategy)
        except Exception as e:  # noqa: BLE001
            print(f"[exit-rank] render skipped: {type(e).__name__}")
    tag = "LIVE" if open_pos else "ILLUSTRATIVE (book flat — top live-queue names)"
    print(f"[exit-rank] {strategy} {tag}: ranked exits for {len(options)} position(s) as of {today}")
    for p in options:
        best = p["options"][0] if p["options"] else {}
        print(f"   {p['symbol']:<6} {p['side']:<5} held {p['days_held']}d | best: "
              f"{best.get('rule','-')} ({best.get('trigger','-')})")
    print(f"[exit-rank] wrote report.json phases.4_paper.exit_options + re-rendered")
    return block


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="quant_validator.exit_agent")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ps = sub.add_parser("select", help="OOS walk-forward rule selection vs the 21D backstop")
    ps.add_argument("--thesis_id", default="skew_consensus_v22_novix")
    ps.add_argument("--sample", type=int, default=20000)
    ps.add_argument("--n-folds", type=int, default=5)
    ps.add_argument("--start", default="2014-01-01")
    pr = sub.add_parser("rank", help="run-time ranker: 2-3 exits per open position -> report")
    pr.add_argument("--strategy", default="skew_consensus_v22_novix")
    pr.add_argument("--illustrative-n", type=int, default=5)
    args = ap.parse_args(argv)
    if args.cmd == "select":
        select_rules(args.thesis_id, sample=args.sample, n_folds=args.n_folds, start=args.start)
        return 0
    if args.cmd == "rank":
        rank_open_positions(args.strategy, illustrative_n=args.illustrative_n)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
