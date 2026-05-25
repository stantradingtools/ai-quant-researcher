# Stage 1 — first Mode A end-to-end run (skew-consensus regression)

**Date:** 2026-05-25 · **Branch:** claude/funny-elbakyan-1e8456
**Thesis:** `skew_consensus_modeA` (economic-only prose; spec made to EMERGE)
**Focus:** exercise the Coder Agent (Stage 3). Philosophy: **surface & LOG gaps, do not fix blind.**
**Reference (Mode B):** 21d increment **+18.3 bps**, gross **+106.5**, z **9.12** (verdict_rerun pass 1).

## Verdict: **PARTIAL** — exactly as predicted

A prose thesis **flows cleanly through Stages 1→3** (refiner → critic → Coder) and the
generated strategy **reproduces the Mode-B verdict to the decimal** — *but only by
bypassing the generic engine*. It does **NOT** flow through the pipeline as-wired:
Stage 5 (`quant_validator.backtest`) does not exist, which starves Stages 6/6.5. The
real consensus verdict is produced by the separate `signal_vs_random` harness, not by
the `/validate-thesis` pipeline.

## Predicted vs actual (vs `mode_a_readiness.md`)

| Gap | Predicted | Actual | Verdict |
|---|---|---|---|
| G1 `quant_validator.backtest` missing | Certain | `No module named quant_validator.backtest` | ✅ confirmed |
| G1b `quant_validator.sandbox` missing | Certain | Coder confirmed it's absent (its self-check used functional-equivalence instead) | ✅ confirmed |
| G2 two `strategy()` contracts (positions vs fires) | Certain | Coder followed SKILL.md fires-frame; generic sandbox/engine would reject it | ✅ confirmed |
| G3 data-model mismatch (Series×returns vs panel) | Certain | Stages 6/6.5 demand `returns.csv`/`positions.csv`; consensus produces a fires frame | ✅ confirmed |
| G4 VsRandom is a different test | High | `vs_random` errors (needs positions/returns); consensus pool test lives in `signal_vs_random` | ✅ confirmed |
| G5 per-thesis fetch ≠ the panel | High | not reached (used the prebuilt panel directly) | ~ (bypassed) |
| G6 `bridge/` empty | High | still empty; de-facto adapter = `signal_vs_random` reached directly | ✅ confirmed |
| G8 subagents need no API key | Medium | refiner/critic/Coder all ran as subagents, no key | ✅ confirmed |
| **NEW** double-screen | not predicted | feeding the strategy's pre-screened fires into `run_test` (which re-screens) drops 70 fires (475,430→475,360), nudging z 9.12→8.98 | ⚠ new gap |
| **NEW** Stage 8 gates run on empty inputs | not predicted | `gates evaluate` runs and returns `first_failure: deflated_sharpe` on degenerate inputs (no real returns) — fails silently-ish | ⚠ new gap |

## PARITY A — spec emergence (Stage 1, refiner)

The refiner re-derived the consensus spec from **economic-only** prose. Diff vs known spec:

| Element | Known | Emerged | Match |
|---|---|---|---|
| M1 skew corner | putP≤25 & callP≥75 → SHORT; mirror → LONG | identical (callP≥75 & putP≤25 → SHORT; putP≥75 & callP≤25 → LONG) | ✅ |
| M2 IV×RR | ivP≥75 & (rrP≥75 \| ≤25) | ivP≥75 & rrP≥75 → SHORT; ≤25 → LONG | ✅ |
| M3 | sigma-stall OR skew-divergence | same (\|Δ3\|<0.3 plateau; div>0.2) | ✅ |
| freshness / hi-lo / sigma_thr | 3 / 75-25 / 1.0 | 3 / 75-25 / 1.0 | ✅ |
| filters | earnings KEEP, short-trend KEEP, VIX REMOVED | same | ✅ |

**Divergences logged (refiner-chosen defaults, prose was silent):** short-trend threshold
`+15%/21d` (exact value not in known spec); warm-up buffer `504` vs `252`; maxHold `10`
(matches). **Parity A = MATCH** on all load-bearing logic.

## PARITY B — code/verdict fidelity (Stages 3→4)

Generated `generated/strategy_skew_modeA.py` vs reference `compute_consensus`
(independently re-run, not the agent's self-report):

- **Fire-level fidelity: 100.0000%** side agreement on all 477,331 co-fired (ticker, date)
  pairs — **0 disagreements, 0 only-generated**. The 42,653 reference fires the strategy
  doesn't emit are exactly the `$1` / `av_matched` / `fwd_available` screen removals
  (correct, not a logic gap). Target was 98.9% fire / 100% side-stall-div → **exceeded.**
- **21d verdict on generated fires:** increment **+18.3 bps** (ref +18.3, Δ 0.0), gross
  **+106.6** (ref +106.5, Δ +0.1 rounding), z **8.98** (ref 9.12), beat 51.6%, n 475,360.
  The z gap is the *new double-screen gap* above (70-fire pool difference); the
  deterministic increment/gross match exactly. On the raw (pre-screen) consensus — proven
  byte-identical — the verdict is exact.

**Parity B = PASS.** The Coder reproduced a known reference from an emergent spec.

## Stage-by-stage status

| # | Stage | Path used | Status |
|---|---|---|---|
| 1 | Hypo-Refiner | hypothesis-refiner subagent | ✅ refined.json (Parity A match) |
| 2 | Pre-Critic | critic-pre subagent | ✅ PASS + 5 warnings |
| 3 | Coder | code subagent + SKILL.md | ✅ quarantined strategy, 100% parity |
| 4 | Backtest | `signal_vs_random` (de-facto adapter) | ✅ via bypass; ❌ generic `quant_validator.backtest` missing |
| 5 | (pipeline backtest) | `quant_validator.backtest run` | ❌ No module — hard break |
| 6 | Stats | `quant_validator.stats compute` | ❌ needs `results/returns.csv` (Stage-5 output) |
| 6.5 | VsRandom | `quant_validator.vs_random run` | ❌ needs positions/returns; real test = `signal_vs_random` |
| 7 | Critic-Validator | critic-validator subagent | ⏸ reachable, but standard inputs (`results/metrics.json`) absent (G1) |
| 8 | Gatekeeper | `quant_validator.gates evaluate` | ⚠ runs but on degenerate inputs → `first_failure: deflated_sharpe` |
| 9 | Risk | risk subagent | ⏸ reachable, but needs positions/Greeks/results (absent) |
| 10 | Memory | `quant_validator.memory record` | ✅ recorded trial id=3 |

## Ranked integration gaps (triage list — NOT fixed here)

1. **No Mode-A panel backtest adapter.** The pipeline's Stage 4/5 (`quant_validator.backtest`,
   `quant_validator.sandbox`) don't exist; the engine is `ai_quant_lab.backtest` (single
   `pd.Series` positions×returns). A **fires-frame → verdict** adapter (wrapping
   `signal_vs_random.run_test`) is the missing keystone. **Highest priority.**
2. **One canonical `strategy()` contract.** `code.md` (positions [-1,1]) vs
   `strategy-authoring/SKILL.md` (fires frame) contradict. Pick one; for the panel it must
   be the fires frame. Reconcile `code.md` to defer to the skill.
3. **Stages 6/6.5 are positions×returns-shaped.** `stats`/`vs_random` demand
   `results/returns.csv` & `positions.csv`. For a panel strategy the canonical artifact is
   the fires frame + the date/direction-matched pool. Either an adapter emits a
   `returns.csv` proxy, or these stages get panel-aware variants.
4. **Stage 8 gates run on empty inputs** and report a "failure" rather than "not-applicable."
   Should detect absent results and emit `not_available`, like the Mode-B vs_random does.
5. **Double-screen.** A strategy that pre-screens, then `run_test` re-screens, drops fires.
   Define screening once (in the strategy OR the engine, not both).
6. `bridge/` is still an empty README — the intended Mode-A adapter layer.

## SKILL.md gaps to patch (Stage 1b — from the Coder, verified plausible)

1. **Divergence side-mapping is INVERTED** in both `SKILL.md` (worked-reference) and the
   refined.json prose vs the reference: skewDelta neg-then-up = **BULL** (not BEAR). The
   Coder matched the reference; the doc misleads. **Fix the doc.** (Highest-value skill fix.)
2. **Pin the exact float algebra:** `d3 > d2 + 0.2` (not `(d3-d2) > 0.2`) and shift-AND stall
   (not `rolling(4).sum()==4`) — only these give byte parity (~332 ULP cases otherwise).
3. **Freshness arithmetic underspecified:** it's `rolling(3, min_periods=1).max()` (today + 2
   prior) and "M1∧M2 co-fire" = both `recent`-flags True on the same bar.
4. **Read-vs-recompute:** `putP/callP/ivP/rrP` are used as-is; `sigma`/`skewDelta` arrive
   pre-rounded so the rounding rule is a no-op on this panel. Distinguish "trust provider"
   vs "PIT rebuild."
5. **Fires-frame schema undefined** (`symbol` vs `ticker`, `date` dtype, flag booleans).
6. **Where filters live** when their data is absent and the parity target is pre-filter
   (Coder carried earnings/short-trend as unevaluated flags).
7. **The `fwd5 @ 2019-06-03 = 0.111192` anchor is wrong for the AV panel** (it's the ORATS
   `clsPx` build). Binding anchor here = 519,984-fire / AAPL-2015-08-05 parity. Label anchors per panel.
8. **`python -m quant_validator.sandbox validate` doesn't exist** — point the skill at the
   real validation path (functional equivalence vs `compute_consensus`).

## One-line answer
**Does a prose thesis flow end-to-end through Mode A?** PARTIAL — the *intelligence*
(refine → critique → code → reproduce a known verdict) works flawlessly; the *plumbing*
(a panel-aware backtest adapter + a single strategy contract) is missing. Build gap #1
and the chain closes.
