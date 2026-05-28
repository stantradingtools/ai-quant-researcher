"""Tests for the Mutation agent's PRIMARY search + pre-2018 leak-guard (Prompt 3).

  1. search inherits SPLIT_CUTOFF  — optimise() defaults its search window to PRIMARY (>=2018).
  2. window partition              — _eligible(PRIMARY) and _eligible(pre-2018 OOS) are disjoint
     at the cutoff (search and holdout never overlap).
  3. end-to-end holdout            — optimise(write=False) on a small sample returns a search_window
     pinned to PRIMARY and a pre-2018 holdout block (the leak-guard the search never touched).
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pandas as pd
import pytest

from quant_validator import backtest as bt
from quant_validator import mutation_agent as M

CLEAN = Path("data/av/signal_panel_clean.parquet")
pytestmark = pytest.mark.skipif(not CLEAN.exists(), reason="clean panel not present (data/ gitignored)")


def test_search_inherits_split_cutoff():
    assert inspect.signature(M.optimise).parameters["start"].default == bt.SPLIT_CUTOFF == "2018-01-01"
    assert M.OOS_END == "2017-12-31"


def test_eligible_window_partition():
    rows = M._sample_panel(60, seed=1)
    prim = M._eligible(rows, M.SPLIT_CUTOFF)                 # PRIMARY search window (>=2018)
    oos = M._eligible(rows, "2010-01-01", M.OOS_END)        # pre-2018 leak-guard window
    assert len(prim) > 0 and len(oos) > 0
    assert prim["tradeDate"].min() >= pd.Timestamp(M.SPLIT_CUTOFF)
    assert oos["tradeDate"].max() <= pd.Timestamp(M.OOS_END)
    # the holdout window holds NOTHING the search sees
    assert (oos["tradeDate"] >= pd.Timestamp(M.SPLIT_CUTOFF)).sum() == 0


def test_optimise_runs_with_pre2018_holdout():
    r = M.optimise(n_tickers=60, n_folds=3, do_entry=False, write=False)
    assert r["search_window"]["start"] == bt.SPLIT_CUTOFF       # search on PRIMARY
    h = r["holdout"]
    assert h["status"] in ("ok", "not_available")
    if h["status"] == "ok":
        assert h["window"][1] == bt.OOS_END                    # held out the pre-2018 OOS
        assert h["n_fires"] > 0
        assert isinstance(h["confirms"], bool)
        assert "same_sign_as_primary" in h
    # tiny sample -> no validated improvement expected, but the verdict must be well-formed
    assert isinstance(r["promoted"], bool) and r["verdict"]
