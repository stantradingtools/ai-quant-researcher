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

CLI:
    python -m quant_validator.fills run --strategy skew_consensus_v22_novix \
        --fills alpaca_paper --submit --limit 5
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

HOLD_BDAYS = 21
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
        self.tif = tif
        self.poll_s, self.poll_n = poll_s, poll_n

    def _shortable(self, symbol: str) -> tuple[bool, str]:
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
        try:
            order = self.client.submit_order(MarketOrderRequest(**kw))
        except Exception as e:  # noqa: BLE001 — reject/halt/BP, surface (no key/URL leak)
            return Fill(t.symbol, str(side.value), 0.0, 0.0, "rejected",
                        submitted_at=_now(), note=f"{type(e).__name__}: {str(e)[:120]}")
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
        try:
            o = self.client.close_position(symbol)
            return Fill(symbol, "SELL" if open_sign > 0 else "BUY", float(qty), 0.0,
                        "accepted", order_id=str(getattr(o, "id", "")), submitted_at=_now(),
                        note="alpaca close_position")
        except Exception as e:  # noqa: BLE001
            return Fill(symbol, "", 0.0, 0.0, "rejected", submitted_at=_now(),
                        note=f"close failed {type(e).__name__}: {str(e)[:100]}")

    def reconcile(self, intended: dict[str, float]) -> dict:
        acct = self.client.get_account()
        positions = {p.symbol: p for p in self.client.get_all_positions()}
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="quant_validator.fills")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pr = sub.add_parser("run")
    pr.add_argument("--strategy", default="skew_consensus_v22_novix")
    pr.add_argument("--fills", default="alpaca_paper", choices=["modeled", "alpaca_paper", "alpaca_live"])
    pr.add_argument("--submit", action="store_true", help="actually submit the demo book (paper)")
    pr.add_argument("--limit", type=int, default=5)
    pr.add_argument("--sample", type=int, default=50, help="# short symbols to gate for the coverage stat")
    pr.add_argument("--tif", default="OPG", choices=["OPG", "DAY"])
    args = ap.parse_args(argv)
    if args.cmd == "run":
        run(strategy=args.strategy, fills_mode=args.fills, submit=args.submit,
            limit=args.limit, sample=args.sample, tif=args.tif)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
