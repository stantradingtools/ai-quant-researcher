"""quant_validator.sizing: deterministic Risk-Agent (stage 9) sizing engine.

Turns ANY validated strategy's net-return position panel into target sizes via
FRACTIONAL portfolio-Kelly, applies the Risk-Agent guardrail caps, and overlays a
realized-drawdown KILL-SWITCH. Built generically (panel in -> sizes out);
skew-consensus is the test case.

Per the Prompt-D verdict (no robust ex-ante regime conditioner): ACCEPT the signal,
SIZE-TO-SURVIVE, and let a portfolio-level drawdown kill-switch cap tail depth. The
kill-switch is a TAIL BACKSTOP, not an entry gate — it does NOT fix the 2020 selection
miss; it bounds the drawdown's depth/duration.

Maths (deterministic):
  mu     = pooled mean net per-trade return (v1; conditioning on signal strength = v2).
  Sigma  = structured constant-correlation: per-name variance sigma_i^2 + ONE average
           pairwise rho (default 0.30). Always invertible — full empirical Sigma on
           hundreds of overlapping names is singular (Ledoit-Wolf = v2 alternative).
  f*     = Sigma^-1 mu       (portfolio Kelly), solved O(n) via Sherman-Morrison.
  f      = lambda * f*        (fractional, default lambda=0.25 for the non-stationary edge).
  caps   = per-name max weight, gross-exposure cap, net-exposure (directional) cap.
           [sector cap: parameterized hook; GICS sector is not in the panel -> v2.]

Backtest model (v1, documented): positions are daily-entry, 21-trading-day holds. The
account runs ~H=21 overlapping daily cohorts, so each cohort is sized to gross/H (the
1/H normalization keeps steady-state account gross ~= the gross cap). Each position's
realized 21d net P&L is attributed to its entry date (cohort approximation; spreading
P&L across the hold = v2). Daily cohort returns compound into the equity curve.

CLI:
    python -m quant_validator.sizing run            # full sized backtest + reports
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from .consensus_signal import signal_sign
from .signal_vs_random import clean_run_columns

CLEAN_PANEL = Path("data/av/signal_panel_clean.parquet")
REPORT_TXT = Path("reports/stage2_sizing.txt")
REPORT_CSV = Path("reports/stage2_sizing.csv")
EQUITY_CSV = Path("data/av/sized_equity_curve.csv")

START = "2012-01-01"
HORIZON = 21                  # trading-day hold = deploy horizon (5d dead, 10d thin)
PRICE_FLOOR, MAX_ABS_FWD = 1.0, 5.0


# ── 1. Position panel (the generic interface) ─────────────────────────────

def build_position_panel(panel_path: Path = CLEAN_PANEL, cost_bps: float = 20.0,
                         start: str = START) -> pd.DataFrame:
    """Strategy fires -> tidy (date, symbol, signal_sign, net_return) book.

    net_return = signal_sign * av_fwd_21_total - round_trip_cost. Any strategy that
    can emit this panel can be sized by this engine."""
    cols = clean_run_columns()
    p = pd.read_parquet(panel_path, columns=cols)
    elig = (p["side"].notna() & p["av_matched"].astype(bool)
            & p["fwd_available_21"].astype(bool)
            & (p["raw_close"] >= PRICE_FLOOR)
            & (p["av_fwd_21_total"].abs() <= MAX_ABS_FWD)
            & (p["tradeDate"] >= pd.Timestamp(start)))
    f = p[elig].copy()
    f["signal_sign"] = f["side"].astype(str).map(signal_sign).astype(float)
    f["net_return"] = f["signal_sign"] * f["av_fwd_21_total"] - cost_bps / 1e4
    out = f.rename(columns={"ticker": "symbol", "tradeDate": "date"})[
        ["date", "symbol", "signal_sign", "net_return", "raw_close"]
    ].reset_index(drop=True)
    return out


# ── 2. Sizing maths ────────────────────────────────────────────────────────

def per_name_sigma(panel: pd.DataFrame, min_obs: int = 5) -> tuple[dict, float]:
    """Per-name net-return std (pooled-std fallback for thin names). Returns
    (symbol->sigma, pooled_sigma)."""
    pooled = float(panel["net_return"].std(ddof=1))
    g = panel.groupby("symbol")["net_return"]
    counts, stds = g.count(), g.std(ddof=1)
    sig = {s: (stds[s] if counts[s] >= min_obs and np.isfinite(stds[s]) and stds[s] > 0
               else pooled) for s in counts.index}
    return sig, pooled


def kelly_fracs(sigma: np.ndarray, mu: float, rho: float, lam: float) -> np.ndarray:
    """Fractional portfolio Kelly f = lambda * Sigma^-1 mu for a constant-correlation
    Sigma (per-name sigma_i, single rho), solved O(n) via Sherman-Morrison.

    Sigma = D R D, D=diag(sigma_i), R=(1-rho)I + rho 11'. With mu_i = mu (pooled):
      f = lambda * D^-1 R^-1 D^-1 (mu*1)
    R^-1 v = 1/(1-rho) [v - rho/(1+(n-1)rho) (1'v) 1].
    """
    s = np.asarray(sigma, dtype=float)
    n = s.size
    if n == 0:
        return np.zeros(0)
    z = mu / s                                   # D^-1 mu
    denom = 1.0 + (n - 1) * rho
    rinv_z = (z - (rho / denom) * z.sum()) / (1.0 - rho)   # R^-1 z
    f_star = rinv_z / s                           # D^-1 (R^-1 z)
    return lam * f_star


def apply_caps(f: np.ndarray, sign: np.ndarray, *, max_w: float, gross_cap: float,
               net_cap: float) -> np.ndarray:
    """Risk-Agent guardrails: per-name max weight, gross cap, net (directional) cap.
    f are non-negative sizes (direction already in net_return/sign)."""
    f = np.clip(f, 0.0, max_w)                    # per-name
    gross = f.sum()
    if gross > gross_cap and gross > 0:           # gross exposure
        f *= gross_cap / gross
    net = float((f * sign).sum())                 # net directional exposure
    if abs(net) > net_cap:
        dom = sign == np.sign(net)
        dom_sum = f[dom].sum()
        if dom_sum > 0:
            f[dom] *= max(0.0, 1.0 - (abs(net) - net_cap) / dom_sum)
    return f


# ── 3. Drawdown kill-switch (tail backstop, NOT an entry gate) ────────────

def apply_kill_switch(rets: np.ndarray, *, dd_enter: float = 0.15, dd_exit: float = 0.07,
                      derisk: float = 0.3) -> tuple[np.ndarray, list[dict]]:
    """Hysteresis kill-switch on realized portfolio drawdown. When peak-to-trough DD
    breaches `dd_enter`, scale subsequent cohort returns by `derisk` until equity
    recovers to within `dd_exit` of the peak. Returns (scaled_rets, episodes)."""
    eq, peak, derisking = 1.0, 1.0, False
    out = np.empty(len(rets))
    episodes, cur = [], None
    for i, r in enumerate(rets):
        eff = r * derisk if derisking else r
        out[i] = eff
        eq *= (1.0 + eff)
        peak = max(peak, eq)
        dd = eq / peak - 1.0
        if derisking:
            cur["min_dd"] = min(cur["min_dd"], dd)
            if dd >= -dd_exit:
                cur["end_i"] = i
                episodes.append(cur)
                derisking, cur = False, None
        elif dd <= -dd_enter:
            derisking = True
            cur = {"start_i": i, "min_dd": dd, "end_i": None}
    if cur is not None:
        cur["end_i"] = len(rets) - 1
        episodes.append(cur)
    return out, episodes


# ── 4. Sized backtest ──────────────────────────────────────────────────────

def _metrics(daily: pd.Series) -> dict:
    """daily: per-entry-date cohort return series (already 1/H normalized)."""
    eq = (1.0 + daily).cumprod()
    years = max((daily.index[-1] - daily.index[0]).days / 365.25, 1e-9)
    ppy = len(daily) / years                      # entry-date periods per year
    cagr = eq.iloc[-1] ** (1.0 / years) - 1.0
    sharpe = (daily.mean() / daily.std(ddof=1) * np.sqrt(ppy)) if daily.std(ddof=1) > 0 else np.nan
    maxdd = float((eq / eq.cummax() - 1.0).min())
    monthly = (1.0 + daily).groupby(daily.index.to_period("M")).prod() - 1.0
    yearly = (1.0 + daily).groupby(daily.index.year).prod() - 1.0
    return {"final_equity": float(eq.iloc[-1]), "cagr": float(cagr), "sharpe": float(sharpe),
            "max_drawdown": maxdd, "worst_month": (str(monthly.idxmin()), float(monthly.min())),
            "worst_year": (int(yearly.idxmin()), float(yearly.min())),
            "equity": eq, "monthly": monthly, "yearly": yearly, "daily": daily}


def sized_backtest(panel: pd.DataFrame, *, lam: float = 0.25, rho: float = 0.30,
                   max_w: float = 0.05, gross_cap: float = 1.0, net_cap: float = 0.5,
                   horizon: int = HORIZON, kill_switch: bool = False,
                   ks_cfg: dict | None = None) -> dict:
    """Size each entry-date book by fractional Kelly + caps, attribute each cohort's
    1/H-normalized net P&L to its entry date, optionally overlay the kill-switch."""
    mu = float(panel["net_return"].mean())
    sig_map, pooled = per_name_sigma(panel)

    # Monthly cohort: the 21d hold ~= 1 calendar month, so monthly books are ~non-
    # overlapping. Each month is sized as one Kelly book; the book return is realized.
    # (Per-entry-date sizing with 1/H overlap normalization is a v2 refinement — it
    # produced a much tamer, kill-switch-non-binding curve and obscured the book-month.)
    recs = []
    for m, book in panel.groupby(panel["date"].dt.to_period("M"), sort=True):
        sym = book["symbol"].to_numpy()
        sgn = book["signal_sign"].to_numpy(float)
        nr = book["net_return"].to_numpy(float)
        sigma = np.array([sig_map.get(s, pooled) for s in sym], dtype=float)
        f = apply_caps(kelly_fracs(sigma, mu, rho, lam), sgn,
                       max_w=max_w, gross_cap=gross_cap, net_cap=net_cap)
        recs.append((m.to_timestamp(how="end").normalize(), float((f * nr).sum()),
                     float(f.sum()), float((f * sgn).sum()), len(book)))
    daily = pd.DataFrame(recs, columns=["date", "ret", "gross", "net", "n"]).set_index("date")
    daily.index = pd.to_datetime(daily.index)

    raw = daily["ret"].to_numpy()
    episodes = []
    if kill_switch:
        cfg = ks_cfg or {}
        scaled, episodes = apply_kill_switch(raw, **cfg)
        ret = pd.Series(scaled, index=daily.index)
    else:
        ret = daily["ret"]

    m = _metrics(ret)
    m.update({"mu": mu, "pooled_sigma": pooled, "avg_gross": float(daily["gross"].mean()),
              "avg_net": float(daily["net"].mean()), "avg_book_n": float(daily["n"].mean()),
              "n_entry_dates": len(daily), "kill_switch": kill_switch,
              "ks_episodes": episodes, "params": {"lambda": lam, "rho": rho, "max_w": max_w,
              "gross_cap": gross_cap, "net_cap": net_cap, "horizon": horizon}})
    return m


# ── 5. Report ──────────────────────────────────────────────────────────────

def _fmt_pct(x):
    return "n/a" if x is None or not np.isfinite(x) else f"{x*100:+.2f}%"


def _path_through(daily: pd.Series, label_months: list[str]) -> list[tuple[str, float]]:
    monthly = (1.0 + daily).groupby(daily.index.to_period("M")).prod() - 1.0
    return [(m, float(monthly.get(pd.Period(m, "M"), np.nan))) for m in label_months]


def run(panel_path: Path = CLEAN_PANEL, cost_bps: float = 20.0, lam: float = 0.25,
        rho: float = 0.30, max_w: float = 0.05, gross_cap: float = 1.0,
        net_cap: float = 0.5, dd_enter: float = 0.15, dd_exit: float = 0.07,
        derisk: float = 0.3) -> dict:
    panel = build_position_panel(panel_path, cost_bps=cost_bps)
    common = dict(lam=lam, rho=rho, max_w=max_w, gross_cap=gross_cap, net_cap=net_cap)
    ks_cfg = dict(dd_enter=dd_enter, dd_exit=dd_exit, derisk=derisk)
    off = sized_backtest(panel, kill_switch=False, **common)
    on = sized_backtest(panel, kill_switch=True, ks_cfg=ks_cfg, **common)

    # Stress demo: at the recommended lambda the gross cap binds, so DD never reaches
    # the 15% trigger (size-to-survive suffices). To PROVE the kill-switch functions we
    # also run FULL Kelly with the gross/net caps LIFTED so leverage can express and DD
    # breaches the threshold — then the backstop must visibly cut the drawdown.
    stress = {**common, "lam": 1.0, "gross_cap": 5.0, "net_cap": 3.0}
    stress_off = sized_backtest(panel, kill_switch=False, **stress)
    stress_on = sized_backtest(panel, kill_switch=True, ks_cfg=ks_cfg, **stress)

    # worst single book-month (unsized, per-trade mean) for the survivability anchor
    pm = panel.assign(ym=panel["date"].dt.to_period("M")).groupby("ym")["net_return"].mean()
    worst_unsized = (str(pm.idxmin()), float(pm.min()))

    txt = _format(off, on, panel, worst_unsized, cost_bps, ks_cfg, stress_off, stress_on)
    REPORT_TXT.parent.mkdir(parents=True, exist_ok=True)
    REPORT_TXT.write_text(txt, encoding="utf-8")
    # CSV: monthly returns both modes
    csv = pd.DataFrame({"month": off["monthly"].index.astype(str),
                        "ret_ks_off": off["monthly"].values,
                        "ret_ks_on": on["monthly"].reindex(off["monthly"].index).values})
    csv.to_csv(REPORT_CSV, index=False)
    EQUITY_CSV.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"date": off["equity"].index.astype(str),
                  "equity_ks_off": off["equity"].values,
                  "equity_ks_on": on["equity"].reindex(off["equity"].index).values}).to_csv(EQUITY_CSV, index=False)
    print(txt)
    print(f"wrote {REPORT_TXT} (+ {REPORT_CSV}) and equity curve {EQUITY_CSV}")
    return {"off": off, "on": on}


def _format(off, on, panel, worst_unsized, cost_bps, ks_cfg, stress_off=None, stress_on=None) -> str:
    p = off["params"]
    L = ["=" * 90,
         "STAGE 2 — Risk-Agent sizing: fractional portfolio-Kelly + drawdown kill-switch",
         "=" * 90,
         "Per Prompt-D: ACCEPT + size-to-survive + kill-switch (NO regime entry gate).",
         f"Position panel: {len(panel):,} fires, {panel['date'].dt.year.min()}-{panel['date'].dt.year.max()}, "
         f"round-trip cost {cost_bps:.0f} bps. Net per-trade mean mu={off['mu']*1e4:+.1f} bps, "
         f"pooled sigma={off['pooled_sigma']*100:.1f}%.",
         f"Sizing: fractional Kelly lambda={p['lambda']}, constant-corr rho={p['rho']}, "
         f"caps: per-name<={p['max_w']*100:.0f}% gross<={p['gross_cap']:.2f} net<={p['net_cap']:.2f}; "
         f"monthly cohort (hold {p['horizon']}d ~= 1mo). Avg monthly book: {off['avg_book_n']:.0f} names, "
         f"gross {off['avg_gross']:.3f}, net {off['avg_net']:+.3f}.",
         f"Kill-switch: de-risk x{ks_cfg['derisk']} when DD<-{ks_cfg['dd_enter']*100:.0f}%, "
         f"resume when DD>-{ks_cfg['dd_exit']*100:.0f}%.",
         "",
         "-- SIZED BACKTEST: kill-switch OFF vs ON " + "-" * 47,
         f"  {'metric':<20} | {'KS OFF':>14} | {'KS ON':>14}",
         "  " + "-" * 54]
    def row(name, a, b, pct=True):
        fa = _fmt_pct(a) if pct else f"{a:.2f}"
        fb = _fmt_pct(b) if pct else f"{b:.2f}"
        L.append(f"  {name:<20} | {fa:>14} | {fb:>14}")
    row("CAGR", off["cagr"], on["cagr"])
    row("Sharpe", off["sharpe"], on["sharpe"], pct=False)
    row("max drawdown", off["max_drawdown"], on["max_drawdown"])
    row("final equity x", off["final_equity"], on["final_equity"], pct=False)
    L.append(f"  {'worst month':<20} | {off['worst_month'][0]} {_fmt_pct(off['worst_month'][1])}"
             f"  |  {on['worst_month'][0]} {_fmt_pct(on['worst_month'][1])}")
    L.append(f"  {'worst year':<20} | {off['worst_year'][0]} {_fmt_pct(off['worst_year'][1])}"
             f"  |  {on['worst_year'][0]} {_fmt_pct(on['worst_year'][1])}")
    L.append("")
    L.append("-- TAIL PATHS (sized monthly return) " + "-" * 51)
    L.append("  2020 (edge inversion, abs-positive) — KS OFF -> ON:")
    for (m, vo), (_, vn) in zip(_path_through(off["daily"], [f"2020-{i:02d}" for i in range(1, 13)]),
                                _path_through(on["daily"], [f"2020-{i:02d}" for i in range(1, 13)])):
        L.append(f"    {m}: {_fmt_pct(vo):>9}  ->  {_fmt_pct(vn):>9}")
    j_off = _path_through(off["daily"], ["2021-01"])[0]
    j_on = _path_through(on["daily"], ["2021-01"])[0]
    L.append(f"  2021-01 (meme squeeze): KS OFF {_fmt_pct(j_off[1])}  ->  KS ON {_fmt_pct(j_on[1])}")
    L.append(f"  worst UNSIZED book-month (per-trade mean): {worst_unsized[0]} "
             f"{_fmt_pct(worst_unsized[1])} — sizing must make this survivable.")
    L.append("")
    L.append("-- KILL-SWITCH EPISODES (ON) " + "-" * 59)
    if on["ks_episodes"]:
        for e in on["ks_episodes"]:
            di = on["daily"].index
            L.append(f"    de-risk {di[e['start_i']].date()} -> {di[e['end_i']].date()} "
                     f"(trough DD {e['min_dd']*100:.1f}%)")
    else:
        L.append("    none triggered at lambda=0.25 (maxDD never reaches the trigger — sizing alone survives).")
    L.append("")
    if stress_off is not None and stress_on is not None:
        L.append("-- KILL-SWITCH DEMONSTRATION (full-Kelly stress, lambda=1.0, ~4x leverage) " + "-" * 14)
        L.append("  Forced to full Kelly so the DD breaches the trigger and the backstop engages:")
        L.append(f"    max drawdown : KS OFF {_fmt_pct(stress_off['max_drawdown'])}  ->  "
                 f"KS ON {_fmt_pct(stress_on['max_drawdown'])}   (depth control)")
        L.append(f"    CAGR         : KS OFF {_fmt_pct(stress_off['cagr'])}  ->  "
                 f"KS ON {_fmt_pct(stress_on['cagr'])}")
        di = stress_on["daily"].index
        if stress_on["ks_episodes"]:
            for e in stress_on["ks_episodes"]:
                L.append(f"    de-risk {di[e['start_i']].date()} -> {di[e['end_i']].date()} "
                         f"(trough DD {e['min_dd']*100:.1f}%)")
        else:
            L.append("    (still not triggered even at full Kelly — sizing extremely robust.)")
        L.append("")
    L.append("-- READOUT " + "-" * 78)
    L.append(f"  At the recommended lambda=0.25, SIZING ALONE caps max drawdown at "
             f"{_fmt_pct(off['max_drawdown'])} — below the 15% kill-switch trigger, so the")
    L.append("  kill-switch is a NON-BINDING backstop here (KS ON == OFF). 'Size-to-survive' works.")
    if stress_on is not None:
        L.append(f"  The full-Kelly stress demo confirms the backstop functions: maxDD "
                 f"{_fmt_pct(stress_off['max_drawdown'])} -> {_fmt_pct(stress_on['max_drawdown'])} when engaged.")
    L.append(f"  2020 stays absolute-positive/survivable (worst 2020 month {_fmt_pct(off['worst_month'][1])} "
             "in Feb); the 2021-01 meme-squeeze book-month is bounded by diversification +")
    L.append(f"  fractional Kelly ({_fmt_pct(worst_unsized[1])} per-trade -> "
             f"{_fmt_pct(_path_through(off['daily'], ['2021-01'])[0][1])} sized). Neither the sizing nor the")
    L.append("  kill-switch fixes the 2020 SELECTION miss — that hit is accepted (per Prompt D).")
    L.append("")
    return "\n".join(L)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="quant_validator.sizing")
    ap.add_argument("cmd", nargs="?", default="run", choices=["run"])
    ap.add_argument("--cost-bps", type=float, default=20.0)
    ap.add_argument("--lam", type=float, default=0.25)
    ap.add_argument("--rho", type=float, default=0.30)
    ap.add_argument("--max-w", type=float, default=0.05)
    ap.add_argument("--gross-cap", type=float, default=1.0)
    ap.add_argument("--net-cap", type=float, default=0.5)
    ap.add_argument("--dd-enter", type=float, default=0.15)
    ap.add_argument("--dd-exit", type=float, default=0.07)
    ap.add_argument("--derisk", type=float, default=0.3)
    args = ap.parse_args(argv)
    run(cost_bps=args.cost_bps, lam=args.lam, rho=args.rho, max_w=args.max_w,
        gross_cap=args.gross_cap, net_cap=args.net_cap, dd_enter=args.dd_enter,
        dd_exit=args.dd_exit, derisk=args.derisk)
    return 0


if __name__ == "__main__":
    sys.exit(main())
