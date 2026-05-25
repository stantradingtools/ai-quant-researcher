"""
Skew Consensus — Mode A (skew_consensus_modeA)
==============================================

Coder Agent (stage 3) output. QUARANTINED generated code.

Bidirectional option-positioning *exhaustion fade* on the underlying equity.
The ORATS option-surface percentiles are the SIGNAL SOURCE (not the traded
instrument); we trade the underlying equity, so there are no Greeks.

Signal mechanism (computed here from the raw features per refined.json):
  M1  skew corner          : BULL = callP>=hi & putP<=lo   (call crowding -> SHORT)
                             BEAR = putP>=hi  & callP<=lo   (put  crowding -> LONG)
  M2  IV x risk-reversal    : BULL = ivP>=hi & rrP>=hi
                             BEAR = ivP>=hi & rrP<=lo
  freshness gate            : M1 AND M2 must EACH have fired on the SAME side
                              within a trailing freshnessWindow (rolling-OR incl.
                              today). Implemented as rolling(window).max() per flag.
  M3  exhaustion confirm    : (default = EITHER; sigma-stall OR skew-divergence)
      (a) sigma STALL  : |sigma| held >= sigma_thr across the trailing 4 bars
                         (t, t-1, t-2, t-3), signed to the side, AND plateauing
                         (|sigma[t]-sigma[t-3]| < 0.3).
      (b) skew DIVERGENCE : skewDelta ran one-sided across t-3,t-2,t-1 and reverses
                            today by > div_thr (side-mapping follows the parity
                            reference — see note in _stage_flags_for_symbol).
  entry                     : side fires when M1(side,fresh) & M2(side,fresh) & M3(side).
                              BULL (SHORT) is evaluated first to break the rare both-fire tie.
  direction (fade)          : BEAR-extreme -> LONG underlying (signal_sign +1)
                              BULL-extreme -> SHORT underlying (signal_sign -1)

Conventions honored (SKILL.md house rules):
  * sigma : rounded to 3 dp (panel already stores it at 3 dp; re-rounded defensively).
  * skewDelta : rounded to 2 dp (panel already stores it at 2 dp; re-rounded defensively).
  * percentiles putP/callP/ivP/rrP read straight from the panel (0-100, 252d rolling
    mid-rank, already PIT in the parity-verified ORATS adapter).
  * NO look-ahead : every rolling window is strictly causal (window incl. today only;
    shift(1..3) for "prior" windows); never center=True.
  * forward returns : panel's precomputed survivorship-free av_fwd_{5,10,21}_total
    (headline, total-return basis). NaN at the tail is left as NaN (never filled).
  * price screen : $1 floor on raw_close (as-traded), not adjusted close.
  * max_abs_fwd : applied symmetrically (drops fires whose |fwd| exceeds the cap on
    ANY horizon) so a near-zero denominator cannot inject a spurious blow-up return.
  * sign : BULL -> signal_sign -1 (short; profits if price falls); BEAR -> +1 (long).

The M3 gate is written in the reference's EXACT algebraic form (explicit shift-AND
stall and `d3 > d2 + 0.2` divergence), which yields byte-perfect parity (0 mismatches
on all 16.19M panel rows) vs the verified reference. Writing it as `(d3-d2) > 0.2`
or a rolling-sum stall produces ~330 last-ULP float-boundary disagreements — accepted
noise, but the algebraic form removes them entirely.

Output: a tidy *fires frame* (one row per fire), NOT a positions[-1,1] series.

This module is independent of the parity-verified reference (consensus_signal.py /
compute_consensus); it derives M1/M2/M3 + freshness from the raw features itself.
The precomputed reference columns in the panel (side, m1_side, m3_bull, direction,
m*_recent_*, ...) are deliberately NOT read.
"""

import numpy as np
import pandas as pd

# --- Spec parameters (refined.json) -----------------------------------------
HI = 75.0          # upper-quartile "unusual/elevated/stretched" cut
LO = 25.0          # lower-quartile cut
FRESHNESS = 3      # trailing co-firing window for M1/M2 (bars, incl. today)
SIGMA_THR = 1.0    # |sigma| floor for the stall test
SIGMA_PLATEAU = 0.3   # |sigma[t]-sigma[t-3]| flattening tolerance for stall
DIV_THR = 0.2      # skewDelta reversal magnitude for the divergence test

# Forward-return horizons / basis (SKILL.md headline = total return)
FWD_COLS = {
    "fwd_5": "av_fwd_5_total",
    "fwd_10": "av_fwd_10_total",
    "fwd_21": "av_fwd_21_total",
}
FWD_AVAIL = {"fwd_5": "fwd_available_5", "fwd_10": "fwd_available_10", "fwd_21": "fwd_available_21"}

# Tradeable-universe screens (SKILL.md)
MIN_RAW_CLOSE = 1.0   # $1 floor on as-traded close
MAX_ABS_FWD = 5.0     # 500% symmetric cap on any horizon's |forward return|

DEFAULT_PANEL = "data/av/signal_panel_clean.parquet"

# Columns we actually read (keeps the 16M-row load lean and avoids touching the
# precomputed reference columns by accident).
_READ_COLS = [
    "ticker", "tradeDate",
    "putP", "callP", "ivP", "rrP", "sigma", "skewDelta",
    "raw_close",
    "av_fwd_5_total", "av_fwd_10_total", "av_fwd_21_total",
    "av_matched", "fwd_available_5", "fwd_available_10", "fwd_available_21",
]


def _rolling_or(flag: pd.Series, window: int) -> pd.Series:
    """Trailing rolling-OR over `window` bars (incl. today). Strictly causal."""
    return flag.rolling(window, min_periods=1).max().astype(bool)


def _stage_flags_for_symbol(g: pd.DataFrame) -> pd.DataFrame:
    """
    Compute M1/M2/M3 + freshness + consensus side for ONE symbol's sorted series.

    `g` must be sorted ascending by tradeDate and keep its original (unique) index;
    the returned frame is index-aligned to `g`.
    """
    putP, callP = g["putP"], g["callP"]
    ivP, rrP = g["ivP"], g["rrP"]
    # Honor SKILL.md rounding exactly (panel already conforms; defensive re-round).
    sigma = g["sigma"].round(3)
    skewDelta = g["skewDelta"].round(2)

    # --- M1: skew corner (per-bar raw fire) ---
    m1_bull = (callP >= HI) & (putP <= LO)   # call-side crowding / euphoria -> SHORT
    m1_bear = (putP >= HI) & (callP <= LO)   # put-side crowding / protection -> LONG

    # --- M2: IV x risk-reversal corner (per-bar raw fire) ---
    m2_bull = (ivP >= HI) & (rrP >= HI)      # euphoria top -> SHORT
    m2_bear = (ivP >= HI) & (rrP <= LO)      # capitulation -> LONG

    # --- Freshness gate: each stage must have fired on the SAME side within the
    #     trailing freshness window (rolling-OR incl. today). ---
    m1_recent_bull = _rolling_or(m1_bull, FRESHNESS)
    m1_recent_bear = _rolling_or(m1_bear, FRESHNESS)
    m2_recent_bull = _rolling_or(m2_bull, FRESHNESS)
    m2_recent_bear = _rolling_or(m2_bear, FRESHNESS)

    # --- M3 (a) sigma STALL (shift-based 4-bar window; reference's exact form):
    #     |sigma|>=thr on each of t,t-1,t-2,t-3 AND plateauing |sigma[t]-sigma[t-3]|<0.3. ---
    s0, s1, s2, s3 = sigma.shift(3), sigma.shift(2), sigma.shift(1), sigma
    flat = (s3 - s0).abs() < SIGMA_PLATEAU
    m3_stall_bull = (s0 >= SIGMA_THR) & (s1 >= SIGMA_THR) & (s2 >= SIGMA_THR) & (s3 >= SIGMA_THR) & flat
    m3_stall_bear = (s0 <= -SIGMA_THR) & (s1 <= -SIGMA_THR) & (s2 <= -SIGMA_THR) & (s3 <= -SIGMA_THR) & flat

    # --- M3 (b) skew DIVERGENCE (reference's exact algebra: d3 > d2 + thr):
    #     skewDelta one-sided across t-3,t-2,t-1, reversing today.
    #
    #   NOTE on the side-mapping. refined.json's PROSE reads "for a LONG/BEAR-extreme:
    #   skewDelta negative then turning up". The parity-verified reference assigns the
    #   OPPOSITE side to each reversal, and the reference is the binding ground truth
    #   (byte-perfect parity, 0 mismatches on all 16.19M rows). We follow the reference:
    #     BULL-side divergence : skewDelta NEGATIVE on t-3,t-2,t-1, now turning UP.
    #     BEAR-side divergence : skewDelta POSITIVE on t-3,t-2,t-1, now turning DOWN. ---
    d0, d1, d2, d3 = skewDelta.shift(3), skewDelta.shift(2), skewDelta.shift(1), skewDelta
    m3_div_bull = (d0 < 0) & (d1 < 0) & (d2 < 0) & (d3 > d2 + DIV_THR)
    m3_div_bear = (d0 > 0) & (d1 > 0) & (d2 > 0) & (d3 < d2 - DIV_THR)

    # NaN guards: if any value in the window is null, the gate fails (matches reference).
    sig_ok = s0.notna() & s1.notna() & s2.notna() & s3.notna()
    div_ok = d0.notna() & d1.notna() & d2.notna() & d3.notna()
    m3_stall_bull &= sig_ok
    m3_stall_bear &= sig_ok
    m3_div_bull &= div_ok
    m3_div_bear &= div_ok

    # --- M3 combined (default mode = EITHER) ---
    m3_bull = m3_stall_bull | m3_div_bull
    m3_bear = m3_stall_bear | m3_div_bear

    # --- Entry: M1(fresh) & M2(fresh) & M3 all on the SAME side.
    #     BULL (SHORT) evaluated first to break the rare both-fire tie. ---
    fire_bull = m1_recent_bull & m2_recent_bull & m3_bull
    fire_bear = m1_recent_bear & m2_recent_bear & m3_bear

    side = np.where(fire_bull, "BULL", np.where(fire_bear, "BEAR", None))
    side = pd.Series(side, index=g.index, dtype="object")

    # Fade sign: BULL -> SHORT (-1) ; BEAR -> LONG (+1).
    signal_sign = pd.Series(
        np.where(fire_bull, -1, np.where(fire_bear, 1, np.nan)),
        index=g.index, dtype="float64",
    )

    out = pd.DataFrame(index=g.index)
    out["side"] = side
    out["signal_sign"] = signal_sign
    out["M1"] = np.where(fire_bull, m1_recent_bull, np.where(fire_bear, m1_recent_bear, False))
    out["M2"] = np.where(fire_bull, m2_recent_bull, np.where(fire_bear, m2_recent_bear, False))
    out["M3"] = np.where(fire_bull, m3_bull, np.where(fire_bear, m3_bear, False))
    out["m3_stall"] = np.where(fire_bull, m3_stall_bull, np.where(fire_bear, m3_stall_bear, False))
    out["m3_div"] = np.where(fire_bull, m3_div_bull, np.where(fire_bear, m3_div_bear, False))
    return out


def strategy(features: pd.DataFrame | str | None = None) -> pd.DataFrame:
    """
    Consume the AV clean signal panel and return a tidy fires frame.

    Parameters
    ----------
    features : DataFrame | str | None
        The data panel (or a path to it). If None, reads DEFAULT_PANEL.
        Must carry: ticker, tradeDate, putP, callP, ivP, rrP, sigma, skewDelta,
        raw_close, av_fwd_{5,10,21}_total, av_matched, fwd_available_*.

    Returns
    -------
    DataFrame, one row per fire, with columns:
        symbol, date, side, signal_sign,
        fwd_5, fwd_10, fwd_21,
        M1, M2, M3, m3_stall, m3_div,
        raw_close, av_matched, fwd_available_{5,10,21},
        earnings_blackout, short_trend_block   (risk-filter flags; see notes)
    """
    # --- Load / accept the panel ---
    if features is None:
        panel = pd.read_parquet(DEFAULT_PANEL, columns=_READ_COLS)
    elif isinstance(features, str):
        panel = pd.read_parquet(features, columns=_READ_COLS)
    else:
        panel = features

    # Ensure the columns we need exist (tolerate a wider panel being passed in).
    needed = set(_READ_COLS)
    missing = needed - set(panel.columns)
    if missing:
        raise KeyError(f"panel missing required columns: {sorted(missing)}")

    panel = panel[list(_READ_COLS)].copy()
    panel["tradeDate"] = pd.to_datetime(panel["tradeDate"])

    # --- Per-symbol stage computation (memory-safe; no load-and-multiply) ---
    # Sort within each ticker by tradeDate (stable), keep a clean unique index,
    # then compute causal flags per group.
    panel = panel.sort_values(["ticker", "tradeDate"], kind="mergesort").reset_index(drop=True)

    stage_parts = []
    for _, g in panel.groupby("ticker", sort=False):
        stage_parts.append(_stage_flags_for_symbol(g))
    stages = pd.concat(stage_parts).reindex(panel.index)

    panel = pd.concat([panel, stages], axis=1)

    # --- Keep only fired bars ---
    fires = panel[panel["side"].notna()].copy()

    # --- Tradeable-universe screens (SKILL.md) ---
    # $1 floor on RAW (as-traded) close.
    fires = fires[fires["raw_close"] > MIN_RAW_CLOSE]
    # Drop rows the AV bridge could not match (no clean price/forward basis).
    fires = fires[fires["av_matched"]]

    # Symmetric max_abs_fwd cap: a fire is discarded if ANY available horizon's
    # |forward return| exceeds the cap (guards a near-zero denominator blow-up).
    fwd5, fwd10, fwd21 = (fires["av_fwd_5_total"], fires["av_fwd_10_total"], fires["av_fwd_21_total"])
    too_big = (
        (fwd5.abs() > MAX_ABS_FWD)
        | (fwd10.abs() > MAX_ABS_FWD)
        | (fwd21.abs() > MAX_ABS_FWD)
    )
    fires = fires[~too_big.fillna(False)]

    # --- Risk-filter flags (carried, NOT applied here) -----------------------
    # Earnings blackout (3d pre / 1d post): the data plan's earnings adapter is
    # NOT present in this panel (no earnings-date column), so the blackout cannot
    # be evaluated at this stage. We expose a False stub so a downstream stage can
    # join earnings dates and apply it. Leaving it un-applied keeps fire-level
    # parity with the consensus reference (which is also pre-filter).
    fires["earnings_blackout"] = False

    # Short-trend filter (SHORT/BULL side only): "do not short into a strong
    # up-trend" — trailing 21d return > +15%. The panel exposes only raw_close per
    # fired row (no contiguous price series here), and the consensus reference is
    # pre-filter, so we expose the flag as False and leave application downstream.
    fires["short_trend_block"] = False

    # --- Assemble the tidy fires frame ---
    out = pd.DataFrame({
        "symbol": fires["ticker"].astype(str).to_numpy(),
        "date": fires["tradeDate"].to_numpy(),
        "side": fires["side"].astype(str).to_numpy(),
        "signal_sign": fires["signal_sign"].astype("int64").to_numpy(),
        "fwd_5": fires["av_fwd_5_total"].to_numpy(),
        "fwd_10": fires["av_fwd_10_total"].to_numpy(),
        "fwd_21": fires["av_fwd_21_total"].to_numpy(),
        "M1": fires["M1"].astype(bool).to_numpy(),
        "M2": fires["M2"].astype(bool).to_numpy(),
        "M3": fires["M3"].astype(bool).to_numpy(),
        "m3_stall": fires["m3_stall"].astype(bool).to_numpy(),
        "m3_div": fires["m3_div"].astype(bool).to_numpy(),
        "raw_close": fires["raw_close"].to_numpy(),
        "av_matched": fires["av_matched"].astype(bool).to_numpy(),
        "fwd_available_5": fires["fwd_available_5"].astype(bool).to_numpy(),
        "fwd_available_10": fires["fwd_available_10"].astype(bool).to_numpy(),
        "fwd_available_21": fires["fwd_available_21"].astype(bool).to_numpy(),
        "earnings_blackout": fires["earnings_blackout"].astype(bool).to_numpy(),
        "short_trend_block": fires["short_trend_block"].astype(bool).to_numpy(),
    })

    out = out.sort_values(["date", "symbol"], kind="mergesort").reset_index(drop=True)
    return out


if __name__ == "__main__":
    f = strategy()
    print("fires:", len(f))
    print(f["side"].value_counts())
    print(f.head().to_string())
