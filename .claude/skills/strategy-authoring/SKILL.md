---
name: strategy-authoring
description: >-
  House rules for turning a validated strategy spec into a runnable strategy()
  function in this quant-research pipeline. The Coder Agent MUST read this before
  writing or editing ANY strategy() code, signal generator, backtest adapter, or
  feature function — even when the strategy "looks simple." Use it whenever a JSON
  spec needs to become Python that produces (ticker, date, side) fires and forward
  returns, whenever a strategy must call the ORATS/AV data adapters, or whenever
  code must reproduce a known reference (parity work). The conventions here encode
  bugs that already cost four debug rounds; skipping them reintroduces them.
---

# Strategy Authoring

You are the Coder Agent (stage 3). Your job is to turn a spec from the Hypo-Refiner
Agent into a single, runnable `strategy()` that the Backtest Engine (stage 4) can
execute over the data panel. Downstream stages (VsRandom, Validator, Gatekeeper,
Risk, Reporter) depend on your output matching the contract below exactly.

Most strategy bugs in this system are not logic errors — they are *convention*
errors: a percentile rounded to the wrong place, a return computed across a split,
a window that peeks one day into the future. The maths "works" and the numbers are
plausibly wrong. This guide exists to stop that.

## The contract (what `strategy()` must produce)

`strategy()` consumes the data panel and returns one row per fire with at least:

- `symbol` (or `ticker`) — the instrument
- `date` — the entry (signal) date
- `side` — `BULL` / `BEAR` (the directional read), and
- `signal_sign` — the position direction: **`BULL` -> `-1` (short; profits if price falls)**, **`BEAR` -> `+1` (long)**. This sign convention is load-bearing for every downstream P&L calculation — get it backwards and the verdict inverts.
- the forward returns for each horizon (see Forward returns below)
- any stage flags the spec defines (e.g. `M1`, `M2`, `M3`)

Do not return prices, do not return signed P&L — return the fire and its forward
returns; the Backtest Engine and VsRandom Module turn those into P&L. Keep
`strategy()` pure and deterministic: same panel in, same fires out.

## Data interface — where inputs live and what they mean

Read from the existing adapters; never re-pull or re-derive data a panel already holds.

- **Signal inputs (ORATS):** `data/orats/universe_signal.parquet` — the option-surface
  features the consensus reads: `putP`, `callP`, `ivP`, `rrP`, `sigma`, `skewDelta`.
  These are percentiles/measures already computed by the ORATS adapter (parity-verified).
- **Clean prices & forward returns (AV):** `data/av/signal_panel_clean.parquet` — survivorship-free,
  split/dividend-adjusted. Columns include `raw_close` (as-traded), `av_fwd_{5,10,21}_total`
  (split+dividend, the headline basis), `av_fwd_{5,10,21}_split` (split-only, robustness basis),
  `av_matched`, `fwd_available`.
- **Symbol bridge:** `data/av/symbol_map.csv` maps ORATS tickers to AV symbols (with
  share-class/suffix normalization). Join through it — never assume the two sources use
  the same symbol string.

Critical, counter-intuitive fact, learned by validation: **ORATS `clsPx` is ALREADY
split+dividend adjusted** (it tracks AV `adjusted_close`, not raw price). Do not "adjust"
it again, and do not assume any price column is raw unless its name says so (`raw_close`).

## Non-negotiable conventions (the bug-magnets)

Each of these caused a real, silent error. Reproduce them exactly.

### sigma
`sigma` = population variance over the **strictly-prior** `hv20` window, rounded to **3 dp**.
"Strictly-prior" means the current day is excluded — including it leaks today into today.

### skewDelta
Round `skewDelta` to **2 dp**. Mid-rank tie-noise of +/-0.2 is accepted and must never be
"fixed" by adding precision — at 2 dp it never flips a 25/75 gate.

### percentiles & freshness
`putP / callP / ivP / rrP` are percentiles on a 0-100 scale. Gate thresholds are the
spec's `hi/lo` (default **75 / 25**). Apply the spec's `freshness` window (default **3**) as a
trailing rolling-OR over each stage flag: `recent = flag.rolling(freshness, min_periods=1).max()`
(today + the `freshness − 1` prior bars). **M1∧M2 co-fire = both `recent`-flags True on the SAME
bar** — not merely each having fired somewhere in the window independently. Match the
reference percentile method bit-for-bit; off-by-one ranking changes which trades fire.

### signal warm-up — 3 years / 756 trading days (PROJECT CONVENTION)
The first tradeable signal date = **panel start + `WARMUP_BDAYS` (756) business days**.
No fire may occur before that date, so every percentile / freshness / `sigma` / `skewDelta`
window has a full **3 years** of ORATS history behind it. This is a single source of truth:
`WARMUP_BDAYS = 756` and `WARMUP_START` (= ORATS panel start `2011-01-03` + 756 bdays =
`2013-11-26`) live in `quant_validator/signal_vs_random.py`; `ai_quant_lab.config.settings.warmup_bdays`
mirrors it (env `AI_QUANT_LAB_WARMUP_BDAYS`). It replaces the ad-hoc 252/504 a refiner may
propose — **do not** let a spec's `warm_up_buffer` override it downward.

- **Backtest / Mode-A adapter:** compute the start with `warmup_start_date(panel["tradeDate"].min())`
  when no explicit `start` is passed; never hard-code a calendar date.
- **Paper live-signal loop:** computing the signal for the next session uses the **trailing
  756-bday ORATS window** ending at that session — same look-back as the backtest, so live and
  historical fires are constructed identically (`fills.LIVE_SIGNAL_WARMUP_BDAYS`).
- Moving the warm-up later drops the earliest fires (≈2012 + most of 2013, ~4–5% of fires for
  the skew consensus). The verdict must stay materially unchanged — those early fires sat near
  the pooled average, so the 21d increment holds (~+18.3 bps / gross +106.5). If it moves a lot,
  the early years were carrying the edge and that is a finding, not a warm-up tweak.

### forward returns
Compute close-to-close, on a **trading-day** offset **within each symbol's own sorted
series**:

```
fwd_h[i] = clsPx[i + h] / clsPx[i] - 1
```

- Horizons are **5 / 10 / 21** trading days unless the spec says otherwise.
- The last `h` rows of each symbol have **no** forward return -> `NaN`. **Never forward-fill.**
- For delisted names, a window that runs past the last trade or the delisting date is
  `NaN` (you could not have held it). This is correct, not a gap to patch.

**Sanity anchor — anchors are panel-specific; always label which panel an anchor belongs to.**

- **AV panel** (`signal_panel_clean.parquet`) **binding anchor:** 519,984 fires; verify
  **AAPL 2015-08-05**. This is the anchor for Mode-A work on the AV panel.
- **ORATS `clsPx` build anchor** (a DIFFERENT panel — do NOT use it for the AV panel):
  historical only.

If your `strategy()` doesn't reproduce the binding anchor for the panel you're on, stop and
debug before going further.

### returns basis
Default to **total-return** (`av_fwd_*_total`) as the headline — it matches the
historical ORATS basis. Keep **split-only** (`av_fwd_*_split`) available as a
robustness check. Build split-only from `raw_close / (cumulative product of
split_coefficient for all splits strictly AFTER that date)` — validate that windows
NOT crossing a split equal the raw return.

### price screen on RAW close
The tradeable-universe screen (e.g. the `$1` floor) runs on **`raw_close` (as-traded)**,
not on adjusted close. Adjusted prices are back-adjusted, so historical *levels* are
distorted and a `$1` test on them flags the wrong names. Apply `max_abs_fwd` (default
500%) **symmetrically** to signal and any comparison pool — a near-zero denominator
once produced a spurious +1900% return and inflated a random baseline to ~1756%.

## Look-ahead & point-in-time discipline

The fastest way to fake an edge is to peek. Every window must be strictly prior; every
percentile must be computable from data available on or before the entry date.

- Prefer a **local point-in-time rebuild** of any percentile whose provider point-in-time-ness
  is unverifiable (the `pit_ivp` pattern: rebuild `ivP` locally with zero provider look-ahead).
  A signal that only survives with the provider's value but dies under a local PIT rebuild
  was leaking.
- Use the **survivorship-free** universe (active + delisted). Building on survivors only
  flatters the edge — especially in stress years where delistings cluster.

**Trust boundary (read vs recompute).** Percentiles `putP / callP / ivP / rrP` are **TRUSTED
as-is** from the panel (already PIT in the parity-verified ORATS adapter). `sigma` / `skewDelta`
are provider-supplied; **re-round defensively (3 dp / 2 dp)** — a no-op on this panel, but keep
it. The `ivP` point-in-time local rebuild is the **look-ahead guard**, not a licence to recompute
the others — do NOT re-derive `putP / callP / rrP` from scratch.

## Parity & validation discipline

When a reference implementation exists (it usually does for a re-run), your generated
code must be checked against it — functional equivalence, not byte-identity.

- **Quarantine generated code.** Write to a fresh path (e.g. `generated/strategy_<name>.py`).
  **Never overwrite a parity-verified module** (e.g. `consensus_signal.py` / `compute_consensus`).
- **Report fire-level fidelity:** match the reference fires and report the rate (the
  consensus reference hits 98.9% fire, 100% side/stall/divergence fidelity; residuals are
  boundary float-noise and data gaps, not logic).
- **Reproduce a known anchor** before trusting anything (the panel-specific binding anchor
  above; an AAPL self-check such as 2015-08-05 -> 98.8 / 78.97 / 1.6 / 5.6).
- **Check the verdict, not just the code.** If the strategy has a known Mode B result,
  the generated strategy's vs-random verdict must land on it (reference 21d: increment
  +18.3 bps, gross +106.5, z 9.12). A code diff that "looks fine" but moves the verdict
  is a real divergence.
- **Validate by FUNCTIONAL EQUIVALENCE vs `compute_consensus`, not a CLI** — there is no
  `python -m quant_validator.sandbox validate` (it does not exist). Use
  `quant_validator.parity_gate.assert_fire_parity(gen_flags_fn, compute_consensus, panel)` and
  the standing `tests/test_parity_gate.py`. Target: **0 side disagreements** on the parity
  tickers (the runtime materialization gate enforces the same inside `backtest.run`).

## Where tail control belongs — NOT inside `strategy()`

Tail/regime control is a **Risk Agent (stage 9)** concern — a portfolio-level drawdown/
regime kill-switch — not a per-trade entry filter inside `strategy()`. A blunt
volatility/VIX entry gate both **costs alpha** (it discarded trades worth ~+294 bps/trade)
and is **mis-targeted** (it removed the crash fires where fading worked and kept the
recovery losers, making the worst year worse). Do not bake regime/VIX gates into a
strategy as alpha filters.

Targeted, alpha-positive filters are fine and should stay in `strategy()` when the spec
calls for them — e.g. an earnings blackout (tail control: blocked trades carried ~1.8x
volatility) and a short-trend filter (blocks losing momentum shorts, ~-1.54%). The test
for an entry filter: does it block *negative*-expectancy trades, or just *high-variance*
ones? Only the former belongs here; the latter belongs in sizing.

**The fires-frame is PRE-FILTER when the parity target is pre-filter.** Reproducing
`compute_consensus` (which is pre-filter) means `earnings_blackout` / `short_trend` are
**carried as flags (default `False`), NOT applied inside `strategy()`** — applying them here
drops fires the reference keeps and **breaks parity**. The filters then act **downstream**
(sizing / backtest), joining their earnings-date / trailing-return data there.

## Output schema (what stage 4+ expect)

Return a tidy frame (one row per fire) with, at minimum:

| column | dtype | meaning |
|---|---|---|
| `symbol` | `str` (from `ticker`) | instrument |
| `date` | `datetime64[ns]` | entry (signal) date |
| `side` | `str` ∈ {`BULL`, `BEAR`} | directional read |
| `signal_sign` | `int64` ∈ {`-1`, `+1`} | `-1` BULL/short, `+1` BEAR/long |
| `fwd_5` / `fwd_10` / `fwd_21` | `float` | forward returns; **NaN at the tail, never filled** |
| `M1` / `M2` / `M3` / `m3_stall` / `m3_div` | `bool` | stage flags |
| `raw_close` | `float` | as-traded close (the price screen runs on this) |
| `av_matched` | `bool` | AV-bridge match flag |
| `fwd_available_5` / `_10` / `_21` | `bool` | clean-forward availability |

This is the concrete schema the Coder emits (state the forward-return basis used — headline =
total return). Carry `av_matched` / `fwd_available_*` so downstream stages can filter to clean rows.

## Worked reference — skew-consensus

A spec realized correctly. Use it as the shape to imitate, not as values to hardcode.

- **M1 (corner):** `putP <= 25 & callP >= 75 -> BULL/SHORT`; mirror `putP >= 75 & callP <= 25 -> BEAR/LONG`.
- **M2:** `ivP >= 75 & (rrP >= 75 OR rrP <= 25)`.
- **M3:** sigma-stall OR skew-divergence.

  **M3 sigma-stall (verbatim from `compute_consensus`):** `s0, s1, s2, s3 = sigma.shift(3),
  sigma.shift(2), sigma.shift(1), sigma` (s0 oldest = t−3 … s3 = today). The stall is a
  **shift-AND across all 4 bars** — `|sigma| >= sigma_thr` on each of `s0,s1,s2,s3` (signed to
  the side) **and** plateauing `flat = (s3 - s0).abs() < 0.3`. A NaN anywhere in the window fails
  the gate.

  **M3 skew-divergence direction is NOT free — it MUST match the M1 corner side.** Verbatim from
  the verified `compute_consensus` (`d0..d3 = skewDelta.shift(3..0)`; d0 oldest = t−3 … d3 = today):
  - side `BULL` (call-rich corner -> SHORT fade):
    `fading  = d0<0 and d1<0 and d2<0` (skew one-sided negative over the prior 3 bars) **and**
    `turning = d3 > d2 + 0.2` (today turns up) -> **confirms SHORT**
  - side `BEAR` (put-rich corner -> LONG fade):
    `rising  = d0>0 and d1>0 and d2>0` **and**
    `turning = d3 < d2 - 0.2` (today turns down) -> **confirms LONG**

  (Reference: `quant_validator/consensus_signal.py` `compute_consensus` — `m3_div_bull` / `m3_div_bear`.)

  ```
  # INVARIANT: M3 divergence side == M1 corner side. BULL is always a SHORT fade,
  # BEAR always a LONG fade. Never confirm a BULL corner with a BEAR-shaped divergence.
  ```

  > **FLOAT FORM IS LOAD-BEARING.** Write `d3 > d2 + 0.2` (NOT `(d3 - d2) > 0.2`) and a shift-AND
  > across 4 bars (NOT `rolling(4).sum() == 4`). The forms are **algebraically equal but NOT
  > bit-equal** (~332 ULP disagreements vs the reference). Parity here is byte-level.

- **Params:** `freshness 3`, `hi/lo 75/25`, `sigma_thr 1.0`.
- **Sign:** BULL -> `signal_sign -1` (profits if price falls).

## Failure modes seen in production (avoid these)

The ORATS adapter took four debug rounds for exactly these — design against them up front:

- **Token / resource leak** in long pulls — stream and release.
- **Contango overflow** — guard arithmetic that can blow up on extreme term structure.
- **Memory-safe streaming** — a 16M-row, multi-thousand-ticker panel will not fit naively;
  process per-symbol / per-date, don't load-and-multiply.
- **Cache-pollution dedup** — de-duplicate on write; a polluted cache silently corrupts results.
- **Boundary float-noise** — accept it where it can't flip a gate (the +/-0.2 callP case);
  don't chase it with spurious precision.

## Checklist before returning code

1. Output matches the contract (fire rows, `signal_sign` direction correct).
2. `sigma` (pop-var, strictly-prior, 3dp) and `skewDelta` (2dp) exact.
3. Percentiles + `freshness` match the reference method; no off-by-one.
4. Forward returns close-to-close, trading-day offset, `NaN` (not filled) at the tail.
5. Warm-up honored: first fire >= panel start + 756 bdays (`warmup_start_date`); spec's
   `warm_up_buffer` not allowed to shorten it. Live loop uses the trailing 756-bday window.
6. Price screen on `raw_close`; `max_abs_fwd` symmetric.
7. No look-ahead; PIT-safe; survivorship-free universe.
8. Code quarantined; reference module untouched.
9. A known anchor reproduced; if a reference verdict exists, it matches.
10. No regime/VIX alpha gate baked in; tail control left to the Risk Agent.
11. Fires-frame is PRE-FILTER when matching a pre-filter reference: `earnings_blackout` /
    `short_trend` carried as flags (default `False`), applied downstream — never inside `strategy()`.

## Known gaps — codegen checklist

Self-check these BEFORE emitting `strategy()`. Each is a real bug class that has bitten this
pipeline; tick every one or explain why it doesn't apply.

1. [FIXED] **M3 skew-divergence direction was underspecified** — the Coder could confirm a BULL
   (call-rich) corner with a BEAR-shaped divergence (presents as an inverted side-mapping). Now
   explicit and **locked to the M1 corner side** per the INVARIANT above (BULL = SHORT fade,
   BEAR = LONG fade; verbatim from `compute_consensus`).
2. [FIXED] **Float form is load-bearing** — `d3 > d2 + 0.2` (not `(d3-d2) > 0.2`) and a 4-bar
   shift-AND stall (not `rolling(4).sum()==4`); algebraically equal, NOT bit-equal (~332 ULP).
3. [FIXED] **Freshness arithmetic** — `flag.rolling(freshness, min_periods=1).max()` (today +
   `freshness − 1` prior bars); M1∧M2 co-fire = both `recent`-flags True on the SAME bar.
4. [FIXED] **Trust boundary** — `putP/callP/ivP/rrP` trusted as-is; `sigma/skewDelta` re-rounded
   defensively (3dp/2dp); the `ivP` PIT rebuild is the look-ahead guard, not a recompute of the rest.
5. [FIXED] **Concrete fires-frame schema** — dtypes pinned (`symbol` str from `ticker`, `date`
   datetime64[ns], `signal_sign` int64 ∈ {−1,+1}, flags bool, `fwd_*` float NaN-at-tail). See Output schema.
6. [FIXED] **Fires-frame is PRE-FILTER** — `earnings_blackout`/`short_trend` carried as flags
   (default `False`), applied downstream; applying them in `strategy()` breaks pre-filter parity.
7. [FIXED] **Anchor was a BUG** — removed the ORATS `fwd5 @ 2019-06-03 = 0.111192` (wrong panel);
   AV binding anchor = 519,984 fires / AAPL 2015-08-05; all anchors now panel-labelled.
8. [FIXED] **Validation path** — functional equivalence via `parity_gate.assert_fire_parity` +
   `tests/test_parity_gate.py`; the dead `quant_validator.sandbox validate` CLI does not exist.
