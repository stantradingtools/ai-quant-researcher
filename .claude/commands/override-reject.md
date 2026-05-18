---
description: Subjectively override a soft rejection from the validation pipeline. Requires an explicit justification text. Logs the override permanently in decision.json and memory.db. Resumes the pipeline from the failed step + 1.
argument-hint: <thesis_id> <failure_key> --reason "<justification>"
allowed-tools: Read, Write, Bash
---

You are processing a subjective override request for a thesis rejection.

ARGUMENTS: $ARGUMENTS

Parse $ARGUMENTS into three components:
1. thesis_id (first token)
2. failure_key (second token — one of: critic_pre, critic_validator,
   gates:deflated_sharpe, gates:correlation, gates:pca, risk)
3. --reason "<text>" (everything after --reason flag)

═══════════════════════════════════════════════════════════════
STEP 1 — Validate the override request

- Confirm theses/<thesis_id>/ exists
- Confirm theses/<thesis_id>/decision.json exists and has
  decision == "rejected" (cannot override an already-accepted thesis)
- Confirm rejection_reason matches the failure_key (cannot override
  a different failure than the one that occurred)
- Confirm --reason text is non-empty and at least 20 characters
  (forces real justification, not "yolo")

If any validation fails, print the specific error and stop.
Do NOT prompt the user to fix; they must re-run the command correctly.

═══════════════════════════════════════════════════════════════
STEP 2 — Check that the failure is overridable

OVERRIDABLE failures (allow override to proceed):
- critic_pre
- critic_validator
- gates:deflated_sharpe
- gates:correlation
- gates:pca
- risk

NON-OVERRIDABLE failures (refuse and explain):
- sandbox_error (broken code)
- backtest_error (computation failure)
- missing_data (information absent)
- missing_results_file (Mode B prerequisite missing)

If failure_key is non-overridable:
- Print: "This failure cannot be overridden. <Reason>.
  Fix the underlying issue and re-run /validate-thesis."
- Stop.

═══════════════════════════════════════════════════════════════
STEP 3 — Apply the override

A. Read theses/<thesis_id>/decision.json.

B. Append to the override_log array:
   {
     "step": <original stopped_at_step>,
     "failure": "<failure_key>",
     "computed_value": <from the relevant *.json output>,
     "threshold": <from the relevant *.json output>,
     "reason": "<user justification text>",
     "timestamp": "<ISO 8601 UTC>"
   }

C. Update decision.json fields:
   - decision: "accepted_with_override"
   - rejection_reason: null
   - stopped_at_step: null

D. Append override event to theses/<thesis_id>/audit_log.jsonl with
   event_type: "override_applied".

E. Update the trial row in memory.db (via Bash):
   python -m quant_validator.memory apply_override \
     --thesis_id <thesis_id> \
     --failure <failure_key> \
     --reason "<text>"

═══════════════════════════════════════════════════════════════
STEP 4 — Resume the pipeline from the step AFTER the failure

Determine the resume_step:
- critic_pre failure (step 2)             → resume step 3
- critic_validator failure (step 7)       → resume step 8
- gates:* failure (step 8)                → resume step 9
- risk failure (step 9)                   → resume step 10

Then run the remaining pipeline steps in order. Each step still has
its own checkpoints — if another step later fails, ANOTHER override
may be needed (each is independent).

The pipeline can in principle accumulate multiple overrides for one
thesis. Each is logged separately. Three or more overrides on a single
thesis should make the user pause: print a one-line warning if the
override_log has length >= 3 before resuming.

═══════════════════════════════════════════════════════════════
STEP 5 — Print the override summary

Format:
  ═══════════════════════════════════════
  OVERRIDE APPLIED
  ───────────────────────────────────────
  Thesis:        <thesis_id>
  Failure:       <failure_key>
  Computed:      <value> (threshold: <threshold>)
  Reason:        <user justification text>
  Logged at:     <timestamp>
  
  Resuming pipeline from Step <N>...
  ═══════════════════════════════════════

Then continue execution of the remaining steps.
