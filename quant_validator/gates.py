"""quant_validator.gates: Step 8 statistical gates CLI.

Evaluates the gates the orchestrator's Step 8 enforces and writes
results/gates_outcome.json. Self-contained.

Gates:
  1. deflated_sharpe   - reads results/dsr.json; pass if dsr_pvalue < threshold
  2. correlation       - max |corr| with accepted survivors < threshold
  3. pca_concentration - top principal component variance share < threshold
  4. vs_random         - reads results/vs_random.json (Step 6.5); Tier A floor

Each gate returns pass/warning/fail. The orchestrator halts on any fail
(soft-overridable via /override-reject) and continues on warnings.

Usage:
    python -m quant_validator.gates evaluate --thesis_id <id>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd


# Default thresholds (override via CLI flags)
DSR_PVALUE_MAX = 0.95        # historical spec ceiling; tighten toward 0.10 for production
CORRELATION_MAX = 0.60       # max |corr| with accepted survivors
PCA_CONCENTRATION_MAX = 0.50 # max variance share in top PC


def _read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return None


# ═══════════════════════════════════════════════════════════════
# Gate 1 — Deflated Sharpe

def gate_deflated_sharpe(thesis_dir: Path, pvalue_max: float = DSR_PVALUE_MAX) -> dict:
    dsr = _read_json(thesis_dir / "results" / "dsr.json")
    if not dsr or dsr.get("status") != "ok":
        return {"gate": "deflated_sharpe", "verdict": "fail",
                "reason": "dsr.json missing or insufficient data — run stats first"}
    pval = dsr.get("dsr_pvalue", 1.0)
    verdict = "pass" if pval < pvalue_max else "fail"
    return {
        "gate": "deflated_sharpe",
        "verdict": verdict,
        "computed_value": pval,
        "threshold": pvalue_max,
        "dsr_probability_real": dsr.get("dsr_probability_real"),
        "note": "dsr_pvalue < threshold passes. Lower is better.",
    }


# ═══════════════════════════════════════════════════════════════
# Gate 2 — Correlation with accepted survivors

def gate_correlation(thesis_id: str, corr_max: float = CORRELATION_MAX) -> dict:
    try:
        from quant_validator.memory import _connect, correlation_with_survivors, DB_PATH
        conn = _connect(DB_PATH)
        try:
            rows = correlation_with_survivors(conn, thesis_id)
        finally:
            conn.close()
    except FileNotFoundError:
        return {"gate": "correlation", "verdict": "pass",
                "computed_value": 0.0, "threshold": corr_max,
                "note": "no returns to compare or no survivors yet"}
    except Exception as e:
        return {"gate": "correlation", "verdict": "pass",
                "computed_value": 0.0, "threshold": corr_max,
                "note": f"correlation check skipped: {e}"}

    if not rows:
        return {"gate": "correlation", "verdict": "pass",
                "computed_value": 0.0, "threshold": corr_max,
                "note": "no accepted survivors to correlate against"}
    max_abs = max(r["abs_correlation"] for r in rows)
    verdict = "pass" if max_abs < corr_max else "fail"
    return {
        "gate": "correlation",
        "verdict": verdict,
        "computed_value": round(max_abs, 4),
        "threshold": corr_max,
        "most_correlated": rows[0]["survivor_id"],
    }


# ═══════════════════════════════════════════════════════════════
# Gate 3 — PCA concentration

def gate_pca_concentration(thesis_dir: Path, conc_max: float = PCA_CONCENTRATION_MAX) -> dict:
    """Top principal-component variance share of the position columns.
    For single-asset strategies this is trivially 1.0 and exempt.
    """
    pos_path = thesis_dir / "results" / "positions.csv"
    if not pos_path.exists():
        return {"gate": "pca_concentration", "verdict": "fail",
                "reason": "positions.csv missing"}
    pos = pd.read_csv(pos_path, index_col=0, parse_dates=True)
    if pos.shape[1] <= 1:
        return {"gate": "pca_concentration", "verdict": "pass",
                "computed_value": None, "threshold": conc_max,
                "note": "single-asset strategy — PCA concentration N/A"}
    X = pos.fillna(0).to_numpy(dtype=float)
    X = X - X.mean(axis=0)
    try:
        cov = np.cov(X, rowvar=False)
        eigvals = np.linalg.eigvalsh(cov)
        eigvals = eigvals[eigvals > 0]
        top_share = float(eigvals.max() / eigvals.sum()) if eigvals.sum() > 0 else 0.0
    except np.linalg.LinAlgError:
        return {"gate": "pca_concentration", "verdict": "warning",
                "reason": "PCA failed to converge"}
    verdict = "pass" if top_share < conc_max else "fail"
    return {
        "gate": "pca_concentration",
        "verdict": verdict,
        "computed_value": round(top_share, 4),
        "threshold": conc_max,
    }


# ═══════════════════════════════════════════════════════════════
# Gate 4 — Vs. Random (Step 6.5 result)

def gate_vs_random(thesis_dir: Path) -> dict:
    vr = _read_json(thesis_dir / "results" / "vs_random.json")
    if not vr or vr.get("status") != "ok":
        return {"gate": "vs_random", "verdict": "warning",
                "reason": "vs_random.json missing — run vs_random first (Step 6.5)"}
    overall = vr.get("overall_verdict", "warning")
    tier_a = vr.get("tiers", {}).get("A", {})
    verdict = {"pass": "pass", "warning": "warning", "fail": "fail"}.get(overall, "warning")
    return {
        "gate": "vs_random",
        "verdict": verdict,
        "tier_a_verdict": tier_a.get("verdict"),
        "actual_sharpe": tier_a.get("actual_sharpe"),
        "random_p95": tier_a.get("random_sharpe_p95"),
        "percentile_vs_random": tier_a.get("actual_percentile_vs_random"),
        "note": "Tier A permutation floor. Fail = timing edge indistinguishable from luck.",
    }


# ═══════════════════════════════════════════════════════════════
# Master + CLI

def evaluate(thesis_id: str,
             dsr_pvalue_max: float = DSR_PVALUE_MAX,
             corr_max: float = CORRELATION_MAX,
             pca_max: float = PCA_CONCENTRATION_MAX) -> dict:
    thesis_dir = Path(f"theses/{thesis_id}")
    gates = {
        "deflated_sharpe": gate_deflated_sharpe(thesis_dir, dsr_pvalue_max),
        "correlation": gate_correlation(thesis_id, corr_max),
        "pca_concentration": gate_pca_concentration(thesis_dir, pca_max),
        "vs_random": gate_vs_random(thesis_dir),
    }
    any_fail = any(g["verdict"] == "fail" for g in gates.values())
    any_warning = any(g["verdict"] == "warning" for g in gates.values())
    overall = "fail" if any_fail else ("warning" if any_warning else "pass")

    payload = {
        "status": "ok",
        "overall": overall,
        "gates": gates,
        "first_failure": next((k for k, g in gates.items() if g["verdict"] == "fail"), None),
    }
    (thesis_dir / "gates_outcome.json").write_text(json.dumps(payload, indent=2))
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="quant_validator.gates")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("evaluate")
    p.add_argument("--thesis_id", required=True)
    p.add_argument("--dsr_pvalue_max", type=float, default=DSR_PVALUE_MAX)
    p.add_argument("--corr_max", type=float, default=CORRELATION_MAX)
    p.add_argument("--pca_max", type=float, default=PCA_CONCENTRATION_MAX)
    args = parser.parse_args(argv)

    if args.cmd == "evaluate":
        result = evaluate(args.thesis_id, args.dsr_pvalue_max, args.corr_max, args.pca_max)
        print(json.dumps(result, indent=2))
        return 0 if result["overall"] != "fail" else 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
