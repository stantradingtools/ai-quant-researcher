"""Dealer-exposure features from Flash Alpha or computed from chain.

STATE: STUB. Phase 2 implementation.
"""

from __future__ import annotations

import pandas as pd


def gex_distance(spot_price: pd.Series, gamma_flip_price: pd.Series) -> pd.Series:
    """Distance of spot from gamma flip, in % terms. Tradeable at t+1."""
    return (spot_price.shift(1) - gamma_flip_price.shift(1)) / spot_price.shift(1)


def dealer_alignment(net_gex: pd.Series) -> pd.Series:
    """+1 if dealers are net long gamma (suppressive), -1 if short.
    Tradeable at t+1.
    """
    return pd.Series(
        [1.0 if v > 0 else -1.0 for v in net_gex.shift(1)],
        index=net_gex.index,
    )
