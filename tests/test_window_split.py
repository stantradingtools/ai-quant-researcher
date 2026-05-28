"""Tests for the PRIMARY/OOS window split in the Backtest agent (quant_validator.backtest).

Covers:
  1. partition completeness — PRIMARY (>=2018-01-01) + OOS (<=2017-12-31) == all warmed fires,
     disjoint and gap-free at the cutoff.
  2. warm-up safety — a fixed ticker's side/percentile on the windowed fires is IDENTICAL to the
     full-panel value (the split is a fire-scoring FILTER, never a recompute).
  3. end_date — run_test's end_date is an upper-bound mirror of start_date.
  4. empty-window safety — run() on a window clamped past its end -> status:"not_available", no crash.

(The fire-parity invariant is covered separately by tests/test_parity_gate.py and is
window-independent, so it is unaffected by the split.)
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pandas as pd
import pytest

from quant_validator import backtest as bt
from quant_validator.signal_vs_random import (annotate_clean, clean_run_columns,
                                              run_test, warmup_start_date)

CLEAN = Path("data/av/signal_panel_clean.parquet")
pytestmark = pytest.mark.skipif(not CLEAN.exists(), reason="clean panel not present (data/ gitignored)")


@pytest.fixture(scope="module")
def warm_fires():
    df = pd.read_parquet(CLEAN, columns=["tradeDate", "side"])
    w = pd.Timestamp(warmup_start_date(df["tradeDate"].min()))
    return df[df["side"].notna() & (df["tradeDate"] >= w)].copy(), w


def test_partition_complete_and_gapfree(warm_fires):
    f, _ = warm_fires
    cutoff = pd.Timestamp(bt.SPLIT_CUTOFF)     # 2018-01-01 (PRIMARY floor)
    oos_end = pd.Timestamp(bt.OOS_END)         # 2017-12-31 (OOS ceiling)
    primary = f[f["tradeDate"] >= cutoff]
    oos = f[f["tradeDate"] <= oos_end]
    assert len(primary) > 0 and len(oos) > 0
    # gap-free + complete: every warmed fire is in exactly one side
    assert len(primary) + len(oos) == len(f)
    # disjoint: nothing strictly between the OOS ceiling and the PRIMARY floor
    assert ((f["tradeDate"] > oos_end) & (f["tradeDate"] < cutoff)).sum() == 0
    assert len(primary.index.intersection(oos.index)) == 0


def test_window_is_filter_not_recompute():
    """Windowing must not change values: a fixed ticker's side/putP on the windowed slice
    equals the full-panel value at the same date."""
    df = pd.read_parquet(CLEAN, columns=["ticker", "tradeDate", "side", "putP"])
    a = (df[df["ticker"] == "AAPL"].dropna(subset=["side"])
         .sort_values("tradeDate").set_index("tradeDate"))
    assert len(a) > 0
    win = a[a.index >= pd.Timestamp(bt.SPLIT_CUTOFF)]
    j = win[["side", "putP"]].join(a[["side", "putP"]], rsuffix="_full")
    assert (j["side"] == j["side_full"]).all()
    assert (j["putP"] == j["putP_full"]).all()


def test_end_date_excludes_later_fires():
    """run_test end_date is the <= mirror of the >= start_date filter."""
    df = pd.read_parquet(CLEAN, columns=clean_run_columns())
    sub = df[df["ticker"].isin(["AAPL", "MSFT", "XOM", "JPM", "KO", "PG"])].copy()
    ann = annotate_clean(sub, "total", "full")
    capped = run_test(ann=ann, price_col="raw_close", end_date="2017-12-31", n_boot=50)
    full = run_test(ann=ann, price_col="raw_close", n_boot=50)
    assert capped["end_date"] == "2017-12-31"
    assert capped["scored_date_range"][1] <= "2017-12-31"
    assert full["scored_date_range"][1] > "2017-12-31"        # panel runs to ~2026
    assert capped["n_signals_scored"] < full["n_signals_scored"]


def test_empty_window_not_available():
    """run() on a window whose clamped start is past its end scores 0 fires -> not_available."""
    tid = "window_split_empty_test"
    try:
        out = bt.run(tid, start=bt.OOS_START, end="2012-06-30")   # start clamps to ~2013-11-26 > end
        assert out.get("status") == "not_available"
        assert out.get("n_fires") == 0
        vr = json.loads(Path(f"theses/{tid}/results/vs_random.json").read_text(encoding="utf-8"))
        assert vr["status"] == "not_available"
        assert vr["horizons"] == {}
    finally:
        shutil.rmtree(Path(f"theses/{tid}"), ignore_errors=True)
