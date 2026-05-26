"""Standing tests for the Coder->Backtest fire-parity gate (quant_validator.parity_gate).

Covers BOTH placements:
  1. test_gen_matches_reference        — Parity-A regression: the GENERATED flag-fn
     (_stage_flags_for_symbol) reproduces compute_consensus bit-for-bit (the 699-fire /
     0-disagreement case). Standing only — the generated module is quarantined out of scoring.
  2. test_panel_side_matches_reference — MATERIALIZATION parity: the shipped clean panel's
     STORED `side` (what run_test scores) equals a fresh compute_consensus recompute. This
     mirrors the runtime gate wired into backtest.run.
  3. test_injected_inversion_fails     — a BULL<->BEAR swap MUST raise ParityError (proves the
     gate actually bites — the regression in strategy-authoring SKILL 'Known gaps' #1).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pandas as pd
import pytest

from quant_validator import parity_gate as pg
from quant_validator.consensus_signal import ConsensusOpts, compute_consensus
from quant_validator.signal_vs_random import clean_run_columns

CLEAN = Path("data/av/signal_panel_clean.parquet")
GEN = Path("generated/strategy_skew_modeA.py")


def _ref_fn(sub):
    """The verified reference: compute_consensus on a per-symbol features frame."""
    return compute_consensus(sub.sort_values("tradeDate"), ConsensusOpts())


def _load_gen():
    spec = importlib.util.spec_from_file_location("gen_strategy_skew_modeA", GEN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def feature_panel():
    if not CLEAN.exists():
        pytest.skip("clean panel not present (data/ gitignored)")
    df = pd.read_parquet(CLEAN, columns=pg.FEATURE_COLS + ["ticker", "tradeDate"])
    return df[df["ticker"].isin(pg.PARITY_TICKERS)].copy()


@pytest.fixture(scope="module")
def side_panel():
    if not CLEAN.exists():
        pytest.skip("clean panel not present (data/ gitignored)")
    df = pd.read_parquet(CLEAN, columns=clean_run_columns())
    return df[df["ticker"].isin(pg.PARITY_TICKERS)].copy()


def test_gen_matches_reference(feature_panel):
    """Parity-A: the generated codegen reproduces the reference fire sides (0 disagreements)."""
    gen = _load_gen()
    rep = pg.assert_fire_parity(gen._stage_flags_for_symbol, _ref_fn, feature_panel)
    assert rep["passed"] and rep["disagreements"] == 0
    assert rep["fires"] > 0          # the ~699-fire AAPL/MSFT/XOM/JPM case


def test_panel_side_matches_reference(side_panel, feature_panel):
    """Materialization: the shipped panel's stored side == reference recompute (the runtime gate)."""
    rep = pg.assert_materialization_parity(side_panel, _ref_fn, feature_panel)
    assert rep["passed"] and rep["disagreements"] == 0 and rep["fires"] > 0


def test_injected_inversion_fails(feature_panel):
    """A BULL<->BEAR swap in the generated flag-fn MUST raise ParityError (the gate bites)."""
    gen = _load_gen()
    base = gen._stage_flags_for_symbol
    swap = {"BULL": "BEAR", "BEAR": "BULL"}

    def _inverted(g):
        out = base(g).copy()
        out["side"] = out["side"].map(lambda x: swap.get(x, x))   # swap fires, keep None as None
        return out

    with pytest.raises(pg.ParityError):
        pg.assert_fire_parity(_inverted, _ref_fn, feature_panel)
