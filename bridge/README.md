# bridge/ — Wiring existing HTML tools into the validation pipeline

Your existing HTML tools at
`C:\Users\stanw\Dropbox\PC (2)\Desktop\stan-trading-tools\`
already produce backtest results. The validation pipeline can validate
those results directly without regenerating them. This is **Mode B**.

## How Mode B works

1. Your HTML tool produces its usual output (CSV downloads or copy-paste data)
2. You manually drop the exported files into a thesis folder under
   `theses/<thesis_id>/`
3. You run `/validate-thesis <thesis_id>` in Claude Code
4. The orchestrator detects `results/positions.csv` exists → enters Mode B
5. Steps 3-5 (data fetch, code generation, backtest) are SKIPPED
6. Steps 1, 2, 6-11 run normally:
   - hypothesis-refiner formalizes your thesis from prose you provide
   - critic-pre still adversarially reviews the idea before stats
   - stats, critic-validator, gates, risk all run against your provided results
   - decision.json records the final accept/reject

## Required files for Mode B

You must place these in `theses/<thesis_id>/` before running:

```
thesis.md                           Required — prose description of the strategy
results/positions.csv               Required — DatetimeIndex, single column "position"
                                    (or per-ticker columns for cross-sectional)
results/returns.csv                 Required — DatetimeIndex, single column "returns"
                                    or per-bar net return after costs
results/equity_curve.csv            Required — cumulative equity series
results/greeks.csv                  Required IF thesis involves options
                                    Columns: delta, gamma, vega, theta
                                    in share/per-$1/per-1%/per-day units
```

## File format examples

### positions.csv (single-asset)
```
timestamp,position
2023-01-03,0.0
2023-01-04,0.5
2023-01-05,1.0
...
```

### positions.csv (cross-sectional)
```
timestamp,AAPL,MSFT,NVDA,GOOG
2023-01-03,0.25,0.25,-0.25,-0.25
2023-01-04,0.30,0.20,-0.30,-0.20
...
```

### greeks.csv (options thesis)
```
timestamp,delta,gamma,vega,theta
2023-01-03,15.5,0.8,420.0,-32.0
2023-01-04,18.2,0.9,455.0,-35.0
...
```

## Reusing thesis IDs

If you've already validated a similar thesis and want to track iterations
(e.g., Skew_backtest PATCH-21h → PATCH-22), use a versioned thesis_id:
```
theses/skew_consensus_v21/
theses/skew_consensus_v22/
```
The memory module treats these as separate trials, both counting toward
your DSR n_trials. Correlation gate will compare v22 against v21 since
they're survivors.

## Wiring shortcut (Phase 2.5)

Once Vibe-Trading MCP is installed (Phase 2.5), the
`analyze_trade_journal` skill can directly parse Moomoo and Tiger Trade
CSV exports and produce the Mode B-required results files automatically.
Until then, the HTML tool exports → CSV → thesis folder is manual.
