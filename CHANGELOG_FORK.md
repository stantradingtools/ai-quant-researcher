# CHANGELOG — Stan's fork

Changes layered on top of upstream `zostaff/ai-quant-researcher`.

## v0.3 — Vs. Random gate + live-testing fixes (May 2026)

Added after live-testing v0.2 surfaced 5 spec issues and a request to add
the Woodriff/BuildAlpha Vs. Random robustness test. Full detail in
PATCH_NOTES_v0.3.md. Verification suite expanded 12 → 16 tests.

- NEW `quant_validator/vs_random.py` — Vs. Random gate (Step 6.5). Tier A
  permutation test fully implemented (always runs, Mode A + B). Tier B
  constraint-matched random rule search scaffolded with skew_consensus
  rule grammar. Tier C block-bootstrap scaffolded.
- NEW `quant_validator/stats.py` — Step 6 stats CLI (Sharpe/Sortino/Calmar/
  drawdown/moments, DSR, k-fold walk-forward). Was referenced by orchestrator
  but never existed.
- NEW `quant_validator/gates.py` — Step 8 gates CLI (deflated_sharpe,
  correlation, pca_concentration, vs_random). Was referenced but never existed.
- FIX memory.py — apply_override now inserts a placeholder row pre-Step-10
  (deployment_status='override_pending'); record_trial upserts + merges
  override_log at Step 10 instead of inserting a duplicate.
- FIX adapters (massive/alpha_vantage/flash_alpha/orats) — raise
  NotImplementedError first with honest stub message, before the key check.
- FIX hypothesis-refiner.md — market_type is the TRADED INSTRUMENT, not the
  signal source; "skew/vol/gamma" in prose are signal names and don't imply
  options or cross-sectional.
- WIRING — validate-thesis.md Step 6.5; override-reject.md vs_random
  overridable with resume map; critic-validator.md reads vs_random.json.
- Version bump 0.1.0 → 0.3.0.

## v0.1 — Initial overlay (May 2026)

### Subagent architecture (.claude/agents/)
- Added `hypothesis-refiner` (refiner role, not proposer)
- Customized `code` agent for options tuple-return contract
  (`(positions, greeks)`)
- Customized `critic-pre` with 4 per-market templates: OPTIONS (default),
  EQUITIES, INDEXES_AND_INDEX_ETFS, CRYPTO. Routing by universe/signal.
- Added `critic-validator` 9-criteria post-backtest checker
- Customized `risk` thresholds (loosened from upstream defaults; path
  to size > 0.8 = Sharpe positive in 2 of 3 regimes, not 3 of 3)
- Added `memory` clerk subagent for slash-command interaction with SQLite

### Orchestrator (.claude/commands/)
- Replaced upstream `loop.py` batch (50 iterations) with `/validate-thesis`
  single-thesis pipeline
- Auto-detects Mode A (from-prose) vs Mode B (from-results)
- Structured summary block prints after every step
- Step summaries also written to `theses/<id>/step_summaries/<NN>.md`
- Audit log appended to `audit_log.jsonl` per step
- User interactions logged to `user_interactions.jsonl`
- Added `/override-reject` for subjective overrides with ≥20-char
  justification requirement

### Memory module (quant_validator/memory.py)
- Extended schema: market_type, deployment_status, paper_start_date,
  live_start_date, size_multiplier, override_log_json columns
- New `trial_greeks` side table for per-trial Greek summary
- `portfolio_greeks` computes on-demand from latest backtest exit
  positions across deployed strategies (NOT a daily snapshot — broker
  feed comes in Phase 5)
- `seed_historical` for honest n_trials initialization (defaults to 30,
  representing pre-system trials)
- `overrides` audit query
- CLI for all operations the memory subagent invokes

### Audit (quant_validator/audit.py — new module)
- Append-only JSONL writers for pipeline events and user interactions
- Read helpers for re-summarizing past runs

### Risk statistics (quant_validator/risk_stats.py — new module)
- Deterministic helpers consumed by the Risk subagent before LLM judgment
- Computes: position_stats, regime_breakdown, tail_metrics, greek_summary,
  concentration_stats, event_window_concentration

### Data adapters (adapters/ — new package)
- `event_calendar.py` IMPLEMENTED — unified calendar across all event types
- `deribit.py` IMPLEMENTED — public DVOL OHLC + chain snapshot (free)
- `crypto_data_download.py` IMPLEMENTED — free DVOL CSV mirror
- `unusual_whales.py` SCAFFOLD — raises UnusualWhalesNotSubscribed pattern
- `massive.py`, `alpha_vantage.py`, `flash_alpha.py`, `orats.py` STUBS
  — interface defined, Phase 1 implementation pending

### Custom features (features_custom/ — new package)
- `skew.py` — skew_z_score, skew_change_5d implemented; Tian & Wu Phase 2
- `vol.py` — vrp_pct, iv_rv_spread, term_structure_slope implemented
- `exposure.py` — gex_distance, dealer_alignment implemented
- `pe_quadrant.py` — pe_zscore_252, pe_quadrant_label implemented

### Configs (config/)
- `portfolio_targets.json` — Greek limits
- `market_holidays.csv` — 2026 & 2027 NYSE/NASDAQ schedule
  (VERIFY before live use)
- `fomc_dates.csv` — 2026 announced (VERIFY before live use)
- `cpi_dates.csv` — 2026 announced (VERIFY before live use)
- `jpm_collar_history.csv` — empty starter for manual JHEQX strike entry

### Other
- `theses/_template/README.md` — explains the per-thesis folder structure
- `bridge/README.md` — Pattern 3 wiring guide for HTML tool exports
- `.env.example` — API key template
- `README_FORK.md` — fork-specific top-level docs

## Upstream pieces NOT used / disabled

- `loop.py` batch driver — not invoked; orchestrator handles dispatch
- Upstream sandbox SIGALRM timeout — non-functional on Windows;
  manual Ctrl+C if hang. Stan accepted this constraint to stay native
  Windows rather than introduce WSL.

## Roadmap

- Phase 1: implement REST clients in adapters/massive, alpha_vantage,
  flash_alpha, orats
- Phase 2: Deribit historical chain reconstruction; complete custom features
- Phase 2.5: evaluate Vibe-Trading MCP integration
- Phase 3: activate Unusual Whales adapter (if subscribed)
- Phase 4: paper-trading wiring + first end-to-end live thesis
- Phase 5: broker feed for live position Greek aggregation
