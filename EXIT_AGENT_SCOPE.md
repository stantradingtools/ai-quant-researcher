# Exit Agent — Scope & Guardrails (parked, companion to the Mutation Agent)

> **Status:** parked — build alongside the Mutation Agent phase. Agents **#11 (Mutation)** and
> **#12 (Exit)** ship together: the Exit Agent manages exits for EXISTING positions, and is
> required for the Mutation Agent's exit-overlay variants to be properly managed on the live book.
> **Roster position:** agent #12 (run-time portfolio exit management), companion to #11
> (Mutation Agent, design-time variant search).
> **Build prerequisites:** the Phase-4 manage loop (the live position ledger + mechanical 21D
> close + kill-switch) and the live ORATS feed must exist first; the Exit Agent layers intelligent
> early-exit ranking on top of them.

## Purpose

For **every open position**, find the **best exit point AFTER entry — before the 21-day hard time
exit** — and surface **2–3 ranked exit options** for the current portfolio. Advisory by default;
can act through the Phase-4 manage loop when the user enables auto-exit.

It exists because the strategy's only exit today is the fixed 21-trading-day hold + the portfolio
kill-switch. The Exit Agent adds the intelligent layer: take profit when the fade has reverted,
or cut early when the vol/skew regime turns against the position — rather than always riding to
day 21.

## Relationship to the Mutation Agent (why they ship together)

- **Mutation Agent (#11) — design-time:** proposes and *validates* exit RULES as strategy variants
  (e.g. a Bollinger-band exit overlay), each re-run through the full pipeline with the
  multiple-testing / DSR penalty. It decides which exit rules are *allowed*.
- **Exit Agent (#12) — run-time:** applies those validated rules to LIVE positions, ranking the
  best exit per position day-to-day, inside the 21D backstop. It *enforces / recommends* them.
- One without the other is incomplete: validated exit rules with nothing to run them, or a
  run-time exiter using unvalidated rules. **Build both in the same phase.**

## Exit signals (grounded in the Options_Sell_Signal tool + the entry data)

1. **Price mean-reversion target — Bollinger band.** Because the strategy is a fade, the profit
   target is reversion toward the mean / opposite band. Exit (take profit) when price reverts to
   the middle band or the far band. *(Data: AV daily OHLC.)*
2. **Volatility-spike exit — realized and/or implied vol, last ~5D.** Exit when 5-day realized vol
   (Yang-Zhang **YZ5**) or **ATM IV** spikes, signalling a regime change against the position. Use
   the tool's YZ5/10/20/30 RV, ATM IV, **VRP = IV − RV**, and the 252-day **VRP percentile** as the
   trigger surface. *(Data: AV/Massive for YZ RV; Flash Alpha / ORATS / AV HISTORICAL_OPTIONS for
   ATM IV.)*
3. **Squeeze / skew flag.** Negative skew (call IV > put IV) is the squeeze signature. For a short
   (BULL-fade), an emerging squeeze = exit early. This is the direct tail-control answer to the
   **2021-01 meme-squeeze worst month**, where the fade shorts were run over — a squeeze-detector
   exit is exactly what was missing there. *(Data: ORATS skew / the tool's squeeze radar.)*
4. **Time backstop — the 21-day hard exit.** Non-negotiable. Nothing is held beyond it.
5. **Stop / trailing.** Hard stop + trailing stop for tail control between entry and day 21.

## Output — 2–3 ranked exit options per position

For each open position, present 2–3 ranked options with trigger, rationale, and projected outcome,
e.g.:

- **Best:** Bollinger mean-touch — target ~`<price>`, est. ~`<N>` days, ~`+<Y>%` projected.
- **Alt:** vol-spike exit — exit now if YZ5 / ATM IV / VRP-pct crosses `<threshold>` (regime turn).
- **Backstop:** 21D hard exit on `<date>` (always present).

Plus the squeeze flag when active ("squeeze building — consider exiting the short now"). Advisory
by default; the user (or the manage loop, if auto-exit is enabled) picks.

## Guardrails (same discipline as the Mutation Agent)

- **Validated rules only.** Band period/width and vol thresholds are a prime overfitting surface —
  they must be backtested and OOS-validated (via the Mutation Agent) before going live. No live
  exit rule that hasn't cleared validation.
- **21D hard exit is the non-negotiable backstop.**
- **Hard stop + portfolio kill-switch remain** for tail control.
- **Advisory by default;** auto-exit only on explicit enable; every exit decision logged and
  reflected in the living report.
- **Edge-preserving.** The agent optimizes the *exit* only — it never touches the entry signal or
  the validated edge.

## Integration

- Plugs into the **Phase-4 manage loop**, which already does the mechanical 21D close + kill-switch
  via the Alpaca API. The Exit Agent adds the early-exit ranking on top, reading the live position
  ledger (entry date, days held, scheduled exit).
- Writes an **"Exit options"** block per open position into the living `report.html` (via the
  report-rendering skill).

## Data sources (summary)

| Signal | Source |
|---|---|
| Bollinger bands (price) | AV daily OHLC |
| Realized vol YZ5/10/20/30 | AV / Massive (Yang-Zhang) |
| ATM IV | Flash Alpha / ORATS / AV HISTORICAL_OPTIONS |
| VRP = IV − RV + 252-day percentile | derived (per the Options_Sell_Signal tool) |
| Skew / squeeze (negative skew = call-IV-rich) | ORATS / the tool's squeeze radar |
