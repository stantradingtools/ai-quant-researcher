"""quant_validator.verdict_rerun: re-run the signal-vs-random verdict on the
AV-clean, survivorship-free panel.

Context (1c validation corrected the original premise): ORATS clsPx was ALREADY
split+dividend adjusted — NOT split-contaminated. The real ORATS artifact was the
505 zero-close rows. So this re-run's purpose is SURVIVORSHIP + zero-close
robustness, with total-return as the matched basis vs the original ORATS reference.

Passes (all start_date=2012-01-01 to match the original reference window):
  1. HEADLINE               --returns total  --universe full   (survivorship-free)
  2. SURVIVORSHIP DIAGNOSTIC --returns total  --universe active (survivors only)
  3. BASIS ROBUSTNESS       --returns split  --universe full
  + CONFOUND RECHECK: pass 1 with match_high_iv (edge != just an IV tilt?)

Writes reports/step2_verdict_rerun.txt (+ .csv). The signal is never recomputed.

CLI:  python -m quant_validator.verdict_rerun [--panel data/av/signal_panel_clean.parquet]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

from .signal_vs_random import (HORIZONS, WARMUP_START, annotate_clean, run_test,
                               survivor_tickers_from_map)

CLEAN_PANEL = Path("data/av/signal_panel_clean.parquet")
SYMBOL_MAP = Path("data/av/symbol_map.csv")
REPORT_DIR = Path("reports")
REPORT_TXT = REPORT_DIR / "step2_verdict_rerun.txt"
REPORT_CSV = REPORT_DIR / "step2_verdict_rerun.csv"

START = WARMUP_START           # 3yr / 756-bday ORATS warm-up convention (panel_start+756)
REF_21D_INCREMENT = 0.0022607   # original ORATS reference: 22.6 bps (svr_verdict_clip.json)
MEANINGFUL_FLOOR = 0.0010       # 10 bps — below this the 21d edge is "collapsed"
SURV_FLAG_BPS = 0.0005          # active-minus-full > 5 bps => survivorship-inflated


def _pass(panel: pd.DataFrame, returns: str, universe: str,
          survivors: set[str] | None, *, match_high_iv: bool = False,
          n_boot: int = 2000, seed: int = 0) -> dict:
    ann = annotate_clean(panel, returns=returns, universe=universe,
                         survivor_tickers=survivors)
    return run_test(ann=ann, price_col="raw_close", start_date=START,
                    n_boot=n_boot, seed=seed, match_high_iv=match_high_iv)


def _metrics(res: dict, h: int) -> dict | None:
    r = res.get("horizons", {}).get(h) or res.get("horizons", {}).get(str(h))
    if not r or r.get("n", 0) == 0 or "signal_mean" not in r:
        return None
    return {
        "n_fires": int(r["n"]),
        "increment": r["signal_mean"] - r["random_mean"],
        "random_mean": r["random_mean"],
        "gross_per_trade": r["signal_mean"],
        "z": r.get("z"),
        "p_value": r.get("p_value"),
        "beat_pool_median": r.get("beat_pool_median_rate"),
    }


def _bps(x) -> str:
    return "   n/a" if x is None else f"{x * 1e4:+7.1f}"


def run(panel_path: Path = CLEAN_PANEL, symbol_map: Path = SYMBOL_MAP,
        n_boot: int = 2000) -> dict:
    if not Path(panel_path).exists():
        raise RuntimeError(f"{panel_path} not found — run rebuild_returns (1c) first.")
    panel = pd.read_parquet(panel_path)
    survivors = survivor_tickers_from_map(symbol_map)

    passes = {
        "1_HEADLINE_total_full":        _pass(panel, "total", "full", None, n_boot=n_boot),
        "2_SURVIVORSHIP_total_active":  _pass(panel, "total", "active", survivors, n_boot=n_boot),
        "3_BASIS_split_full":           _pass(panel, "split", "full", None, n_boot=n_boot),
        "4_CONFOUND_total_full_highIV": _pass(panel, "total", "full", None,
                                              match_high_iv=True, n_boot=n_boot),
    }

    # ── flatten to CSV ────────────────────────────────────────────────────
    rows = []
    for name, res in passes.items():
        for h in HORIZONS:
            m = _metrics(res, h)
            if m is None:
                continue
            rows.append({"pass": name, "horizon": h, **m,
                         "increment_bps": m["increment"] * 1e4,
                         "random_mean_bps": m["random_mean"] * 1e4,
                         "gross_per_trade_bps": m["gross_per_trade"] * 1e4})
    csv_df = pd.DataFrame(rows)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    csv_df.to_csv(REPORT_CSV, index=False)

    txt = _format_report(passes)
    REPORT_TXT.write_text(txt, encoding="utf-8")
    print(txt)
    print(f"\nwrote {REPORT_TXT} (+ {REPORT_CSV})")
    return {"passes": passes, "csv": str(REPORT_CSV), "txt": str(REPORT_TXT)}


def _format_report(passes: dict) -> str:
    L = []
    L.append("=" * 88)
    L.append("STEP-2 VERDICT RE-RUN — signal-vs-random on the AV-clean survivorship-free panel")
    L.append("=" * 88)
    L.append("Premise correction (1c): ORATS clsPx was ALREADY split+dividend adjusted — NOT")
    L.append("split-contaminated. The real ORATS artifact was the 505 zero-close rows. This")
    L.append("re-run tests SURVIVORSHIP + zero-close robustness; total-return is the matched")
    L.append("basis vs the original ORATS reference. The consensus signal is UNCHANGED.")
    L.append(f"Scored window: from {START}. increment = signal_mean - random_mean.")
    L.append("")

    titles = {
        "1_HEADLINE_total_full": "PASS 1 — HEADLINE (total-return, full survivorship-free universe)",
        "2_SURVIVORSHIP_total_active": "PASS 2 — SURVIVORSHIP DIAGNOSTIC (total-return, survivors only)",
        "3_BASIS_split_full": "PASS 3 — BASIS ROBUSTNESS (split-only, full universe)",
        "4_CONFOUND_total_full_highIV": "PASS 4 — CONFOUND RECHECK (match_high_iv on pass 1)",
    }
    hdr = (f"  {'h':>3} | {'n_fires':>9} | {'incr(bps)':>9} | {'rand(bps)':>9} | "
           f"{'gross(bps)':>10} | {'z':>6} | {'p':>7} | {'beat_med':>8}")
    for name, res in passes.items():
        L.append(titles.get(name, name))
        L.append(f"  raw fires (pre-window): {res.get('n_signals_raw', 'n/a')} | "
                 f"scored: {res.get('n_signals_scored', 'n/a')}")
        L.append(hdr)
        L.append("  " + "-" * (len(hdr) - 2))
        for h in HORIZONS:
            m = _metrics(res, h)
            if m is None:
                L.append(f"  {h:>3} |    (no usable fires)")
                continue
            z = f"{m['z']:+.2f}" if m["z"] is not None else "n/a"
            p = f"{m['p_value']:.4f}" if m["p_value"] is not None else "n/a"
            bm = f"{m['beat_pool_median']*100:.1f}%" if m["beat_pool_median"] is not None else "n/a"
            L.append(f"  {h:>3} | {m['n_fires']:>9,} | {_bps(m['increment'])} | "
                     f"{_bps(m['random_mean'])} | {m['gross_per_trade']*1e4:>10.1f} | "
                     f"{z:>6} | {p:>7} | {bm:>8}")
        L.append("")

    # ── READOUT ───────────────────────────────────────────────────────────
    h1 = _metrics(passes["1_HEADLINE_total_full"], 21)
    h2 = _metrics(passes["2_SURVIVORSHIP_total_active"], 21)
    h3 = _metrics(passes["3_BASIS_split_full"], 21)
    h4 = _metrics(passes["4_CONFOUND_total_full_highIV"], 21)
    L.append("-- READOUT (21-day) " + "-" * 68)
    L.append(f"  Original ORATS reference 21d increment : {REF_21D_INCREMENT*1e4:+.1f} bps")
    if h1:
        meaningful = h1["increment"] >= MEANINGFUL_FLOOR
        beat = (h1["beat_pool_median"] or 0) > 0.5
        verdict = "PASS" if (meaningful and beat) else "FAIL"
        L.append(f"  HEADLINE 21d increment                 : {h1['increment']*1e4:+.1f} bps "
                 f"({h1['increment']/REF_21D_INCREMENT*100:.0f}% of reference), "
                 f"beat_pool_median={ (h1['beat_pool_median'] or 0)*100:.1f}%")
        L.append(f"    -> {verdict}: economically meaningful (>= {MEANINGFUL_FLOOR*1e4:.0f} bps)"
                 f"={meaningful} AND beat_pool_median>50%={beat}")
    if h1 and h2:
        delta = h2["increment"] - h1["increment"]
        flag = "FLAG (survivorship-inflated)" if delta > SURV_FLAG_BPS else "OK (no material inflation)"
        L.append(f"  Survivorship delta (active - full)     : {delta*1e4:+.1f} bps -> {flag}")
        L.append(f"    active 21d={h2['increment']*1e4:+.1f} bps vs full 21d={h1['increment']*1e4:+.1f} bps")
    if h1 and h3:
        bdelta = h1["increment"] - h3["increment"]
        L.append(f"  Basis delta (total - split)            : {bdelta*1e4:+.1f} bps "
                 f"(expect small, dividend-only)")
    if h4:
        L.append(f"  Confound (match_high_iv) 21d increment : {h4['increment']*1e4:+.1f} bps, "
                 f"z={h4['z']:+.2f} -> edge {'persists' if h4['increment'] >= MEANINGFUL_FLOOR else 'WEAKENS'} "
                 f"under an IV-matched pool")
    L.append("")
    return "\n".join(L)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="quant_validator.verdict_rerun")
    ap.add_argument("--panel", default=str(CLEAN_PANEL))
    ap.add_argument("--symbol-map", default=str(SYMBOL_MAP))
    ap.add_argument("--n-boot", type=int, default=2000)
    args = ap.parse_args(argv)
    run(panel_path=Path(args.panel), symbol_map=Path(args.symbol_map), n_boot=args.n_boot)
    return 0


if __name__ == "__main__":
    sys.exit(main())
