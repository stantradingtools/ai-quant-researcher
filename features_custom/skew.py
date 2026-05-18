"""Skew-related features for options strategies.

STATE: STUB. Phase 2 implementation.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def skew_z_score(skew_series: pd.Series, lookback: int = 252) -> pd.Series:
    """Z-score of skew measure over trailing window. Tradeable at t+1.
    
    Input: skew_series indexed by timestamp (e.g., 25-delta put IV - 25-delta call IV).
    Output: z-score series, same index, shifted to be tradeable.
    """
    if skew_series.empty:
        return skew_series.copy()
    mean = skew_series.shift(1).rolling(lookback, min_periods=lookback // 2).mean()
    std = skew_series.shift(1).rolling(lookback, min_periods=lookback // 2).std()
    z = (skew_series.shift(1) - mean) / std
    return z


def skew_change_5d(skew_series: pd.Series) -> pd.Series:
    """5-day change in skew. Tradeable at t+1."""
    return skew_series.shift(1).diff(5)


# TODO Phase 2: skew_residualized — Tian & Wu's decomposition vs sector beta and DTD
