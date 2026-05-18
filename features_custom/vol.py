"""Volatility-related features (VRP, IV/RV spreads, term structure).

STATE: STUB. Phase 2 implementation.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def vrp_pct(iv30: pd.Series, rv20: pd.Series) -> pd.Series:
    """VRP = IV30 - RV20, percentile-ranked over trailing window.
    
    Both inputs must be in same units (annualized vol points).
    """
    raw_vrp = iv30.shift(1) - rv20.shift(1)
    pct = raw_vrp.rolling(252, min_periods=120).rank(pct=True)
    return pct


def iv_rv_spread(iv: pd.Series, rv: pd.Series) -> pd.Series:
    """Simple IV - RV spread, tradeable at t+1."""
    return iv.shift(1) - rv.shift(1)


def term_structure_slope(iv_front: pd.Series, iv_back: pd.Series) -> pd.Series:
    """Front-back IV term structure slope, tradeable at t+1.
    
    Negative slope (backwardation) often signals fear; positive slope normal.
    """
    return iv_front.shift(1) - iv_back.shift(1)
