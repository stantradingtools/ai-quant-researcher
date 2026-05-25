"""quant_validator.fills: pluggable FillSource seam for Phase-4 paper / Phase-5 live.

ONE interface, swappable implementations (config: fills = modeled | alpaca_paper):
  - ModeledFillSource  — v1: deterministic next-session-open modeled fill (no broker).
  - AlpacaFillSource   — real paper fills via alpaca-py TradingClient(paper=True).

Phase 5 (live) = AlpacaFillSource(paper=False) + live keys — the SAME code path; gate
go-live on a paper track-record threshold (see PHASE5 note below).

Real-world execution filters baked in (AlpacaFillSource):
  - Shortability gate (short side only): get_asset must be tradable & shortable
    (& easy_to_borrow). Non-shortable -> SKIP + log (NOT a hard reject). The key filter.
  - Side: BEAR/long -> BUY; BULL/short -> SELL (short). TIF OPG (market-on-open, mirrors
    the v1 next-bar-open fill) or DAY (param). notional or qty (fractional via fractionable).
  - Poll order -> ACTUAL fill price/qty; handle partial / rejected / halted.
The PaperTracker manages the book: no simultaneous long+short (flip => close first) and
scheduled ~21-trading-day exits (Alpaca has no native exit-in-N-days). Bracket/trailing
exits are left as a seam for the Mutation Agent's Bollinger work.

CLI (DRY-RUN by default everywhere; live actions require explicit --submit):
    python -m quant_validator.fills daily        # EOD production loop on the LIVE ORATS feed:
        #   refresh feed -> manage (exits + kill-switch) -> current-signal queue (staleness
        #   passes) -> first-day REVIEW GATE (dry-run). Fire with --submit (start graduated):
        #   ... daily --submit --deploy-fraction 0.25 --limit 10 --max-notional 5000
    python -m quant_validator.fills manage --submit            # close 21bd expiries + kill-switch
    python -m quant_validator.fills queue --submit --limit 4 --max-notional 2000  # static-panel book
    python -m quant_validator.fills clean-demos --submit       # cancel/close the demo orders

Unattended scheduling (Windows Task Scheduler, after-close EOD ~17:15 ET) — see the block
comment above `daily()` for the exact `schtasks /Create` line. `manage` runs every session.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone

import pandas as pd

from .signal_vs_random import WARMUP_BDAYS

HOLD_BDAYS = 21
# Live signal warm-up: for each next signal date, the live signal is computed off the
# trailing WARMUP_BDAYS (756 = 3yr) ORATS window — the SAME convention as the backtest,
# so the paper signal matches what was validated. (The demo book below reads the
# already-warmed clean panel; the production live-compute path slices the trailing 756.)
LIVE_SIGNAL_WARMUP_BDAYS = WARMUP_BDAYS
REPORTS = pathlib.Path("reports")


# ── env (adapters read os.environ; load .env if a key is absent) ──────────

def _load_env() -> None:
    p = pathlib.Path(".env")
    if not p.exists():
        return
    for ln in p.read_text().splitlines():
        ln = ln.strip()
        if ln and not ln.startswith("#") and "=" in ln:
            k, v = ln.split("=", 1)
            v = v.strip().strip('"').strip("'")
            if v and not os.environ.get(k.strip()):
                os.environ[k.strip()] = v


# ── data records ───────────────────────────────────────────────────────────

@dataclass
class TargetPosition:
    symbol: str
    side: str                 # 'BULL' (fade -> short) | 'BEAR' (fade -> long)
    signal_sign: int          # -1 short, +1 long
    notional: float | None = None
    qty: float | None = None
    ref_price: float | None = None     # for the modeled fill / notional->qty
    entry_date: str | None = None
    client_order_id: str | None = None  # idempotency key f"{strategy}:{symbol}:{signal_date}"


@dataclass
class Fill:
    symbol: str
    order_side: str           # BUY | SELL
    qty: float
    price: float
    status: str               # filled | partial | accepted | rejected | halted | skipped_not_shortable
    order_id: str | None = None
    submitted_at: str = ""
    note: str = ""


# ── the seam ────────────────────────────────────────────────────────────────

class FillSource(ABC):
    """Implementations turn a TargetPosition into a Fill and can close/reconcile."""

    @abstractmethod
    def submit(self, t: TargetPosition) -> Fill: ...

    @abstractmethod
    def close(self, symbol: str, qty: float, open_sign: int) -> Fill: ...

    def reconcile(self, intended: dict[str, float]) -> dict:
        """Compare intended book to the venue. Default: nothing to reconcile (modeled)."""
        return {"source": type(self).__name__, "discrepancies": []}


class ModeledFillSource(FillSource):
    """v1 — deterministic modeled fill at the next-session reference price (no broker).
    Mirrors the vectorized/backtest 'next-bar open' assumption. Always fills."""

    def submit(self, t: TargetPosition) -> Fill:
        px = float(t.ref_price) if t.ref_price else 0.0
        qty = (t.qty if t.qty is not None
               else (t.notional / px if t.notional and px > 0 else 0.0))
        side = "BUY" if t.signal_sign > 0 else "SELL"
        return Fill(t.symbol, side, round(qty, 4), px, "filled",
                    order_id=f"modeled-{t.symbol}", submitted_at=_now(), note="modeled next-open fill")

    def close(self, symbol: str, qty: float, open_sign: int) -> Fill:
        side = "SELL" if open_sign > 0 else "BUY"
        return Fill(symbol, side, qty, 0.0, "filled", order_id=f"modeled-close-{symbol}",
                    submitted_at=_now(), note="modeled close")


class AlpacaFillSource(FillSource):
    """Real (paper) fills via alpaca-py. Phase 5 live = paper=False + live keys."""

    def __init__(self, paper: bool = True, tif: str = "OPG", poll_s: float = 1.5,
                 poll_n: int = 4):
        _load_env()
        ak, sk = os.environ.get("ALPACA_API_KEY"), os.environ.get("ALPACA_SECRET_KEY")
        if not ak or not sk:
            raise RuntimeError("ALPACA_API_KEY / ALPACA_SECRET_KEY not set in env/.env")
        from alpaca.trading.client import TradingClient
        from alpaca.trading.enums import OrderSide, TimeInForce
        self._OrderSide, self._TIF = OrderSide, TimeInForce
        self.client = TradingClient(ak, sk, paper=paper)
        self.paper = paper
        self.tif = tif
        self.poll_s, self.poll_n = poll_s, poll_n
        self.api_log: list[str] = []          # every venue API call (auditable; no key/URL leak)

    def _api(self, desc: str) -> None:
        self.api_log.append(desc)
        print(f"   [api{'/paper' if self.paper else '/LIVE'}] {desc}")

    # ── logged read wrappers (every API call goes through one of these) ──
    def account(self):
        self._api("get_account")
        return self.client.get_account()

    def positions(self) -> list:
        self._api("get_all_positions")
        return list(self.client.get_all_positions())

    def open_orders(self) -> list:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest
        self._api("get_orders(status=OPEN)")
        return list(self.client.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.OPEN)))

    def clock(self):
        self._api("get_clock")
        return self.client.get_clock()

    def cancel(self, order_id: str) -> bool:
        self._api(f"cancel_order_by_id {order_id}")
        try:
            self.client.cancel_order_by_id(order_id)
            return True
        except Exception as e:  # noqa: BLE001
            self._api(f"cancel FAILED {order_id}: {type(e).__name__}")
            return False

    def equity_series(self, period: str = "1M", timeframe: str = "1D") -> list[float]:
        """Account equity series for the drawdown kill-switch (best-effort)."""
        try:
            from alpaca.trading.requests import GetPortfolioHistoryRequest
            self._api(f"get_portfolio_history({period},{timeframe})")
            ph = self.client.get_portfolio_history(GetPortfolioHistoryRequest(period=period, timeframe=timeframe))
            return [float(x) for x in (ph.equity or []) if x is not None]
        except Exception as e:  # noqa: BLE001
            self._api(f"get_portfolio_history unavailable: {type(e).__name__}")
            return []

    def _shortable(self, symbol: str) -> tuple[bool, str]:
        self._api(f"get_asset {symbol}")
        try:
            a = self.client.get_asset(symbol)
        except Exception as e:  # noqa: BLE001
            return False, f"asset lookup failed: {type(e).__name__}"
        if not a.tradable:
            return False, "not tradable"
        if not getattr(a, "shortable", False):
            return False, "not shortable"
        if not getattr(a, "easy_to_borrow", False):
            return False, "not easy-to-borrow (HTB)"
        return True, "shortable & ETB"

    def submit(self, t: TargetPosition) -> Fill:
        import time
        from alpaca.trading.requests import MarketOrderRequest
        # PRE-TRADE shortability gate — short side only. Skip (don't hard-reject).
        if t.signal_sign < 0:
            ok, why = self._shortable(t.symbol)
            if not ok:
                return Fill(t.symbol, "SELL", 0.0, 0.0, "skipped_not_shortable",
                            submitted_at=_now(), note=why)
        side = self._OrderSide.BUY if t.signal_sign > 0 else self._OrderSide.SELL
        tif = getattr(self._TIF, self.tif, self._TIF.DAY)
        # OPG does not support fractional notional; prefer qty for OPG, notional otherwise.
        kw = {"symbol": t.symbol, "side": side, "time_in_force": tif}
        if t.qty is not None:
            kw["qty"] = t.qty
        else:
            kw["notional"] = t.notional
        if t.client_order_id:
            kw["client_order_id"] = t.client_order_id   # idempotency: Alpaca rejects duplicates
        self._api(f"submit_order {t.symbol} {side.value} qty={t.qty} tif={self.tif} coid={t.client_order_id}")
        try:
            order = self.client.submit_order(MarketOrderRequest(**kw))
        except Exception as e:  # noqa: BLE001 — reject/halt/BP/duplicate, surface (no key/URL leak)
            msg = str(e)
            # duplicate client_order_id => already submitted on a prior run (idempotent no-op)
            if "client_order_id" in msg.lower() or "already exist" in msg.lower() or "duplicate" in msg.lower():
                return Fill(t.symbol, str(side.value), 0.0, 0.0, "duplicate",
                            order_id=t.client_order_id, submitted_at=_now(),
                            note="idempotent skip: client_order_id already submitted")
            return Fill(t.symbol, str(side.value), 0.0, 0.0, "rejected",
                        submitted_at=_now(), note=f"{type(e).__name__}: {msg[:120]}")
        oid = str(order.id)
        # Poll for an actual fill (market may be closed -> stays accepted/pending until open).
        st = str(order.status).split(".")[-1].lower()
        fp, fq = order.filled_avg_price, order.filled_qty
        for _ in range(self.poll_n):
            if st in ("filled", "rejected", "canceled", "expired"):
                break
            time.sleep(self.poll_s)
            o = self.client.get_order_by_id(oid)
            st = str(o.status).split(".")[-1].lower()
            fp, fq = o.filled_avg_price, o.filled_qty
        status = ("filled" if st == "filled"
                  else "partial" if (fq and float(fq) > 0)
                  else "rejected" if st in ("rejected", "canceled", "expired")
                  else "accepted")        # queued (e.g. market closed / OPG)
        return Fill(t.symbol, str(side.value), float(fq or 0), float(fp or 0), status,
                    order_id=oid, submitted_at=_now(),
                    note=f"alpaca status={st} tif={self.tif}")

    def close(self, symbol: str, qty: float, open_sign: int) -> Fill:
        self._api(f"close_position {symbol} (covering {'long' if open_sign > 0 else 'short'})")
        try:
            o = self.client.close_position(symbol)
            return Fill(symbol, "SELL" if open_sign > 0 else "BUY", float(qty), 0.0,
                        "accepted", order_id=str(getattr(o, "id", "")), submitted_at=_now(),
                        note="alpaca close_position")
        except Exception as e:  # noqa: BLE001
            return Fill(symbol, "", 0.0, 0.0, "rejected", submitted_at=_now(),
                        note=f"close failed {type(e).__name__}: {str(e)[:100]}")

    def reconcile(self, intended: dict[str, float]) -> dict:
        acct = self.account()
        positions = {p.symbol: p for p in self.positions()}
        disc = []
        for sym, want_sign in intended.items():
            p = positions.get(sym)
            have = 0 if p is None else (1 if float(p.qty) > 0 else -1)
            if have != (1 if want_sign > 0 else -1):
                disc.append({"symbol": sym, "intended_sign": int(want_sign),
                             "venue_sign": have, "venue_qty": (float(p.qty) if p else 0.0)})
        return {"source": "AlpacaFillSource",
                "equity": float(acct.equity), "buying_power": float(acct.buying_power),
                "cash": float(acct.cash), "n_venue_positions": len(positions),
                "discrepancies": disc}


def make_fill_source(mode: str, **kw) -> FillSource:
    """Config swap: fills = modeled | alpaca_paper (| alpaca_live for Phase 5)."""
    if mode == "modeled":
        return ModeledFillSource()
    if mode == "alpaca_paper":
        return AlpacaFillSource(paper=True, **kw)
    if mode == "alpaca_live":
        # PHASE5: identical path, real money. Gate this on a paper track-record threshold
        # (e.g. >= 60 paper sessions with live Sharpe >= 0.5x backtest) before enabling.
        return AlpacaFillSource(paper=False, **kw)
    raise ValueError(f"unknown fills mode: {mode!r} (modeled | alpaca_paper | alpaca_live)")


# ── paper tracker (book mgmt: no-flip, scheduled exits, reconcile) ────────

@dataclass
class PaperTracker:
    fill_source: FillSource
    strategy: str
    open_book: dict[str, dict] = field(default_factory=dict)   # symbol -> {sign, qty, entry_date, exit_date}
    fills: list[Fill] = field(default_factory=list)
    n_short_skipped: int = 0
    n_short_attempted: int = 0

    def submit_book(self, targets: list[TargetPosition]) -> None:
        for t in targets:
            # No simultaneous long+short same symbol: if a fire flips an open name, close first.
            held = self.open_book.get(t.symbol)
            if held and held["sign"] != t.signal_sign:
                self.fills.append(self.fill_source.close(t.symbol, held["qty"], held["sign"]))
                self.open_book.pop(t.symbol, None)
            if t.signal_sign < 0:
                self.n_short_attempted += 1
            f = self.fill_source.submit(t)
            self.fills.append(f)
            if f.status == "skipped_not_shortable":
                self.n_short_skipped += 1
                continue
            if f.status in ("filled", "partial", "accepted"):
                exit_date = str((pd.Timestamp(t.entry_date or pd.Timestamp.utcnow())
                                 + pd.offsets.BDay(HOLD_BDAYS)).date())
                self.open_book[t.symbol] = {"sign": t.signal_sign, "qty": f.qty or 0.0,
                                            "entry_date": t.entry_date, "exit_date": exit_date}

    def step_exits(self, today: str | None = None) -> None:
        """Close positions whose ~21-bday hold has elapsed (managed in the loop)."""
        today = today or str(pd.Timestamp.utcnow().date())
        for sym, pos in list(self.open_book.items()):
            if pos["exit_date"] and pos["exit_date"] <= today:
                self.fills.append(self.fill_source.close(sym, pos["qty"], pos["sign"]))
                self.open_book.pop(sym, None)

    def shortability_coverage(self) -> float:
        return (1.0 - self.n_short_skipped / self.n_short_attempted) if self.n_short_attempted else 1.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── run a paper cycle + write the Phase-4 report slice ────────────────────

def run(strategy: str = "skew_consensus_v22_novix", fills_mode: str = "alpaca_paper",
        submit: bool = False, limit: int = 5, sample: int = 50, tif: str = "OPG",
        notional: float = 1000.0) -> dict:
    import random
    from .sizing import build_position_panel

    pp = build_position_panel().sort_values("date")          # date,symbol,signal_sign,net_return,raw_close
    # Demo book = the most recent `limit` fires (a realistic "today's signals" book).
    demo = pp.tail(limit)
    targets: list[TargetPosition] = []
    for r in demo.itertuples():
        px = float(r.raw_close)
        qty = max(1, int(notional / px)) if px > 0 else 1     # whole shares (OPG-safe; notional via DAY)
        targets.append(TargetPosition(
            symbol=str(r.symbol), side=("BULL" if r.signal_sign < 0 else "BEAR"),
            signal_sign=int(r.signal_sign), qty=qty, ref_price=px,
            entry_date=str(pd.Timestamp(r.date).date())))

    fs = make_fill_source(fills_mode, tif=tif) if fills_mode != "modeled" else make_fill_source(fills_mode)
    tracker = PaperTracker(fs, strategy)

    # Shortability coverage (honest execution-gap stat): dry-gate a sample of the
    # strategy's distinct SHORT (BULL-fade) symbols — how many are shortable today.
    coverage = None
    if isinstance(fs, AlpacaFillSource):
        shorts = sorted(pp.loc[pp["signal_sign"] < 0, "symbol"].astype(str).unique())
        random.seed(0)
        samp = random.sample(shorts, min(sample, len(shorts)))
        ok = sum(1 for s in samp if fs._shortable(s)[0])
        coverage = {"sample": len(samp), "shortable": ok,
                    "coverage_pct": round(100 * ok / len(samp), 1) if samp else None,
                    "non_shortable": len(samp) - ok}

    if submit:
        tracker.submit_book(targets)

    intended = {t.symbol: t.signal_sign for t in targets}
    recon = fs.reconcile(intended) if isinstance(fs, AlpacaFillSource) else {"source": "modeled"}

    state = {
        "fills_mode": fills_mode, "submitted": submit, "tif": tif,
        "updated_at": _now(),
        "account": {k: recon.get(k) for k in ("equity", "buying_power", "cash")} if recon else {},
        "n_targets": len(targets),
        "fills": [vars(f) for f in tracker.fills],
        "open_positions": tracker.open_book,
        "short_attempted": tracker.n_short_attempted, "short_skipped": tracker.n_short_skipped,
        "shortability_coverage": coverage,
        "reconciliation": recon,
        "expectation": {"sized_sharpe": 1.14, "sized_cagr": 0.0575, "note": "vs Stage-2 sized backtest"},
        "oos": {"days_live": 0, "status": "warming_up — insufficient live history vs expectation"},
    }
    out = REPORTS / strategy
    out.mkdir(parents=True, exist_ok=True)
    (out / "paper_state.json").write_text(json.dumps(state, indent=2), encoding="utf-8")
    _write_paper_phase(strategy, state)

    cov = coverage["coverage_pct"] if coverage else "n/a"
    print(f"[fills] {fills_mode} | submitted={submit} targets={len(targets)} "
          f"fills={len(tracker.fills)} short_skipped={tracker.n_short_skipped}/{tracker.n_short_attempted} "
          f"shortability_coverage={cov}% | equity={state['account'].get('equity')}")
    for f in tracker.fills:
        print(f"   {f.order_side:>4} {f.symbol:<6} qty={f.qty} status={f.status}  {f.note}")
    return state


def _write_paper_phase(strategy: str, state: dict) -> None:
    """Update the report.json phases.4_paper slice + re-render report.html (per the
    report-rendering skill: write only this slice, then rebuild)."""
    from . import reporter
    jpath = REPORTS / strategy / "report.json"
    if not jpath.exists():
        return
    rep = json.loads(jpath.read_text(encoding="utf-8"))
    submitted = state["submitted"]
    accepted = sum(1 for f in state["fills"] if f["status"] in ("filled", "partial", "accepted"))
    cov = state["shortability_coverage"]
    status = "deployed" if (submitted and accepted > 0) else ("paused" if submitted else "not_started")
    cov_txt = (f"shortability coverage {cov['coverage_pct']}% ({cov['non_shortable']}/{cov['sample']} short "
               f"names non-shortable)" if cov else "shortability n/a")
    note = (f"Alpaca PAPER ({state['fills_mode']}, tif={state['tif']}): {state['n_targets']} targets, "
            f"{accepted} accepted, {state['short_skipped']}/{state['short_attempted']} short fires "
            f"skipped non-shortable; equity ${state['account'].get('equity')}; {cov_txt}; ~21-bday "
            f"scheduled exits; no-flip-without-close. OOS vs sized backtest: warming up (0 sessions). "
            f"Phase-5 live = same path, paper=False + live keys.") if submitted else (
            f"Paper seam wired (Alpaca reachable, equity ${state['account'].get('equity')}); "
            f"{cov_txt}. No orders submitted (dry run). Phase-5 live = paper=False + live keys.")
    rep.setdefault("phases", {})["4_paper"] = {"status": status, "updated_at": state["updated_at"],
                                               "note": note}
    rep["updated_at"] = state["updated_at"]
    jpath.write_text(json.dumps(rep, indent=2), encoding="utf-8")
    reporter.render(strategy)


# ── next-open trade queue (the REAL sized book for the next session) ──────
#
# SIGNAL SOURCE (the live-feed gap, surfaced honestly): the book is computed off the
# LATEST date in the STATIC clean ORATS panel (data/av/signal_panel_clean.parquet), NOT
# a live daily ORATS pull. Genuine next-day trading needs a daily ORATS refresh; until
# that feed is wired the "latest signal date" is the panel's last date — reported as a gap.

SIZING_LAM, SIZING_RHO = 0.25, 0.30                       # Stage-2 fractional Kelly + const-corr rho
SIZING_MAXW, SIZING_GROSS, SIZING_NET = 0.05, 1.0, 0.50   # Risk-Agent caps
DEPLOY_MULT = 0.5                                         # Risk Agent: deploy at 0.5x approved size
FALLBACK_EQUITY = 1_000_000.0
CLEAN_PANEL = pathlib.Path("data/av/signal_panel_clean.parquet")

# Safety guards (all overridable on the CLI):
MAX_SIGNAL_AGE_BDAYS = 2            # staleness: refuse to fire on a signal older than this (--allow-stale)
DD_PAUSE, DD_RESUME, DD_HARD = 0.15, 0.07, 0.25   # kill-switch: pause / resume(hysteresis) / hard-flatten


# ── persistent paper ledger (source of truth across runs; gitignored) ─────
# One record per position keyed by the idempotent client_order_id. Survives restarts so
# re-running queue --submit never double-fires and manage always knows what is OURS.

def _ledger_path(strategy: str) -> pathlib.Path:
    return REPORTS / strategy / "paper_ledger.json"


def _load_ledger(strategy: str) -> dict:
    p = _ledger_path(strategy)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {"strategy": strategy, "updated_at": _now(), "peak_equity": None,
            "kill_switch_state": "deployed", "positions": {}}


def _save_ledger(strategy: str, led: dict) -> None:
    led["updated_at"] = _now()
    p = _ledger_path(strategy)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(led, indent=2), encoding="utf-8")


def _client_order_id(strategy: str, symbol: str, signal_date: str) -> str:
    """Idempotency key. Alpaca enforces client_order_id uniqueness -> re-runs can't double-submit."""
    return f"{strategy}:{symbol}:{signal_date}"


def _today() -> pd.Timestamp:
    return pd.Timestamp.today().normalize()


def _signal_age_bdays(signal_date: str) -> int:
    import numpy as np
    return int(np.busday_count(pd.Timestamp(signal_date).date(), _today().date()))


def _scheduled_exit(entry_date: str, hold_bdays: int = HOLD_BDAYS) -> str:
    return str((pd.Timestamp(entry_date) + pd.offsets.BDay(hold_bdays)).date())


def _next_session(last: pd.Timestamp) -> str:
    """Next trading session after `last` — US-holiday-aware so a long weekend (e.g. Memorial
    Day) isn't shown as the fill date. Falls back to a plain business day if the calendar
    is unavailable. (Misses market-only closures like Good Friday; OPG fills at the real open.)"""
    try:
        from pandas.tseries.holiday import USFederalHolidayCalendar
        from pandas.tseries.offsets import CustomBusinessDay
        return str((pd.Timestamp(last) + CustomBusinessDay(calendar=USFederalHolidayCalendar())).date())
    except Exception:  # noqa: BLE001
        return str((pd.Timestamp(last) + pd.offsets.BDay(1)).date())


def _owned_view(led: dict) -> list[dict]:
    """Open ledger positions with days-held + scheduled exit (for the report's owned table)."""
    import numpy as np
    today = _today().date()
    out = []
    for coid, r in led.get("positions", {}).items():
        if r.get("status") not in ("open", "pending"):
            continue
        base = r.get("entry_fill_date") or r.get("entry_signal_date")
        days = int(np.busday_count(pd.Timestamp(base).date(), today)) if base else None
        out.append({"symbol": r["symbol"], "side": r["side"], "qty": r["qty"],
                    "entry_signal_date": r.get("entry_signal_date"),
                    "entry_fill_date": r.get("entry_fill_date"), "days_held": days,
                    "scheduled_exit_date": r.get("scheduled_exit_date"),
                    "status": r.get("status"), "client_order_id": coid})
    return out


def _latest_signal_book(panel_path: pathlib.Path = CLEAN_PANEL):
    """LATEST tradeable signal date + its fires. NO forward-return requirement — a live
    entry doesn't need the (future) outcome, only that the signal fired and the name is
    tradeable. Returns (signal_ts, frame[symbol, side, signal_sign, raw_close])."""
    from .consensus_signal import signal_sign
    from .signal_vs_random import clean_run_columns
    p = pd.read_parquet(panel_path, columns=clean_run_columns())
    elig = p["side"].notna() & p["av_matched"].astype(bool) & (p["raw_close"] >= 1.0)
    f = p[elig]
    last = f["tradeDate"].max()
    b = f[f["tradeDate"] == last].copy()
    b["signal_sign"] = b["side"].astype(str).map(signal_sign).astype(float)
    b = b.rename(columns={"ticker": "symbol"})[["symbol", "side", "signal_sign", "raw_close"]]
    return last, b.reset_index(drop=True)


def _size_book(book: pd.DataFrame, panel_path: pathlib.Path = CLEAN_PANEL):
    """Size ONE date's book with the Stage-2 engine (lambda=0.25, const-corr rho, caps).
    mu + per-name sigma come from the fwd-complete HISTORY (this book has no outcome yet)."""
    import numpy as np

    from .sizing import (apply_caps, build_position_panel, kelly_fracs, per_name_sigma)
    hist = build_position_panel(panel_path)               # fwd-complete -> mu + sigma inputs
    mu = float(hist["net_return"].mean())
    sig_map, pooled = per_name_sigma(hist)
    sym = book["symbol"].to_numpy()
    sgn = book["signal_sign"].to_numpy(float)
    sigma = np.array([sig_map.get(str(s), pooled) for s in sym], dtype=float)
    f = apply_caps(kelly_fracs(sigma, mu, SIZING_RHO, SIZING_LAM), sgn,
                   max_w=SIZING_MAXW, gross_cap=SIZING_GROSS, net_cap=SIZING_NET)
    out = book.copy()
    out["kelly_weight"] = f                               # fraction of NAV (gross<=1), pre-deploy-mult
    return out, mu, pooled


def _existing_open_symbols(fs: AlpacaFillSource) -> tuple[set, list]:
    """Symbols already represented on the venue (open order OR live position) — so the
    queue does NOT double-submit them (e.g. the 3 open demo orders)."""
    syms, open_orders = set(), []
    try:
        for o in fs.open_orders():
            syms.add(str(o.symbol))
            open_orders.append({"symbol": str(o.symbol), "side": str(o.side).split(".")[-1].lower(),
                                "qty": float(o.qty or 0), "status": str(o.status).split(".")[-1].lower(),
                                "client_order_id": str(getattr(o, "client_order_id", "") or ""),
                                "order_id": str(o.id)})
    except Exception:  # noqa: BLE001
        pass
    try:
        for p in fs.positions():
            syms.add(str(p.symbol))
    except Exception:  # noqa: BLE001
        pass
    return syms, open_orders


def _live_signal_book():
    """Latest LIVE ORATS fires as the queue book (symbol, side, signal_sign, raw_close=clsPx
    current price). Reads the CSV the live feed (adapters.orats live) wrote, else recomputes.
    $1 floor; signal date = the latest live session -> the staleness guard passes."""
    import glob
    csvs = sorted(glob.glob("data/orats/live_fires_*.csv"))
    if csvs:
        lf = pd.read_csv(csvs[-1])
    else:
        from adapters import orats as _orats
        lf = _orats.live_fires()
    if lf is None or len(lf) == 0:
        raise RuntimeError("no live fires — run `python -m adapters.orats live` "
                           "(or `fills daily` with feed refresh) first")
    last = pd.Timestamp(pd.to_datetime(lf["tradeDate"]).iloc[0])
    book = (lf.rename(columns={"ticker": "symbol", "clsPx": "raw_close"})
            .loc[lambda d: d["raw_close"] >= 1.0, ["symbol", "side", "signal_sign", "raw_close"]]
            .reset_index(drop=True))
    return last, book


def build_next_open_queue(strategy: str = "skew_consensus_v22_novix",
                          fills_mode: str = "alpaca_paper", deploy_mult: float = DEPLOY_MULT,
                          gate_shorts: bool = True, panel_path: pathlib.Path = CLEAN_PANEL,
                          fs: "FillSource | None" = None, live: bool = False) -> dict:
    """Build the real next-session book: signal date -> fires -> Stage-2 sizing -> shortability
    gate (short side) -> reconcile vs open venue orders -> queue. READ-ONLY (no submit). `fs`
    may be shared with the submit path. `live=True` sources the book from the live ORATS feed
    (signal = the latest session, so the staleness guard passes); else the static panel."""
    if live:
        last, book = _live_signal_book()
    else:
        last, book = _latest_signal_book(panel_path)
    book, mu, pooled = _size_book(book, panel_path)
    signal_date, next_session = str(last.date()), _next_session(last)

    if fs is None:
        fs = (make_fill_source(fills_mode, tif="OPG") if fills_mode != "modeled"
              else make_fill_source(fills_mode))
    equity, existing, open_orders, recon = FALLBACK_EQUITY, set(), [], {"source": "modeled"}
    if isinstance(fs, AlpacaFillSource):
        recon = fs.reconcile({})
        equity = float(recon.get("equity") or FALLBACK_EQUITY)
        existing, open_orders = _existing_open_symbols(fs)
    deployable = deploy_mult * equity

    book = book.sort_values("kelly_weight", ascending=False)
    queue, skipped, gated = [], [], 0
    for r in book.itertuples():
        sym, sign, px = str(r.symbol), int(r.signal_sign), float(r.raw_close)
        w = float(r.kelly_weight) * deploy_mult           # deployed fraction of NAV
        notional = round(w * equity, 2)
        direction = "BULL" if sign < 0 else "BEAR"
        order_side = "sell" if sign < 0 else "buy"
        if sign < 0 and gate_shorts and isinstance(fs, AlpacaFillSource):  # short side only
            gated += 1
            ok, why = fs._shortable(sym)
            if not ok:
                skipped.append({"symbol": sym, "reason": why,
                                "weight": round(w, 5), "notional": notional})
                continue
        qty = max(1, int(notional / px)) if px > 0 else 0
        queue.append({"symbol": sym, "direction": direction, "side": order_side,
                      "qty": qty, "notional": notional, "weight": round(w, 5),
                      "ref_price": round(px, 4), "type": "market", "tif": "OPG",
                      "status": "already_open" if sym in existing else "queued",
                      "expected_fill_session": next_session})

    n_long = sum(1 for q in queue if q["side"] == "buy")
    n_short = sum(1 for q in queue if q["side"] == "sell")
    meta = {
        "signal_date": signal_date, "next_session": next_session,
        "equity": equity, "deploy_mult": deploy_mult, "lambda": SIZING_LAM, "rho": SIZING_RHO,
        "n_fires": int(len(book)), "n_queued": len(queue), "n_long": n_long, "n_short": n_short,
        "n_skipped_non_shortable": len(skipped), "n_shorts_gated": gated,
        "gross_weight": round(sum(q["weight"] for q in queue), 4),
        "net_weight": round(sum(q["weight"] * (1 if q["side"] == "buy" else -1) for q in queue), 4),
        "mu_per_trade_bps": round(mu * 1e4, 1),
        "reconciled_open_orders": open_orders,
        "source": "live" if live else "static",
        "live_feed": ({
            "source": "live daily full-universe ORATS pull (adapters.orats)",
            "is_live_orats": True,
            "gap": "",
        } if live else {
            "source": f"STATIC panel {panel_path.as_posix()} (last date {signal_date})",
            "is_live_orats": False,
            "gap": ("No daily ORATS refresh is wired: the 'latest signal date' is the static "
                    "panel's last date, not today's surface. Genuine next-day trading needs a live "
                    "daily ORATS pull (adapters.orats) feeding this loop — that feed is the next gap."),
        }),
        "submitted": False, "mode": "dry_run",
    }
    return {"queue_meta": meta, "next_open_queue": queue, "shortability_skipped": skipped,
            "fills_mode": fills_mode, "updated_at": _now()}


def _sync_paper_report(strategy: str, led: dict, qstate: dict | None = None,
                       kill_switch: dict | None = None) -> None:
    """Single writer for the Phase-4 report slice: queue (if given) + owned positions
    (from the ledger) + kill-switch state, then re-render once."""
    from . import reporter
    out = REPORTS / strategy
    out.mkdir(parents=True, exist_ok=True)
    if qstate is not None:
        pd.DataFrame(qstate["next_open_queue"]).to_csv(out / "next_open_queue.csv", index=False)
        (out / "next_open_queue.json").write_text(json.dumps(qstate, indent=2), encoding="utf-8")
    jpath = out / "report.json"
    if not jpath.exists():
        return
    rep = json.loads(jpath.read_text(encoding="utf-8"))
    p4 = rep.setdefault("phases", {}).setdefault("4_paper", {"status": "not_started", "note": ""})
    if qstate is not None:
        p4["next_open_queue"] = qstate["next_open_queue"]
        p4["shortability_skipped"] = qstate["shortability_skipped"]
        p4["queue_meta"] = qstate["queue_meta"]
    p4["owned_positions"] = _owned_view(led)
    if kill_switch is not None:
        p4["kill_switch"] = kill_switch
        p4["status"] = "paused" if kill_switch.get("state") == "paused" else "deployed"
    p4["updated_at"] = _now()
    rep["updated_at"] = _now()
    jpath.write_text(json.dumps(rep, indent=2), encoding="utf-8")
    reporter.render(strategy)


# ── ENTRY submission (guarded): caps + ceiling + staleness + idempotency ──

def submit_entries(strategy: str, qstate: dict, fs: FillSource, led: dict, *,
                   limit: int | None = None, max_notional: float | None = None,
                   allow_stale: bool = False, max_age_bdays: int = MAX_SIGNAL_AGE_BDAYS) -> dict:
    """Submit the book LIVE behind every guard. Returns a trace; aborts (submits nothing)
    if any guard trips. Idempotent: ledger + client_order_id stop double-fires on re-run."""
    m = qstate["queue_meta"]
    sig = m["signal_date"]
    eq = float(m.get("equity") or FALLBACK_EQUITY)
    res = {"submitted": [], "skipped": [], "aborted": None, "signal_date": sig,
           "n_orders_cap": limit, "max_notional": max_notional}

    # GATE 1 — kill-switch: never open new risk while paused
    if led.get("kill_switch_state") == "paused":
        res["aborted"] = "kill-switch PAUSED — new entries blocked"
        return res
    # GATE 2 — staleness: do not fire a stale book by accident
    age = _signal_age_bdays(sig)
    if age > max_age_bdays and not allow_stale:
        res["aborted"] = (f"STALE: signal {sig} is {age} bdays old (> max {max_age_bdays}). Refusing to "
                          f"fire a stale book; pass --allow-stale for an explicit plumbing test, or wire "
                          f"the live daily ORATS feed for fresh signals.")
        return res

    # CEILING — top-N by weight
    queue = sorted(qstate["next_open_queue"], key=lambda q: -q["weight"])
    if limit is not None:
        queue = queue[:limit]

    # build + enforce caps PRE-submit (abort if any cap exceeded)
    targets, gross_n, net_n = [], 0.0, 0.0
    for o in queue:
        sym = o["symbol"]
        sign = -1 if o["side"] == "sell" else 1
        coid = _client_order_id(strategy, sym, sig)
        if o["status"] == "already_open":
            res["skipped"].append({"symbol": sym, "reason": "already on venue (reconciled)"})
            continue
        rec = led.get("positions", {}).get(coid)
        if rec and rec.get("status") in ("open", "pending"):
            res["skipped"].append({"symbol": sym, "reason": "already in ledger (idempotent)"})
            continue
        px = float(o.get("ref_price") or 0)
        if px <= 0:
            res["skipped"].append({"symbol": sym, "reason": "no reference price"})
            continue
        if max_notional is not None and px > max_notional:
            res["skipped"].append({"symbol": sym,
                                   "reason": f"1 share (${px:,.0f}) exceeds --max-notional ${max_notional:,.0f}"})
            continue
        tgt = min(o["notional"], max_notional) if max_notional is not None else o["notional"]
        qty = max(1, int(tgt / px))
        order_notional = qty * px
        if order_notional > SIZING_MAXW * eq + 1.0:                      # per-name 5% cap (hard)
            res["aborted"] = f"ABORT: {sym} ${order_notional:,.0f} > per-name cap ${SIZING_MAXW*eq:,.0f}"
            return res
        gross_n += order_notional
        net_n += sign * order_notional
        targets.append((o, coid, sign, qty, px, order_notional))
    if gross_n > SIZING_GROSS * eq + 1.0:                               # gross cap (hard)
        res["aborted"] = f"ABORT: slice gross ${gross_n:,.0f} > gross cap ${SIZING_GROSS*eq:,.0f}"
        return res
    if abs(net_n) > SIZING_NET * eq + 1.0:                              # net (directional) cap (hard)
        res["aborted"] = f"ABORT: slice net ${net_n:,.0f} > net cap ${SIZING_NET*eq:,.0f}"
        return res

    # SUBMIT — idempotent client_order_id; record each in the ledger with its 21-bday exit
    for o, coid, sign, qty, px, order_notional in targets:
        sym = o["symbol"]
        t = TargetPosition(symbol=sym, side=o["direction"], signal_sign=sign, qty=qty,
                           ref_price=px, entry_date=sig, client_order_id=coid)
        f = fs.submit(t)
        if f.status in ("filled", "partial", "accepted"):
            filled = f.status in ("filled", "partial")
            base = str(_today().date()) if filled else sig
            led.setdefault("positions", {})[coid] = {
                "symbol": sym, "side": str(f.order_side), "signal_sign": sign, "qty": qty,
                "client_order_id": coid, "entry_signal_date": sig,
                "entry_submitted_at": f.submitted_at,
                "entry_fill_date": (str(_today().date()) if filled else None),
                "entry_status": f.status, "order_id": f.order_id,
                "scheduled_exit_date": _scheduled_exit(base), "status": "open" if filled else "pending"}
            res["submitted"].append({"symbol": sym, "side": str(f.order_side), "qty": qty,
                                     "status": f.status, "order_id": f.order_id, "coid": coid,
                                     "notional": round(order_notional, 2),
                                     "scheduled_exit_date": _scheduled_exit(base)})
        elif f.status == "duplicate":
            res["skipped"].append({"symbol": sym, "reason": "duplicate client_order_id (idempotent)"})
        elif f.status == "skipped_not_shortable":
            res["skipped"].append({"symbol": sym, "reason": f"not shortable ({f.note})"})
        else:
            res["skipped"].append({"symbol": sym, "reason": f"{f.status}: {f.note}"})
    return res


# ── EXIT + kill-switch ────────────────────────────────────────────────────

def _kill_switch(fs: FillSource, led: dict, equity: float, simulate_dd: float | None,
                 dd_pause: float, dd_resume: float, dd_hard: float) -> dict:
    """Rolling peak-to-current drawdown -> state machine (hysteresis). pause>=15%, resume<7%,
    hard-flatten>=25%. `simulate_dd` overrides DD for a (non-persisted) live test."""
    series = fs.equity_series() if isinstance(fs, AlpacaFillSource) else []
    candidates = [equity] + ([led["peak_equity"]] if led.get("peak_equity") else []) + list(series or [])
    peak = max(candidates) if candidates else equity
    dd = (equity / peak - 1.0) if peak > 0 else 0.0
    simulated = simulate_dd is not None
    if simulated:
        dd = -abs(float(simulate_dd))
    prev = led.get("kill_switch_state", "deployed")
    hard = dd <= -dd_hard
    if hard:
        state = "paused"
    elif prev == "paused":
        state = "deployed" if dd > -dd_resume else "paused"
    else:
        state = "paused" if dd <= -dd_pause else "deployed"
    led["peak_equity"] = peak
    return {"state": state, "drawdown_pct": round(dd * 100, 2), "equity": equity, "peak_equity": peak,
            "hard_breach": bool(hard), "simulated": simulated,
            "dd_pause_pct": dd_pause * 100, "dd_resume_pct": dd_resume * 100, "dd_hard_pct": dd_hard * 100,
            "note": ("SIMULATED drawdown (test only; not persisted)" if simulated
                     else "rolling peak-to-current drawdown from account equity")}


def _exit_one(fs: FillSource, led: dict, coid: str, positions: dict, open_by_coid: dict,
              submit: bool, actions: list, reason: str) -> None:
    """Flatten ONE owned record via API: close a filled position OR cancel its still-open entry
    order. Only ever acts on OUR ledger record (matched by symbol/client_order_id)."""
    r = led["positions"][coid]
    sym, sign = r["symbol"], int(r.get("signal_sign", 1))
    pos = positions.get(sym)
    if pos is not None and abs(float(pos.qty)) > 0:                     # filled -> close via API
        if submit and isinstance(fs, AlpacaFillSource):
            f = fs.close(sym, abs(float(pos.qty)), 1 if float(pos.qty) > 0 else -1)
            r["status"] = "closed" if f.status in ("accepted", "filled") else r["status"]
            r["exit_submitted_at"], r["exit_status"], r["exit_order_id"] = _now(), f.status, f.order_id
            actions.append({"symbol": sym, "action": "close_position", "reason": reason,
                            "status": f.status, "submitted": True})
        else:
            actions.append({"symbol": sym, "action": "would_close_position", "reason": reason,
                            "submitted": False})
        return
    o = open_by_coid.get(coid)
    if o is not None:                                                  # not filled -> cancel entry order
        if submit and isinstance(fs, AlpacaFillSource):
            ok = fs.cancel(str(o.id))
            r["status"] = "closed" if ok else r["status"]
            r["exit_submitted_at"], r["exit_status"] = _now(), ("canceled" if ok else "cancel_failed")
            actions.append({"symbol": sym, "action": "cancel_open_order", "reason": reason,
                            "status": r["exit_status"], "submitted": True})
        else:
            actions.append({"symbol": sym, "action": "would_cancel_open_order", "reason": reason,
                            "submitted": False})
        return
    if submit:                                                         # nothing on venue -> reconcile flat
        r["status"], r["exit_status"], r["exit_submitted_at"] = "closed", "already_flat", _now()
    actions.append({"symbol": sym, "action": "already_flat", "reason": reason, "submitted": submit})


def manage(strategy: str = "skew_consensus_v22_novix", fills_mode: str = "alpaca_paper",
           submit: bool = False, force_exit: str | None = None, simulate_dd: float | None = None,
           dd_pause: float = DD_PAUSE, dd_resume: float = DD_RESUME, dd_hard: float = DD_HARD) -> dict:
    """Daily-runnable, idempotent exit + kill-switch runner. Reconciles vs Alpaca, closes owned
    positions whose 21-bday hold elapsed, and acts the drawdown kill-switch (pause/flatten) — all
    via API. Phase-5 live = same path (fills=alpaca_live). DRY-RUN unless --submit."""
    fs = (make_fill_source(fills_mode, tif="DAY") if fills_mode != "modeled"
          else make_fill_source(fills_mode))
    led = _load_ledger(strategy)
    today = str(_today().date())

    # RECONCILE before any action
    equity, positions, open_by_coid = FALLBACK_EQUITY, {}, {}
    if isinstance(fs, AlpacaFillSource):
        equity = float(fs.account().equity)
        positions = {p.symbol: p for p in fs.positions()}
        for o in fs.open_orders():
            open_by_coid[str(getattr(o, "client_order_id", "") or "")] = o

    # refresh fills: a pending entry that now shows a position becomes open + schedules its exit
    for coid, r in led.get("positions", {}).items():
        if r.get("status") == "pending" and r["symbol"] in positions:
            r["status"], r["entry_fill_date"] = "open", today
            r["scheduled_exit_date"] = _scheduled_exit(today)

    ks = _kill_switch(fs, led, equity, simulate_dd, dd_pause, dd_resume, dd_hard)
    actions: list = []

    if ks["hard_breach"]:                                              # hard breach -> flatten owned
        for coid, r in list(led.get("positions", {}).items()):
            if r.get("status") in ("open", "pending"):
                _exit_one(fs, led, coid, positions, open_by_coid, submit, actions, "kill-switch HARD flatten")
    for coid, r in list(led.get("positions", {}).items()):            # scheduled 21-bday exits
        if r.get("status") in ("open", "pending") and r.get("scheduled_exit_date") and r["scheduled_exit_date"] <= today:
            _exit_one(fs, led, coid, positions, open_by_coid, submit, actions, "scheduled 21bd exit")
    if force_exit:                                                     # manual close (testing the close path)
        want = None if force_exit.lower() == "all" else {s.strip().upper() for s in force_exit.split(",")}
        for coid, r in list(led.get("positions", {}).items()):
            if r.get("status") in ("open", "pending") and (want is None or r["symbol"] in want):
                _exit_one(fs, led, coid, positions, open_by_coid, submit, actions, "force-exit")

    if not ks["simulated"]:        # a simulated DD is a console-only test — don't poison real state
        led["kill_switch_state"] = ks["state"]
    _save_ledger(strategy, led)
    # a simulated run is a demonstration: update owned positions but leave the report's
    # persisted kill-switch on its last REAL value (don't write a fake paused state).
    _sync_paper_report(strategy, led, kill_switch=None if ks["simulated"] else ks)

    owned = _owned_view(led)
    sim = " (SIMULATED)" if ks["simulated"] else ""
    print(f"[manage] {strategy} {'SUBMIT' if submit else 'DRY-RUN'} | kill-switch {ks['state'].upper()}{sim} "
          f"DD {ks['drawdown_pct']}% (pause -{ks['dd_pause_pct']:.0f}% / hard -{ks['dd_hard_pct']:.0f}%) | "
          f"equity ${equity:,.0f} peak ${ks['peak_equity']:,.0f}")
    print(f"[manage] owned open: {len(owned)} | actions: {len(actions)}")
    for a in actions:
        print(f"   {a['action']:<22} {a['symbol']:<6} [{a['reason']}] {a.get('status','')}")
    if not actions:
        print("   (no exits due; nothing to flatten)")
    print(f"[manage] ledger {len(led.get('positions', {}))} record(s); report.json phases.4_paper updated + re-rendered")
    return {"kill_switch": ks, "actions": actions, "owned": owned}


# ── demo cleanup (the 3 OPG demo orders are NOT strategy positions) ───────

def clean_demos(strategy: str = "skew_consensus_v22_novix", fills_mode: str = "alpaca_paper",
                symbols: tuple = ("CERY", "VTIP", "ELME"), submit: bool = False) -> dict:
    """Cancel (or close, if filled) the demo orders so the account starts clean. SAFETY: never
    touches an order whose client_order_id is in our ledger. DRY-RUN unless --submit."""
    fs = (make_fill_source(fills_mode, tif="DAY") if fills_mode != "modeled"
          else make_fill_source(fills_mode))
    led = _load_ledger(strategy)
    ours = set(led.get("positions", {}).keys())
    symset = {s.upper() for s in symbols}
    acted: list = []
    if isinstance(fs, AlpacaFillSource):
        for o in fs.open_orders():
            sym = str(o.symbol)
            if sym not in symset:
                continue
            if str(getattr(o, "client_order_id", "") or "") in ours:   # SAFETY: never cancel our tracked orders
                acted.append({"symbol": sym, "action": "skip_ours", "submitted": False})
                continue
            if submit:
                ok = fs.cancel(str(o.id))
                acted.append({"symbol": sym, "action": "cancel_order", "ok": ok, "submitted": True})
            else:
                acted.append({"symbol": sym, "action": "would_cancel_order", "submitted": False})
        pos = {p.symbol: p for p in fs.positions()}
        for sym in symset:
            if sym in pos:
                if submit:
                    f = fs.close(sym, abs(float(pos[sym].qty)), 1 if float(pos[sym].qty) > 0 else -1)
                    acted.append({"symbol": sym, "action": "close_position", "status": f.status, "submitted": True})
                else:
                    acted.append({"symbol": sym, "action": "would_close_position", "submitted": False})
    print(f"[clean-demos] {'SUBMIT' if submit else 'DRY-RUN'} symbols={sorted(symset)}:")
    for a in acted:
        print(f"   {a['action']:<22} {a['symbol']} {('ok=' + str(a['ok'])) if 'ok' in a else a.get('status', '')}")
    if not acted:
        print("   (no demo orders/positions found — account already clean)")
    return {"acted": acted}


def run_queue(strategy: str = "skew_consensus_v22_novix", fills_mode: str = "alpaca_paper",
              deploy_mult: float = DEPLOY_MULT, gate_shorts: bool = True, submit: bool = False,
              limit: int | None = None, max_notional: float | None = None, allow_stale: bool = False,
              max_age_bdays: int = MAX_SIGNAL_AGE_BDAYS, live: bool = False) -> dict:
    """Build the real next-open book (read-only) and, with --submit, fire it LIVE behind every
    guard. `live=True` sources the book from the live ORATS feed (signal = latest session)."""
    fs = (make_fill_source(fills_mode, tif="OPG") if fills_mode != "modeled"
          else make_fill_source(fills_mode))
    led = _load_ledger(strategy)
    q = build_next_open_queue(strategy, fills_mode, deploy_mult, gate_shorts, fs=fs, live=live)
    m = q["queue_meta"]
    sub_res = None
    if submit:
        sub_res = submit_entries(strategy, q, fs, led, limit=limit, max_notional=max_notional,
                                 allow_stale=allow_stale, max_age_bdays=max_age_bdays)
        _save_ledger(strategy, led)
        m["submitted"] = bool(sub_res and not sub_res["aborted"] and sub_res["submitted"])
        m["mode"] = "submit"
    _sync_paper_report(strategy, led, qstate=q)

    recon = ", ".join(o["symbol"] for o in m["reconciled_open_orders"]) or "none"
    print(f"[queue] {strategy}: signal {m['signal_date']} -> fills {m['next_session']} | "
          f"{m['n_queued']} queued ({m['n_long']}L/{m['n_short']}S), "
          f"{m['n_skipped_non_shortable']}/{m['n_shorts_gated']} shorts non-shortable | "
          f"gross {m['gross_weight']} net {m['net_weight']} | equity ${m['equity']:,.0f} deploy {deploy_mult}x")
    print(f"[queue] reconciled vs {len(m['reconciled_open_orders'])} open venue order(s) [{recon}]")
    if submit and sub_res:
        if sub_res["aborted"]:
            print(f"[queue] SUBMIT ABORTED (guard tripped): {sub_res['aborted']}")
        else:
            print(f"[queue] SUBMITTED {len(sub_res['submitted'])} entr(ies); skipped {len(sub_res['skipped'])}:")
            for s in sub_res["submitted"]:
                print(f"   + {s['side']:>4} {s['symbol']:<6} qty={s['qty']} ${s['notional']:,.0f} "
                      f"status={s['status']} exit~{s['scheduled_exit_date']} coid={s['coid']}")
            for s in sub_res["skipped"][:8]:
                print(f"   - skip {s['symbol']:<6} {s['reason']}")
    else:
        print(f"[queue] DRY-RUN (no --submit). LIVE-FEED GAP: {m['live_feed']['gap']}")
    print(f"[queue] wrote next_open_queue.csv + report.json phases.4_paper + report.html | "
          f"ledger {len(led.get('positions', {}))} record(s)")
    return {"queue": q, "submit": sub_res}


# ── DAILY production loop (EOD cadence; the live feed wired end-to-end) ────
#
# pull live ORATS -> manage (exits + kill-switch) -> build the current-signal queue
# (signal = latest session => staleness guard PASSES) -> optional guarded submit.
#
# FIRST-DAY REVIEW GATE: DRY-RUN by default. The first real current-signal book is built,
# written to report.html, and STOPS — it fires only on an explicit --submit after you have
# eyeballed it. All rails stay on (caps 5%/1.0/0.5, --limit/--max-notional ceiling, idempotent
# client_order_ids, only-act-on-our-ledger). Graduated rollout via --deploy-fraction.
#
# UNATTENDED SCHEDULING (Windows Task Scheduler, after-close EOD trigger ~17:15 ET):
#   schtasks /Create /TN "skew_daily" /SC DAILY /ST 17:15 /TR ^
#     "cmd /c cd /d <repo> && python -m quant_validator.fills daily >> logs\daily.log 2>&1"
#   - DRY-RUN (review-gated) by default; add --submit (and --deploy-fraction) once the live
#     track looks sane. `manage` runs every session inside `daily`, so exits/kill-switch fire
#     daily even on non-entry days. Idempotent: a same-day re-run double-fires nothing.

def daily(strategy: str = "skew_consensus_v22_novix", fills_mode: str = "alpaca_paper",
          submit: bool = False, refresh_feed: bool = True, deploy_fraction: float = 1.0,
          gate_shorts: bool = True, limit: int | None = None, max_notional: float | None = None,
          allow_stale: bool = False, dd_pause: float = DD_PAUSE, dd_resume: float = DD_RESUME,
          dd_hard: float = DD_HARD) -> dict:
    """The daily production loop on the LIVE ORATS feed. See the block comment above."""
    eff_deploy = DEPLOY_MULT * deploy_fraction
    mode = "SUBMIT (LIVE)" if submit else "DRY-RUN (first-day review gate)"
    print("=" * 78)
    print(f"[daily] {strategy} | {mode} | deploy {DEPLOY_MULT}x x fraction {deploy_fraction} "
          f"= {eff_deploy:.4f}x effective | {fills_mode}")
    print("=" * 78)

    # 1) refresh the live feed (backfill to latest session + rebuild trailing panel + flip is_live)
    feed = None
    if refresh_feed:
        from adapters import orats
        feed = orats.run_live(strategy=strategy, update_report=True)
    else:
        print("[daily] --no-refresh: using the existing live panel / fires (no ORATS pull)")

    # 2) manage FIRST so the kill-switch state + closed expiries are current BEFORE new entries
    #    (a paused kill-switch must block today's entries; submit_entries reads led state).
    print("[daily] --- manage (exits + kill-switch) ---")
    mres = manage(strategy=strategy, fills_mode=fills_mode, submit=submit,
                  dd_pause=dd_pause, dd_resume=dd_resume, dd_hard=dd_hard)

    # 3) build the current-signal next-open queue from the LIVE panel (signal = latest session)
    print("[daily] --- queue (current-signal book from the live feed) ---")
    qres = run_queue(strategy=strategy, fills_mode=fills_mode, deploy_mult=eff_deploy,
                     gate_shorts=gate_shorts, submit=submit, limit=limit,
                     max_notional=max_notional, allow_stale=allow_stale, live=True)

    # 4) staleness verdict (visible even in dry-run: proves the guard passes on the current signal)
    sig = qres["queue"]["queue_meta"]["signal_date"]
    age = _signal_age_bdays(sig)
    passes = age <= MAX_SIGNAL_AGE_BDAYS
    print("=" * 78)
    print(f"[daily] staleness guard: signal {sig} age {age} bday(s) "
          f"{'<=' if passes else '>'} max {MAX_SIGNAL_AGE_BDAYS} -> "
          f"{'PASS — fireable on the current signal' if passes else 'BLOCK (stale)'}")
    ks = mres["kill_switch"]["state"]
    print(f"[daily] kill-switch {ks.upper()} | owned open {len(mres['owned'])} | "
          f"effective deploy {eff_deploy:.4f}x")
    if not submit:
        print("[daily] FIRST-DAY REVIEW GATE — DRY-RUN only, nothing fired. Inspect "
              f"reports/{strategy}/report.html (Phase-4 'Next-open trade queue'), then re-run:")
        print(f"        python -m quant_validator.fills daily --submit --deploy-fraction 0.25 "
              f"--limit 10 --max-notional 5000   # start small, scale to full 0.5x when sane")
    print("=" * 78)
    return {"feed": feed, "manage": mres, "queue": qres, "signal_date": sig,
            "staleness_pass": passes, "effective_deploy": eff_deploy, "submitted": submit}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="quant_validator.fills")
    sub = ap.add_subparsers(dest="cmd", required=True)
    _choices = ["modeled", "alpaca_paper", "alpaca_live"]

    pr = sub.add_parser("run", help="legacy demo book (tail-N fires)")
    pr.add_argument("--strategy", default="skew_consensus_v22_novix")
    pr.add_argument("--fills", default="alpaca_paper", choices=_choices)
    pr.add_argument("--submit", action="store_true", help="actually submit the demo book (paper)")
    pr.add_argument("--limit", type=int, default=5)
    pr.add_argument("--sample", type=int, default=50, help="# short symbols to gate for the coverage stat")
    pr.add_argument("--tif", default="OPG", choices=["OPG", "DAY"])

    pq = sub.add_parser("queue", help="build the real next-open book; --submit fires entries (guarded)")
    pq.add_argument("--strategy", default="skew_consensus_v22_novix")
    pq.add_argument("--fills", default="alpaca_paper", choices=_choices)
    pq.add_argument("--deploy-mult", type=float, default=DEPLOY_MULT)
    pq.add_argument("--no-gate", action="store_true", help="skip the live shortability gate")
    pq.add_argument("--submit", action="store_true", help="LIVE: actually submit entries (default DRY-RUN)")
    pq.add_argument("--limit", type=int, default=None, help="SAFETY: at most N orders (top by weight)")
    pq.add_argument("--max-notional", type=float, default=None, help="SAFETY: per-order max $ notional")
    pq.add_argument("--allow-stale", action="store_true", help="override the staleness guard (explicit test)")
    pq.add_argument("--max-signal-age", type=int, default=MAX_SIGNAL_AGE_BDAYS)

    pm = sub.add_parser("manage", help="daily: scheduled exits + drawdown kill-switch (idempotent)")
    pm.add_argument("--strategy", default="skew_consensus_v22_novix")
    pm.add_argument("--fills", default="alpaca_paper", choices=_choices)
    pm.add_argument("--submit", action="store_true", help="LIVE: actually submit closes/cancels (default DRY-RUN)")
    pm.add_argument("--force-exit", default=None, help="'all' or comma symbols: close NOW (tests the close path)")
    pm.add_argument("--simulate-dd", type=float, default=None, help="TEST: override drawdown e.g. 0.20 (not persisted)")
    pm.add_argument("--dd-pause", type=float, default=DD_PAUSE)
    pm.add_argument("--dd-hard", type=float, default=DD_HARD)
    pm.add_argument("--dd-resume", type=float, default=DD_RESUME)

    pc = sub.add_parser("clean-demos", help="cancel/close the 3 open demo orders (not strategy positions)")
    pc.add_argument("--strategy", default="skew_consensus_v22_novix")
    pc.add_argument("--fills", default="alpaca_paper", choices=_choices)
    pc.add_argument("--symbols", default="CERY,VTIP,ELME")
    pc.add_argument("--submit", action="store_true", help="LIVE: actually cancel/close (default DRY-RUN)")

    pdl = sub.add_parser("daily", help="EOD production loop on the live ORATS feed (first-day review gate)")
    pdl.add_argument("--strategy", default="skew_consensus_v22_novix")
    pdl.add_argument("--fills", default="alpaca_paper", choices=_choices)
    pdl.add_argument("--submit", action="store_true",
                     help="LIVE: fire entries + manage closes (default DRY-RUN review gate)")
    pdl.add_argument("--no-refresh", action="store_true",
                     help="skip the ORATS feed pull/rebuild (use the existing live panel)")
    pdl.add_argument("--deploy-fraction", type=float, default=1.0,
                     help="graduated rollout: fraction of the 0.5x deploy (e.g. 0.25 to start)")
    pdl.add_argument("--no-gate", action="store_true", help="skip the live shortability gate")
    pdl.add_argument("--limit", type=int, default=None, help="SAFETY: at most N orders (top by weight)")
    pdl.add_argument("--max-notional", type=float, default=None, help="SAFETY: per-order max $ notional")
    pdl.add_argument("--allow-stale", action="store_true", help="override the staleness guard (rarely needed live)")
    pdl.add_argument("--dd-pause", type=float, default=DD_PAUSE)
    pdl.add_argument("--dd-hard", type=float, default=DD_HARD)
    pdl.add_argument("--dd-resume", type=float, default=DD_RESUME)

    args = ap.parse_args(argv)
    if args.cmd == "run":
        run(strategy=args.strategy, fills_mode=args.fills, submit=args.submit,
            limit=args.limit, sample=args.sample, tif=args.tif)
        return 0
    if args.cmd == "queue":
        run_queue(strategy=args.strategy, fills_mode=args.fills, deploy_mult=args.deploy_mult,
                  gate_shorts=not args.no_gate, submit=args.submit, limit=args.limit,
                  max_notional=args.max_notional, allow_stale=args.allow_stale,
                  max_age_bdays=args.max_signal_age)
        return 0
    if args.cmd == "manage":
        manage(strategy=args.strategy, fills_mode=args.fills, submit=args.submit,
               force_exit=args.force_exit, simulate_dd=args.simulate_dd,
               dd_pause=args.dd_pause, dd_resume=args.dd_resume, dd_hard=args.dd_hard)
        return 0
    if args.cmd == "clean-demos":
        clean_demos(strategy=args.strategy, fills_mode=args.fills,
                    symbols=tuple(s.strip() for s in args.symbols.split(",")), submit=args.submit)
        return 0
    if args.cmd == "daily":
        daily(strategy=args.strategy, fills_mode=args.fills, submit=args.submit,
              refresh_feed=not args.no_refresh, deploy_fraction=args.deploy_fraction,
              gate_shorts=not args.no_gate, limit=args.limit, max_notional=args.max_notional,
              allow_stale=args.allow_stale, dd_pause=args.dd_pause, dd_resume=args.dd_resume,
              dd_hard=args.dd_hard)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
