---
description: Run the full thesis validation pipeline. Auto-detects Mode A (from-prose, generates code + backtests) or Mode B (from-results, validates existing data). Prints a structured summary after every step. Pauses at decision points. Allows subjective override of soft rejections with logged justification. Writes complete audit trail to theses/<thesis_id>/.
argument-hint: <thesis_id or quoted prose hypothesis>
allowed-tools: Read, Write, Bash, Grep, Glob
---

You are orchestrating a single thesis through the full validation pipeline.

ARGUMENTS: $ARGUMENTS

═══════════════════════════════════════════════════════════════
GENERAL RULES (apply to every step):

1. After EVERY step completes (success OR failure), print a structured
   summary block in the format:

   ═══════════════════════════════════════
   STEP N — <Step Name>  <✓ PASS | ✗ FAIL | ⚠ WARNING>
   ───────────────────────────────────────
   Inputs read:    <files>
   Output written: <files>
   <Per-step specific summary content>
   Recommendation: <continue | stop | ask user>
   To inspect: <bash command for the user>
   ═══════════════════════════════════════

   ALSO write the same block as Markdown to
   theses/<thesis_id>/step_summaries/<NN_step_name>.md so the audit
   trail captures every step in human-readable form.

   ALSO append a JSON record to theses/<thesis_id>/audit_log.jsonl with
   timestamp, step number, outcome, key metrics, and any user input.

2. If the user interrupts to ask about a previous step, retrieve the
   relevant file from theses/<thesis_id>/ and summarize it. Then ask:
   "Continue with Step N?" before resuming.

3. Hard rejections that BLOCK further work (sandbox errors, missing data,
   backtest engine errors) cannot be overridden — explain why and stop.

4. Soft rejections (critic-pre kill, critic-validator fatal, DSR fail,
   correlation fail, PCA fail, risk >= 8) PRINT THE OVERRIDE INSTRUCTIONS
   alongside the rejection. The user may then run:
     /override-reject <thesis_id> <failure_key> --reason "<text>"
   This logs the override in decision.json and resumes the pipeline.

5. EVERY user interaction (question asked, response received, override
   applied) is recorded in theses/<thesis_id>/user_interactions.jsonl
   for the audit trail.

═══════════════════════════════════════════════════════════════
STEP 0 — Locate or create thesis folder

If $ARGUMENTS matches an existing directory under theses/, set
thesis_id and proceed. Otherwise treat $ARGUMENTS as prose:
- Derive snake_case thesis_id from first 6 meaningful words
- Create theses/<thesis_id>/ with subdirectories: data/, code/, results/,
  step_summaries/
- Write the prose to theses/<thesis_id>/thesis.md
- Initialize theses/<thesis_id>/audit_log.jsonl
- Initialize theses/<thesis_id>/user_interactions.jsonl

Detect mode:
- If theses/<thesis_id>/results/positions.csv exists → MODE_B
- Otherwise → MODE_A

Announce: "Starting validation for <thesis_id> in MODE_<A|B>."
Print Step 0 summary block.

═══════════════════════════════════════════════════════════════
STEP 1 — Hypothesis-refiner (always)

Invoke the hypothesis-refiner subagent on theses/<thesis_id>/thesis.md.
It writes refined.json and step_summaries/01_refiner.md.

CHECKPOINT: read refined.json. If "mode" == "ASK_USER", ask the user:
"This thesis could run as single-asset (signal per ticker) or
cross-sectional (rank tickers each bar). Which?"
Update refined.json with the user's answer.
Record the interaction in user_interactions.jsonl.

Print Step 1 summary block including: hypothesis_id, formalized spec,
3 stress-test concerns, data plan adapters, expected Sharpe range.

═══════════════════════════════════════════════════════════════
STEP 2 — Critic-pre (always)

Invoke critic-pre subagent. Reads refined.json, writes critique_pre.json
and step_summaries/02_critic_pre.md.

CHECKPOINT: if critique_pre.json["verdict"] == "kill":
- Print Step 2 summary block with kill reasons
- Print OVERRIDE INSTRUCTIONS:
    "Critic-pre killed the thesis. Reasons: [list].
     To override: /override-reject <thesis_id> critic_pre --reason '...'
     To abort: do nothing. Re-run /validate-thesis after revising."
- Run Step 11 with rejection_reason="critic_pre" then STOP.

If "verdict" == "pass" with warning_flags non-empty, log warnings to
audit_log.jsonl and continue.

═══════════════════════════════════════════════════════════════
STEP 3 — Data fetch (Mode A only)

For each adapter listed in refined.json["data_plan"]["adapters"]:
  Run via Bash: python -m <adapter_module> fetch \
    --thesis_id <thesis_id> --start <start> --end <end>

CHECKPOINT: if any adapter raises *NotSubscribed exception
(e.g. UnusualWhalesNotSubscribed, TardisNotSubscribed), ask:
"Adapter X not subscribed. Skip this data source or abort?"
Default on --no-pause: abort.
Record decision in user_interactions.jsonl.

═══════════════════════════════════════════════════════════════
STEP 4 — Code agent (Mode A only)

Invoke code subagent. Reads refined.json, writes code/signal.py
and step_summaries/04_code.md.

Validate by FUNCTIONAL EQUIVALENCE vs compute_consensus -- NOT a CLI (no sandbox
command exists). Use quant_validator.parity_gate.assert_fire_parity(gen_flags_fn,
compute_consensus, panel) and the standing tests/test_parity_gate.py; the runtime
materialization gate in backtest.run enforces the same. Target: 0 side disagreements
on the parity tickers.

CHECKPOINT: if the parity check fails (side disagreements > 0), ask: "Parity check
rejected the generated code. Options: retry with critic hint, manual edit, or abort?"
Default on --no-pause: abort.

═══════════════════════════════════════════════════════════════
STEP 5 — Backtest (Mode A) or verify (Mode B)

MODE_A: Run via Bash:
  python -m quant_validator.backtest run-split \
    --thesis_id <thesis_id>
Scores TWO windows off the warmed panel (the split is a fire-scoring date filter — the signal
is NOT recomputed, so percentiles stay warm-up-correct):
  - PRIMARY (tradeDate >= 2018-01-01): the CANONICAL verdict -> theses/<thesis_id>/results/.
    Drives the entire downstream gate stack unchanged. Writes results/{vs_random.json,
    returns.csv, positions.csv, net_return_panel.csv}.
  - OOS holdout (<= 2017-12-31, requested start clamped UP to the panel floor): a confirmation
    -> theses/<thesis_id>__oos/results/, folded into theses/<thesis_id>/results/oos_confirmation.json.
The fires-frame adapter is the only engine. Legacy single-window baseline (regression):
  python -m quant_validator.backtest run --thesis_id <thesis_id> --window full

WINDOW CONVENTION (applies to STEPS 5-11, EVERY thesis — not skew-specific): VERDICT window =
PRIMARY (tradeDate >= SPLIT_CUTOFF, the constant in quant_validator/backtest.py, currently
2018-01-01) — the canonical verdict that drives every downstream gate (6 Stats / 6.5 VsRandom /
7 Validator / 8 Gates / 9 Risk) and the living report. SIZING window = full-panel — used for
Kelly/drawdown sizing and the DSR computation ONLY, NEVER the accept/reject verdict. pre-cutoff =
held-out OOS leak-guard — a same-sign CONFIRMATION (oos_holdout), never the gate. A missing/empty
PRIMARY is a hard backtest_error (non-overridable); the full-panel run never substitutes for it.

CHECKPOINT (OOS holdout): read theses/<thesis_id>/results/oos_confirmation.json. If
status=="not_available", treat as N/A. Else if NOT same_sign_as_primary OR NOT oos_significant,
emit a SOFT warning (overridable as oos_holdout) — a CONFIRMATION, not a hard fail; the PRIMARY
verdict still drives the gates. Default on --no-pause: warn and continue.

MODE_B: Confirm these files exist in theses/<thesis_id>/results/:
  positions.csv, returns.csv, equity_curve.csv
  greeks.csv (if market_type == options)
If any missing, abort with clear message identifying the missing file.

═══════════════════════════════════════════════════════════════
STEP 6 — Statistics (always)

Run via Bash:
  python -m quant_validator.stats compute --thesis_id <thesis_id>

Computes: Sharpe, Sortino, Calmar, DSR (with n_trials from memory),
walk-forward CV, regime breakdown, tail metrics, Greek summary if
applicable. Writes results/metrics.json, results/walk_forward.json,
results/dsr.json.

═══════════════════════════════════════════════════════════════
STEP 6.5 — Vs. Random gate (always)

Run via Bash:
  python -m quant_validator.vs_random run --thesis_id <thesis_id>

Writes results/vs_random.json. Tier A (permutation) always runs and is the
hard floor: it randomizes position TIMING under the same activity rate and
magnitude, holding asset returns fixed, and checks whether the strategy's
Sharpe beats the 95th percentile of random-timing Sharpes.

This is the Woodriff/BuildAlpha "Vs. Random" test promoted to a first-class
gate (previously critic-validator ran a placebo ad hoc only when critic-pre
flagged it). Tier B (constraint-matched random rule search) runs in Mode A
when a feature matrix + backtest fn are available; in Mode B it records
"not_available" rather than silently skipping.

Print the Step 6.5 summary block with: actual Sharpe, random p95, percentile,
and Tier A verdict (pass / borderline / fail).

CHECKPOINT: Tier A verdict drives behavior:
- "pass"       → continue
- "borderline" → continue but attach a warning to the decision record
- "fail"       → this is a soft-overridable rejection. Print:
    "Vs. Random Tier A FAILED: actual Sharpe <x> is below the 95th
     percentile of random-timing strategies (<p95>). The entry timing
     shows no demonstrable edge over luck.
     To override: /override-reject <thesis_id> vs_random --reason '...'
     To abort: do nothing."
  Run Step 11 with rejection_reason="vs_random" then STOP.

═══════════════════════════════════════════════════════════════
STEP 7 — Critic-validator (always)

Invoke critic-validator subagent. Reads refined.json, code/, results/.
Writes critique_post.json and step_summaries/07_critic_validator.md.

CHECKPOINT: if critique_post.json["any_fatal"] == true:
- Print Step 7 summary block with 9-criteria table
- Print OVERRIDE INSTRUCTIONS:
    "Critic-validator flagged a fatal in criteria: [list].
     To override: /override-reject <thesis_id> critic_validator \
       --reason '<your justification>'
     To abort: do nothing."
- Run Step 11 with rejection_reason="critic_validator" then STOP.

═══════════════════════════════════════════════════════════════
STEP 8 — Statistical gates (always)

Run via Bash:
  python -m quant_validator.gates evaluate --thesis_id <thesis_id>

Output: theses/<thesis_id>/gates_outcome.json
Checks: DSR (p<0.95), correlation (max<0.6), PCA concentration (<0.5).

CHECKPOINT: if any gate fails:
- Print Step 8 summary block with which gate, computed value, threshold
- Print OVERRIDE INSTRUCTIONS specific to which gate failed:
    "Gate <gate_name> failed: computed <value>, threshold <threshold>.
     What this means: <explanation>.
     To override: /override-reject <thesis_id> gates:<gate_name> \
       --reason '<your justification>'
     To abort: do nothing."
- Run Step 11 with rejection_reason="gates:<gate_name>" then STOP.

═══════════════════════════════════════════════════════════════
STEP 9 — Risk (always)

Invoke risk subagent. Writes risk.json and step_summaries/09_risk.md.

If risk.json["risk_score"] == 7, the subagent itself asks the user
which path (cap/override/reject). Record response in user_interactions.

CHECKPOINT: if risk_score >= 8 (after any user input):
- Print Step 9 summary block with concerns
- Print OVERRIDE INSTRUCTIONS:
    "Risk score: <score>. Concerns: [list].
     To override: /override-reject <thesis_id> risk \
       --reason '<your justification>'
     To abort: do nothing."
- Run Step 11 with rejection_reason="risk" then STOP.

═══════════════════════════════════════════════════════════════
STEP 10 — Memory record (always, on success or override-resumed)

Run via Bash:
  python -m quant_validator.memory record \
    --thesis_id <thesis_id> --accepted true \
    --size_multiplier <from risk.json>

deployment_status defaults to "archived". User explicitly promotes
to "paper" or "live" via /deploy-strategy later.

If any overrides were applied during this run, include override_log
in the recorded trial row.

═══════════════════════════════════════════════════════════════
STEP 11 — Final report (always, success or failure)

Compose theses/<thesis_id>/decision.json:
{
  "thesis_id": "<id>",
  "mode": "A" | "B",
  "decision": "accepted" | "rejected" | "accepted_with_override",
  "stopped_at_step": <int 0-11 or null>,
  "rejection_reason": "<null or reason key>",
  "size_recommendation": <float or null>,
  "summary": "<2-3 sentence human-readable summary>",
  "warnings": ["<warning flags from critic-pre + validator>"],
  "key_metrics": {
    "sharpe_in_sample": ...,
    "sharpe_out_of_sample": ...,
    "dsr_p_value": ...,
    "max_drawdown": ...,
    "max_correlation_with_survivors": ...,
    "excess_kurtosis": ...
  },
  "files_to_review": [
    "refined.json", "critique_pre.json", "critique_post.json",
    "risk.json", "results/metrics.json"
  ],
  "step_summaries": [<list of per-step status>],
  "override_log": [<list of any overrides applied>]
}

Write to theses/<thesis_id>/decision.json.
Write step_summaries/11_final.md with formatted summary.

Print to user:
- Top line: ✓ ACCEPTED or ✗ REJECTED (reason) or 🔶 ACCEPTED_WITH_OVERRIDE
- thesis_id, mode
- Key metrics
- Any warnings
- "Audit trail at: theses/<thesis_id>/ (decision.json, audit_log.jsonl,
   step_summaries/)"
- If accepted: "Next step: /deploy-strategy <id> paper <multiplier>
   when ready."

═══════════════════════════════════════════════════════════════

DEFAULTS for --no-pause flag (confirmed by user):
- Missing API key → abort
- Sandbox failure → abort
- Borderline risk (score 7) → cap size at 0.3, continue
- Successful accept → archive (do not auto-promote to paper)
