"""Standing Coder->Backtest gate: a strategy's fire SIDES must reproduce its declared
reference bit-for-bit, or scoring is refused. Catches the class of bug where codegen
silently inverts/drops a side (e.g. a BULL corner confirmed by a BEAR-shaped divergence).
See strategy-authoring SKILL 'Known gaps' #1 (commit cb7ca3e).

TWO placements of the same comparison:
  - assert_fire_parity            : GENERATED flag-fn vs reference (Parity-A regression).
                                    Held as a STANDING TEST — the generated module is
                                    quarantined out of the live scoring path.
  - assert_materialization_parity : the panel's STORED `side` (what run_test actually scores)
                                    vs a fresh reference recompute on the SAME warmed features.
                                    This is the RUNTIME gate wired into backtest.run — it guards
                                    exactly what gets scored (stale/ drifted panel -> refuse).

Pure & deterministic: no fetching. The runtime gate reads the 6 feature columns for a fixed
ticker set off the SAME warmed clean panel the adapter already loaded (no network, no warm-up
mismatch, no look-ahead).
"""

from __future__ import annotations

import pandas as pd

# Fixed, liquid, cross-sector tickers — the standing parity sample (deterministic).
PARITY_TICKERS = ["AAPL", "MSFT", "XOM", "JPM"]
SIDE_VALUES = {None, "BULL", "BEAR"}
# The 6 consensus feature columns compute_consensus / _stage_flags_for_symbol read
# (confirmed in discovery). The adapter's run_test panel does NOT carry these, so the gate
# reads them itself for PARITY_TICKERS off the same on-disk warmed panel.
FEATURE_COLS = ["putP", "callP", "ivP", "rrP", "sigma", "skewDelta"]


class ParityError(AssertionError):
    """Raised when a strategy's fire sides disagree with its declared reference."""


def _resolved_side_series(flags_df: pd.DataFrame) -> pd.Series:
    """Collapse a flags/consensus frame -> one fire side per row in {None,'BULL','BEAR'},
    indexed by tradeDate so generated/stored vs reference align by DATE (not by a frame's
    incidental row index). Raises if `side`/`tradeDate` are absent or a side value is unexpected
    — so this never silently passes."""
    if "side" not in flags_df.columns:
        raise ParityError("flags frame has no 'side' column — cannot resolve the fire side")
    if "tradeDate" not in flags_df.columns:
        raise ParityError("flags frame has no 'tradeDate' — needed to align gen/stored vs reference")
    s = flags_df["side"]
    bad = set(pd.unique(s.dropna().astype(object))) - {"BULL", "BEAR"}
    if bad:
        raise ParityError(f"unexpected side values {sorted(bad)} (allowed {SIDE_VALUES})")
    out = pd.Series(s.to_numpy(dtype=object),
                    index=pd.to_datetime(flags_df["tradeDate"]).to_numpy(), name="side")
    out.index.name = "tradeDate"
    return out[~out.index.duplicated(keep="first")]   # clean panel is unique per (ticker,date)


def _compare(a: pd.Series, b: pd.Series, col_a: str, col_b: str):
    """Inner-join two side series on tradeDate; return (n_ref_fires, n_disagree, disagreement_df).
    A disagreement = the two differ AND at least one side fired (None==None is agreement)."""
    aligned = a.to_frame(col_a).join(b.to_frame(col_b), how="inner")
    ref_fires = int(aligned[col_b].notna().sum())
    disagree = aligned[col_a].ne(aligned[col_b]) & (aligned[col_a].notna() | aligned[col_b].notna())
    return ref_fires, int(disagree.sum()), aligned[disagree]


def assert_fire_parity(gen_flags_fn, reference_fn, panel: pd.DataFrame, *,
                       tickers=PARITY_TICKERS, tol: int = 0) -> dict:
    """STANDING regression: the GENERATED per-symbol flag fn must reproduce the reference's
    fire sides on `panel` (which must carry the 6 features + ticker + tradeDate). Raises
    ParityError on > tol disagreements; returns a report on success."""
    total_fires = total_disagree = 0
    samples = []
    for sym in tickers:
        sub = panel[panel["ticker"] == sym].sort_values("tradeDate")
        if sub.empty:
            continue
        gen_out = gen_flags_fn(sub)
        if "tradeDate" not in getattr(gen_out, "columns", []):   # gen flag-fn drops tradeDate
            gen_out = gen_out.assign(tradeDate=sub["tradeDate"].to_numpy())
        gen = _resolved_side_series(gen_out)
        ref = _resolved_side_series(reference_fn(sub))
        nf, nd, ddf = _compare(gen, ref, "gen", "ref")
        total_fires += nf
        total_disagree += nd
        if nd:
            samples.append((sym, ddf.head(5)))
    if total_disagree > tol:
        raise ParityError(
            f"FIRE-PARITY FAIL: {total_disagree}/{total_fires} rows disagree (tol={tol}).\n"
            + "\n".join(f"  {s}:\n{df.to_string()}" for s, df in samples))
    return {"check": "fire_parity", "tickers": list(tickers), "fires": total_fires,
            "disagreements": total_disagree, "passed": True}


def assert_materialization_parity(panel: pd.DataFrame, reference_fn, feature_panel: pd.DataFrame, *,
                                  tickers=PARITY_TICKERS, tol: int = 0) -> dict:
    """RUNTIME gate for the scoring path: the panel's STORED side (what run_test scores) must
    equal a fresh reference recompute on the same warmed features. Catches panel staleness /
    reference-vs-materialization drift. No generated module involved. Raises on > tol."""
    total_fires = total_disagree = 0
    samples = []
    for sym in tickers:
        stored = _resolved_side_series(panel[panel["ticker"] == sym])
        recomp = _resolved_side_series(reference_fn(feature_panel[feature_panel["ticker"] == sym]
                                                    .sort_values("tradeDate")))
        nf, nd, ddf = _compare(stored, recomp, "stored", "recomp")
        total_fires += nf
        total_disagree += nd
        if nd:
            samples.append((sym, ddf.head(5)))
    if total_disagree > tol:
        raise ParityError(
            f"MATERIALIZATION-PARITY FAIL: {total_disagree}/{total_fires} stored-vs-reference "
            f"rows disagree (tol={tol}).\n"
            + "\n".join(f"  {s}:\n{df.to_string()}" for s, df in samples))
    return {"check": "materialization_parity", "tickers": list(tickers), "fires": total_fires,
            "disagreements": total_disagree, "passed": True}
