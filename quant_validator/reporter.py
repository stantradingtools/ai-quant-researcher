"""quant_validator.reporter: Reporter (stage 10) — the living strategy report.

Reads reports/<strategy>/report.json (the canonical record) and writes a single
self-contained reports/<strategy>/report.html. Per the report-rendering skill:
charts are inline SVG baked at render time (NO runtime JS / NO CDN), so the file
opens offline by double-click and prints cleanly to PDF. The LOOK follows the repo
CLAUDE.md design system (Arial, light-theme colour vars, flat/clean).

CLI:
    python -m quant_validator.reporter backfill-v22     # assemble report.json from artifacts
    python -m quant_validator.reporter render --strategy skew_consensus_v22_novix
"""

from __future__ import annotations

import argparse
import html
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

REPORTS = Path("reports")

# status -> (display label, chip css class). Skill enum + live-phase states.
_CHIP = {"done": ("done", "ok"), "pass": ("pass", "ok"), "deployed": ("deployed", "blue"),
         "running": ("running", "amber"), "pending": ("pending", "amber"),
         "flagged": ("flagged", "amber"), "paused": ("paused", "amber"),
         "fail": ("fail", "red"), "not_started": ("not started", "gray")}

_CSS = """
:root{--bg:#f4f6f9;--bg2:#ffffff;--bg3:#eef0f5;--accent:#0072c6;--green:#0a7a3c;
--red:#c80030;--text:#1a2332;--text2:#455a72;--text3:#8a9bb0;--border:#dde2ea;
--amber:#b85c00;--purple:#6b3fbf;}
*{box-sizing:border-box}
body{font-family:Arial,Helvetica,sans-serif;background:var(--bg);color:var(--text);
margin:0;padding:24px;line-height:1.45;font-size:14px}
.wrap{max-width:920px;margin:0 auto}
header.band{background:var(--bg2);border:1px solid var(--border);border-radius:10px;
padding:18px 22px;margin-bottom:18px}
header.band h1{margin:0 0 6px;font-size:22px}
.meta{color:var(--text2);font-size:12.5px}
.meta b{color:var(--text)}
section{background:var(--bg2);border:1px solid var(--border);border-radius:10px;
padding:16px 20px;margin-bottom:14px}
section h2{margin:0 0 10px;font-size:16px;display:flex;align-items:center;gap:10px}
section .ts{margin-left:auto;font-weight:normal;color:var(--text3);font-size:11.5px}
.chip{display:inline-block;padding:2px 9px;border-radius:11px;font-size:11px;
font-weight:bold;letter-spacing:.02em;border:1px solid transparent}
.chip.ok{background:#e7f4ec;color:var(--green);border-color:#bfe3cd}
.chip.blue{background:#e3f0fb;color:var(--accent);border-color:#bcdcf5}
.chip.amber{background:#fbf0e1;color:var(--amber);border-color:#f0d8b6}
.chip.red{background:#fae6ec;color:var(--red);border-color:#f2bcce}
.chip.gray{background:var(--bg3);color:var(--text3);border-color:var(--border)}
p.note{margin:6px 0;color:var(--text2)}
table{border-collapse:collapse;width:100%;margin:8px 0;font-size:13px}
th,td{text-align:right;padding:5px 9px;border-bottom:1px solid var(--border)}
th:first-child,td:first-child{text-align:left}
thead th{color:var(--text2);font-weight:bold;border-bottom:2px solid var(--border)}
.pos{color:var(--green)}.neg{color:var(--red)}
.svgwrap{margin:10px 0}
.cap{color:var(--text3);font-size:11.5px;margin-top:2px}
.grid2{display:flex;gap:16px;flex-wrap:wrap}.grid2>div{flex:1;min-width:300px}
@media print{body{background:#fff;padding:0}section,header.band{border-color:#ccc;
break-inside:avoid}}
"""


def _esc(s) -> str:
    return html.escape(str(s)) if s is not None else ""


def _chip(status: str) -> str:
    label, cls = _CHIP.get(status, (status, "gray"))
    return f'<span class="chip {cls}">{_esc(label)}</span>'


def _git_sha() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"],
                                       text=True).strip()
    except Exception:
        return "unknown"


# ── inline-SVG charts (baked at render time; no runtime JS) ───────────────

def _svg_line(series: list, *, w=860, h=240, pad=40, color="#0072c6", baseline=1.0) -> str:
    """series: list of [label, value]. Equity-curve line with a baseline at `baseline`."""
    vals = [float(v) for _, v in series]
    if not vals:
        return "<p class='cap'>no data</p>"
    vmin, vmax = min(vals + [baseline]), max(vals + [baseline])
    if vmax == vmin:
        vmax += 1e-9
    n = len(series)

    def X(i):
        return pad + (w - 2 * pad) * i / max(n - 1, 1)

    def Y(v):
        return h - pad - (h - 2 * pad) * (v - vmin) / (vmax - vmin)

    pts = " ".join(f"{X(i):.1f},{Y(v):.1f}" for i, v in enumerate(vals))
    by = Y(baseline)
    ticks = [0, n // 2, n - 1]
    xlabels = "".join(
        f'<text x="{X(i):.1f}" y="{h-pad+16:.1f}" text-anchor="middle" '
        f'font-size="10" fill="#8a9bb0">{_esc(series[i][0])}</text>' for i in ticks)
    return (
        f'<svg viewBox="0 0 {w} {h}" width="100%" role="img">'
        f'<rect x="0" y="0" width="{w}" height="{h}" fill="#ffffff"/>'
        f'<line x1="{pad}" y1="{by:.1f}" x2="{w-pad}" y2="{by:.1f}" stroke="#dde2ea" '
        f'stroke-dasharray="4 3"/>'
        f'<text x="{pad-4}" y="{by-3:.1f}" text-anchor="end" font-size="10" '
        f'fill="#8a9bb0">{baseline:g}x</text>'
        f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="2"/>'
        f'<text x="{X(n-1):.1f}" y="{Y(vals[-1])-6:.1f}" text-anchor="end" font-size="11" '
        f'fill="{color}" font-weight="bold">{vals[-1]:.2f}x</text>'
        f'{xlabels}</svg>')


def _svg_bars(pairs: list, *, w=860, h=240, pad=40, ref: float | None = None,
              unit="") -> str:
    """pairs: list of [label, value]; signed bars (green +, red -) from a zero baseline.
    Optional horizontal `ref` line (e.g. realistic-cost threshold)."""
    vals = [float(v) for _, v in pairs]
    if not vals:
        return "<p class='cap'>no data</p>"
    lo, hi = min(vals + [0.0, ref or 0.0]), max(vals + [0.0, ref or 0.0])
    if hi == lo:
        hi += 1e-9
    n = len(pairs)
    bw = (w - 2 * pad) / n * 0.62

    def Xc(i):
        return pad + (w - 2 * pad) * (i + 0.5) / n

    def Y(v):
        return h - pad - (h - 2 * pad) * (v - lo) / (hi - lo)

    y0 = Y(0.0)
    bars = []
    for i, (lab, v) in enumerate(pairs):
        xc = Xc(i)
        yv = Y(v)
        top, height = (yv, y0 - yv) if v >= 0 else (y0, yv - y0)
        col = "#0a7a3c" if v >= 0 else "#c80030"
        bars.append(
            f'<rect x="{xc-bw/2:.1f}" y="{top:.1f}" width="{bw:.1f}" height="{max(height,0.5):.1f}" '
            f'fill="{col}" opacity="0.85"/>'
            f'<text x="{xc:.1f}" y="{h-pad+15:.1f}" text-anchor="middle" font-size="9.5" '
            f'fill="#8a9bb0" transform="rotate(0 {xc:.1f} {h-pad+15:.1f})">{_esc(lab)}</text>'
            f'<text x="{xc:.1f}" y="{(top-3) if v>=0 else (top+height+11):.1f}" text-anchor="middle" '
            f'font-size="9" fill="#455a72">{v:.0f}</text>')
    refline = ""
    if ref is not None:
        ry = Y(ref)
        refline = (f'<line x1="{pad}" y1="{ry:.1f}" x2="{w-pad}" y2="{ry:.1f}" '
                   f'stroke="#b85c00" stroke-dasharray="5 3"/>'
                   f'<text x="{w-pad}" y="{ry-3:.1f}" text-anchor="end" font-size="10" '
                   f'fill="#b85c00">{ref:g}{unit}</text>')
    return (f'<svg viewBox="0 0 {w} {h}" width="100%" role="img">'
            f'<rect width="{w}" height="{h}" fill="#ffffff"/>'
            f'<line x1="{pad}" y1="{y0:.1f}" x2="{w-pad}" y2="{y0:.1f}" stroke="#aab6c6"/>'
            f'{refline}{"".join(bars)}</svg>')


def _kv_table(rows: list) -> str:
    body = "".join(f"<tr><td>{_esc(k)}</td><td>{_esc(v)}</td></tr>" for k, v in rows)
    return f"<table><tbody>{body}</tbody></table>"


# ── section renderers ──────────────────────────────────────────────────────

_STAGE_TITLES = {
    "1_hypothesis": "1. Hypothesis (Hypo-Refiner)", "2_pre_critic": "2. Pre-Critic",
    "3_code": "3. Code (Coder Agent)", "4_backtest": "4. Backtest", "5_stats": "5. Stats",
    "6_vs_random": "6. VsRandom", "7_validator": "7. Validator", "8_gates": "8. Gates",
    "9_risk": "9. Risk & Sizing", "10_memory": "10. Memory / Final verdict"}


def _section(title: str, status: str, ts: str, inner: str) -> str:
    return (f'<section><h2>{_esc(title)} {_chip(status)}'
            f'<span class="ts">{_esc(ts or "")}</span></h2>{inner}</section>')


def _verdict_table(v: dict) -> str:
    head = ("<thead><tr><th>horizon</th><th>increment(bps)</th><th>random(bps)</th>"
            "<th>gross/trade</th><th>z</th><th>p</th><th>beat-median</th></tr></thead>")
    rows = ""
    for h in ("5", "10", "21"):
        r = v.get(h) or v.get(int(h))
        if not r:
            continue
        cls = "pos" if r["increment"] >= 0 else "neg"
        rows += (f"<tr><td>{h}d</td><td class='{cls}'>{r['increment']:+.1f}</td>"
                 f"<td>{r['random']:+.1f}</td><td>{r['gross']:+.1f}</td>"
                 f"<td>{r['z']:+.2f}</td><td>{r['p']:.4f}</td><td>{r['beat']*100:.1f}%</td></tr>")
    return f"<table>{head}<tbody>{rows}</tbody></table>"


def render_html(rep: dict) -> str:
    s = rep.get("stages", {})

    def slice_note(key):
        sl = s.get(key, {})
        return sl.get("status", "not_started"), sl.get("updated_at", ""), sl.get("note", "")

    secs = []
    for key, title in _STAGE_TITLES.items():
        st, ts, note = slice_note(key)
        inner = f'<p class="note">{_esc(note)}</p>' if note else ""
        if key == "6_vs_random" and s.get(key, {}).get("verdict"):
            inner += _verdict_table(s[key]["verdict"])
        if key == "9_risk":
            r9 = s.get(key, {})
            if r9.get("metrics"):
                inner += _kv_table(list(r9["metrics"].items()))
            if r9.get("equity_curve"):
                inner += ('<div class="svgwrap">' + _svg_line(r9["equity_curve"])
                          + '<div class="cap">Sized equity curve (fractional Kelly &lambda;=0.25, '
                            'monthly cohort; KS non-binding at this &lambda;).</div></div>')
        secs.append(_section(title, st, ts, inner))

    # Robustness
    rob = rep.get("robustness", {})
    rob_inner = ""
    cost = rob.get("cost", {})
    if cost.get("breakeven"):
        pairs = [[f"{k}d", cost["breakeven"][k]] for k in ("5", "10", "21") if k in cost["breakeven"]]
        rob_inner += ('<div class="grid2"><div><b>Cost survival</b> — breakeven round-trip (bps)'
                      f'<div class="svgwrap">{_svg_bars(pairs, ref=cost.get("realistic_bps", 20), unit="bps")}</div>'
                      f'<div class="cap">{_esc(cost.get("note",""))}</div></div>')
    temp = rob.get("temporal", {})
    if temp.get("per_year"):
        pairs = [[str(y), val] for y, val in temp["per_year"]]
        rob_inner += ('<div><b>Temporal stability</b> — 21d increment per year (bps)'
                      f'<div class="svgwrap">{_svg_bars(pairs)}</div>'
                      f'<div class="cap">{_esc(temp.get("note",""))}</div></div></div>')
    reg = rob.get("regime", {})
    if reg:
        rob_inner += f'<p class="note"><b>Regime diagnosis:</b> {_esc(reg.get("note",""))}</p>'
    if rob_inner:
        secs.append(_section("Robustness", rob.get("status", "done"), rob.get("updated_at", ""), rob_inner))

    # Phases
    ph = rep.get("phases", {})
    ph_inner = ""
    for k, label in (("4_paper", "Phase 4 — Paper"), ("5_live", "Phase 5 — Live")):
        pl = ph.get(k, {"status": "not_started"})
        ph_inner += (f'<p class="note">{_esc(label)} {_chip(pl.get("status","not_started"))} '
                     f'{_esc(pl.get("note",""))}</p>')
    secs.append(_section("Phases", "not_started", "", ph_inner))

    hdr = (
        f'<header class="band"><h1>{_esc(rep.get("strategy"))} {_chip(_overall_chip(rep))}</h1>'
        f'<div class="meta"><b>{_esc(rep.get("status"))}</b><br>'
        f'run mode <b>{_esc(rep.get("run_mode"))}</b> &middot; version <b>{rep.get("version",1)}</b> '
        f'&middot; {_esc(rep.get("updated_at"))} &middot; commit <b>{_esc(rep.get("git_commit"))}</b></div></header>')
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{_esc(rep.get('strategy'))} — report</title><style>{_CSS}</style></head>"
            f"<body><div class='wrap'>{hdr}{''.join(secs)}"
            f"<p class='cap'>Generated by quant_validator.reporter — self-contained, offline. "
            f"Ctrl-P &rarr; Save as PDF.</p></div></body></html>")


def _overall_chip(rep: dict) -> str:
    st = (rep.get("status") or "").lower()
    if "fail" in st or "reject" in st:
        return "fail"
    if "pending" in st or "partial" in st or "plumbing" in st:
        return "flagged"
    return "pass"


# ── backfill: assemble report.json for skew_consensus_v22_novix ───────────

def backfill_v22(strategy: str = "skew_consensus_v22_novix") -> dict:
    """Assemble the canonical report.json from the existing artifacts (Mode-A run,
    gating reports, Stage-2 sizing)."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    # per-year 21d increments (temporal gate)
    per_year, temp_note = [], ""
    tcsv = REPORTS / "gate_temporal_stability.csv"
    if tcsv.exists():
        t = pd.read_csv(tcsv)
        t = t[t["horizon"] == 21]
        per_year = [[int(r.year), round(float(r.increment_bps), 1)] for r in t.itertuples()]
        flagged = [int(r.year) for r in t.itertuples() if bool(r.year_flagged)]
        temp_note = (f"regime-concentrated — flagged years {flagged}; 2020 inverts "
                     "(-41.9 bps, z -5.06); 9/15 years strongly positive.")
    # sized monthly equity curve (downsample for the SVG)
    equity = []
    ecsv = Path("data/av/sized_equity_curve.csv")
    if ecsv.exists():
        e = pd.read_csv(ecsv)
        step = max(len(e) // 80, 1)
        equity = [[str(r.date)[:7], round(float(r.equity_ks_off), 4)]
                  for r in e.iloc[::step].itertuples()]
        if str(e.iloc[-1].date)[:7] != equity[-1][0]:
            equity.append([str(e.iloc[-1].date)[:7], round(float(e.iloc[-1].equity_ks_off), 4)])

    rep = {
        "strategy": strategy, "version": 1, "run_mode": "A",
        "status": "validated + sized — Mode-A plumbing + paper pending",
        "updated_at": now, "git_commit": _git_sha(),
        "stages": {
            "1_hypothesis": {"status": "done", "updated_at": now,
                "note": "Economic-only prose -> refined spec. Parity A: spec EMERGED to a full "
                        "match of the consensus reference (M1 75/25 skew corner, M2 IV x RR, M3 "
                        "stall/divergence; freshness 3, sigma 1.0; earnings + short-trend ON, VIX off)."},
            "2_pre_critic": {"status": "pass", "updated_at": now,
                "note": "PASS with warnings: narrow-corner fragility, M3 exhaustion look-ahead, "
                        "long-side survivorship/HTB, cost realism, 75/25 threshold non-stationarity."},
            "3_code": {"status": "pass", "updated_at": now,
                "note": "Coder generated quarantined generated/strategy_skew_modeA.py from the spec; "
                        "100% fire-side fidelity vs compute_consensus (0 disagreements); reproduced "
                        "the Mode-B verdict. Reference module untouched."},
            "4_backtest": {"status": "flagged", "updated_at": now,
                "note": "475,430 fires, 2012-2026, survivorship-free panel. Run via the consensus "
                        "harness (signal_vs_random); the generic Stage-4/5 plumbing "
                        "(quant_validator.backtest/sandbox) is MISSING — Mode-A adapter pending."},
            "5_stats": {"status": "pending", "updated_at": now,
                "note": "Generic stats CLI needs results/returns.csv (the positions x returns shape); "
                        "panel strategy is a fires frame -> Mode-A plumbing gap. Sized stats in stage 9."},
            "6_vs_random": {"status": "pass", "updated_at": now,
                "note": "Date/direction-matched random pool, total-return, full survivorship-free "
                        "universe, from 2012.",
                "verdict": {
                    "5":  {"increment": 1.4, "random": 13.5, "gross": 15.0, "z": 1.40, "p": 0.078, "beat": 0.510},
                    "10": {"increment": 5.9, "random": 27.6, "gross": 33.5, "z": 4.18, "p": 0.0005, "beat": 0.513},
                    "21": {"increment": 18.3, "random": 88.3, "gross": 106.6, "z": 8.98, "p": 0.0005, "beat": 0.516}}},
            "7_validator": {"status": "pending", "updated_at": now,
                "note": "Critic-validator (9-criteria) not yet run end-to-end (Mode-A plumbing); "
                        "pre-critic warnings stand as the open items."},
            "8_gates": {"status": "flagged", "updated_at": now,
                "note": "gates evaluate ran but on degenerate inputs (first_failure: deflated_sharpe) "
                        "because no results/returns.csv exists yet — needs the Mode-A backtest adapter."},
            "9_risk": {"status": "pass", "updated_at": now,
                "note": "Fractional portfolio Kelly lambda=0.25, constant-corr Sigma (rho 0.30), caps "
                        "per-name 5% / gross 1.0 / net 0.5; drawdown kill-switch -15%/-7%/x0.30 "
                        "(NON-binding at lambda=0.25 — sizing alone survives; proven in a full-Kelly "
                        "stress, maxDD -49.6%->-27.9%). Risk Agent: 4/10 -> DEPLOY at 0.5x.",
                "metrics": {"CAGR": "+5.75%", "Sharpe": "1.14", "max drawdown": "-9.85%",
                            "final equity": "2.22x (2012-2026)", "worst month": "2020-02 -7.21%",
                            "2021-01 meme squeeze": "-9.88%/trade -> -0.77% sized",
                            "size multiplier": "0.5x"},
                "equity_curve": equity},
            "10_memory": {"status": "done", "updated_at": now,
                "note": "Trial recorded. Decision: ACCEPT (validated + sized) -> deploy 0.5x; "
                        "paper phase pending. Residual: 2020 selection miss accepted (no robust gate)."}},
        "robustness": {
            "status": "done", "updated_at": now,
            "cost": {"breakeven": {"5": 15.0, "10": 33.3, "21": 106.5}, "realistic_bps": 20,
                     "note": "Equity round-trip breakeven; 5d dies (<20 bps), 21d has a big cushion. "
                             "Edge (vs random) is cost-invariant; absolute profitability is the binding constraint."},
            "temporal": {"per_year": per_year, "note": temp_note},
            "regime": {"note": "ACCEPT + size-to-survive + drawdown kill-switch; NO ex-ante regime "
                               "entry gate (n=1; the tested vol gate BACKFIRED — worsened 2020 and "
                               "discarded +294 bps/trade of profit)."}},
        "phases": {"4_paper": {"status": "not_started", "note": "(promote via deploy when ready)"},
                   "5_live": {"status": "not_started", "note": ""}}}

    out = REPORTS / strategy
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(json.dumps(rep, indent=2), encoding="utf-8")
    print(f"backfilled {out/'report.json'} ({len(per_year)} years, {len(equity)} equity points)")
    return rep


def render(strategy: str) -> Path:
    jpath = REPORTS / strategy / "report.json"
    if not jpath.exists():
        raise SystemExit(f"{jpath} not found — run backfill first.")
    rep = json.loads(jpath.read_text(encoding="utf-8"))
    hpath = REPORTS / strategy / "report.html"
    hpath.write_text(render_html(rep), encoding="utf-8")
    print(f"rendered {hpath} ({hpath.stat().st_size//1024} KB, self-contained)")
    return hpath


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="quant_validator.reporter")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("backfill-v22")
    pr = sub.add_parser("render"); pr.add_argument("--strategy", required=True)
    pb = sub.add_parser("backfill"); pb.add_argument("--strategy", default="skew_consensus_v22_novix")
    args = ap.parse_args(argv)
    if args.cmd == "backfill-v22":
        backfill_v22(); render("skew_consensus_v22_novix")
    elif args.cmd == "backfill":
        backfill_v22(args.strategy); render(args.strategy)
    elif args.cmd == "render":
        render(args.strategy)
    return 0


if __name__ == "__main__":
    sys.exit(main())
