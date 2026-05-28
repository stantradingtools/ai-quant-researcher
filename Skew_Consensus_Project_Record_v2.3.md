# Skew-Consensus Validation System
## Project Record & Roadmap — v2.3

*From Hypothesis to Validation to Live Paper Trading — and the Optimisation Layer*

A single-thesis-at-a-time validation pipeline that pushes a trading hypothesis through gated
LLM-critic and deterministic statistical checks, then sizes it, deploys it to enforced paper
trading, and refines it through a disciplined, drawdown-constrained optimisation loop.

| Field | Value |
|---|---|
| Board | AI gents — Thesis to Test to Risk |
| Base | Fork of zostaff/ai-quant-researcher, v0.3 (branch claude/funny-elbakyan-1e8456) |
| Status | Pipeline complete end-to-end; v22_novix validated, sized, and deployed to enforced Alpaca paper trading on a live ORATS feed. Optimisation layer (Mutation #11 + Exit #12) BUILT and run end-to-end. Verdict split: PRIMARY (≥2018) is canonical (+19.15 bps, z 7.53), the pre-2018 OOS confirms (+15.27 bps, z 4.58). Mutation (PRIMARY-search) finds NO validated improvement — the v22+6b-exits baseline now CLEARS the MT penalty on the recent regime (prob-real 0.9885 ≥ 0.95, baseline Sharpe 1.437), no variant promoted (local optimum); the pre-2018 leak-guard confirms. Paper track-record accrual underway. |
| Prepared | May 2026 (v2.3 supersedes the v2.2 May baseline) |

---

## 1. Executive Summary (v2.3)

Since the v1 baseline, the system has gone the full distance: the validation pipeline now runs
clean end-to-end, the strategy is validated and sized, it is deployed to paper trading with entry
and exit conditions enforced on a real (paper) brokerage via API fed by a live daily options-data
signal, and the disciplined optimisation layer is now built and run.

- **Pipeline complete (1→10):** any prose thesis now flows start-to-finish (Mode A works); the
  skew strategy is the regression case, reproducing its verdict bit-for-bit through the proper
  backtest adapter.
- **Validated (verdict split):** the consensus signal beats a date- and direction-matched random
  pool. Canonical PRIMARY window (≥2018): 21-day increment +19.15 bps, gross +114.3, z 7.53
  (341,506 fires). Pre-2018 OOS holdout CONFIRMS (+15.27 bps, z 4.58, 113,292 fires, same-sign).
  All-data reference (full window, the sizing basis): +18.15 bps, gross +106.96, z 8.873
  (454,798 fires); Deflated Sharpe (multiple-testing-aware) p 0.006, prob-real 0.994 (n_trials=3).
  Confounds (look-ahead, IV-tilt) ruled out; survivorship benign.
- **Sized:** fractional portfolio Kelly (lambda 0.25) + drawdown kill-switch → Sharpe 1.12,
  CAGR +5.82%, max drawdown -9.86%, every year positive; the Jan-2021 meme-squeeze book-month
  tamed from -9.88%/trade to -0.88% sized. Risk Agent: deploy 0.5x.
- **Deployed to paper (Phase 4):** entry submission and a scheduled exit + kill-switch runner are
  enforced on Alpaca via API, with idempotency, caps, a staleness guard, and a persistent ledger.
  A live full-universe daily ORATS feed generates current signals (signal parity bit-identical to
  the historical panel). The 6b-validated exits (hard_stop_8 + boll_reversion_band + 21D) are
  enforced in the manage loop, parity-locked to the backtester.
- **Optimised (Phase 6, NEW):** Mutation (#11) + Exit (#12) built and run. The constrained search
  found no validated improvement over v22 + the 6b OOS-validated exits — the guardrails (DD bound +
  whole-search DSR penalty) correctly refused to promote an overfit; the strategy is at a local
  optimum. The search now runs on PRIMARY (≥2018) with the pre-2018 OOS held out as the leak-guard —
  still no variant promoted, and on the recent regime the baseline itself clears the MT penalty
  (prob-real 0.9885); the leak-guard confirms. Exit Agent surfaces hard_stop_8 + boll_reversion +
  21D backstop (sign-aware, advisory by default).
- **Honest read:** the edge is real and robust but economically modest — a ~1.1 Sharpe with one
  known regime weakness (a sharp V-recovery runs over the fade, as in 2020). That is exactly why
  the optimisation layer is constrained, not a free-running curve-fitter.
- **Next:** the build work is done — the agent workflow is now internally consistent and
  fire-parity-gated (skill, both agent docs, the validate-thesis command, the sandbox allowlist
  and the runtime gate all describe the same real paths). Next is paper track-record accrual: run
  the daily loop forward before any leverage increase or the live transition.

## 2. The Agent Roster & Pipeline

Twelve agents now make up the system: ten in the linear validation pipeline (built and proven),
plus the two-agent optimisation layer (built and run this phase). Deterministic modules handle the
maths; LLM agents handle judgement.

| # | Agent / Stage | Role | State |
|---|---|---|---|
| 1 | Hypo-Refiner | Prose thesis → JSON spec (refines, does not invent) | Done |
| 2 | Pre-Critic | Adversarial pre-backtest kill/pass, per-market templates | Done |
| 3 | Coder | Spec → runnable strategy() emitting a fires frame | Done |
| 4 | Backtest | Runs the strategy via the fires-frame adapter → verdict | Done |
| 5 | Stats | Computes performance + deflated-Sharpe inputs | Done |
| 6 | VsRandom | Selection vs a date/direction-matched random pool | Done |
| 7 | Validator | 9-criteria post-backtest review + placebo test | Done |
| 8 | Gatekeeper | DSR / correlation / PCA / vs-random gates | Done |
| 9 | Risk | Risk score + size cap (Kelly + kill-switch) | Done |
| 10 | Memory & Reporter | Logs every accept/reject; emits the living report | Done |
| 11 | Mutation | Drawdown-constrained alpha optimiser (closed loop) | Done |
| 12 | Exit | Technical-analysis exit finder (design + run time) | Done |

## 3. The Optimisation Layer (NEW) — Mutation Agent #11 + Exit Agent #12

The pipeline so far answers one question: is this strategy real and how big can it be? The
optimisation layer answers the next: can we make it better — sharper alpha at the same or lower
drawdown — without fooling ourselves? The two agents were built together and interact continuously.

### 3.1 Mutation Agent (#11) — a drawdown-constrained alpha optimiser

**What it is.** A closed-loop optimiser over the strategy's tunable surface — entry signals, exit
rules, and filters — that proposes a variant, re-runs it through the backtest, has the Risk Agent
score it, takes the feedback, and proposes the next variant. It iterates toward better
risk-adjusted metrics.

**Objective (constrained, not free-running).** Maximise alpha (selection increment / Sharpe)
SUBJECT TO a maximum-drawdown bound (e.g. keep maxDD at or below the current level, or a chosen
target). The drawdown constraint is a hard governor — a variant that lifts Sharpe but breaches the
DD bound is rejected. This is the formal version of the goal: sharpen alpha while maintaining a
certain drawdown risk.

**What it is allowed to mutate:**

- **Entry:** the consensus thresholds (the 75/25 percentiles, freshness, sigma threshold), and the
  M1/M2/M3 combination logic.
- **Filters:** the earnings blackout, short-trend, and sector-cap filters — tightening, loosening,
  or adding screens (e.g. liquidity tiers).
- **Exit:** the exit-rule set, supplied and co-validated by the Exit Agent (Bollinger, vol-spike,
  squeeze, trailing, profit-target, time-stop).
- **Expression / universe:** equity vs ~30-delta options; full universe vs a tradeable/liquid subset.

**The guardrails are the point.** A loop that "keeps mutating until the metrics improve" is,
unconstrained, an overfitting machine — it will always find a better-looking number on the training
data. This design's primary job is to search hard while refusing to fool itself:

- **Multiple-testing / DSR penalty on the WHOLE search.** Every variant evaluated inflates
  false-discovery risk; the Gatekeeper deflates significance for the total count of mutations tried
  across the entire loop. Without this, the optimiser is worthless.
- **Holdout discipline — concrete (PRIMARY search + pre-2018 leak-guard).** The search runs ONLY on
  PRIMARY fires (tradeDate ≥ 2018-01-01); the pre-2018 OOS (warm-up floor → 2017-12-31) is held out —
  never read during the search, touched once per promotion candidate as the leak-guard. A
  PRIMARY-only improvement that fails the pre-2018 OOS is rejected as regime-overfit, not promoted.
- **Thesis-locked.** It tunes expression, filters, exits, and parameters — never the core economic
  hypothesis (the skew/RR/IV fade).
- **Full re-validation.** Each promoted variant clears the entire gate stack again — no acceptance
  on a Sharpe/DD improvement alone.
- **Bounded search.** A budget on iterations caps the multiple-testing burden and forces
  convergence; the loop stops on no-validated-improvement or budget exhaustion.

**Result (PRIMARY-search run).** 18 candidates (11 exit / 2 universe / 4 entry); cumulative MT
trials 30; best variant = baseline (no validated lift), recorded to the living report's Mutations
section. On the PRIMARY (≥2018) regime the baseline is stronger (Sharpe 1.036 → **1.437**) and now
**clears** the MT penalty (deflated prob-real **0.9885 ≥ 0.95**) — a NEW, stronger recent-regime
measurement, NOT the old 0.874 metric "improving". No variant beat it on a majority of folds, so
none was promoted (local optimum). The **pre-2018 leak-guard CONFIRMS** (+21.5 bps, same-sign, on a
sampled 7,722-fire walk-forward holdout — distinct from the full-universe pre-2018 OOS verdict
of +15.27 bps).

### 3.2 Exit Agent (#12) — a technical-analysis exit finder

**What it is.** The specialist for the exit dimension. Previously the only exit was mechanical — a
fixed 21-trading-day hold plus the portfolio kill-switch. The Exit Agent replaces "always ride to
day 21" with finding the best exit: the point that captures the most profit before the fade reverts
and pulls back.

**Two roles, two interactions:**

- **Design time (with the Mutation Agent):** it proposes candidate exit RULES that the Mutation
  Agent optimises over and co-validates — so the exit search is fed by real technical-analysis
  logic, not guesses.
- **Run time (with the Phase-4 manage loop):** for each open position it ranks 2-3 best exit
  options, surfaced to you (advisory by default) or acted on when enabled.

**Exit signals (grounded in the Options_Sell_Signal tool):**

- **Bollinger mean-reversion target:** the fade's profit target — exit as price reverts to the
  mean / opposite band, aiming for the reversion peak before the pullback. (Entry-context-aware and
  sign-locked: a short above its mean reverts down = profit; mirror for a long.)
- **Volatility-spike exit:** 5-day realised vol (Yang-Zhang YZ5) or ATM implied vol spiking, plus
  VRP (IV − RV) and its percentile — a regime turn against the position.
- **Squeeze / skew flag:** negative skew (call-IV rich) = squeeze signature; for a short, an
  emerging squeeze means exit early. This directly targets the 2021-01 meme-squeeze worst month,
  where the fade shorts were run over.
- **Backstop + tail control:** the 21-day hard exit (non-negotiable) plus hard-stop / trailing.

**OOS-validated set.** Walk-forward selection against the 21-day backstop kept exactly two rules:
**hard_stop_8** (Sharpe 0.88 → 1.08, maxDD −24% → −10%, +516 bps on 2021-01) and
**boll_reversion_band** (keeps ~97% of the edge, cuts drawdown on all folds). Tail rules
(squeeze / vol-spike) cut the worst months even where they trim mean return. Deployed canonical
policy = **hard_stop_8 + boll_reversion_band + 21D backstop** (sign-aware; protective rules enforced
in the manage loop, profit-target advisory by default).

**"Most profit before pullback."** The fade reverts, peaks, then gives profit back. The Exit
Agent's edge is detecting that exhaustion — a band touch, a vol spike, a squeeze flag — and locking
the gain rather than surrendering it on the slow ride to day 21. Same anti-overfitting discipline as
the Mutation Agent: band/vol parameters are validated out-of-sample before any live use; the 21-day
backstop is never removed.

## 4. How the Optimisation Layer Incorporates into the Workflow

The existing pipeline is linear (stages 1→10). The optimisation layer wraps it in a closed loop at
design time, and enriches the manage loop at run time.

**Design-time optimisation loop**

- A validated, sized strategy enters the loop (it must already have passed 1→10).
- Mutation Agent proposes a candidate (entry / filter / exit / expression mutation); the Exit Agent
  supplies the exit-rule candidates.
- Re-enter the pipeline at stage 3→4 (Coder → Backtest) → Stats → VsRandom → Gatekeeper, with the
  DSR penalty counting every mutation tried.
- Risk Agent scores the candidate on Sharpe and drawdown against the DD constraint, and feeds the
  result back to the Mutation Agent.
- Leak-guard check: the search runs on PRIMARY (≥2018) only; the held-out pre-2018 OOS confirms each promotion candidate (same-sign + positive) or it is rejected as regime-overfit;
  the rest are logged and rejected.
- Loop until no validated improvement or the iteration budget is spent; the best validated variant
  is recorded by the Memory & Reporter and written into the living report's "Mutations" section.

**Run-time exit enrichment**

In Phase 4, the Exit Agent augments the manage loop: instead of only the mechanical 21-day close, it
ranks the best exit per open position (Bollinger / vol-spike / squeeze) within the 21-day backstop,
writing an "Exit options" block per position into the report. The drawdown kill-switch and the
21-day backstop remain the non-negotiable governors throughout.

**The two governors on the whole loop:** (1) the maximum-drawdown constraint, and (2) the
multiple-testing / DSR penalty. Together they let the system search aggressively for better metrics
while refusing to deploy an overfit or over-levered variant.

## 5. Strategy Status — skew_consensus_v22_novix

**Signal.** Reads the ORATS options surface to fade extremes, trading the underlying equity (options
for direction only). Fires on the M1/M2/M3 consensus: M1 corner (putP≤25 & callP≥75 → BULL/short;
mirror → BEAR/long), M2 (ivP≥75 & risk-reversal percentile extreme), M3 (sigma-stall OR
skew-divergence); freshness 3, thresholds 75/25. A BULL signal carries sign −1: it profits if price
falls (a fade).

**Filters.** Earnings blackout kept (tail control), short-trend filter kept, sector cap kept, VIX
filter removed (it was destroying alpha).

| Window / metric | Value |
|---|---|
| **Canonical verdict — PRIMARY (≥2018)** | 21-day increment **+19.15 bps**, gross +114.3, **z 7.53**, 341,506 fires |
| **OOS holdout — pre-2018 (held out)** | +15.27 bps, gross +84.85, z 4.58, 113,292 fires, realized 2013-11-26 — **CONFIRMS** (same-sign) |
| All-data reference (full window, sizing basis) | +18.15 bps, gross +106.96, z 8.873, 454,798 fires; DSR p 0.006, prob-real 0.994 (n_trials=3) |
| Sized book (full-panel) | Sharpe 1.12, CAGR +5.82%, max drawdown -9.86%, every year positive |
| Worst month (sized) | 2020-02 -7.20% |
| 2021-01 meme squeeze | -9.88%/trade raw → -0.88% sized |
| Deploy size | 0.5x (Risk Agent 4/10) |
| Windows (deliberate split) | **verdict = PRIMARY (≥2018); sizing = full-panel** (verdict-only split) |

**Recent-regime nuance (honest).** The raw selection edge is modestly weaker on PRIMARY (z ~7.53)
than over all data (z ~8.87), yet the deployed book is *sharper* on the recent regime (baseline
Sharpe ~1.437) — the OOS-validated exits (hard_stop_8 + boll_reversion_band) compress the
2020/2021 hard-tail months they were built for, which dominate the recent window.

**Live book (current).** Queue as of 2026-05-22 → fills at the 2026-05-26 open: 194 OPG orders
(110 long / 84 short; 21 intended shorts dropped non-shortable); gross 0.4337 / net 0.0898 at 0.5x.
Held in DRY-RUN behind the first-day review gate — nothing fires without an explicit, graduated
`--submit`.

## 6. Build Phases — Updated Status

| Phase | Scope | Detail | Status |
|---|---|---|---|
| 0 | Foundation | Agents, orchestrator, memory/audit/stats/gates/vsrandom | Done |
| 1 | Data layer | ORATS + Alpha Vantage + verdict; clean survivorship-free panel; Mode A end-to-end | Done |
| 2 | Crypto + features | Deribit done; features_custom (skew/vol/exposure/pe_quadrant) | Partial |
| 2.5 | Vibe-Trading MCP | Trade-journal / factor tooling | Optional |
| 3 | Unusual Whales | Flow / dark-pool adapter | Deferred (needs sub) |
| 4 | Paper trading | Enforced entry+exit on Alpaca via API; live daily ORATS feed; living report | Done |
| 5 | Broker / live | paper=False + live keys, gated on a paper track record | Gated on track record |
| 6 | Optimisation layer | Mutation Agent (#11) + Exit Agent (#12) — built together + run end-to-end | Done |

## 7. Next Steps

- **Phase 6 complete.** Next: accumulate the paper OOS track record before any leverage/live step.
- **Accumulate a paper track record:** run the daily loop forward; let the live OOS results build
  before any leverage increase or live transition.
- **Then Phase 5 (live):** same code, paper=False + live keys, gated on the paper track record.
- **Housekeeping — strategy-authoring 8-gap patch COMPLETE + agent-workflow consistency hardened:**
  - **Gap 1** (M3 divergence side-mapping): FIXED (cb7ca3e). Root cause was an omission — the
    divergence direction was unspecified; now explicit and locked to the M1 corner side.
  - **Gaps 2-8:** CLOSED (2c53e3a). One real bug (gap 7: a wrong-panel forward-return anchor,
    0.111192 from the ORATS build, replaced with the AV binding anchor 519,984 fires /
    AAPL 2015-08-05; anchors now panel-labelled) + six omissions baked in (exact float / shift-AND
    forms for byte parity, freshness rolling-max + co-fire, read-vs-recompute trust boundary,
    concrete fires-frame schema/dtypes, pre-filter carry-not-apply, validation recipe).
  - **Standing fire-parity gate** (b3705ac): the Coder→Backtest handoff enforces MATERIALIZATION
    parity (scored side == compute_consensus) at runtime, with gen-vs-reference held as a standing
    test; an injected BULL↔BEAR swap raises ParityError.
  - **Agent-doc consistency:** dead `sandbox validate` CLI repointed to the real parity path in
    code.md + validate-thesis.md (c761ec8); validate-thesis STEP 5 stale two-engine cross-check
    replaced with the single real `fires` invocation (e33a1b0); code.md import allowlist narrowed
    to the real sandbox _ALLOWED_IMPORTS, features_custom.* deferred to Phase 2 gated on an
    import-safety audit (0ab8fcd).
  - **Net:** skill, both agent docs, the validate-thesis command, the sandbox allowlist, and the
    runtime gate all describe the same real paths — no dead ends, no contradictory anchors, no
    silent conventions.

## 8. Glossary — v2 Additions

| Term | Meaning |
|---|---|
| Optimisation loop | The closed design-time cycle: Mutation proposes → Backtest → Risk scores → feedback → mutate, under DD + multiple-testing governors. |
| DD-constrained | Maximise alpha subject to a maximum-drawdown bound; variants breaching the bound are rejected. |
| Multiple-testing / DSR penalty | Deflating significance for the total number of variants tried across the whole search — the core anti-overfitting guard. |
| Holdout discipline | Search on PRIMARY (≥2018); the pre-2018 OOS is held out as the leak-guard — never read during search, confirmed once per promotion candidate. |
| Exit rule | A validated, technical-analysis exit condition (Bollinger / vol-spike / squeeze / trailing / profit-target) layered on the 21-day backstop. |
| Staleness guard | Refuses to submit live orders on a signal older than the freshness bound — protects against trading stale signals. |

---

*Confidential working document — Skew-Consensus Validation System, Project Record v2.3, May 2026.*
