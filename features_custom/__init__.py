"""features_custom: Stan's domain-specific feature functions.

All feature functions obey the contract from ai_quant_lab.features.library:
- Input: pd.Series or pd.DataFrame indexed by timestamp (or MultiIndex
  for cross-sectional)
- Output: same index, where value at time t uses ONLY rows with index <= t
- Always .shift(1) to make values tradeable at t+1
- Never center=True
- Annualize at the metric layer, not in feature space

Modules:
- skew: skew_z_score, skew_change_5d, skew_residualized (Tian & Wu future)
- vol: vrp_pct, iv_rv_spread, term_structure_slope
- exposure: gex_distance, gamma_flip_distance, dealer_alignment
- pe_quadrant: pe_zscore_252, pe_quadrant_label
"""
