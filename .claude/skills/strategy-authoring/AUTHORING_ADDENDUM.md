# Strategy-authoring ADDENDUM — skew-consensus

This is the **strategy-specific** companion to the generic `strategy-authoring/SKILL.md` house-rules.
Its content is the value of `strategy_config.AUTHORING_ADDENDUM` for the skew-consensus strategy:
the exact features, precisions, gate rules, float-forms, params, panel anchors, and the per-strategy
known-gaps checklist. Read it alongside the generic SKILL. Generic conventions (warm-up, verdict
windows, forward returns, PIT, parity mechanism, output schema) are NOT repeated here — see the SKILL.

## Data interface (skew-consensus specifics)

- **Signal inputs (ORATS):** `data/orats/universe_signal.parquet` — the option-surface features the
  consensus reads: `putP`, `callP`, `ivP`, `rrP`, `sigma`, `skewDelta` (this is the strategy's
  `FEATURE_COLS`). Percentiles/measures already computed by the parity-verified ORATS adapter.
- **Clean prices & forward returns (AV):** `data/av/signal_panel_clean.parquet` — `raw_close`,
  `av_fwd_{5,10,21}_{total,split}`, `av_matched`, `fwd_available_*`.
- **Symbol bridge:** `data/av/symbol_map.csv` maps ORATS tickers → AV symbols.
- **Counter-intuitive fact:** ORATS `clsPx` is ALREADY split+dividend adjusted (it tracks AV
  `adjusted_close`, not raw). Do not "adjust" it again; only a `raw_*` column is as-traded.

## Feature precisions (exact)

- **`sigma`** = population variance over the **strictly-prior** `hv20` window, rounded to **3 dp**.
  "Strictly-prior" excludes the current day — including it leaks today into today.
- **`skewDelta`** rounded to **2 dp**. Mid-rank tie-noise of ±0.2 is accepted and must never be
  "fixed" by adding precision — at 2 dp it never flips a 25/75 gate.
- Trust boundary: `putP / callP / ivP / rrP` TRUSTED as-is (already PIT in the ORATS adapter);
  `sigma / skewDelta` re-rounded defensively (3 dp / 2 dp). The `ivP` local PIT rebuild (`pit_ivp`)
  is the look-ahead guard — NOT a licence to recompute `putP / callP / rrP`.

## Gate thresholds & params

- `hi/lo` = **75 / 25**, `freshness` = **3**, `sigma_thr` = **1.0**. (These are the 9-field
  `ConsensusOpts` defaults; `strategy_config.CONSENSUS_OPTS` carries all nine — dropping any one
  silently changes the gate.)

## Worked reference — skew-consensus

A spec realized correctly. Imitate the shape; the reference is
`quant_validator/consensus_signal.py` `compute_consensus`.

- **M1 (corner):** `putP <= 25 & callP >= 75 -> BULL/SHORT`; mirror `putP >= 75 & callP <= 25 -> BEAR/LONG`.
- **M2:** `ivP >= 75 & (rrP >= 75 OR rrP <= 25)`.
- **M3:** sigma-stall OR skew-divergence.

  **M3 sigma-stall (verbatim):** `s0, s1, s2, s3 = sigma.shift(3), sigma.shift(2), sigma.shift(1),
  sigma` (s0 oldest = t−3 … s3 = today). Shift-AND across all 4 bars — `|sigma| >= sigma_thr` on each
  (signed to the side) **and** plateauing `flat = (s3 - s0).abs() < 0.3`. A NaN anywhere fails the gate.

  **M3 skew-divergence direction MUST match the M1 corner side** (`d0..d3 = skewDelta.shift(3..0)`):
  - `BULL` (call-rich -> SHORT fade): `fading = d0<0 and d1<0 and d2<0` **and** `turning = d3 > d2 + 0.2`.
  - `BEAR` (put-rich -> LONG fade): `rising = d0>0 and d1>0 and d2>0` **and** `turning = d3 < d2 - 0.2`.

  ```
  # INVARIANT: M3 divergence side == M1 corner side. BULL is always a SHORT fade,
  # BEAR always a LONG fade. Never confirm a BULL corner with a BEAR-shaped divergence.
  ```

  > **FLOAT FORM IS LOAD-BEARING.** Write `d3 > d2 + 0.2` (NOT `(d3 - d2) > 0.2`) and a 4-bar
  > shift-AND (NOT `rolling(4).sum() == 4`). Algebraically equal, NOT bit-equal (~332 ULP
  > disagreements vs the reference). Parity here is byte-level.

- **Sign:** BULL -> `signal_sign -1` (profits if price falls).

## Panel anchors (skew-consensus)

- **AV panel** (`signal_panel_clean.parquet`) binding anchor: 519,984 fires; verify **AAPL
  2015-08-05** (self-check → 98.8 / 78.97 / 1.6 / 5.6). This is the anchor for Mode-A AV-panel work.
- **Reference verdict (CANONICAL PRIMARY, ≥2018):** 21d +19.15 bps, gross +114.3, z 7.53, 341,506
  fires — what the gates read. Pre-2018 OOS must CONFIRM same-sign (+15.27 bps, z 4.58). Full-panel
  (+18.15 bps, gross +106.96, z 8.873) is the SIZING basis only, never the verdict.
- `WARMUP_START` for this panel = ORATS panel start `2011-01-03` + 756 bdays = `2013-11-26`.

## Pre-filter note (skew-consensus)

`compute_consensus` is PRE-FILTER, so `earnings_blackout` / `short_trend` are carried as flags
(default `False`), NOT applied inside `strategy()` — applying them breaks parity. They act downstream
(sizing / backtest), joining their earnings-date / trailing-return data there.

## Known gaps — codegen checklist (skew-consensus)

Self-check these BEFORE emitting `strategy()`. Each is a real bug class that has bitten this strategy.

1. **M3 skew-divergence direction** — locked to the M1 corner side per the INVARIANT (BULL = SHORT
   fade, BEAR = LONG fade); never confirm a BULL corner with a BEAR-shaped divergence.
2. **Float form is load-bearing** — `d3 > d2 + 0.2` (not `(d3-d2) > 0.2`) and a 4-bar shift-AND stall
   (not `rolling(4).sum()==4`); algebraically equal, NOT bit-equal (~332 ULP).
3. **Freshness arithmetic** — `flag.rolling(freshness, min_periods=1).max()` (today + `freshness − 1`
   prior bars); M1∧M2 co-fire = both `recent`-flags True on the SAME bar.
4. **Trust boundary** — `putP/callP/ivP/rrP` trusted as-is; `sigma/skewDelta` re-rounded (3dp/2dp);
   the `ivP` PIT rebuild is the look-ahead guard, not a recompute of the rest.
5. **Concrete fires-frame schema** — dtypes pinned (`symbol` str from `ticker`, `date`
   datetime64[ns], `signal_sign` int64 ∈ {−1,+1}, flags bool, `fwd_*` float NaN-at-tail).
6. **Fires-frame is PRE-FILTER** — `earnings_blackout`/`short_trend` carried as flags, applied
   downstream; applying them in `strategy()` breaks pre-filter parity.
7. **Anchor discipline** — AV binding anchor = 519,984 fires / AAPL 2015-08-05; every anchor
   panel-labelled (do not mix the ORATS `clsPx` build anchor with the AV panel).
8. **Validation path** — functional equivalence via `parity_gate.assert_fire_parity` + the standing
   parity tests; the dead `quant_validator.sandbox validate` CLI does not exist.

## ORATS-adapter failure modes (design against up front)

The ORATS adapter took four debug rounds for exactly these: token/resource leak in long pulls;
contango overflow on extreme term structure; memory-safe per-symbol streaming on the 16M-row panel;
cache-pollution dedup on write; boundary float-noise accepted only where it can't flip a gate.
