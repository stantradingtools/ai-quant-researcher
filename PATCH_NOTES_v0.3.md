# PATCH NOTES — v0.3

Patch built after live testing of v0.2 surfaced 5 spec issues plus a
request to add the Vs. Random robustness test. All changes verified by an
expanded 16-test verification suite (`scripts/verify_install.py`).

## Headline feature — Vs. Random gate (Step 6.5)

New module `quant_validator/vs_random.py` implements the Woodriff /
BuildAlpha "Vs. Random" test as a first-class pipeline gate, inserted
between Step 6 (stats) and Step 7 (critic-validator).

Previously a placebo/shuffle test was run ad hoc by the critic-validator
subagent only when critic-pre happened to flag edge-attribution. It is now
deterministic and always runs.

Three tiers, cheapest first:

- **Tier A — permutation test (ALWAYS RUNS, Mode A + Mode B).** Holds asset
  returns fixed, randomizes position TIMING under the same activity rate
  and magnitude distribution, builds N random-timing strategies, and checks
  whether the real Sharpe beats the 95th percentile. Fully implemented.
  Works from positions.csv + returns.csv (reconstructs asset returns) or
  from an optional explicit results/asset_returns.csv.
    - pass:       actual Sharpe > random p95
    - borderline: actual > random median but < p95  (warning, continues)
    - fail:       actual <= random median  (soft-overridable rejection)

- **Tier B — constraint-matched random rule search (Mode A).** Generates
  random entry/exit rules under the SAME constraint space as the real
  strategy (RuleSpace dataclass encodes skew_consensus's grammar:
  skew/IV-RR/sigma-stall stages, freshnessWindow grid, maxHoldDays,
  direction, trend filter), backtests each, takes the BEST random Sharpe,
  and requires the real strategy to beat it by >=10% (BuildAlpha default).
  Scaffolded — needs a feature matrix + backtest fn; records
  "not_available" in Mode B rather than silently skipping.

- **Tier C — randomized-data test (Mode A).** Block-bootstraps the price
  series to destroy signal structure, re-runs the strategy, confirms it
  does NOT keep its edge on noise. Scaffolded (Phase 2).

Wiring:
- Orchestrator (`validate-thesis.md`): Step 6.5 added with checkpoint
  behavior (pass/borderline/fail) and override instructions on fail.
- Gates (`gates.py`): vs_random added as a 4th gate reading vs_random.json.
- Override (`override-reject.md`): vs_random registered as overridable with
  resume mapping (Step 6.5 fail → resume Step 7).
- Critic-validator (`critic-validator.md`): now READS vs_random.json
  instead of running its own ad-hoc placebo; weights Tier A verdict into
  criterion-8 commentary.

Match-the-fitness rule (BuildAlpha Mistake #3) is honored: comparison is on
Sharpe because the pipeline optimizes around Sharpe.

## Bug fixes from v0.2 live testing

### Fix 1 — `apply_override` no-op before Step 10 (memory.py)
When an override was applied before Step 10 (e.g. overriding critic_pre at
Step 2), no trial row existed yet, so `apply_override` silently did nothing
and the override lived only in decision.json — invisible to
`/memory-overrides`. Now `apply_override` inserts a placeholder row
(deployment_status='override_pending') so the override is tracked
immediately, and `record_trial` UPSERTS onto that row at Step 10, merging
the override_log instead of inserting a duplicate. Verified: total_trials
stays at 1 and the override survives the upsert.

### Fix 2 — misleading adapter stub errors (massive/alpha_vantage/flash_alpha/orats)
Stub adapters checked for the API key first and raised
`RuntimeError("X_API_KEY not set")` — misleading, since the adapter is a
stub and would not work even WITH the key. Now each stub raises
`NotImplementedError` first with an honest message ("Stub — even with
X_API_KEY set, fetch logic is not yet implemented"). When you implement
Phase 1, replace the NotImplementedError with real code and the key check
naturally becomes the gate.

### Fix 3 — `quant_validator.stats` CLI did not exist (stats.py NEW)
The orchestrator referenced `python -m quant_validator.stats compute` but
the module was never written; Claude Code improvised inline during testing.
Now a self-contained module computes Sharpe/Sortino/Calmar/returns/drawdown/
moments (metrics.json), the Deflated Sharpe Ratio (dsr.json), and k-fold
walk-forward stability (walk_forward.json). DSR uses the Bailey & Lopez de
Prado formula and reports BOTH dsr_probability_real (higher better) and
dsr_pvalue (lower better; the field the gate compares).

### Fix 4 — `quant_validator.gates` CLI did not exist (gates.py NEW)
Same story. Now a self-contained module evaluates 4 gates (deflated_sharpe,
correlation, pca_concentration, vs_random) and writes gates_outcome.json.

### Fix 5 — hypothesis-refiner misclassified market_type (hypothesis-refiner.md)
The refiner classified skew_consensus_v21 as "options / cross_sectional"
because the thesis prose contained "skew" — but the strategy trades the
underlying EQUITY using option-derived signals. Added an explicit rule:
market_type is the TRADED INSTRUMENT, not the signal source. Signal names
(skew, IV, vol, gamma) never imply options trading or cross-sectional
ranking. When unsure, ASK rather than guess.

## Threshold note for your attention (not a code change)

The DSR gate threshold remains `dsr_pvalue < 0.95` per the original spec.
This is very permissive — it allows up to a 95% probability that the Sharpe
is luck. During testing, a negative-Sharpe synthetic strategy "passed" DSR
mechanically at p=0.81. Consider tightening to 0.10 or 0.05 for production:
  python -m quant_validator.gates evaluate --thesis_id <id> --dsr_pvalue_max 0.10
The default is left at 0.95 to preserve consistency with your earlier runs;
change it when you're ready.

## Recommended action after installing v0.3

Re-run your accepted strategy through the new gate — its v21 ACCEPT predates
the Vs. Random check:

  python -m quant_validator.vs_random run --thesis_id skew_consensus_v21
  python -m quant_validator.gates evaluate --thesis_id skew_consensus_v21

If Tier A passes (actual Sharpe > random p95), confidence rises. Given the
+6.24 skew on PATCH-21h, watch for a "borderline" — that would indicate a
handful of trades carry the edge and the timing is weaker than the headline
Sharpe suggests. To get Tier A working best, export an asset_returns.csv
(per-bar underlying returns) into results/ alongside positions/returns;
otherwise Tier A reconstructs asset returns from strategy_return/position,
which is noisier on near-zero-position bars.

## Files changed in v0.3

New:
- quant_validator/vs_random.py
- quant_validator/stats.py
- quant_validator/gates.py
- PATCH_NOTES_v0.3.md

Modified:
- quant_validator/memory.py (apply_override + record_trial upsert)
- adapters/massive.py, alpha_vantage.py, flash_alpha.py, orats.py (error order)
- .claude/agents/hypothesis-refiner.md (market_type classification rule)
- .claude/agents/critic-validator.md (reads vs_random.json)
- .claude/commands/validate-thesis.md (Step 6.5 inserted)
- .claude/commands/override-reject.md (vs_random overridable + resume map)
- scripts/verify_install.py (12 → 16 tests)
