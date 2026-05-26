# Mutation Agent — Scope & Guardrails (parked)

> **Status:** parked — capture now, build later.
> **Roster position:** agent #11 (post-validation), in the named pipeline
> (1 Hypo-Refiner -> 2 Pre-Critic -> 3 Coder -> 4 Backtest -> 5 Stats -> 6 VsRandom
> -> 7 Validator -> 8 Gatekeeper -> 9 Risk -> 10 Memory & Reporter -> **11 Mutation**).
> **Build prerequisites:** the Mode-A backtest adapter (keystone plumbing), Stage 2 sizing
> (done, `7c343cb`), and Phase 4 paper trading must exist first — the Mutation Agent needs a
> working end-to-end validation loop to spawn child runs into.

## Purpose

Take a strategy that has already cleared the pipeline and propose **variations that improve
risk-adjusted performance (Sharpe / drawdown)** without changing the validated edge. It
formalises the guarded refinement we have been doing by hand (removing the VIX filter,
weighing options vs equity, considering liquidity tiers) into a repeatable, re-validated loop.

Distinct from the Hypo-Refiner (agent #1), which turns prose into a spec for a *new* thesis.
The Mutation Agent explores variants of an *already-validated* one.

## Core concept

- Input: a validated, sized strategy (passed gates + sizing).
- The agent proposes N guarded variants, each a new spec that **re-enters the pipeline at the
  Coder/Backtest stage** and runs the full validation again.
- Variants are compared against the baseline in the living report (a "Mutations" section).
- A variant is **recommended only if** it improves the target metric AND re-clears every gate
  AND survives the multiple-testing penalty below.

## Non-negotiable guardrails

These are what make an optimiser safe — without them a mutation search is an overfitting machine.

1. **Thesis-locked.** Cannot alter the economic hypothesis or the validated signal logic
   (M1/M2/M3). It mutates only *expression* (instrument), *universe*, *exit*, and *risk params* —
   never the edge itself.
2. **Mandatory re-validation.** Every variant runs the full gate stack (vs_random, cost,
   temporal stability, critic-validator). No acceptance on a Sharpe/DD improvement alone.
3. **Multiple-testing / DSR penalty (the critical one).** A mix-and-match search over
   {instrument x universe x exit} is a multiple-comparisons machine. The agent MUST log how many
   variants it tried so the Gatekeeper's deflated Sharpe penalises for the full search space —
   otherwise it will "find" a spurious better edge.
4. **Ex-ante selection only.** Any universe cut or ranking must use a *forward-available*
   criterion (liquidity, persistent signal-following), validated out-of-sample / across regimes —
   never in-sample winners.
5. **Instrument-appropriate cost gate.** An equity->options mutation must be cost-modelled as
   *options* (wider spreads, theta) — it cannot borrow the equity cost cushion the underlying-only
   result earned.
6. **Holdout discipline.** Variants are validated on data not used to propose them.

## Scoped mutation dimensions (initial set)

### 1. Options expression (~30-delta, buy or sell, off the same signal)

- BULL (= short underlying, profits if price falls) -> buy ~30-delta puts / sell ~30-delta calls.
  BEAR (= long) -> buy ~30-delta calls. Test buy *and* sell expressions; let the gates decide.
- Map the 21d horizon to ~21-30 DTE.
- **Guardrail / note:** re-cost-gate as options (spread + theta) — theta can eat an ~18 bps edge
  fast. Long options give defined risk (better DD); short options collect theta but open the tail.

### 2. Universe tightening (top ~10 tickers)

- Reduce to the best thesis-followers with good options liquidity / equity tradeability.
- **Guardrail / note (overfitting trap):** "best performing" must mean *ex-ante and persistent*
  (rolling/expanding signal-following + option-spread/ADV liquidity), re-validated on holdout and
  across regimes — never in-sample top returns. Top-10 concentrates idiosyncratic risk, so the
  sizing caps and kill-switch matter more.

### 3. Exit overlay (Bollinger band)

- Shift from a fixed 21d hold to event-driven exits: hard stop for the initial exit + trailing
  loss; take-profit / stop at the reverting / adverse band. Extensible to other exit rules.
- **Guardrail / note:** this needs a *path-dependent* backtest (daily OHLC, which AV provides) —
  the current forward-return panel cannot see intrabar band touches. Band params (period, width)
  are a prime overfitting surface and need out-of-sample validation. This dimension has the
  largest engineering footprint (a path-aware backtester).

## Mix-and-match

The intent is to combine the dimensions ({instrument} x {universe} x {exit}) to search for a
better edge. The combinatorial blow-up is exactly why guardrail #3 (multiple-testing / DSR
penalty) is non-negotiable: log every combination tried; deflate significance for the whole space.

## Output

Writes a "Mutations" section into the strategy's living `report.html` (via the report-rendering
skill): each variant, its gated result, and its delta vs the baseline, so the search is auditable.
