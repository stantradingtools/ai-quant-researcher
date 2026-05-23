"""quant_validator.consensus_signal: faithful Python port of the tool's
`replayConsensus` gate (Skew_backtest_orats PATCH-35a).

This reproduces the RAW consensus signal generator — M1 ∧ M2 ∧ M3, same-side,
fade direction — WITHOUT the downstream selection filters (trend / earnings /
VIX / sector). Those are separate layers; the signal-generation-vs-random test
isolates whether *generating a signal at all* picks better-than-random points.

Exact gate (extracted from the tool, defaults shown):
  M1 corner (252d pctl, hi=75/lo=25):
      putP<=lo & callP>=hi -> BULL   (call rich -> fade -> SHORT)
      putP>=hi & callP<=lo -> BEAR   (put rich  -> fade -> LONG)
  M2 corner:
      ivP>=hi & rrP>=hi     -> BULL   (euphoria top)
      ivP>=hi & rrP<=lo     -> BEAR   (capitulation)
  M1/M2 "recent": corner fired on any of the last `freshness`(=3) days incl. today.
  M3 sigma-stall: sigma[t-3..t] all >= +thr (BULL) / <= -thr (BEAR), thr=1.0,
      AND |sigma[t]-sigma[t-3]| < 0.3 (momentum sustained but flattening).
  M3 skew-divergence: skewDelta[t-3..t-1] all <0 then [t] > [t-1]+0.2 (BULL);
      mirror (all >0 then [t] < [t-1]-0.2) for BEAR.
  M3 confirms (default both modes ON, requireBoth=False): stall OR divergence.
  Fire (BULL checked first): same side clears M1-recent ∧ M2-recent ∧ M3.
      BULL -> 'CONSENSUS SHORT' (side='BULL'); BEAR -> 'CONSENSUS LONG' (side='BEAR').
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class ConsensusOpts:
    hi: float = 75.0
    lo: float = 25.0
    freshness: int = 3
    sigma_threshold: float = 1.0
    sigma_mode: bool = True
    skew_mode: bool = True
    require_both: bool = False
    allow_bull: bool = True
    allow_bear: bool = True


REQUIRED_COLS = ("putP", "callP", "ivP", "rrP", "sigma", "skewDelta")


def compute_consensus(df: pd.DataFrame, opts: ConsensusOpts = ConsensusOpts()) -> pd.DataFrame:
    """Annotate a SINGLE ticker's signal frame (sorted ascending by tradeDate)
    with the consensus gate. Returns a copy with added columns:
      m1_side, m2_side (per-day corner side or None),
      m1_recent_bull/bear, m2_recent_bull/bear,
      m3_stall_bull/bear, m3_div_bull/bear, m3_bull/m3_bear,
      side ('BULL'/'BEAR'/None), direction ('CONSENSUS SHORT'/'LONG'/None).
    NaN inputs propagate to False in every comparison, matching the tool's
    explicit null-guards (a null in any required window fails that gate).
    """
    missing = [c for c in REQUIRED_COLS if c not in df.columns]
    if missing:
        raise ValueError(f"compute_consensus missing columns: {missing}")

    d = df.copy()
    hi, lo = opts.hi, opts.lo
    putP, callP = d["putP"], d["callP"]
    ivP, rrP = d["ivP"], d["rrP"]

    # ── M1 / M2 same-day corners ────────────────────────────────────────
    m1_bull = (putP <= lo) & (callP >= hi)
    m1_bear = (putP >= hi) & (callP <= lo)
    m2_bull = (ivP >= hi) & (rrP >= hi)
    m2_bear = (ivP >= hi) & (rrP <= lo)

    def _side_col(bull: pd.Series, bear: pd.Series) -> pd.Series:
        col = pd.Series([None] * len(d), index=d.index, dtype=object)
        col[bull.to_numpy()] = "BULL"
        col[bear.to_numpy()] = "BEAR"
        return col

    d["m1_side"] = _side_col(m1_bull, m1_bear)
    d["m2_side"] = _side_col(m2_bull, m2_bear)

    # ── "recent corner" within freshness window (rolling OR incl. today) ─
    def _recent(mask: pd.Series) -> pd.Series:
        return (mask.astype(float).rolling(opts.freshness, min_periods=1).max() > 0)

    d["m1_recent_bull"] = _recent(m1_bull)
    d["m1_recent_bear"] = _recent(m1_bear)
    d["m2_recent_bull"] = _recent(m2_bull)
    d["m2_recent_bear"] = _recent(m2_bear)

    # ── M3 sigma stall (shift-based 4-day window; NaN -> False) ──────────
    s = d["sigma"]
    s0, s1, s2, s3 = s.shift(3), s.shift(2), s.shift(1), s
    thr = opts.sigma_threshold
    flat = (s3 - s0).abs() < 0.3
    d["m3_stall_bull"] = (s0 >= thr) & (s1 >= thr) & (s2 >= thr) & (s3 >= thr) & flat
    d["m3_stall_bear"] = (s0 <= -thr) & (s1 <= -thr) & (s2 <= -thr) & (s3 <= -thr) & flat

    # ── M3 skew divergence ──────────────────────────────────────────────
    sd = d["skewDelta"]
    d0, d1, d2, d3 = sd.shift(3), sd.shift(2), sd.shift(1), sd
    d["m3_div_bull"] = (d0 < 0) & (d1 < 0) & (d2 < 0) & (d3 > d2 + 0.2)
    d["m3_div_bear"] = (d0 > 0) & (d1 > 0) & (d2 > 0) & (d3 < d2 - 0.2)

    # NaN guards: if any sigma/skewDelta in the window is null, the gate fails.
    sig_ok = s0.notna() & s1.notna() & s2.notna() & s3.notna()
    div_ok = d0.notna() & d1.notna() & d2.notna() & d3.notna()
    d["m3_stall_bull"] &= sig_ok
    d["m3_stall_bear"] &= sig_ok
    d["m3_div_bull"] &= div_ok
    d["m3_div_bear"] &= div_ok

    # ── M3 confirm combination ──────────────────────────────────────────
    def _confirm(stall: pd.Series, div: pd.Series) -> pd.Series:
        if not opts.sigma_mode and not opts.skew_mode:
            return pd.Series(True, index=d.index)
        if opts.require_both:
            if opts.sigma_mode and opts.skew_mode:
                return stall & div
            return (stall if opts.sigma_mode else div)
        # default OR across active modes
        out = pd.Series(False, index=d.index)
        if opts.sigma_mode:
            out = out | stall
        if opts.skew_mode:
            out = out | div
        return out

    d["m3_bull"] = _confirm(d["m3_stall_bull"], d["m3_div_bull"])
    d["m3_bear"] = _confirm(d["m3_stall_bear"], d["m3_div_bear"])

    # ── Fire (BULL priority) ────────────────────────────────────────────
    fire_bull = bool(opts.allow_bull) & d["m1_recent_bull"] & d["m2_recent_bull"] & d["m3_bull"]
    fire_bear = bool(opts.allow_bear) & d["m1_recent_bear"] & d["m2_recent_bear"] & d["m3_bear"]
    side = pd.Series([None] * len(d), index=d.index, dtype=object)
    direction = pd.Series([None] * len(d), index=d.index, dtype=object)
    side[fire_bull.to_numpy()] = "BULL"
    side[fire_bear.to_numpy() & ~fire_bull.to_numpy()] = "BEAR"  # BULL priority
    direction[fire_bull.to_numpy()] = "CONSENSUS SHORT"
    direction[fire_bear.to_numpy() & ~fire_bull.to_numpy()] = "CONSENSUS LONG"
    d["side"] = side
    d["direction"] = direction
    return d


def consensus_fires(df: pd.DataFrame, opts: ConsensusOpts = ConsensusOpts()) -> pd.DataFrame:
    """Return only the rows where the consensus fires, with the diagnostic flags
    needed to validate against the tool's CSV (m1/m2/stall/divergence)."""
    d = compute_consensus(df, opts)
    fires = d[d["side"].notna()].copy()
    keep = [c for c in ("ticker", "tradeDate", "side", "direction", "m1_side", "m2_side",
                         "m3_stall_bull", "m3_stall_bear", "m3_div_bull", "m3_div_bear",
                         "putP", "callP", "ivP", "rrP", "sigma", "skewDelta", "clsPx")
            if c in fires.columns]
    return fires[keep]


def signal_sign(side: str) -> int:
    """Signed P&L multiplier for a fired side.
    BULL setup -> CONSENSUS SHORT -> profit if price falls -> -1.
    BEAR setup -> CONSENSUS LONG  -> profit if price rises -> +1.
    """
    return -1 if side == "BULL" else (1 if side == "BEAR" else 0)
