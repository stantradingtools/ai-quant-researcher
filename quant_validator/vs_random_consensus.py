"""quant_validator.vs_random_consensus: Tier B for the Skew_backtest consensus export.

The consensus CSV export is a CANDIDATE PANEL: every signal the 3-stage
consensus logic generated (2011-2026), tagged ACCEPTED or BLOCKED_* with the
filter that fired, each carrying parsed features (m1, m2, stall, divergence,
iv, rr, put, call) and a counterfactual Net% (what the trade returned / would
have returned).

This lets Tier B (constraint-matched random rule search) run WITHOUT the
Phase 1 data adapters: random rules are generated on the same features the
real strategy uses, applied to the candidate panel, and the BEST random rule's
Sharpe is compared to the real selection's Sharpe.

SCOPE / HONESTY NOTE: the candidate panel already passed the core 3-stage
consensus signal logic. So this Tier B tests the SELECTION / FILTER layer
(does the consensus+filter selection beat random rules on the same features),
NOT raw signal generation across the untraded universe. Testing signal
generation vs random needs the non-signal bars, which require Phase 1 data.

Usage:
    python -m quant_validator.vs_random_consensus run <consensus.csv>
    python -m quant_validator.vs_random_consensus run <consensus.csv> --n 5000 --seed 7
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd


# ═══════════════════════════════════════════════════════════════
# Loading + feature parsing

NUMERIC_FEATURES = ["iv", "rr", "put", "call"]
CATEGORICAL_FEATURES = ["m1", "m2", "stall", "divergence"]


def _parse_extra(s: str) -> dict:
    d = {}
    if pd.isna(s):
        return d
    for kv in str(s).split(";"):
        if "=" in kv:
            k, v = kv.split("=", 1)
            d[k.strip()] = v.strip()
    return d


def load_consensus_panel(csv_path: Path) -> pd.DataFrame:
    """Load the consensus export, parse ExtraInputs into feature columns,
    coerce numeric features, return one row per candidate signal."""
    df = pd.read_csv(csv_path)
    parsed = df["ExtraInputs"].apply(_parse_extra).apply(pd.Series)
    for col in NUMERIC_FEATURES:
        if col in parsed.columns:
            parsed[col] = pd.to_numeric(parsed[col], errors="coerce")
    out = pd.concat([df, parsed], axis=1)
    out["Entry"] = pd.to_datetime(out["Entry"], errors="coerce")
    out["net"] = pd.to_numeric(out["Net%"], errors="coerce") / 100.0
    out["is_accepted"] = out["Status"] == "ACCEPTED"
    return out


def span_years(df: pd.DataFrame) -> float:
    return max((df["Entry"].max() - df["Entry"].min()).days / 365.25, 1e-9)


# ═══════════════════════════════════════════════════════════════
# Trade-level Sharpe

def trade_sharpe(net: np.ndarray, years: float, annualize: bool = True) -> float:
    """Trade-level Sharpe of a net-return array (decimal). Annualized by
    realized trade frequency so a rule that finds more good trades scores
    higher — frequency is part of edge."""
    net = net[~np.isnan(net)]
    if net.size < 2:
        return 0.0
    sd = net.std(ddof=1)
    if sd == 0:
        return 0.0
    s = net.mean() / sd
    if annualize:
        trades_per_year = net.size / years
        s *= np.sqrt(trades_per_year)
    return float(s)


# ═══════════════════════════════════════════════════════════════
# Random rule grammar (matched to the consensus feature panel)

def sample_random_rule(df: pd.DataFrame, rng: np.random.Generator) -> dict:
    """Sample a random rule of 1-4 ANDed conditions on the same features the
    consensus logic uses, plus a direction restriction. Constraints matched
    to the real strategy's feature set (BuildAlpha Mistake #2)."""
    n_conditions = int(rng.integers(1, 5))
    conditions = []
    available = CATEGORICAL_FEATURES + NUMERIC_FEATURES
    chosen = rng.choice(available, size=min(n_conditions, len(available)), replace=False)
    for feat in chosen:
        if feat in CATEGORICAL_FEATURES:
            vals = df[feat].dropna().unique().tolist()
            if not vals:
                continue
            target = str(rng.choice(vals))
            conditions.append(("cat", feat, target))
        else:
            col = df[feat].dropna()
            if col.empty:
                continue
            thresh = float(rng.uniform(col.quantile(0.1), col.quantile(0.9)))
            op = str(rng.choice([">", "<"]))
            conditions.append(("num", feat, op, thresh))
    direction = str(rng.choice(["long", "short", "both"]))
    return {"conditions": conditions, "direction": direction}


def apply_rule(df: pd.DataFrame, rule: dict) -> pd.Series:
    """Return a boolean mask of candidates selected by the rule."""
    mask = pd.Series(True, index=df.index)
    for cond in rule["conditions"]:
        if cond[0] == "cat":
            _, feat, target = cond
            mask &= (df[feat] == target)
        else:
            _, feat, op, thresh = cond
            mask &= (df[feat] > thresh) if op == ">" else (df[feat] < thresh)
    if rule["direction"] == "long":
        mask &= df["Direction"].str.contains("LONG", na=False)
    elif rule["direction"] == "short":
        mask &= df["Direction"].str.contains("SHORT", na=False)
    return mask


# ═══════════════════════════════════════════════════════════════
# Tier B runner

def run_tier_b(
    csv_path: Path,
    n_random: int = 2000,
    min_trades: int = 50,
    margin: float = 0.10,
    seed: int = 7,
) -> dict:
    df = load_consensus_panel(csv_path)
    years = span_years(df)

    accepted = df[df["is_accepted"]]
    real_net = accepted["net"].to_numpy()
    real_sharpe = trade_sharpe(real_net, years, annualize=True)
    real_pertrade = trade_sharpe(real_net, years, annualize=False)

    # Full-pool baseline: take EVERY candidate (loosest possible "rule")
    full_pool_sharpe = trade_sharpe(df["net"].to_numpy(), years, annualize=True)

    rng = np.random.default_rng(seed)
    best_random = -np.inf
    best_rule = None
    random_sharpes = []
    valid_rules = 0
    for _ in range(n_random):
        rule = sample_random_rule(df, rng)
        mask = apply_rule(df, rule)
        sel = df.loc[mask, "net"].to_numpy()
        if sel.size < min_trades:
            continue
        valid_rules += 1
        # annualize by THIS rule's own frequency
        sub_years = years  # signals span same window; freq = n/sub_years
        sr = trade_sharpe(sel, sub_years, annualize=True)
        random_sharpes.append(sr)
        if sr > best_random:
            best_random = sr
            best_rule = {"rule": rule, "n_selected": int(sel.size),
                         "mean_net_pct": round(float(np.nanmean(sel) * 100), 3)}

    random_sharpes = np.array(random_sharpes) if random_sharpes else np.array([0.0])
    p95 = float(np.percentile(random_sharpes, 95))
    p50 = float(np.percentile(random_sharpes, 50))
    pct_of_real = float((random_sharpes < real_sharpe).mean() * 100)

    required = best_random * (1 + margin) if best_random > 0 else best_random
    if real_sharpe >= required and real_sharpe > 0:
        verdict = "pass"
    elif real_sharpe >= best_random:
        verdict = "borderline"
    else:
        verdict = "fail"

    return {
        "tier": "B_constraint_matched_consensus",
        "status": "ok",
        "source_csv": csv_path.name,
        "span_years": round(years, 2),
        "n_candidates": int(len(df)),
        "n_accepted": int(len(accepted)),
        "real_annualized_sharpe": round(real_sharpe, 4),
        "real_pertrade_sharpe": round(real_pertrade, 4),
        "full_pool_sharpe": round(full_pool_sharpe, 4),
        "best_random_sharpe": round(best_random, 4),
        "best_random_rule": best_rule,
        "random_sharpe_p50": round(p50, 4),
        "random_sharpe_p95": round(p95, 4),
        "real_percentile_vs_random": round(pct_of_real, 2),
        "n_valid_random_rules": valid_rules,
        "margin_required": margin,
        "real_beats_best_random_by_pct": round((real_sharpe / best_random - 1) * 100, 2) if best_random > 0 else None,
        "verdict": verdict,
    }


def filter_value_table(csv_path: Path) -> dict:
    """Counterfactual value of each filter — RISK-AWARE.

    A filter is judged not only on the mean of what it blocked, but on the
    tail. A filter that blocks slightly-positive-mean trades that carry large
    downside variance is GOOD risk control (e.g. an earnings blackout cutting
    gap risk), even though a naive mean test would call it 'costs alpha'.
    """
    df = load_consensus_panel(csv_path)
    acc = df[df["is_accepted"]]["net"]
    acc_mean = float(acc.mean() * 100)
    acc_std = float(acc.std() * 100)
    acc_worst = float(acc.min() * 100)
    out = {
        "accepted_mean_net_pct": round(acc_mean, 3),
        "accepted_std_pct": round(acc_std, 3),
        "accepted_worst_pct": round(acc_worst, 3),
        "filters": {},
    }
    for status in df["Status"].unique():
        if status == "ACCEPTED":
            continue
        b = df[df["Status"] == status]["net"]
        mean_pct = float(b.mean() * 100)
        std_pct = float(b.std() * 100)
        worst_pct = float(b.min() * 100)
        is_capital = "BUDGET" in status or "SECTOR" in status

        if is_capital:
            assessment = "capital_constraint_not_signal_filter"
        elif mean_pct < 0:
            assessment = "ADDS_VALUE_blocked_losers"
        elif std_pct > 1.5 * acc_std or worst_pct < 1.8 * acc_worst:
            # Blocked positive-mean trades, but they were far riskier than
            # what was kept — good risk control, not an alpha leak.
            assessment = "GOOD_RISK_CONTROL_blocked_high_variance_winners"
        else:
            assessment = "COSTS_ALPHA_blocked_clean_winners"
        out["filters"][status] = {
            "n_blocked": int(len(b)),
            "blocked_mean_net_pct": round(mean_pct, 3),
            "blocked_std_pct": round(std_pct, 3),
            "blocked_worst_pct": round(worst_pct, 3),
            "assessment": assessment,
        }
    return out


# ═══════════════════════════════════════════════════════════════
# CLI

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="quant_validator.vs_random_consensus")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run")
    p.add_argument("csv", help="path to consensus export CSV")
    p.add_argument("--n", type=int, default=2000)
    p.add_argument("--min_trades", type=int, default=50)
    p.add_argument("--margin", type=float, default=0.10)
    p.add_argument("--seed", type=int, default=7)
    args = parser.parse_args(argv)

    if args.cmd == "run":
        csv_path = Path(args.csv)
        tier_b = run_tier_b(csv_path, n_random=args.n, min_trades=args.min_trades,
                            margin=args.margin, seed=args.seed)
        filters = filter_value_table(csv_path)
        print(json.dumps({"tier_b": tier_b, "filter_value": filters}, indent=2))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
