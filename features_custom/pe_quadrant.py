"""PE Quadrant features and signals.

STATE: STUB. Phase 2 implementation — port from existing PE Quadrant HTML tool.
"""

from __future__ import annotations

import pandas as pd


def pe_zscore_252(pe_ratio: pd.Series) -> pd.Series:
    """Z-score of PE over trailing 252 days. Tradeable at t+1."""
    pe = pe_ratio.shift(1)
    mean = pe.rolling(252, min_periods=120).mean()
    std = pe.rolling(252, min_periods=120).std()
    return (pe - mean) / std


def pe_quadrant_label(pe_z: pd.Series, vix: pd.Series, vix_threshold: float = 20.0) -> pd.Series:
    """Returns one of: 'TL', 'TR', 'BL', 'BR'
    Top-Left: PE high, VIX low (sell premium / iron condor regime)
    Top-Right: PE high, VIX high (defensive)
    Bottom-Left: PE low, VIX low (long premium / cheap vol)
    Bottom-Right: PE low, VIX high (capitulation, contrarian long)
    """
    pe_high = pe_z > 0
    vix_high = vix.shift(1) > vix_threshold
    labels = []
    for ph, vh in zip(pe_high, vix_high):
        if ph and not vh:
            labels.append("TL")
        elif ph and vh:
            labels.append("TR")
        elif not ph and not vh:
            labels.append("BL")
        else:
            labels.append("BR")
    return pd.Series(labels, index=pe_z.index)
