# Mode A readiness — pre-flight prediction

**Date:** 2026-05-25 · **Branch:** claude/funny-elbakyan-1e8456
**Goal:** predict where the *never-run* Mode A chain breaks, BEFORE running it, so we
can compare prediction vs reality. Regression case: skew-consensus (known-good Mode B
verdict: 21d increment **+18.3 bps**, gross **+106.5**, z **9.12**).

This is written from reading the contracts only (Stage 0 — chain not yet run):
`ai_quant_lab/orchestrator/loop.py`, `orchestrator/sandbox.py`,
`.claude/agents/code.md`, `.claude/commands/validate-thesis.md`, and the
`quant_validator` / `ai_quant_lab` module inventory.

## The two pipelines (root cause of most gaps)

There are **two** orchestrators that don't share a data model:

1. **`ai_quant_lab` loop** (`run_research_loop`): `price_data: pd.Series` →
   `run_strategy(source, price_data)` → **positions** → `vectorized_backtest(positions,
   returns)` → `evaluate_gates(strategy_returns,…)`. A **single/multi-asset
   positions×returns** engine. Agents are LLM-backed Python classes.
2. **`/validate-thesis` command** (the Mode A/B pipeline this task targets): drives the
   **Claude Code subagents** (hypothesis-refiner → critic-pre → code → critic-validator →
   risk → memory) and shells out to `python -m quant_validator.{sandbox,backtest,stats,
   vs_random,gates,memory}`.

Skew-consensus is **neither** a single price tape nor a positions×returns strategy — it
is a **cross-sectional options panel** (`data/av/signal_panel_clean.parquet`, ~16M rows,
8,519 tickers, features putP/callP/ivP/rrP/sigma/skewDelta, with **pre-computed forward
returns**) validated by a **date/direction-matched random pool** (`signal_vs_random`).
The mismatch between this and the two generic engines is the source of the predicted
breaks.

## Predicted gaps (ranked by how early/hard they break)

| # | Stage | Predicted break | Confidence |
|---|---|---|---|
| G1 | 4–5 | `quant_validator.sandbox` and `quant_validator.backtest` **do not exist** (verified: `find_spec` → MISSING). `/validate-thesis` Step 4 (`python -m quant_validator.sandbox validate`) and Step 5 (`python -m quant_validator.backtest run`) will `ModuleNotFoundError`. The sandbox/engine live in `ai_quant_lab.orchestrator.sandbox` / `ai_quant_lab.backtest.engine` with **no `quant_validator` CLI wrapper**. | **Certain** |
| G2 | 3→4 | **Two conflicting `strategy()` contracts.** `code.md` (Coder base): `strategy(features)->pd.Series` positions in **[-1,1]** matching the price index (for the sandbox/vectorized engine). `strategy-authoring/SKILL.md`: return a **fires frame** (symbol,date,side,signal_sign,fwd_*). The Coder is told to produce *both shapes*. Skew-consensus needs the **fires frame**; the generic sandbox/`vectorized_backtest` needs **positions**. | **Certain** |
| G3 | 4 | **Data-model mismatch.** `vectorized_backtest(positions, returns)` has nowhere to put the option-surface features or the pre-computed `av_fwd_*`; it recomputes `returns = price_data.pct_change()` from a single tape. The skew panel can't be fed as a `pd.Series`, and as a DataFrame the engine would treat columns as *assets*, not *features*. | **Certain** |
| G4 | 6.5 | **VsRandom is a different test.** `/validate-thesis` Step 6.5 (`quant_validator.vs_random` Tier A) permutes **position timing on one returns series**. Skew-consensus VsRandom (`signal_vs_random.run_test` / `vs_random_consensus`) draws a **date/direction-matched random ticker pool** on the panel. Not interchangeable; the generic Tier A can't even be constructed without a positions/returns series. | High |
| G5 | 3 (data) | **Per-thesis data fetch doesn't build the panel.** Step 3 runs `python -m adapters.alpha_vantage fetch --thesis_id …` (the stub bars/options path). The consensus signal needs the **global** prebuilt `signal_panel_clean.parquet` + `data/orats/universe_signal.parquet`, not a per-thesis fetch. | High |
| G6 | — | **`bridge/` is empty** (README only). The intended Mode-A↔panel adapter layer doesn't exist; the de-facto Mode-A backtest adapter for consensus is `quant_validator.signal_vs_random` / `vs_random_consensus`, reached directly, not through the pipeline. | High |
| G7 | 1b | **SKILL.md vs code.md drift.** Now that `strategy-authoring/SKILL.md` exists it *contradicts* `code.md` on the output contract (G2). Whichever the Coder follows, the other half of the pipeline rejects it. This is the first skill-patch candidate. | High |
| G8 | 1,2,7,9 | **Subagent path is fine; LLM-key path is not.** The Claude Code subagents (hypothesis-refiner/code/critic-*/risk) run as the session model — **no API key needed**, so Stages 1/2/3/7/9 should *invoke*. (The `ai_quant_lab` Python agents would need `ANTHROPIC_API_KEY`, which is empty — but we're not using that path.) | Medium |

## What I predict WILL work
- Stage 1 (hypothesis-refiner), Stage 2 (critic-pre), Stage 3 (code) — as **subagents** they run; the question is *contract fidelity* (G2/G7), not whether they execute.
- Parity B is **reachable** by ignoring the broken generic Stage-4/5 and running the
  generated `strategy()` through the consensus harness directly. Expectation: if the
  Coder follows SKILL.md faithfully, fire-level fidelity ≈ 98.9% and the 21d verdict
  lands on +18.3 / +106.5 / z 9.12.

## Predicted end-state
**PARTIAL.** A prose thesis should flow cleanly through 1→3 (refiner→critic→Coder) and,
*bypassing the generic engine*, reproduce the Mode-B verdict (Parity B). But it will
**NOT** flow through the pipeline *as wired*: Steps 4–5 die on missing
`quant_validator.{sandbox,backtest}` (G1) and the positions-vs-fires contract clash
(G2/G3). The headline integration work is a **Mode-A panel backtest adapter** + a single
canonical `strategy()` contract.

*(Reality check appended in `reports/stage1_mode_a_run.md` after the run.)*
