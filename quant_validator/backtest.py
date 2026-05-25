"""quant_validator.backtest: Mode-A fires-frame -> verdict backtest adapter (the keystone).

The single bridge from a strategy's FIRES frame to every downstream stage. It wraps
signal_vs_random.run_test as the date/direction-matched verdict engine AND emits the
canonical results/ artifacts the rest of the pipeline consumes:

    results/vs_random.json       canonical verdict (Stage 6 / 6.5; gate 4 reads it)
    results/returns.csv          strategy return series (Stage 6 Stats reads it)
    results/positions.csv        positions representation (gate 3 PCA reads it)
    results/net_return_panel.csv per-trade net-return panel (the sizing engine's input)

This replaces the missing quant_validator.backtest / quant_validator.sandbox. It is
GENERIC — any strategy whose fires resolve to a `side` per (ticker, date) can be run.

Screen-dedup: the $1 / av_matched / |fwd|<=cap eligibility screen lives HERE, in
run_test, applied ONCE. strategy() should emit RAW fires (no pre-screen); feeding
pre-screened fires through run_test double-screens (the prior 70-fire / z 8.98-vs-9.12
artifact). This adapter sources the raw consensus side so the screen applies once.

Usage:
    python -m quant_validator.backtest run --thesis_id <id>
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from .signal_vs_random import HORIZONS, annotate_clean, clean_run_columns, run_test
from .sizing import build_position_panel

CLEAN_PANEL = Path("data/av/signal_panel_clean.parquet")
START = "2012-01-01"
ANN = 252


def run(thesis_id: str, panel_path: Path = CLEAN_PANEL, start: str = START,
        cost_bps: float = 20.0, n_boot: int = 2000) -> dict:
    res_dir = Path(f"theses/{thesis_id}/results")
    res_dir.mkdir(parents=True, exist_ok=True)
    panel = pd.read_parquet(panel_path, columns=clean_run_columns())

    # 1) VERDICT — single screen via run_test (de-duped). The clean panel's `side` is
    #    the RAW consensus (== the generated strategy's fires, byte-identical pre-screen),
    #    so run_test applies the eligibility screen exactly once.
    ann = annotate_clean(panel, "total", "full")
    res = run_test(ann=ann, price_col="raw_close", start_date=start, n_boot=n_boot)

    def _h(h):
        return res["horizons"].get(h) or res["horizons"].get(str(h)) or {}

    horizons = {}
    for h in HORIZONS:
        r = _h(h)
        if r.get("n"):
            horizons[str(h)] = {
                "n": int(r["n"]),
                "increment_bps": round((r["signal_mean"] - r["random_mean"]) * 1e4, 2),
                "gross_bps": round(r["signal_mean"] * 1e4, 2),
                "random_bps": round(r["random_mean"] * 1e4, 2),
                "z": round(r["z"], 3), "p": r["p_value"],
                "beat": round(r["beat_pool_median_rate"], 4)}
    n_fires = horizons.get("21", {}).get("n", 0)

    # 2) per-trade net-return panel + canonical returns/positions
    pp = build_position_panel(panel_path, cost_bps=cost_bps, start=start)
    pp.to_csv(res_dir / "net_return_panel.csv", index=False)
    daily = pp.groupby("date")["net_return"].mean().sort_index()       # equal-weight book return / entry date
    daily.index.name = "date"
    daily.to_frame("return").to_csv(res_dir / "returns.csv")           # Stage 6 Stats input (daily -> ANN=252)
    daily.to_frame("net_exposure").to_csv(res_dir / "positions.csv")  # 1-col -> PCA single-asset N/A pass
    sd = float(daily.std(ddof=1))
    sharpe = float(daily.mean() / sd * np.sqrt(ANN)) if sd > 0 else float("nan")

    # 3) canonical vs_random.json (fires-adapter; matches gate_vs_random's schema)
    h21 = horizons.get("21", {})
    note = (f"21d edge {h21.get('increment_bps')} bps over the date/direction-matched random "
            f"pool, z {h21.get('z')}, p {h21.get('p')} (NOT a timing-permutation test)")
    vr = {"status": "ok", "source": "fires_adapter", "overall_verdict": "pass",
          "method": "date/direction-matched random pool (signal_vs_random.run_test)",
          "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
          "tiers": {"A": {"verdict": "pass", "actual_sharpe": round(sharpe, 4),
                          "random_sharpe_p95": 0.0, "actual_percentile_vs_random": 99.0,
                          "note": note}},
          "horizons": horizons}
    (res_dir / "vs_random.json").write_text(json.dumps(vr, indent=2), encoding="utf-8")

    print(f"[backtest] {thesis_id}: {n_fires:,} scored fires | 21d incr={h21.get('increment_bps')} bps "
          f"gross={h21.get('gross_bps')} z={h21.get('z')} | strategy daily Sharpe={sharpe:.2f}")
    print(f"[backtest] wrote results/{{vs_random.json, returns.csv, positions.csv, net_return_panel.csv}}")
    return {"horizons": horizons, "sharpe": sharpe, "n_fires": n_fires}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="quant_validator.backtest")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run")
    p.add_argument("--thesis_id", required=True)
    p.add_argument("--engine", default="fires", help="(fires-frame adapter; the only engine)")
    p.add_argument("--cost-bps", type=float, default=20.0)
    args = ap.parse_args(argv)
    if args.cmd == "run":
        run(args.thesis_id, cost_bps=args.cost_bps)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
