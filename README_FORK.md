# Stan's Fork — ai-quant-researcher Customizations

A personal quant research validation system. Forked from upstream
`github.com/zostaff/ai-quant-researcher` (a paper-faithful implementation of
"Validation Architecture for AI-Driven Quant Research") and customized for
single-user, single-thesis workflow over the trader's existing brokers and
data sources.

## What this fork does

The upstream repo runs an autonomous batch loop: generate 50 hypotheses,
backtest each, accept survivors. **This fork inverts that.** You provide
hypotheses one at a time; the system formalizes, stress-tests, backtests,
and validates each — but never auto-generates ideas you didn't ask for.

Six subagents collaborate via a single slash command:

```
/validate-thesis "<your hypothesis prose, or an existing thesis_id>"
```

The orchestrator walks 12 steps, pausing at decision points, printing
a structured summary after every step, and writing a complete audit
trail to `theses/<thesis_id>/`.

## What's added on top of upstream

### Subagents (`.claude/agents/`)
- `hypothesis-refiner` — formalizes user prose into JSON spec (does NOT
  invent hypotheses; refines yours)
- `code` — translates spec to one strategy function; options theses return
  `(positions, greeks)` tuple
- `critic-pre` — adversarial pre-backtest review with 4 per-market templates
- `critic-validator` — 9-criteria post-backtest checker
- `risk` — 4th gate with loosened thresholds and on-demand Greeks
- `memory` — clerk over SQLite; computes portfolio Greeks from latest
  backtest exit positions

### Slash commands (`.claude/commands/`)
- `/validate-thesis` — single-thesis pipeline with full transparency
- `/override-reject` — subjectively override soft rejections with logged
  justification (≥20 char)

### Python helpers (`quant_validator/`)
- `memory.py` — extended SQLite schema (market_type, deployment_status,
  size_multiplier, override_log_json, trial_greeks side table)
- `audit.py` — append-only JSONL audit log helpers
- `risk_stats.py` — deterministic risk statistics

### Data adapters (`adapters/`)
- `event_calendar.py` — unified calendar (triple witching, OPEX, VIX expiry,
  NFP, FOMC, CPI, holidays, JPM collar rolls) — **implemented**
- `deribit.py` — public REST API for crypto vol — **implemented (DVOL + chain)**
- `crypto_data_download.py` — free DVOL CSV mirror — **implemented**
- `unusual_whales.py` — scaffold; inert without `UW_API_KEY`
- `massive.py`, `alpha_vantage.py`, `flash_alpha.py`, `orats.py` — stubs
  for Phase 1 implementation

### Custom features (`features_custom/`)
- `skew.py`, `vol.py`, `exposure.py`, `pe_quadrant.py` — stubs for Phase 2
  porting from existing HTML tools

### Configs (`config/`)
- `portfolio_targets.json` — Greek limits for portfolio check
- `market_holidays.csv` — 2026-2027 NYSE/NASDAQ schedule (verify before use)
- `fomc_dates.csv`, `cpi_dates.csv` — 2026 announced dates (verify before use)
- `jpm_collar_history.csv` — manual entry for JHEQX strikes

## Build phases

| Phase | What | Status |
|---|---|---|
| 1 | Foundation: fork repo, subagents, event_calendar, memory, audit | ✓ This overlay |
| 1 | Implement Massive/AV/Flash Alpha/ORATS REST clients | TODO |
| 2 | Crypto adapters + custom features | Partial (Deribit done) |
| 2.5 | Vibe-Trading MCP evaluation (optional) | Pending |
| 3 | Unusual Whales activation if subscribed | Pending |
| 4 | First end-to-end run on a migrated HTML tool thesis | Pending |
| 5 | Broker feed for true live position Greeks | Pending |

## Installation

```
# 1. Fork zostaff/ai-quant-researcher on GitHub to your account
# 2. Clone your fork
cd "C:\Users\stanw\Dropbox\PC (2)\Desktop\stan-trading-tools"
git clone https://github.com/<your-username>/ai-quant-researcher.git
cd ai-quant-researcher

# 3. Unzip this overlay ON TOP of the repo
#    (Right-click the zip → Extract Here → overwrites where needed,
#     adds new files alongside)

# 4. Install upstream Python deps
pip install -r requirements.txt

# 5. Copy .env.example to .env and add your API keys
copy .env.example .env
notepad .env

# 6. Initialize memory with honest n_trials seed (30 = your pre-system trials)
python -m quant_validator.memory seed_historical --count 30 \
  --note "PE Quadrant + skew_quadrant + Skew_backtest PATCH-1 through PATCH-21h"

# 7. Open the project in Claude Code
claude

# 8. Verify subagents are discovered
> /agents
```

## First test run

```
> /validate-thesis "SPX 1DTE put credit spreads on JHEQX collar roll
  Fridays; sell -10 delta vertical, take profit at 50% premium decayed."
```

Watch the structured summary blocks print after each step. If anything
gets rejected and you want to push through, use:

```
> /override-reject spx_jhqx_1dte_pcs gates:deflated_sharpe \
  --reason "Edge is collar-specific; only 3 prior trials of this exact
   mechanism, not the lifetime total"
```

## Audit trail

Every thesis writes 4 layers of audit:
- `decision.json` — final state at a glance
- `audit_log.jsonl` — chronological event log
- `step_summaries/*.md` — human-readable per-step write-ups
- `user_interactions.jsonl` — every question + your response

See `theses/_template/README.md` for full folder structure.

## License

Inherits upstream license (MIT). Personal use; not for distribution.
