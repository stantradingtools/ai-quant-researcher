"""quant_validator.stats: Step 6 statistics CLI.

Computes the metrics the orchestrator's Step 6 needs and writes them to
the thesis results folder. Self-contained — does not depend on upstream
ai_quant_lab internals, so it works regardless of upstream version.

Outputs:
  results/metrics.json       Sharpe, Sortino, Calmar, returns, drawdown, moments
  results/dsr.json           Deflated Sharpe Ratio (Bailey & Lopez de Prado)
  results/walk_forward.json  k-fold walk-forward Sharpe stability

DEFLATED SHARPE CONVENTION (read carefully):
  We report TWO numbers to avoid ambiguity:
    - dsr_probability_real:  P(true SR > expected-max-under-N-trials).
                             HIGHER is better. ~1.0 = strong, ~0.5 = coin-flip.
    - dsr_pvalue:            1 - dsr_probability_real. LOWER is better.
                             This is the field the gate threshold compares.
  The pipeline's historical convention is "dsr_pvalue < 0.95 = pass".
  NOTE: 0.95 is a very permissive ceiling (allows up to 95% chance of luck).
  Consider tightening to 0.10 or 0.05 for production. See gates.py threshold.

Usage:
    python -m quant_validator.stats compute --thesis_id <id>
    python -m quant_validator.stats compute --thesis_id <id> --n_trials 30
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import norm


ANN = 252
GAMMA = 0.5772156649015329  # Euler-Mascheroni


# ═══════════════════════════════════════════════════════════════
# Core metrics

def _sharpe(r: pd.Series, ann: int = ANN) -> float:
    r = r.dropna()
    if len(r) < 2 or r.std(ddof=1) == 0:
        return 0.0
    return float(r.mean() / r.std(ddof=1) * np.sqrt(ann))


def _sortino(r: pd.Series, ann: int = ANN) -> float:
    r = r.dropna()
    downside = r[r < 0]
    if len(downside) < 2 or downside.std(ddof=1) == 0:
        return 0.0
    return float(r.mean() / downside.std(ddof=1) * np.sqrt(ann))


def _max_drawdown(r: pd.Series) -> tuple[float, int]:
    equity = (1 + r.fillna(0)).cumprod()
    peak = equity.cummax()
    dd = (equity - peak) / peak
    max_dd = float(dd.min())
    # duration: longest consecutive run below peak
    below = (dd < 0).astype(int)
    if below.any():
        groups = (below.diff() != 0).cumsum()
        dur = int(below.groupby(groups).sum().max())
    else:
        dur = 0
    return max_dd, dur


def _calmar(r: pd.Series, ann: int = ANN) -> float:
    ann_ret = float(r.mean() * ann)
    max_dd, _ = _max_drawdown(r)
    if max_dd == 0:
        return 0.0
    return ann_ret / abs(max_dd)


def core_metrics(returns: pd.Series, ann: int = ANN) -> dict:
    r = returns.dropna()
    max_dd, dd_dur = _max_drawdown(r)
    return {
        "n_bars": int(len(r)),
        "sharpe_ratio": round(_sharpe(r, ann), 4),
        "sortino_ratio": round(_sortino(r, ann), 4),
        "calmar_ratio": round(_calmar(r, ann), 4),
        "annual_return": round(float(r.mean() * ann), 5),
        "annual_vol": round(float(r.std(ddof=1) * np.sqrt(ann)), 5),
        "max_drawdown_pct": round(max_dd, 5),
        "max_drawdown_duration_bars": dd_dur,
        "skewness": round(float(r.skew()), 4),
        "excess_kurtosis": round(float(r.kurt()), 4),
        "win_rate": round(float((r > 0).mean()), 4),
    }


# ═══════════════════════════════════════════════════════════════
# Deflated Sharpe Ratio (Bailey & Lopez de Prado, 2014)

def deflated_sharpe(returns: pd.Series, n_trials: int, ann: int = ANN) -> dict:
    """Probability that the observed Sharpe exceeds the expected MAXIMUM
    Sharpe achievable across n_trials independent random strategies with
    the same higher moments.
    """
    r = returns.dropna()
    T = len(r)
    if T < 3:
        return {"status": "insufficient_data", "n_bars": T}

    # Per-bar (non-annualized) Sharpe
    sr = r.mean() / r.std(ddof=1) if r.std(ddof=1) > 0 else 0.0
    skew = float(r.skew())
    kurt = float(r.kurt()) + 3.0  # pandas kurt() is excess; formula uses raw

    n_trials = max(int(n_trials), 1)

    # Expected maximum Sharpe under the null across N trials (in SR-estimate
    # standard-deviation units), via the extreme-value approximation.
    if n_trials > 1:
        z = ((1 - GAMMA) * norm.ppf(1 - 1.0 / n_trials)
             + GAMMA * norm.ppf(1 - 1.0 / (n_trials * np.e)))
    else:
        z = 0.0

    # Standard deviation of the Sharpe estimator (accounts for non-normality)
    var_sr = (1.0 / (T - 1)) * (1 - skew * sr + ((kurt - 1) / 4.0) * sr * sr)
    var_sr = max(var_sr, 1e-12)
    sr_std = np.sqrt(var_sr)
    sr_star = sr_std * z  # expected max Sharpe under null

    # Deflated Sharpe: P(true SR > sr_star)
    denom = np.sqrt(max(1 - skew * sr + ((kurt - 1) / 4.0) * sr * sr, 1e-12))
    dsr_prob = float(norm.cdf((sr - sr_star) * np.sqrt(T - 1) / denom))

    return {
        "status": "ok",
        "per_bar_sharpe": round(float(sr), 5),
        "annualized_sharpe": round(float(sr * np.sqrt(ann)), 4),
        "n_trials": n_trials,
        "expected_max_sharpe_under_null": round(float(sr_star), 5),
        "skewness": round(skew, 4),
        "kurtosis": round(kurt, 4),
        "dsr_probability_real": round(dsr_prob, 4),
        "dsr_pvalue": round(1 - dsr_prob, 4),
        "note": "dsr_pvalue LOWER is better; gate compares this. dsr_probability_real HIGHER is better.",
    }


# ═══════════════════════════════════════════════════════════════
# Walk-forward stability

def walk_forward(returns: pd.Series, k: int = 4, ann: int = ANN) -> dict:
    r = returns.dropna()
    if len(r) < k * 10:
        return {"status": "insufficient_data", "n_bars": len(r)}
    folds = np.array_split(r.to_numpy(), k)
    fold_sharpes = []
    for i, f in enumerate(folds):
        fs = pd.Series(f)
        fold_sharpes.append(round(_sharpe(fs, ann), 4))
    return {
        "status": "ok",
        "k_folds": k,
        "fold_sharpes": fold_sharpes,
        "min_fold_sharpe": min(fold_sharpes),
        "max_fold_sharpe": max(fold_sharpes),
        "mean_fold_sharpe": round(float(np.mean(fold_sharpes)), 4),
        "n_negative_folds": int(sum(1 for s in fold_sharpes if s < 0)),
    }


# ═══════════════════════════════════════════════════════════════
# n_trials lookup from memory

def _n_trials_from_memory(default: int = 1) -> int:
    try:
        from quant_validator.memory import _connect, n_trials, DB_PATH
        conn = _connect(DB_PATH)
        try:
            n = n_trials(conn)
        finally:
            conn.close()
        return max(n, 1)
    except Exception:
        return default


# ═══════════════════════════════════════════════════════════════
# Master + CLI

def compute(thesis_dir: Path, n_trials: int | None = None, ann: int = ANN) -> dict:
    ret_path = thesis_dir / "results" / "returns.csv"
    if not ret_path.exists():
        return {"status": "error", "reason": f"missing returns.csv in {thesis_dir}/results/"}

    returns = pd.read_csv(ret_path, index_col=0, parse_dates=True).squeeze("columns")

    if n_trials is None:
        n_trials = _n_trials_from_memory(default=1)

    metrics = core_metrics(returns, ann)
    dsr = deflated_sharpe(returns, n_trials, ann)
    wf = walk_forward(returns, k=4, ann=ann)

    results_dir = thesis_dir / "results"
    (results_dir / "metrics.json").write_text(json.dumps(metrics, indent=2))
    (results_dir / "dsr.json").write_text(json.dumps(dsr, indent=2))
    (results_dir / "walk_forward.json").write_text(json.dumps(wf, indent=2))

    return {"status": "ok", "metrics": metrics, "dsr": dsr, "walk_forward": wf}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="quant_validator.stats")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("compute")
    p.add_argument("--thesis_id", required=True)
    p.add_argument("--n_trials", type=int, default=None,
                   help="Override n_trials for DSR; defaults to memory.db count")
    args = parser.parse_args(argv)

    if args.cmd == "compute":
        result = compute(Path(f"theses/{args.thesis_id}"), n_trials=args.n_trials)
        print(json.dumps(result, indent=2))
        return 0 if result.get("status") == "ok" else 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
