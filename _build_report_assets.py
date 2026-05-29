"""Generate all flow diagrams + charts for the Extended Agent Report (PDF).

Diagrams are hand-laid matplotlib (boxes + arrows) on a 0..100 canvas.
Charts read the REAL result artifacts where present, else documented fallbacks.
Outputs PNGs into report_assets/. Temp file — deleted after the PDF is built.
"""
from __future__ import annotations

import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

plt.rcParams.update({
    "font.family": "sans-serif",
    "font.sans-serif": ["Arial", "DejaVu Sans"],
    "figure.dpi": 150,
    "savefig.dpi": 150,
})

ROOT = Path(".")
OUT = ROOT / "report_assets"
OUT.mkdir(exist_ok=True)

# Design-system palette
NAVY = "#16284d"; NAVY2 = "#1f3a5f"; ACCENT = "#0072c6"; GREEN = "#0a7a3c"
RED = "#c80030"; AMBER = "#b85c00"; PURPLE = "#6b3fbf"; SLATE = "#455a72"
BG3 = "#eef0f5"; BORDER = "#c9d3e0"; LIGHT = "#dfe7f2"; GREENL = "#dff0e6"; REDL = "#fbe1e7"


def canvas(w=12.0, h=7.0):
    fig, ax = plt.subplots(figsize=(w, h))
    ax.set_xlim(0, 100); ax.set_ylim(0, 100); ax.axis("off")
    return fig, ax


def node(ax, cx, cy, w, h, title, sub=None, fc=NAVY, tc="white", fs=9.5, ec=None, lw=1.3):
    ax.add_patch(FancyBboxPatch((cx - w / 2, cy - h / 2), w, h,
                 boxstyle="round,pad=0.3,rounding_size=1.2",
                 fc=fc, ec=ec or fc, lw=lw, zorder=2))
    label = title if not sub else f"{title}\n{sub}"
    ax.text(cx, cy, label, ha="center", va="center", color=tc, fontsize=fs,
            zorder=3, linespacing=1.25, fontweight="bold" if not sub else "normal")


def arrow(ax, p1, p2, color=SLATE, lw=1.5, style="-|>", ms=12, ls="-", rad=0.0):
    cs = f"arc3,rad={rad}"
    ax.add_patch(FancyArrowPatch(p1, p2, arrowstyle=style, mutation_scale=ms,
                 lw=lw, color=color, zorder=1, linestyle=ls, connectionstyle=cs,
                 shrinkA=2, shrinkB=2))


def label(ax, x, y, text, fs=8, color=SLATE, ha="center", style="italic", weight="normal"):
    ax.text(x, y, text, ha=ha, va="center", fontsize=fs, color=color,
            style=style, fontweight=weight, zorder=4)


def save(fig, name):
    fig.savefig(OUT / name, bbox_inches="tight", facecolor="white", pad_inches=0.15)
    plt.close(fig)
    print("  wrote", name)


# ----------------------------------------------------------------------------- data
def load_equity():
    df = pd.read_csv(ROOT / "data/av/sized_equity_curve.csv", parse_dates=["date"])
    return df


def load_peryear():
    m = pd.read_csv(ROOT / "reports/stage2_sizing.csv")
    m["year"] = m["month"].str.slice(0, 4).astype(int)
    g = m.groupby("year")["ret_ks_on"].apply(lambda r: (1 + r).prod() - 1)
    return g


def load_temporal():
    t = pd.read_csv(ROOT / "reports/gate_temporal_stability.csv")
    return t[t["horizon"] == 21].copy()


def load_json(p):
    return json.loads((ROOT / p).read_text(encoding="utf-8"))


# ============================================================ DIAGRAMS
def fig01_master():
    fig, ax = canvas(13.5, 8.6)
    label(ax, 50, 97, "FIGURE 1  —  Master pipeline: from a prose thesis to enforced paper trading",
          fs=12, color=NAVY, style="normal", weight="bold")
    # Stage row 1 (1-5), row 2 (6-10) snake
    s1 = [("1\nHypo-Refiner", PURPLE), ("2\nPre-Critic", PURPLE), ("3\nCoder", PURPLE),
          ("4\nBacktest", ACCENT), ("5\nStats", ACCENT)]
    s2 = [("6\nVs-Random", ACCENT), ("7\nValidator", PURPLE), ("8\nGatekeeper", ACCENT),
          ("9\nRisk", PURPLE), ("10\nMemory+Report", NAVY)]
    xs = [9, 27, 45, 63, 81]
    for (t, c), x in zip(s1, xs):
        node(ax, x, 80, 15, 9, t, fc=c, fs=9)
    for i in range(4):
        arrow(ax, (xs[i] + 7.5, 80), (xs[i + 1] - 7.5, 80), color=NAVY)
    arrow(ax, (81, 75.5), (81, 64.5), color=NAVY)  # snake down
    for (t, c), x in zip(s2, xs[::-1]):
        node(ax, x, 60, 15, 9, t, fc=c, fs=9)
    for i in range(4):
        a, b = xs[::-1][i], xs[::-1][i + 1]
        arrow(ax, (a - 7.5, 60), (b + 7.5, 60), color=NAVY)
    # optimisation loop (11,12) below
    node(ax, 27, 38, 18, 9, "11  Mutation Agent", "DD-bounded + MT/DSR penalty", fc=AMBER, fs=9)
    node(ax, 60, 38, 18, 9, "12  Exit Agent", "sign-aware OOS exit rules", fc=AMBER, fs=9)
    arrow(ax, (9, 55.5), (24, 42.5), color=AMBER, ls="--", rad=-0.15)
    label(ax, 8, 48, "validated +\nsized strategy", fs=7.5, color=AMBER, ha="center")
    arrow(ax, (36, 38), (51, 38), color=AMBER)
    arrow(ax, (60, 42.5), (45, 64), color=AMBER, ls="--", rad=-0.25)
    label(ax, 70, 52, "promoted variant\nre-enters at stage 3->4\n(full re-validation)",
          fs=7.5, color=AMBER, ha="center")
    # paper loop
    node(ax, 50, 15, 37, 10, "PHASE 4  —  Enforced paper trading (Alpaca)",
         "daily: ORATS feed -> manage -> sized queue -> review gate",
         fc=GREEN, fs=8.0)
    arrow(ax, (9, 55.5), (41, 20), color=GREEN, rad=0.2)
    arrow(ax, (27, 33.5), (45, 20), color=GREEN, ls=":")
    node(ax, 89, 15, 16, 10, "PHASE 5\nLive broker", "gated on paper\ntrack record", fc=SLATE, fs=8.5)
    arrow(ax, (69, 15), (81, 15), color=SLATE, ls="--")
    # legend
    for i, (c, t) in enumerate([(PURPLE, "LLM-judgment agent"), (ACCENT, "deterministic module"),
                                (AMBER, "optimisation layer"), (GREEN, "live execution")]):
        ax.add_patch(FancyBboxPatch((6 + i * 24, 2.5), 2.2, 2.2, boxstyle="round,pad=0.1",
                     fc=c, ec=c)); label(ax, 9 + i * 24, 3.6, t, fs=7.5, ha="left", style="normal")
    save(fig, "fig01_master.png")


def fig02_dataflow():
    fig, ax = canvas(13.5, 8.2)
    label(ax, 50, 97, "FIGURE 2  —  Artifact data-flow: what each stage reads and writes (theses/<id>/)",
          fs=12, color=NAVY, style="normal", weight="bold")
    rows = [
        ("thesis.md (prose)", "1 Hypo-Refiner", "refined.json"),
        ("refined.json", "2 Pre-Critic", "critique_pre.json"),
        ("refined.json", "3 Coder", "code/signal.py"),
        ("signal.py + panel", "4 Backtest", "vs_random.json, returns.csv,\npositions.csv, net_return_panel.csv"),
        ("returns.csv", "5 Stats", "metrics.json, dsr.json,\nwalk_forward.json"),
        ("annotated panel", "6 Vs-Random", "vs_random.json (Tier A/B)"),
        ("all results/*", "7 Validator", "critique_post.json (9 criteria)"),
        ("dsr/positions/corr", "8 Gatekeeper", "gates_outcome.json"),
        ("positions/returns/greeks", "9 Risk", "risk.json (size_recommendation)"),
        ("decision.json", "10 Memory+Reporter", "memory.db row + report.html"),
    ]
    y = 88
    for src, stg, out in rows:
        node(ax, 16, y, 24, 6.0, src, fc=BG3, tc=NAVY, fs=7.6, ec=BORDER)
        node(ax, 50, y, 20, 6.0, stg, fc=NAVY, fs=8.4)
        node(ax, 84, y, 26, 6.0, out, fc=LIGHT, tc=NAVY, fs=7.2, ec=ACCENT)
        arrow(ax, (28.2, y), (39.8, y), color=ACCENT)
        arrow(ax, (60.2, y), (70.8, y), color=GREEN)
        y -= 8.7
    label(ax, 16, 3, "INPUTS (read)", fs=8.5, color=SLATE, style="normal", weight="bold")
    label(ax, 50, 3, "STAGE", fs=8.5, color=SLATE, style="normal", weight="bold")
    label(ax, 84, 3, "OUTPUTS (written, audit-trailed)", fs=8.5, color=SLATE, style="normal", weight="bold")
    save(fig, "fig02_dataflow.png")


def fig03_modeab():
    fig, ax = canvas(12.5, 6.6)
    label(ax, 50, 96, "FIGURE 3  —  Mode detection: A (from prose) vs B (from results)",
          fs=12, color=NAVY, style="normal", weight="bold")
    node(ax, 50, 84, 30, 8, "Step 0", "does results/positions.csv exist?", fc=NAVY, fs=9)
    node(ax, 24, 64, 26, 8, "MODE A  (from-prose)", fc=ACCENT, fs=9.5)
    node(ax, 76, 64, 26, 8, "MODE B  (from-results)", fc=PURPLE, fs=9.5)
    arrow(ax, (42, 80), (28, 68), color=ACCENT); label(ax, 30, 75, "no", fs=8, color=ACCENT)
    arrow(ax, (58, 80), (72, 68), color=PURPLE); label(ax, 70, 75, "yes", fs=8, color=PURPLE)
    a = ["1 Refine prose -> spec", "2 Pre-Critic kill gate", "3 Coder writes strategy()",
         "4 Backtest (fires adapter)", "-> 6..11 (full pipeline)"]
    b = ["skip 1-4 (no codegen)", "verify positions/returns/\nequity_curve exist", "-> 6..11 only",
         "Tier-B vs-random = N/A", "validate EXISTING numbers"]
    for i, t in enumerate(a):
        node(ax, 24, 52 - i * 8.5, 28, 6.4, t, fc=BG3, tc=NAVY, fs=7.8, ec=BORDER)
    for i, t in enumerate(b):
        node(ax, 76, 52 - i * 8.5, 28, 6.4, t, fc=BG3, tc=NAVY, fs=7.8, ec=BORDER)
    save(fig, "fig03_modeab.png")


def fig04_signal():
    fig, ax = canvas(12.5, 8.4)
    label(ax, 50, 97, "FIGURE 4  —  Skew-consensus signal: the M1 AND M2 AND M3 decision (per ticker, per day)",
          fs=12, color=NAVY, style="normal", weight="bold")
    node(ax, 50, 89, 60, 6, "ORATS surface row:  putP, callP, ivP, rrP, sigma, skewDelta  (0-100 percentiles)",
         fc=NAVY2, fs=8.6)
    # M1
    node(ax, 18, 73, 26, 9, "M1  corner", "putP<=25 & callP>=75 -> BULL\nputP>=75 & callP<=25 -> BEAR",
         fc=ACCENT, fs=7.8)
    node(ax, 50, 73, 26, 9, "M2  vol+RR", "ivP>=75 AND\n rrP>=75 (BULL) / rrP<=25 (BEAR)", fc=ACCENT, fs=7.8)
    node(ax, 82, 73, 26, 9, "M3  exhaustion", "sigma-stall (4-bar shift-AND)\nOR skew-divergence (d3>d2+0.2)",
         fc=ACCENT, fs=7.6)
    for x in (18, 50, 82):
        arrow(ax, (50, 86), (x, 77.7), color=SLATE, rad=0.0)
    node(ax, 50, 55, 40, 7.5, "freshness: M1 & M2 'recent' within rolling 3 bars,\nco-fire on the SAME bar; M3 confirms",
         fc=BG3, tc=NAVY, fs=8, ec=BORDER)
    for x in (18, 50, 82):
        arrow(ax, (x, 68.5), (50, 59), color=SLATE)
    node(ax, 50, 41, 22, 6.5, "FIRE?", "BULL priority on ties", fc=NAVY, fs=9)
    arrow(ax, (50, 51), (50, 44.5), color=NAVY)
    node(ax, 24, 25, 30, 9, "side = BULL", "signal_sign = -1\nCONSENSUS SHORT (fade up)", fc=RED, fs=8)
    node(ax, 76, 25, 30, 9, "side = BEAR", "signal_sign = +1\nCONSENSUS LONG (fade down)", fc=GREEN, fs=8)
    arrow(ax, (44, 39), (28, 30), color=RED); arrow(ax, (56, 39), (72, 30), color=GREEN)
    label(ax, 50, 9, "P&L per trade  =  signal_sign  x  21-day forward return  -  costs",
          fs=9.5, color=NAVY, style="normal", weight="bold")
    save(fig, "fig04_signal.png")


def fig05_backtest():
    fig, ax = canvas(13.0, 5.6)
    label(ax, 50, 95, "FIGURE 5  —  Backtest (stage 4): the fires-frame -> verdict adapter",
          fs=12, color=NAVY, style="normal", weight="bold")
    steps = [("RAW fires\n(ticker,date,side)", BG3, NAVY), ("FIRE-PARITY GATE\nstored side ==\ncompute_consensus", AMBER, "white"),
             ("eligibility SCREEN\n(once): px>=$1,\nav_matched, |fwd|<=5", ACCENT, "white"),
             ("run_test\nvs date/direction-\nmatched random pool", ACCENT, "white"),
             ("4 artifacts:\nvs_random.json,returns,\npositions,net_panel", LIGHT, NAVY)]
    xs = [12, 32, 52, 72, 90]
    for (t, c, tc), x in zip(steps, xs):
        node(ax, x, 60, 17, 16, t, fc=c, tc=tc, fs=7.6, ec=BORDER if c in (BG3, LIGHT) else c)
    for i in range(4):
        arrow(ax, (xs[i] + 8.5, 60), (xs[i + 1] - 8.5, 60), color=NAVY)
    label(ax, 32, 44, "ParityError -> REFUSE to score\n(first_failure=fire_parity)", fs=7.5, color=RED)
    label(ax, 52, 44, "screen applied ONCE\n(strategy emits raw)", fs=7.5, color=SLATE)
    label(ax, 50, 20, "Default 21-day close-to-close basis reproduced bit-for-bit by the path engine's time-backstop "
                      "(parity-locked).", fs=8.5, color=NAVY, style="italic")
    save(fig, "fig05_backtest.png")


def fig06_vsrandom():
    fig, ax = canvas(12.5, 6.4)
    label(ax, 50, 96, "FIGURE 6  —  Vs-Random verdict (CANONICAL = PRIMARY ≥2018; full window is the sizing basis)",
          fs=12, color=NAVY, style="normal", weight="bold")
    node(ax, 22, 80, 32, 8, "For each FIRE on date t", "carry its real side/sign", fc=NAVY, fs=8.5)
    node(ax, 22, 60, 36, 9, "Draw N random eligible tickers\nON THE SAME DATE t",
         "(controls market regime)", fc=ACCENT, fs=8)
    node(ax, 22, 40, 36, 9, "Apply the fire's SIGN to each\n(holds fade direction fixed)",
         "tests SELECTION, not a bet", fc=ACCENT, fs=8)
    node(ax, 22, 20, 32, 8, "Bootstrap x2000", "build null distribution", fc=NAVY, fs=8.5)
    for y1, y2 in [(76, 64.5), (55.5, 44.5), (35.5, 24)]:
        arrow(ax, (22, y1), (22, y2), color=SLATE)
    node(ax, 72, 56, 46, 28, "CANONICAL VERDICT — PRIMARY (≥2018), ~341,506 fires",
         "increment +19.15 bps over pool\ngross +114.3 bps   random +95.16 bps\nz = 7.53   p < 0.0005   beat-rate 51.8%\nOOS holdout (pre-2018) CONFIRMS: +15.27 bps, z 4.58 (113,292)\nall-data ref (sizing basis): +18.15 bps, z 8.87 (454,798)",
         fc=GREENL, tc=NAVY, fs=8.6, ec=GREEN)
    arrow(ax, (40, 40), (50, 52), color=GREEN)
    label(ax, 72, 30, "Tier A (permutation) is the hard floor; Tier B = constraint-matched rule search (Mode A).",
          fs=8, color=SLATE)
    save(fig, "fig06_vsrandom.png")


def fig07_gates():
    fig, ax = canvas(12.5, 5.8)
    label(ax, 50, 95, "FIGURE 7  —  Stage 8 statistical gate stack (soft-overridable on fail)",
          fs=12, color=NAVY, style="normal", weight="bold")
    gates = [("Deflated Sharpe", "dsr_pvalue < 0.95", "0.006 PASS"),
             ("Correlation", "|corr| vs survivors\n< 0.60", "PASS"),
             ("PCA concentration", "top-PC share < 0.50", "single-asset N/A"),
             ("Vs-Random", "Tier-A permutation\nfloor", "PASS")]
    xs = [14, 38, 62, 86]
    for (t, cond, res), x in zip(gates, xs):
        node(ax, x, 64, 20, 13, t, cond, fc=NAVY, fs=8.2)
        node(ax, x, 44, 20, 6.5, res, fc=GREENL, tc=GREEN, fs=8.2, ec=GREEN)
        arrow(ax, (x, 57.3), (x, 47.5), color=GREEN)
    for i in range(3):
        arrow(ax, (xs[i] + 10, 64), (xs[i + 1] - 10, 64), color=SLATE, ls=":")
    label(ax, 50, 22, "Any FAIL halts the pipeline but is overridable via /override-reject (>=20-char justification, logged forever).\n"
                      "Hard errors (sandbox/missing data) are NOT overridable. The Mutation Agent reuses this exact stack per variant.",
          fs=8.3, color=SLATE)
    save(fig, "fig07_gates.png")


def fig08_sizing():
    fig, ax = canvas(13.0, 5.6)
    label(ax, 50, 95, "FIGURE 8  —  Stage 9 sizing: fractional Kelly -> caps -> drawdown kill-switch",
          fs=12, color=NAVY, style="normal", weight="bold")
    steps = [("per-trade net return\nsign x fwd21 - 20bps", BG3, NAVY),
             ("fractional portfolio\nKelly  f = lam*Sigma^-1*mu\nlam=0.25, rho=0.30", ACCENT, "white"),
             ("CAPS\nper-name 5%, gross 1.0,\nnet 0.5", ACCENT, "white"),
             ("DD kill-switch\nenter -15% -> x0.3\nexit -7%; hard -25%", RED, "white"),
             ("monthly book\nSharpe 1.12 / DD -9.86%\nCAGR +5.82%", GREENL, NAVY)]
    xs = [12, 33, 52, 71, 90]
    ws = [16, 19, 16, 17, 17]
    for (t, c, tc), x, w in zip(steps, xs, ws):
        node(ax, x, 58, w, 16, t, fc=c, tc=tc, fs=7.5, ec=BORDER if c in (BG3, GREENL) else c)
    for i in range(4):
        arrow(ax, (xs[i] + ws[i] / 2, 58), (xs[i + 1] - ws[i + 1] / 2, 58), color=NAVY)
    label(ax, 50, 30, "Deployed at 0.5x (Risk Agent). Kill-switch is a TAIL BACKSTOP — at deployed sizing maxDD (-9.86%) "
                      "it never trips the -15% trigger;\nit caps the depth of a future shock, it does not fix entry selection (e.g. the 2020 V-recovery).",
          fs=8.2, color=SLATE)
    save(fig, "fig08_sizing.png")


def fig09_optloop():
    fig, ax = canvas(12.0, 7.4)
    label(ax, 50, 97, "FIGURE 9  —  The Mutation Agent's closed optimisation loop (two governors)",
          fs=12, color=NAVY, style="normal", weight="bold")
    cx, cy, r = 50, 52, 27
    pts = {
        "propose": (50, 80, "Propose guarded variant\n(entry/filter/exit/universe)"),
        "refire": (80, 60, "Re-fire + path-backtest\n(walk-forward folds)"),
        "score": (74, 28, "Risk-score: Sharpe vs\nbaseline, DD bound"),
        "penalty": (26, 28, "MT/DSR penalty on\nWHOLE search (cum. trials)"),
        "decide": (20, 60, "Promote only if validated\n+ survives penalty"),
    }
    order = ["propose", "refire", "score", "penalty", "decide"]
    for k in order:
        x, y, t = pts[k]
        node(ax, x, y, 24, 10, t, fc=AMBER, fs=7.8)
    seq = order + ["propose"]
    for a, b in zip(seq, seq[1:]):
        ax1 = pts[a]; bx = pts[b]
        arrow(ax, (ax1[0], ax1[1]), (bx[0], bx[1]), color=NAVY2, rad=0.18, lw=1.6)
    node(ax, 50, 52, 22, 11, "GOVERNORS", "1) max-drawdown bound\n2) cumulative MT / DSR", fc=NAVY, fs=8)
    label(ax, 50, 7,
          "PRIMARY-search run (>=2018): baseline Sharpe 1.437; clears the MT bar (prob-real 0.9885 >= 0.95) "
          "but NO variant beats baseline -> local optimum. Pre-2018 leak-guard CONFIRMS (+21.5 bps, 7,722 sampled fires).",
          fs=8.2, color=SLATE)
    save(fig, "fig09_optloop.png")


def fig10_parity():
    fig, ax = canvas(12.5, 5.4)
    label(ax, 50, 95, "FIGURE 10  —  The standing Coder->Backtest fire-parity gate",
          fs=12, color=NAVY, style="normal", weight="bold")
    node(ax, 18, 62, 26, 13, "Generated strategy()\n(quarantined)", "stored panel side", fc=BG3, tc=NAVY, fs=8, ec=BORDER)
    node(ax, 50, 62, 24, 13, "compute_consensus\n(binding reference)", "recompute on same\nwarmed features", fc=NAVY, fs=8)
    node(ax, 82, 62, 26, 13, "_compare on\nAAPL/MSFT/XOM/JPM", "tol = 0", fc=ACCENT, fs=8.2)
    arrow(ax, (31, 62), (38, 62), color=SLATE)
    arrow(ax, (62, 62), (69, 62), color=SLATE)
    node(ax, 30, 28, 30, 11, "0 disagreements -> SCORE", fc=GREENL, tc=GREEN, fs=9, ec=GREEN)
    node(ax, 74, 28, 34, 11, "any mismatch -> ParityError\nREFUSE to score", fc=REDL, tc=RED, fs=9, ec=RED)
    arrow(ax, (74, 55.5), (36, 34), color=GREEN, rad=0.1)
    arrow(ax, (82, 55.5), (74, 34), color=RED)
    label(ax, 50, 8, "Runtime gate enforces MATERIALIZATION parity; an injected BULL<->BEAR swap MUST raise ParityError "
                     "(3 standing tests).", fs=8.2, color=SLATE)
    save(fig, "fig10_parity.png")


def fig11_paper():
    fig, ax = canvas(13.0, 5.6)
    label(ax, 50, 95, "FIGURE 11  —  Phase-4 daily paper loop (Alpaca, DRY-RUN by default)",
          fs=12, color=NAVY, style="normal", weight="bold")
    steps = [("Live ORATS feed\nbackfill -> trailing-756\n-> fire latest session", ACCENT),
             ("RECONCILE\naccount/positions/orders\nbefore ANY action", NAVY),
             ("MANAGE\nkill-switch, hard_stop_8,\n21D backstop (enforced)", RED),
             ("Build SIZED queue\nKelly+caps, shortability\nidempotent coid", ACCENT),
             ("REVIEW GATE\nDRY-RUN until --submit\n(graduated fraction)", GREEN)]
    xs = [11, 32, 52, 72, 91]
    for (t, c), x in zip(steps, xs):
        node(ax, x, 58, 18, 16, t, fc=c, fs=7.5)
    for i in range(4):
        arrow(ax, (xs[i] + 9, 58), (xs[i + 1] - 9, 58), color=NAVY)
    label(ax, 50, 28, "Idempotent client_order_id = \"{strategy}:{symbol}:{signal_date}\"  |  staleness guard <= 2 bdays  |  "
                      "only ever acts on OUR ledger-tagged positions", fs=8, color=SLATE)
    label(ax, 50, 16, "Live manage-loop exit logic is parity-checked rule-for-rule against the backtester (bit-identical).",
          fs=8.2, color=NAVY, style="italic")
    save(fig, "fig11_paper.png")


def fig12_lifecycle():
    fig, ax = canvas(13.5, 9.0)
    label(ax, 50, 98, "FIGURE 12  —  End-to-end lifecycle: a user-incubated thesis to a live paper fill",
          fs=12.5, color=NAVY, style="normal", weight="bold")
    lanes = [("USER", "#efe7fb"), ("DESIGN-TIME PIPELINE (1-10)", "#e7f0fb"),
             ("OPTIMISE (11-12)", "#fdf0e1"), ("PAPER / LIVE (4-5)", GREENL)]
    ytop = 92
    for i, (nm, c) in enumerate(lanes):
        y0 = ytop - i * 22.5
        ax.add_patch(FancyBboxPatch((2, y0 - 21.5), 96, 21.5, boxstyle="square,pad=0",
                     fc=c, ec=BORDER, lw=1, zorder=0))
        ax.text(4, y0 - 1.6, nm, fontsize=8.5, color=NAVY, fontweight="bold", va="top")
    def L(i):  # lane center y
        return ytop - i * 22.5 - 11
    seq = [
        (12, L(0), "write\nthesis.md", PURPLE),
        (30, L(1), "1-2 refine\n+ kill-gate", PURPLE),
        (48, L(1), "3-4 code +\nbacktest", ACCENT),
        (68, L(1), "5-8 stats,\nvs-random,\nvalidator, gates", ACCENT),
        (88, L(1), "9-10 size +\nrecord", NAVY),
        (30, L(2), "11 mutate\n(DD+MT bound)", AMBER),
        (60, L(2), "12 exit rules\nOOS-validated", AMBER),
        (30, L(3), "deploy 0.5x\nDRY-RUN", GREEN),
        (58, L(3), "daily feed ->\nmanage -> queue", GREEN),
        (86, L(3), "--submit ->\npaper fills", GREEN),
    ]
    pos = {}
    for i, (x, y, t, c) in enumerate(seq):
        node(ax, x, y, 16, 13, t, fc=c, fs=7.5)
        pos[i] = (x, y)
    # connect
    chain = [(0, 1), (1, 2), (2, 3), (3, 4)]
    for a, b in chain:
        arrow(ax, (pos[a][0] + 8, pos[a][1]), (pos[b][0] - 8, pos[b][1]), color=NAVY)
    arrow(ax, (88, L(1) - 6.5), (30, L(2) + 6.5), color=AMBER, rad=0.1, ls="--")
    arrow(ax, (pos[5][0] + 8, pos[5][1]), (pos[6][0] - 8, pos[6][1]), color=AMBER)
    arrow(ax, (60, L(2) + 6.5), (48, L(1) - 6.5), color=AMBER, rad=0.1, ls=":")
    label(ax, 76, L(2) + 9, "promoted variant re-validates", fs=7, color=AMBER)
    arrow(ax, (88, L(1) - 6.5), (30, L(3) + 6.5), color=GREEN, rad=-0.12)
    arrow(ax, (pos[7][0] + 8, pos[7][1]), (pos[8][0] - 8, pos[8][1]), color=GREEN)
    arrow(ax, (pos[8][0] + 8, pos[8][1]), (pos[9][0] - 8, pos[9][1]), color=GREEN)
    arrow(ax, (12, L(0) - 6.5), (30, L(1) + 6.5), color=PURPLE)
    save(fig, "fig12_lifecycle.png")


# ============================================================ CHARTS
def chart01_equity():
    df = load_equity()
    fig, ax = plt.subplots(figsize=(11, 4.6))
    ax.plot(df["date"], df["equity_ks_on"], color=ACCENT, lw=1.8)
    ax.fill_between(df["date"], 1.0, df["equity_ks_on"], where=df["equity_ks_on"] >= 1.0,
                    color=ACCENT, alpha=0.10)
    ax.axhline(1.0, color=SLATE, lw=0.8, ls="--")
    ax.set_title("Chart 1  —  Sized equity curve (deployed config, kill-switch on)",
                 color=NAVY, fontsize=12, fontweight="bold", loc="left")
    ax.set_ylabel("growth of $1"); ax.grid(alpha=0.25)
    fin = df["equity_ks_on"].iloc[-1]
    ax.annotate(f"x{fin:.2f}", (df['date'].iloc[-1], fin), color=GREEN, fontweight="bold",
                fontsize=11, ha="right", va="bottom")
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    save(fig, "chart01_equity.png")


def chart02_peryear():
    g = load_peryear()
    fig, ax = plt.subplots(figsize=(11, 4.2))
    cols = [GREEN if v >= 0 else RED for v in g.values]
    ax.bar(g.index.astype(str), g.values * 100, color=cols)
    for x, v in zip(range(len(g)), g.values):
        ax.text(x, v * 100 + (0.15 if v >= 0 else -0.15), f"{v*100:.1f}%",
                ha="center", va="bottom" if v >= 0 else "top", fontsize=8, color=NAVY)
    ax.axhline(0, color=SLATE, lw=0.9)
    ax.set_title("Chart 2  —  Sized return by calendar year (every year positive)",
                 color=NAVY, fontsize=12, fontweight="bold", loc="left")
    ax.set_ylabel("return %"); ax.grid(axis="y", alpha=0.25)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    save(fig, "chart02_peryear.png")


def chart03_drawdown():
    df = load_equity()
    eq = df["equity_ks_on"]
    dd = (eq / eq.cummax() - 1.0) * 100
    fig, ax = plt.subplots(figsize=(11, 3.8))
    ax.fill_between(df["date"], dd, 0, color=RED, alpha=0.30)
    ax.plot(df["date"], dd, color=RED, lw=1.3)
    ax.axhline(-9.86, color=NAVY, lw=1.0, ls="--")
    ax.text(df["date"].iloc[2], -9.86, " max drawdown -9.86%", color=NAVY, fontsize=9, va="bottom")
    ax.set_title("Chart 3  —  Underwater (drawdown) curve", color=NAVY, fontsize=12,
                 fontweight="bold", loc="left")
    ax.set_ylabel("drawdown %"); ax.grid(alpha=0.25)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    save(fig, "chart03_drawdown.png")


def chart04_verdict():
    vr = load_json("theses/skew_consensus_v22_novix/results/vs_random.json")["horizons"]
    hs = ["5", "10", "21"]
    inc = [vr[h]["increment_bps"] for h in hs]
    z = [vr[h]["z"] for h in hs]
    fig, ax = plt.subplots(figsize=(10.5, 4.4))
    x = np.arange(len(hs))
    b = ax.bar(x, inc, color=ACCENT, width=0.55)
    ax.set_xticks(x); ax.set_xticklabels([f"{h}-day" for h in hs])
    ax.set_ylabel("selection increment (bps)", color=ACCENT)
    for xi, v, zz in zip(x, inc, z):
        ax.text(xi, v + 0.4, f"+{v:.2f} bps\nz={zz:.2f}", ha="center", va="bottom",
                fontsize=9, color=NAVY, fontweight="bold")
    ax.set_title("Chart 4  —  Selection edge vs random pool grows with horizon",
                 color=NAVY, fontsize=12, fontweight="bold", loc="left")
    ax.grid(axis="y", alpha=0.25); ax.set_ylim(0, max(inc) * 1.25)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    save(fig, "chart04_verdict.png")


def chart05_temporal():
    t = load_temporal()
    fig, ax = plt.subplots(figsize=(11, 4.2))
    cols = [AMBER if f else ACCENT for f in t["year_flagged"]]
    ax.bar(t["year"].astype(str), t["increment_bps"], color=cols)
    ax.axhline(0, color=SLATE, lw=0.9)
    ax.set_title("Chart 5  —  21-day selection increment by year (amber = 21d p>0.05 flagged)",
                 color=NAVY, fontsize=12, fontweight="bold", loc="left")
    ax.set_ylabel("increment (bps)"); ax.grid(axis="y", alpha=0.25)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    save(fig, "chart05_temporal.png")


def chart06_mutation():
    m = load_json("reports/mutation_agent.json")
    log = [r for r in m["log"] if r["kind"] != "baseline"]
    base = m["baseline"]["sharpe"]
    names = [r["name"].replace("exit_", "").replace("universe_", "u:").replace("entry_", "e:")
             for r in log]
    sh = [r["pooled_sharpe"] for r in log]
    fig, ax = plt.subplots(figsize=(11.5, 5.0))
    cols = [GREEN if s > base else SLATE for s in sh]
    ax.barh(names, sh, color=cols)
    ax.axvline(base, color=RED, lw=1.6)
    ax.text(base, len(names) - 0.3, f" baseline {base:.3f}", color=RED, fontsize=9, fontweight="bold")
    ax.set_title("Chart 6  —  Mutation candidates' walk-forward Sharpe vs baseline\n"
                 "(some beat raw Sharpe, but NONE cleared the joint promotion + MT-penalty bar)",
                 color=NAVY, fontsize=11.5, fontweight="bold", loc="left")
    ax.set_xlabel("pooled walk-forward Sharpe"); ax.grid(axis="x", alpha=0.25)
    ax.tick_params(axis="y", labelsize=7.5)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    save(fig, "chart06_mutation.png")


def chart06b_mtbar():
    m = load_json("reports/mutation_agent.json")["mt_penalty"]
    fig, ax = plt.subplots(figsize=(6.6, 3.4))
    ax.barh(["best variant\nprob-real"], [m["deflated_prob_real"]], color=AMBER, height=0.5)
    ax.axvline(m["bar"], color=RED, lw=2)
    ax.text(m["bar"] + 0.005, 0, f" bar {m['bar']}", color=RED, va="center", fontsize=10, fontweight="bold")
    ax.text(m["deflated_prob_real"] - 0.005, 0, f"{m['deflated_prob_real']:.3f} ", color="white",
            va="center", ha="right", fontsize=11, fontweight="bold")
    _pass = m['deflated_prob_real'] >= m['bar']
    ax.set_xlim(0.8, 1.0)
    ax.set_title(f"Chart 7  —  MT/DSR penalty at {m['cumulative_trials']} cumulative trials on PRIMARY (>=2018): "
                 f"{'CLEARS' if _pass else 'FAILS'} the {m['bar']} bar",
                 color=NAVY, fontsize=10.5, fontweight="bold", loc="left")
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.set_yticks([])
    save(fig, "chart07_mtbar.png")


def chart08_exit():
    e = load_json("reports/exit_agent_selection.json")
    base = e["baseline"]["pooled"]
    rows = [("baseline_21d", base["sharpe"], base["maxdd"], e["baseline"]["jan2021_bps"], None)]
    for r in e["ranked"]:
        rows.append((r["name"], r["pooled_sharpe"], r["pooled_maxdd"], r["jan2021_bps"], r["survives"]))
    rows = rows[:8]
    names = [r[0] for r in rows]
    sh = [r[1] for r in rows]; dd = [abs(r[2]) * 100 for r in rows]
    surv = [r[4] for r in rows]
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(12, 4.6))
    cols = [GREEN if s is True else (NAVY if s is None else SLATE) for s in surv]
    a1.barh(names[::-1], sh[::-1], color=cols[::-1])
    a1.axvline(base["sharpe"], color=RED, lw=1.4, ls="--")
    a1.set_title("Chart 8a  —  Exit-rule Sharpe (green = OOS-validated)", color=NAVY,
                 fontsize=10.5, fontweight="bold", loc="left")
    a1.tick_params(axis="y", labelsize=7.5); a1.grid(axis="x", alpha=0.25)
    a2.barh(names[::-1], dd[::-1], color=cols[::-1])
    a2.axvline(abs(base["maxdd"]) * 100, color=RED, lw=1.4, ls="--")
    a2.set_title("Chart 8b  —  Max drawdown % (lower = better)", color=NAVY,
                 fontsize=10.5, fontweight="bold", loc="left")
    a2.tick_params(axis="y", labelsize=7.5); a2.grid(axis="x", alpha=0.25)
    for ax in (a1, a2):
        for s in ("top", "right"):
            ax.spines[s].set_visible(False)
    fig.tight_layout()
    save(fig, "chart08_exit.png")


def chart09_squeeze():
    e = load_json("reports/exit_agent_selection.json")
    base_j = e["baseline"]["jan2021_bps"]
    hs = next(r for r in e["ranked"] if r["name"] == "hard_stop_8")["jan2021_bps"]
    fig, ax = plt.subplots(figsize=(8.6, 4.0))
    labels = ["raw fade\n(no stop)", "with\nhard_stop_8", "sized 0.5x\n(book)"]
    vals = [base_j / 100.0, hs / 100.0, -0.88]
    cols = [RED, AMBER, GREEN]
    ax.bar(labels, vals, color=cols, width=0.6)
    for i, v in enumerate(vals):
        ax.text(i, v - 0.15, f"{v:.2f}%", ha="center", va="top", fontsize=10,
                color="white", fontweight="bold")
    ax.axhline(0, color=SLATE, lw=0.9)
    ax.set_title("Chart 9  —  The 2021-01 meme-squeeze: stop + sizing tame the worst month",
                 color=NAVY, fontsize=11, fontweight="bold", loc="left")
    ax.set_ylabel("Jan-2021 P&L (%)"); ax.grid(axis="y", alpha=0.25)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    save(fig, "chart09_squeeze.png")


FIGS = [fig01_master, fig02_dataflow, fig03_modeab, fig04_signal, fig05_backtest, fig06_vsrandom,
        fig07_gates, fig08_sizing, fig09_optloop, fig10_parity, fig11_paper, fig12_lifecycle,
        chart01_equity, chart02_peryear, chart03_drawdown, chart04_verdict, chart05_temporal,
        chart06_mutation, chart06b_mtbar, chart08_exit, chart09_squeeze]

if __name__ == "__main__":
    ok = 0
    for f in FIGS:
        try:
            f(); ok += 1
        except Exception as e:  # noqa: BLE001
            print(f"  !! {f.__name__} FAILED: {e}")
    print(f"\n[assets] {ok}/{len(FIGS)} figures written to {OUT}/")
