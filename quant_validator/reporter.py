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
         "queued": ("queued", "amber"), "already_open": ("open", "blue"),
         "open": ("held", "ok"), "closed": ("closed", "gray"),
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


def _live_orats_block(p4: dict) -> str:
    """Phase-4 live ORATS feed status: today's fire count + L/S split + signal date."""
    lo = p4.get("live_orats")
    if not lo:
        return ""
    live = bool(lo.get("is_live_orats"))
    fg, bg, bd = (("#0a7a3c", "#e7f4ec", "#bfe3cd") if live else ("#b85c00", "#fbf0e1", "#f0d8b6"))
    chip = _chip("deployed" if live else "pending")
    sane = "count in range" if lo.get("sane_range") else "count OUT OF RANGE"
    return (f'<p class="note" style="margin-top:14px;background:{bg};border:1px solid {bd};'
            f'border-radius:6px;padding:8px 10px;color:{fg}"><b>Live ORATS feed</b> {chip} &middot; '
            f"today's fires: <b>{_esc(lo.get('n_fires'))}</b> "
            f"({_esc(lo.get('n_long'))} long / {_esc(lo.get('n_short'))} short) as of "
            f"<b>{_esc(lo.get('signal_date'))}</b> (latest session {_esc(lo.get('latest_session'))}) "
            f"&middot; {sane}. {_esc(lo.get('note',''))}</p>")


def _owned_block(p4: dict) -> str:
    """Phase-4 kill-switch state + owned-positions table (ledger-tracked, scheduled exits)."""
    ks = p4.get("kill_switch")
    owned = p4.get("owned_positions")
    if not ks and owned is None:
        return ""
    out = ""
    if ks:
        sim = " (simulated)" if ks.get("simulated") else ""
        out += (f'<p class="note" style="margin-top:14px"><b>Kill-switch</b> {_chip(ks.get("state","deployed"))}'
                f' &middot; drawdown {ks.get("drawdown_pct","?")}%{sim} '
                f'(pause &minus;{ks.get("dd_pause_pct",15):.0f}% / hard-flatten &minus;{ks.get("dd_hard_pct",25):.0f}%) '
                f'&middot; equity ${ks.get("equity",0):,.0f} / peak ${ks.get("peak_equity",0):,.0f}. '
                f'Paused &rarr; new entries blocked; hard breach &rarr; owned positions flattened via API.</p>')
    out += '<p class="note"><b>Owned positions</b> (ledger-tracked, ~21-bday scheduled exits):</p>'
    if owned:
        head = ("<thead><tr><th>symbol</th><th>side</th><th>qty</th><th>entry signal</th>"
                "<th>fill</th><th>days held</th><th>scheduled exit</th><th>status</th></tr></thead>")
        rows = ""
        for o in owned:
            sidecls = "neg" if str(o.get("side", "")).upper() in ("SELL", "BULL") else "pos"
            rows += (f'<tr><td>{_esc(o.get("symbol"))}</td><td class="{sidecls}">{_esc(o.get("side"))}</td>'
                     f'<td>{_esc(o.get("qty"))}</td><td>{_esc(o.get("entry_signal_date"))}</td>'
                     f'<td>{_esc(o.get("entry_fill_date") or "&mdash;")}</td><td>{_esc(o.get("days_held"))}</td>'
                     f'<td>{_esc(o.get("scheduled_exit_date"))}</td><td>{_chip(o.get("status","open"))}</td></tr>')
        out += f"<table>{head}<tbody>{rows}</tbody></table>"
    else:
        out += ('<p class="cap">None open &mdash; account flat (entries fired then force-exited in the '
                'controlled proof, or none submitted yet).</p>')
    return out


def _exit_options_block(p4: dict) -> str:
    """Phase-4 'Exit options' (Exit Agent #12) — 2-3 ranked exits per open position."""
    eo = p4.get("exit_options")
    if not eo or not eo.get("positions"):
        return ""
    tag = ("LIVE" if eo.get("live") else "ILLUSTRATIVE (book flat — top live-queue names)")
    vr = ", ".join(eo.get("validated_rules") or []) or "none yet"
    out = (f'<p class="note" style="margin-top:14px"><b>Exit options</b> (Exit Agent #12, {tag}) — '
           f'as of <b>{_esc(eo.get("as_of"))}</b>; OOS-validated rules: <b>{_esc(vr)}</b>. '
           f'Advisory; the 21D backstop is always present.</p>')
    head = ("<thead><tr><th>symbol</th><th>side</th><th>held</th><th>rank</th><th>rule</th>"
            "<th>trigger</th><th>rationale</th><th>proj %</th></tr></thead>")
    rows = ""
    _urg = {"act_now": "red", "target": "ok", "backstop": "gray", "watch": "amber"}
    for pos in eo["positions"]:
        opts = pos.get("options", [])
        for i, o in enumerate(opts):
            sym = _esc(pos["symbol"]) if i == 0 else ""
            side = _esc(pos.get("side")) if i == 0 else ""
            held = f'{pos.get("days_held")}d' if i == 0 else ""
            proj = o.get("projected_return_pct")
            projs = f'{proj:+.2f}%' if isinstance(proj, (int, float)) else "—"
            chipcls = _urg.get(o.get("urgency"), "gray")
            rows += (f'<tr><td>{sym}</td><td>{side}</td><td>{held}</td>'
                     f'<td>{i+1}</td><td><span class="chip {chipcls}">{_esc(o.get("rule"))}</span></td>'
                     f'<td>{_esc(o.get("trigger"))}</td><td>{_esc(o.get("rationale"))}</td><td>{projs}</td></tr>')
    return out + f"<table>{head}<tbody>{rows}</tbody></table>"


def _queue_block(p4: dict) -> str:
    """Phase-4 'Next-open trade queue' — one row per order, sized + shortability-gated."""
    q = p4.get("next_open_queue")
    if not q:
        return ""
    m = p4.get("queue_meta", {})
    skipped = p4.get("shortability_skipped", []) or []
    eq = m.get("equity")
    eq_txt = f"${eq:,.0f}" if isinstance(eq, (int, float)) else _esc(eq)
    hdr = (f'<p class="note" style="margin-top:14px"><b>Next-open trade queue</b> — queue as of '
           f'<b>{_esc(m.get("signal_date"))}</b>, fills at <b>{_esc(m.get("next_session"))}</b> '
           f'(market-on-open / OPG). Stage-2 fractional Kelly &lambda;={_esc(m.get("lambda"))}, '
           f'deploy {_esc(m.get("deploy_mult"))}&times; on {eq_txt}.</p>')
    stats = (f'<p class="cap">{len(q)} orders &middot; {m.get("n_long","?")} long / '
             f'{m.get("n_short","?")} short &middot; gross {m.get("gross_weight","?")} / '
             f'net {m.get("net_weight","?")} &middot; {len(skipped)} non-shortable skipped '
             f'&middot; reconciled vs {len(m.get("reconciled_open_orders", []))} open demo order(s) '
             f'&middot; <b>DRY-RUN</b> (no new orders submitted).</p>')
    head = ("<thead><tr><th>symbol</th><th>dir</th><th>side</th><th>qty</th><th>notional</th>"
            "<th>weight</th><th>type</th><th>tif</th><th>status</th></tr></thead>")
    rows = ""
    for o in q:
        sidecls = "neg" if o.get("side") == "sell" else "pos"
        rows += (f'<tr><td>{_esc(o.get("symbol"))}</td><td>{_esc(o.get("direction"))}</td>'
                 f'<td class="{sidecls}">{_esc(o.get("side"))}</td><td>{_esc(o.get("qty"))}</td>'
                 f'<td>${o.get("notional",0):,.0f}</td><td>{o.get("weight",0)*100:.2f}%</td>'
                 f'<td>{_esc(o.get("type"))}</td><td>{_esc(o.get("tif"))}</td>'
                 f'<td>{_chip(o.get("status","queued"))}</td></tr>')
    table = f"<table>{head}<tbody>{rows}</tbody></table>"
    skip_html = ""
    if skipped:
        items = "; ".join(f'{_esc(s.get("symbol"))} ({_esc(s.get("reason"))})' for s in skipped)
        skip_html = f'<p class="cap"><b>Non-shortable, skipped ({len(skipped)}):</b> {items}</p>'
    lf = m.get("live_feed") or {}
    gap = lf.get("gap", "")
    if not gap:
        gap_html = ""
    elif lf.get("is_live_orats"):
        gap_html = (f'<p class="note" style="background:#e7f4ec;border:1px solid #bfe3cd;border-radius:6px;'
                    f'padding:8px 10px;color:#0a7a3c"><b>Live ORATS feed:</b> {_esc(gap)}</p>')
    else:
        gap_html = (f'<p class="note" style="background:#fbf0e1;border:1px solid #f0d8b6;border-radius:6px;'
                    f'padding:8px 10px;color:#b85c00"><b>Live-feed gap:</b> {_esc(gap)}</p>')
    return hdr + stats + table + skip_html + gap_html


def _mutations_block(rep: dict) -> str:
    """Mutations section (Agent #11): search space, cumulative MT count, best variant + deltas,
    the MT-penalty-adjusted verdict, and the top candidates."""
    m = rep.get("mutations")
    if not m:
        return ""
    b, bs, mt = m["baseline"], m["best"], m["mt_penalty"]
    ss = m["search_space"]
    chip = _chip("pass" if m.get("promoted") else "flagged")
    inner = (f'<p class="note">{chip} <b>{_esc(m.get("verdict"))}</b></p>'
             f'<p class="cap">Thesis-locked search (core skew/RR/IV fade never mutated): '
             f'{ss.get("exit",0)} exit + {ss.get("universe",0)} universe + {ss.get("entry",0)} entry '
             f'= {m.get("n_candidates_this_run")} candidates this run &middot; cumulative MT trials '
             f'{m.get("prior_trials_6b")} (6b) + {m.get("n_candidates_this_run")} = '
             f'<b>{m.get("cumulative_trials")}</b>.</p>')
    inner += _kv_table([
        ("baseline (v22 + 6b exits)",
         f"Sharpe {b['sharpe']} · maxDD {b['maxdd']} · {b['mean_bps']}bps · 2021-01 {b['jan2021_bps']}bps"),
        ("best variant", f"{bs['name']} ({bs['kind']})"),
        ("best vs baseline",
         f"Sharpe Δ{bs['delta_sharpe']:+} · maxDD Δ{bs['delta_maxdd']:+} · "
         f"folds beating baseline {bs['folds_beat_baseline']}"),
        ("MT / DSR penalty",
         f"n_trials {mt['cumulative_trials']} · deflated prob-real {mt['deflated_prob_real']} "
         f"(bar {mt['bar']}) · survives {mt['survives']}")])
    tc = m.get("top_candidates") or []
    if tc:
        head = ("<thead><tr><th>candidate</th><th>kind</th><th>fires</th><th>Sharpe</th>"
                "<th>maxDD</th><th>mean bps</th><th>2021-01</th></tr></thead>")
        rows = "".join(
            f"<tr><td>{_esc(c['name'])}</td><td>{_esc(c['kind'])}</td><td>{c.get('n_fires',0):,}</td>"
            f"<td>{c['pooled_sharpe']}</td><td>{c['pooled_maxdd']}</td><td>{c['pooled_mean_bps']}</td>"
            f"<td>{c['jan2021_bps']}</td></tr>" for c in tc)
        inner += f"<table>{head}<tbody>{rows}</tbody></table>"
    return inner


def render_html(rep: dict) -> str:
    s = rep.get("stages", {})

    def slice_note(key):
        sl = s.get(key, {})
        return sl.get("status", "not_started"), sl.get("updated_at", ""), sl.get("note", "")

    secs = []
    dp = rep.get("deployed_policy")
    if dp:
        secs.append(_section("Deployed policy", "deployed", dp.get("updated_at", ""),
            f'<p class="note"><b>Entry:</b> {_esc(dp.get("entry"))}</p>'
            f'<p class="note"><b>Exit:</b> {_esc(dp.get("exit"))}</p>'
            f'<p class="note"><b>enforce_exits:</b> {_esc(dp.get("enforce_exits"))}</p>'
            f'<p class="note"><b>Benefit (honest):</b> {_esc(dp.get("benefit"))}</p>'
            f'<p class="cap">{_esc(dp.get("mutation_verdict"))}</p>'))
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
        if k == "4_paper":
            ph_inner += _live_orats_block(pl)
            ph_inner += _owned_block(pl)
            ph_inner += _exit_options_block(pl)
            ph_inner += _queue_block(pl)
    ph_status = ph.get("4_paper", {}).get("status", "not_started")
    secs.append(_section("Phases", ph_status, ph.get("4_paper", {}).get("updated_at", ""), ph_inner))

    if rep.get("mutations"):
        secs.append(_section("Mutations (Agent #11)",
                             "pass" if rep["mutations"].get("promoted") else "flagged",
                             rep["mutations"].get("updated_at", ""), _mutations_block(rep)))

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
        n_pos = int((t["increment_bps"] > 0).sum())
        temp_note = (f"regime-concentrated — flagged years {flagged}; 2020 inverts "
                     f"(-41.9 bps, z -5.06); {n_pos}/{len(t)} years positive. 756-bday warm-up "
                     "drops 2012; 2013 partial (Nov-Dec).")
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
        "strategy": strategy, "version": 3, "run_mode": "A",
        "status": "validated + sized",
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
            "4_backtest": {"status": "done", "updated_at": now,
                "note": "Mode-A fires-frame backtest ADAPTER (quant_validator.backtest) closes the "
                        "plumbing: wraps signal_vs_random.run_test (single eligibility screen — de-dup) "
                        "and emits canonical results/ (returns, positions, vs_random, net_return_panel). "
                        "454,798 fires, 2013-2026, survivorship-free. WARM-UP: first fire = panel start "
                        "+ 756 bdays (3yr ORATS, 2013-11-26); standardized from the ad-hoc 252/504 so "
                        "every percentile/sigma window is fully warmed (dropped ~20.6k 2012+early-2013 fires)."},
            "5_stats": {"status": "done", "updated_at": now,
                "note": "Stats CLI reads the adapter's results/returns.csv. Strategy Sharpe 0.98 "
                        "(walk-forward fold-mean 1.31, 1 negative fold); Deflated Sharpe p-value "
                        "0.006, prob-real 0.994 (edge survives deflation for n_trials=3)."},
            "6_vs_random": {"status": "pass", "updated_at": now,
                "note": "Date/direction-matched random pool, total-return, full survivorship-free "
                        "universe, from 2013-11-26 (756-bday / 3yr ORATS warm-up). Single eligibility "
                        "screen (de-duped via the adapter) -> 21d z 8.87, parity with the Mode-B "
                        "reference held under the later start (+18.15 vs +18.28 bps; gross +106.96 vs +106.53).",
                "verdict": {
                    "5":  {"increment": 1.3, "random": 13.0, "gross": 14.3, "z": 1.21, "p": 0.114, "beat": 0.510},
                    "10": {"increment": 5.1, "random": 26.3, "gross": 31.4, "z": 3.54, "p": 0.0005, "beat": 0.513},
                    "21": {"increment": 18.2, "random": 88.8, "gross": 107.0, "z": 8.87, "p": 0.0005, "beat": 0.516}}},
            "7_validator": {"status": "pass", "updated_at": now,
                "note": "Critic-validator 9-criteria + placebo from the prior validation "
                        "(critique_post.json) now backed by present results/. Standing items are the "
                        "pre-critic warnings (narrow-corner fragility, long-side survivorship/HTB)."},
            "8_gates": {"status": "pass", "updated_at": now,
                "note": "All gates PASS on the adapter's artifacts: deflated_sharpe (DSR p=0.006 < 0.95, "
                        "prob-real 0.994), correlation (no survivors), pca (single-asset N/A), vs_random "
                        "(fires-adapter verdict). first_failure null; missing-input now reports "
                        "not_available, not a spurious fail."},
            "9_risk": {"status": "pass", "updated_at": now,
                "note": "Fractional portfolio Kelly lambda=0.25, constant-corr Sigma (rho 0.30), caps "
                        "per-name 5% / gross 1.0 / net 0.5; drawdown kill-switch -15%/-7%/x0.30 "
                        "(NON-binding at lambda=0.25 — sizing alone survives; proven in a full-Kelly "
                        "stress, maxDD -49.6%->-27.4%). Risk Agent: 4/10 -> DEPLOY at 0.5x.",
                "metrics": {"CAGR": "+5.82%", "Sharpe": "1.12", "max drawdown": "-9.86%",
                            "final equity": "2.02x (2013-2026)", "worst month": "2020-02 -7.20%",
                            "2021-01 meme squeeze": "-9.88%/trade -> -0.88% sized",
                            "size multiplier": "0.5x"},
                "equity_curve": equity},
            "10_memory": {"status": "done", "updated_at": now,
                "note": "Trial recorded. Decision: ACCEPT (validated + sized) -> deploy 0.5x; "
                        "paper phase pending. Residual: 2020 selection miss accepted (no robust gate)."}},
        "robustness": {
            "status": "done", "updated_at": now,
            "cost": {"breakeven": {"5": 14.3, "10": 31.4, "21": 107.0}, "realistic_bps": 20,
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
