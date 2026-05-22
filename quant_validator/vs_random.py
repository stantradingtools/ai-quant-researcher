"""quant_validator.vs_random: the Vs. Random gate (Step 6.5).

Implements the Woodriff/BuildAlpha "Vs. Random" robustness test as a
pipeline gate. Answers the question: "Could a random strategy with my
same constraints have produced this Sharpe by luck?"

Three escalating tiers, cheapest first:

  Tier A - Permutation test (ALWAYS RUNS, Mode A + Mode B)
    Holds the asset returns fixed, randomizes the position TIMING under
    the same activity rate and magnitude distribution. Tests whether YOUR
    specific entry timing beats random timing of equal intensity.
    Fully implemented. Works from positions.csv + returns.csv alone.

  Tier B - Constraint-matched random rule search (Mode A, or Mode B with
    a feature matrix + backtest fn supplied)
    Generates random entry/exit rule sets under the SAME constraints as
    the real strategy (maxHoldDays, freshnessWindow, direction, universe),
    backtests each on real data, takes the BEST random Sharpe. Strategy
    must beat best-random by >= margin (default 10%, per BuildAlpha).
    Scaffolded: requires a RuleSpace + backtest callable. Raises a clear
    error if not supplied.

  Tier C - Randomized-data test (Mode A)
    Block-bootstrap / phase-randomize the underlying price series to
    destroy signal structure while preserving statistical properties,
    re-run the strategy. Strategy should NOT perform well on structure-
    destroyed data. Scaffolded.

KEY DESIGN RULE (BuildAlpha Mistake #3): match the fitness metric. Since
the rest of the pipeline optimizes around Sharpe, the Vs. Random
comparison is on Sharpe. If you change the optimized metric, change the
comparison metric here too.

Usage:
    python -m quant_validator.vs_random run --thesis_id <id>
    python -m quant_validator.vs_random run --thesis_id <id> --n 2000 --seed 7

Writes: theses/<id>/results/vs_random.json
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path

import numpy as np
import pandas as pd


ANN = 252
GAMMA = 0.5772156649015329  # Euler-Mascheroni, for completeness/shared use


# ═══════════════════════════════════════════════════════════════
# Shared helpers

def _sharpe(returns: np.ndarray, ann: int = ANN) -> float:
    """Annualized Sharpe of a per-bar return array. Zero-safe."""
    returns = returns[~np.isnan(returns)]
    if returns.size < 2:
        return 0.0
    sd = returns.std(ddof=1)
    if sd == 0 or np.isnan(sd):
        return 0.0
    return float(returns.mean() / sd * np.sqrt(ann))


def reconstruct_asset_returns(
    positions: pd.Series, strategy_returns: pd.Series, eps: float = 1e-6
) -> pd.Series:
    """Back out per-bar asset returns from strategy returns and positions.

    strategy_return[t] = position[t-1] * asset_return[t]
    => asset_return[t] = strategy_return[t] / position[t-1]   (when |pos| > eps)

    On bars where the prior position is ~0, the asset return is unobserved
    (we earned nothing because we held nothing). Those bars are returned as
    NaN; the permutation logic treats them as the pool of "available market
    moves you COULD have been positioned into" only where observed.
    """
    prior_pos = positions.shift(1)
    asset = strategy_returns / prior_pos.where(prior_pos.abs() > eps)
    return asset


# ═══════════════════════════════════════════════════════════════
# Tier A — permutation test (fully implemented)

def vs_random_permutation(
    positions: pd.Series,
    strategy_returns: pd.Series,
    asset_returns: pd.Series | None = None,
    n: int = 1000,
    ann: int = ANN,
    seed: int = 7,
) -> dict:
    """Randomize position TIMING, keep asset returns fixed.

    Builds N random strategies that:
      - are active on the same NUMBER of bars as the real strategy
      - draw position magnitudes from the real strategy's |position| pool
      - assign +/- sign at the real strategy's long/short ratio
      - place those positions on RANDOM bars
    Then compares the real Sharpe to the random-Sharpe distribution.

    If asset_returns is None, reconstructs them from strategy_returns/positions.
    """
    rng = np.random.default_rng(seed)

    pos = positions.copy()
    strat = strategy_returns.copy()
    pos, strat = pos.align(strat, join="inner")

    if asset_returns is None:
        asset = reconstruct_asset_returns(pos, strat)
    else:
        asset, _ = asset_returns.align(pos, join="inner")

    asset_vals = asset.to_numpy(dtype=float)
    observed_mask = ~np.isnan(asset_vals)

    # Real strategy stats
    actual_sharpe = _sharpe(strat.to_numpy(dtype=float), ann)

    # Activity profile of the real strategy
    prior_pos = pos.shift(1).to_numpy(dtype=float)
    active_mask = np.abs(prior_pos) > 1e-6
    active_magnitudes = np.abs(prior_pos[active_mask])
    n_active = int(active_mask.sum())
    if n_active == 0:
        return {
            "tier": "A_permutation",
            "status": "error",
            "reason": "no active bars; cannot run permutation",
        }
    long_frac = float((prior_pos[active_mask] > 0).mean())

    # Tradeable universe: bars where we observed an asset return
    tradeable_idx = np.where(observed_mask)[0]
    if tradeable_idx.size < n_active:
        # Not enough observed asset-return bars; fall back to all bars,
        # treating unobserved as 0 (conservative — random strategy earns 0 there)
        asset_filled = np.nan_to_num(asset_vals, nan=0.0)
        tradeable_idx = np.arange(asset_filled.size)
    else:
        asset_filled = np.nan_to_num(asset_vals, nan=0.0)

    random_sharpes = np.empty(n, dtype=float)
    for i in range(n):
        chosen = rng.choice(tradeable_idx, size=n_active, replace=False)
        mags = rng.choice(active_magnitudes, size=n_active, replace=True)
        signs = np.where(rng.random(n_active) < long_frac, 1.0, -1.0)
        rand_pos = np.zeros(asset_filled.size, dtype=float)
        rand_pos[chosen] = mags * signs
        rand_strategy_ret = rand_pos * asset_filled
        random_sharpes[i] = _sharpe(rand_strategy_ret, ann)

    pct = float((random_sharpes < actual_sharpe).mean() * 100)
    p95 = float(np.percentile(random_sharpes, 95))
    p50 = float(np.percentile(random_sharpes, 50))

    if actual_sharpe > p95:
        verdict = "pass"
    elif actual_sharpe > p50:
        verdict = "borderline"
    else:
        verdict = "fail"

    return {
        "tier": "A_permutation",
        "status": "ok",
        "actual_sharpe": round(actual_sharpe, 4),
        "random_sharpe_mean": round(float(random_sharpes.mean()), 4),
        "random_sharpe_std": round(float(random_sharpes.std(ddof=1)), 4),
        "random_sharpe_p50": round(p50, 4),
        "random_sharpe_p95": round(p95, 4),
        "actual_percentile_vs_random": round(pct, 2),
        "n_permutations": n,
        "n_active_bars": n_active,
        "asset_returns_source": "provided" if asset_returns is not None else "reconstructed",
        "verdict": verdict,
        "interpretation": {
            "pass": "Actual Sharpe exceeds 95th percentile of random timing — edge unlikely to be luck-of-timing.",
            "borderline": "Actual Sharpe beats median random but not the 95th percentile — timing edge is weak.",
            "fail": "Actual Sharpe is below median random timing — your entry timing carries no demonstrable edge.",
        }[verdict],
    }


# ═══════════════════════════════════════════════════════════════
# Tier B — constraint-matched random rule search (scaffolded)

@dataclass
class RuleSpace:
    """Constraint space for random rule generation, matched to the real
    strategy. For skew_consensus this encodes the actual rule grammar so
    the random search has the SAME freedom the real search had.

    BuildAlpha Mistake #2: random search must use the same constraints as
    the real strategy search, else the comparison isn't like-to-like.
    """
    # Feature columns the random rules may trigger on
    signal_features: list[str] = field(default_factory=lambda: [
        "skew_z_252", "iv_rr", "sigma_stall", "skew_divergence",
    ])
    # Threshold sampling range per feature (z-score units)
    threshold_low: float = -3.0
    threshold_high: float = 3.0
    # Number of stages the real strategy gates through (M1->M2->M3 = 3)
    n_stages_choices: tuple[int, ...] = (1, 2, 3)
    # Holding period bound (must match real maxHoldDays)
    max_hold_days: int = 10
    # Freshness window choices (must match real grid)
    freshness_window_choices: tuple[int, ...] = (1, 2, 3, 5, 7)
    # Direction options
    direction_choices: tuple[str, ...] = ("long", "short", "both")
    # Trend filter (on/off, and which side)
    trend_filter_choices: tuple[str, ...] = ("none", "short_only", "both")


def sample_random_rule(space: RuleSpace, rng: np.random.Generator) -> dict:
    """Sample one random rule set from the constraint space."""
    n_stages = int(rng.choice(space.n_stages_choices))
    feats = list(rng.choice(space.signal_features, size=n_stages, replace=False))
    rule = {
        "stages": [
            {
                "feature": f,
                "op": str(rng.choice([">", "<"])),
                "threshold": float(rng.uniform(space.threshold_low, space.threshold_high)),
            }
            for f in feats
        ],
        "freshness_window": int(rng.choice(space.freshness_window_choices)),
        "max_hold_days": int(rng.integers(1, space.max_hold_days + 1)),
        "direction": str(rng.choice(space.direction_choices)),
        "trend_filter": str(rng.choice(space.trend_filter_choices)),
    }
    return rule


def vs_random_constraint_matched(
    feature_matrix: pd.DataFrame | None,
    asset_returns: pd.Series | None,
    actual_sharpe: float,
    backtest_fn=None,
    space: RuleSpace | None = None,
    n_random: int = 1000,
    margin: float = 0.10,
    ann: int = ANN,
    seed: int = 7,
) -> dict:
    """Generate N random rule sets under the constraint space, backtest each
    on real data, take the BEST random Sharpe. Real strategy must beat
    best-random by >= margin.

    Requires:
      - feature_matrix: the real per-bar features the rules trigger on
      - asset_returns: per-bar asset returns to score generated positions
      - backtest_fn: callable(rule, feature_matrix, asset_returns) -> Sharpe

    This is fully available only in Mode A (where the feature matrix and a
    backtest function exist). In Mode B without these, it raises a clear
    error so the orchestrator records Tier B as 'not_available' rather than
    silently skipping.
    """
    if feature_matrix is None or asset_returns is None or backtest_fn is None:
        return {
            "tier": "B_constraint_matched",
            "status": "not_available",
            "reason": (
                "Tier B needs feature_matrix + asset_returns + backtest_fn. "
                "Available in Mode A, or in Mode B if you supply a feature "
                "export and a scoring function. Tier A permutation still ran."
            ),
        }

    space = space or RuleSpace()
    rng = np.random.default_rng(seed)
    best_random = -np.inf
    best_rule = None
    for _ in range(n_random):
        rule = sample_random_rule(space, rng)
        try:
            sr = float(backtest_fn(rule, feature_matrix, asset_returns))
        except Exception:
            continue
        if sr > best_random:
            best_random = sr
            best_rule = rule

    if not np.isfinite(best_random):
        return {
            "tier": "B_constraint_matched",
            "status": "error",
            "reason": "no random rule produced a finite Sharpe",
        }

    required = best_random * (1 + margin) if best_random > 0 else best_random + abs(best_random) * margin + 1e-9
    verdict = "pass" if actual_sharpe >= required else (
        "borderline" if actual_sharpe >= best_random else "fail"
    )
    return {
        "tier": "B_constraint_matched",
        "status": "ok",
        "actual_sharpe": round(actual_sharpe, 4),
        "best_random_sharpe": round(best_random, 4),
        "margin_required": margin,
        "actual_vs_best_random_pct": round((actual_sharpe / best_random - 1) * 100, 2) if best_random > 0 else None,
        "best_random_rule": best_rule,
        "n_random_rules": n_random,
        "verdict": verdict,
    }


# ═══════════════════════════════════════════════════════════════
# Tier C — randomized-data test (scaffolded)

def block_bootstrap(series: np.ndarray, block: int, rng: np.random.Generator) -> np.ndarray:
    """Circular block bootstrap — preserves short-range autocorrelation,
    destroys longer signal structure. Used by Tier C.
    """
    n = series.size
    if n == 0:
        return series
    out = np.empty(n, dtype=float)
    i = 0
    while i < n:
        start = int(rng.integers(0, n))
        for j in range(block):
            if i >= n:
                break
            out[i] = series[(start + j) % n]
            i += 1
    return out


def vs_random_randomized_data(
    asset_returns: pd.Series | None,
    actual_sharpe: float,
    strategy_fn=None,
    feature_matrix: pd.DataFrame | None = None,
    n: int = 500,
    block: int = 5,
    ann: int = ANN,
    seed: int = 7,
) -> dict:
    """Destroy signal structure in the price series (block bootstrap),
    re-run the strategy, confirm it does NOT keep its edge on noise.

    Requires strategy_fn to re-run on bootstrapped data. Mode A feature.
    Scaffolded with a clear not_available return otherwise.
    """
    if asset_returns is None or strategy_fn is None or feature_matrix is None:
        return {
            "tier": "C_randomized_data",
            "status": "not_available",
            "reason": (
                "Tier C needs asset_returns + strategy_fn + feature_matrix to "
                "re-run the strategy on structure-destroyed data. Mode A feature."
            ),
        }
    # Scaffolded execution path (used in Mode A once a strategy_fn exists)
    raise NotImplementedError(
        "Tier C execution requires Mode A strategy re-run harness — Phase 2."
    )


# ═══════════════════════════════════════════════════════════════
# Master runner

def run_vs_random(
    thesis_dir: Path,
    n: int = 1000,
    seed: int = 7,
    ann: int = ANN,
    margin: float = 0.10,
) -> dict:
    """Run the available Vs. Random tiers for a thesis and write vs_random.json."""
    results_dir = thesis_dir / "results"
    pos_path = results_dir / "positions.csv"
    ret_path = results_dir / "returns.csv"
    asset_path = results_dir / "asset_returns.csv"  # optional explicit input

    if not pos_path.exists() or not ret_path.exists():
        return {"status": "error", "reason": f"missing positions.csv/returns.csv in {results_dir}"}

    positions = pd.read_csv(pos_path, index_col=0, parse_dates=True)
    if positions.shape[1] > 1:
        # cross-sectional: collapse to net exposure per bar for the timing test
        positions = positions.sum(axis=1)
    else:
        positions = positions.squeeze("columns")
    strategy_returns = pd.read_csv(ret_path, index_col=0, parse_dates=True).squeeze("columns")

    asset_returns = None
    if asset_path.exists():
        asset_returns = pd.read_csv(asset_path, index_col=0, parse_dates=True).squeeze("columns")

    tier_a = vs_random_permutation(
        positions, strategy_returns, asset_returns=asset_returns, n=n, ann=ann, seed=seed
    )

    # Tier B/C are not_available in pure Mode B (no feature matrix / backtest fn here).
    # The orchestrator can pass these in for Mode A. From the CLI we record availability.
    actual_sharpe = tier_a.get("actual_sharpe", 0.0)
    tier_b = vs_random_constraint_matched(
        feature_matrix=None, asset_returns=None, actual_sharpe=actual_sharpe,
        backtest_fn=None, n_random=1000, margin=margin, ann=ann, seed=seed,
    )
    tier_c = {
        "tier": "C_randomized_data",
        "status": "not_available",
        "reason": "Mode A feature; not run from Mode B CLI.",
    }

    # Overall gate verdict: Tier A is the hard floor; B/C inform if available
    a_verdict = tier_a.get("verdict", "error")
    if a_verdict == "pass":
        overall = "pass"
    elif a_verdict == "borderline":
        overall = "warning"
    else:
        overall = "fail"

    payload = {
        "status": "ok",
        "overall_verdict": overall,
        "fitness_metric": "sharpe",
        "tiers": {"A": tier_a, "B": tier_b, "C": tier_c},
        "notes": [
            "Tier A is the always-on permutation floor (timing edge).",
            "Tier B (constraint-matched random search) needs a feature matrix "
            "+ backtest fn — available in Mode A or by supplying a feature export.",
            "Match-the-fitness rule: comparison is on Sharpe because the "
            "pipeline optimizes around Sharpe.",
        ],
    }

    out_path = results_dir / "vs_random.json"
    out_path.write_text(json.dumps(payload, indent=2))
    return payload


# ═══════════════════════════════════════════════════════════════
# CLI

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="quant_validator.vs_random")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_run = sub.add_parser("run")
    p_run.add_argument("--thesis_id", required=True)
    p_run.add_argument("--n", type=int, default=1000)
    p_run.add_argument("--seed", type=int, default=7)
    p_run.add_argument("--margin", type=float, default=0.10)
    args = parser.parse_args(argv)

    if args.cmd == "run":
        thesis_dir = Path(f"theses/{args.thesis_id}")
        result = run_vs_random(thesis_dir, n=args.n, seed=args.seed, margin=args.margin)
        print(json.dumps(result, indent=2))
        return 0 if result.get("status") == "ok" else 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
