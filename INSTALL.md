# INSTALL.md

Step-by-step install guide. Designed for Claude Code to execute end-to-end,
or for you to walk through manually. Specific to Windows; commands assume
Git Bash (default Claude Code shell on Windows).

## TL;DR for Claude Code

If you're Claude Code reading this: execute each numbered step in order.
Pause at Step 1 (browser action), Step 6 (manual .env edit), and Step 9
(switching out to a new shell). Run the verification script at Step 5
and confirm 12 passes before continuing. Report any test failures
immediately rather than continuing.

For each command, show the user the command being run and the output.
Do NOT run all commands silently; the user wants to see each step.

═══════════════════════════════════════════════════════════════

## Prerequisites (one-time setup, may already be done)

- Python 3.10+ — check: `python --version`
  - If missing: install from python.org
- Git — check: `git --version`
  - If missing: install Git for Windows from git-scm.com
- Claude Code — check: `claude --version`
  - If missing: see docs.claude.com for install instructions
- GitHub account — needed for Step 1
- Anthropic API key — get from console.anthropic.com (only needed if
  using the Anthropic API outside Claude Code sessions)

═══════════════════════════════════════════════════════════════

## Step 1 — Fork upstream repo (browser, ~30 seconds, CANNOT be automated)

PAUSE. Open in your browser:

1. Navigate to: https://github.com/zostaff/ai-quant-researcher
2. Click "Fork" (top right corner of GitHub UI)
3. Choose your own account as the destination
4. Wait for the fork to complete (~10 seconds)
5. Note your fork URL — format:
   `https://github.com/YOUR_USERNAME/ai-quant-researcher.git`

When done, tell Claude Code your GitHub username so it can clone the
fork in Step 2.

═══════════════════════════════════════════════════════════════

## Step 2 — Clone your fork locally

Substitute YOUR_USERNAME with your actual GitHub username.

```bash
cd "/c/Users/stanw/Dropbox/PC (2)/Desktop/stan-trading-tools/"
git clone https://github.com/YOUR_USERNAME/ai-quant-researcher.git
cd ai-quant-researcher
pwd
```

Expected `pwd` output:
`/c/Users/stanw/Dropbox/PC (2)/Desktop/stan-trading-tools/ai-quant-researcher`

If the folder `stan-trading-tools` doesn't exist yet, create it first:
```bash
mkdir -p "/c/Users/stanw/Dropbox/PC (2)/Desktop/stan-trading-tools/"
```

═══════════════════════════════════════════════════════════════

## Step 3 — Extract the overlay on top of the cloned repo

Locate the overlay zip — it was downloaded to your Downloads folder when
Claude.ai presented it. Move it into the repo root.

```bash
# Adjust source path if your browser saved it elsewhere
cp "/c/Users/stanw/Downloads/stan_fork_overlay_v0.2.zip" .

# Confirm the zip is here
ls -la stan_fork_overlay_v0.2.zip
```

Extract using Python (most reliable cross-platform extraction):

```bash
python -c "
import zipfile, shutil, os
with zipfile.ZipFile('stan_fork_overlay_v0.2.zip') as z:
    z.extractall('_tmp_overlay/')
src_root = '_tmp_overlay/stan_fork_overlay'
for item in os.listdir(src_root):
    src = os.path.join(src_root, item)
    if os.path.isdir(src):
        shutil.copytree(src, item, dirs_exist_ok=True)
    else:
        shutil.copy2(src, item)
shutil.rmtree('_tmp_overlay')
print('Overlay extracted into', os.getcwd())
"
```

Verify key directories are now in place:

```bash
ls .claude/agents/         # expected: 6 .md files
ls .claude/commands/       # expected: 2 .md files
ls quant_validator/        # expected: __init__.py, memory.py, audit.py, risk_stats.py
ls adapters/               # expected: 8 .py files plus __init__.py
ls features_custom/        # expected: 5 .py files
ls config/                 # expected: 5 config files
ls scripts/                # expected: verify_install.py
ls README_FORK.md INSTALL.md CHANGELOG_FORK.md .env.example  # 4 files
```

If any of those `ls` commands shows missing files, the extract failed.
Re-run the Python extract command.

═══════════════════════════════════════════════════════════════

## Step 4 — Install Python dependencies

```bash
# Upstream dependencies (from cloned repo)
pip install -r requirements.txt

# Extra dependencies the overlay uses (likely already installed via upstream)
pip install requests pandas pyarrow
```

Expected: each package shows "Requirement already satisfied" or installs
without error. Watch for any red error text — flag if anything fails.

═══════════════════════════════════════════════════════════════

## Step 5 — Run the verification suite (NO API keys needed)

This is the critical checkpoint. 12 tests; all must pass before continuing.

```bash
python scripts/verify_install.py
```

Expected last lines of output:
```
SUMMARY
============================================================
  Passed: 12
  Warned: 0
  Failed: 0

All checks passed. Overlay correctly installed.
```

The Deribit live ping test (test 10) requires internet access. If your
firewall blocks it, that one test will WARN instead of PASS — that's
acceptable; the overlay code itself is fine.

If any FAIL: STOP. Report the failed test name and the error message
before continuing. Do not skip to the next step.

═══════════════════════════════════════════════════════════════

## Step 6 — Set up your .env file

PAUSE. This step is manual — Claude Code can copy the template but you
edit the keys yourself.

```bash
cp .env.example .env
```

Open `.env` in any text editor and fill in your API keys:

```bash
# Windows: opens Notepad
notepad .env

# OR use VS Code if installed
code .env
```

Fill in these keys (leave UW_API_KEY and TARDIS_API_KEY blank for now):
- `ANTHROPIC_API_KEY` — from console.anthropic.com
- `MASSIVE_API_KEY` — from your Massive account
- `ALPHA_VANTAGE_API_KEY` — from alphavantage.co (free tier OK)
- `FLASH_ALPHA_API_KEY` — from your Flash Alpha account
- `ORATS_API_TOKEN` — from your ORATS account

Save and close the editor.

═══════════════════════════════════════════════════════════════

## Step 7 — Initialize memory with your 30 pre-system trials

This is your honest n_trials seed for the Deflated Sharpe Ratio.
Counts PE Quadrant + skew_quadrant + Skew_backtest PATCH-1 to 21h.

```bash
python -m quant_validator.memory seed_historical --count 30 \
  --note "PE Quadrant + skew_quadrant + Skew_backtest PATCH-1 to 21h"
```

Expected output: `Seeded 30 historical trial placeholders.`

Verify:

```bash
python -m quant_validator.memory status
```

Expected: `total_trials: 30`, `accepted_count: 0`, `current_dsr_n_trials: 30`.

═══════════════════════════════════════════════════════════════

## Step 8 — Commit and push the overlay to your GitHub fork

This saves your customizations to GitHub so you can clone the configured
fork onto any machine in the future.

```bash
git status                    # see what changed
git add .
git commit -m "Add Stan's fork overlay v0.1 (subagents, validator, adapters)"
git push origin main
```

Note: the `.env` file is NOT pushed — `.gitignore` should already exclude
it (verify with `cat .gitignore | grep .env`). If `.env` is not gitignored,
ADD it before pushing:

```bash
echo ".env" >> .gitignore
echo "memory.db" >> .gitignore
echo "theses/" >> .gitignore       # personal data, don't share
git add .gitignore
git commit -m "Gitignore .env, memory.db, theses/"
git push origin main
```

═══════════════════════════════════════════════════════════════

## Step 9 — Open Claude Code in this folder

PAUSE — this opens a new shell. The current Claude Code session may be
running outside the repo; this step ensures you're inside.

```bash
cd "/c/Users/stanw/Dropbox/PC (2)/Desktop/stan-trading-tools/ai-quant-researcher"
claude
```

In the new Claude Code session, verify subagents are visible:

```
> /agents
```

Expected output should list (in some order):
- hypothesis-refiner
- code
- critic-pre
- critic-validator
- risk
- memory

Verify slash commands:

```
> /help
```

Expected to see `validate-thesis` and `override-reject` among the
project-scoped commands.

═══════════════════════════════════════════════════════════════

## Step 10 — Smoke test the pipeline

Run a deliberately bad thesis through the full pipeline to confirm
wiring works. The critic-pre agent should kill it.

```
> /validate-thesis "Test thesis to verify the pipeline loads correctly.
  No mechanism, no edge, just smoke-test. Expected to be killed by critic-pre."
```

Expected behavior:
- Step 0: orchestrator creates a thesis folder under `theses/`
- Step 1: hypothesis-refiner runs; may ask "single or cross-sectional?"
  — answer "single"
- Step 2: critic-pre runs and KILLS the thesis with reasons
- Step 11: final report shows `decision: rejected, rejection_reason: critic_pre`
- The orchestrator stops cleanly with override instructions printed

Verify the audit folder was created:

```bash
ls theses/                  # should show one folder named after your test thesis
ls theses/*/                # should contain: thesis.md, refined.json,
                            # critique_pre.json, audit_log.jsonl,
                            # user_interactions.jsonl, decision.json,
                            # step_summaries/
cat theses/*/decision.json  # confirm decision: rejected, reason: critic_pre
```

If the audit folder is missing or decision.json is malformed, something
is wired wrong — STOP and report.

═══════════════════════════════════════════════════════════════

## Install complete — next steps

You're ready to use the system. Try one of these:

### Path A — Validate a real thesis from scratch (Mode A)

Pick a hypothesis you've been thinking about. Write it as prose,
copy-paste into `/validate-thesis`. The pipeline will:
- Formalize it (Step 1)
- Adversarially review (Step 2)
- Stop if the data adapters aren't implemented yet (Step 3)

Phase 1 work: implement REST clients in adapters/massive.py,
alpha_vantage.py, flash_alpha.py, orats.py.

### Path B — Validate an existing HTML tool result (Mode B)

Take your Skew_backtest PATCH-21h. Export positions/returns/greeks to
CSV. Drop them into `theses/skew_consensus_v21/results/`. Write the
prose hypothesis into `theses/skew_consensus_v21/thesis.md`. Run:

```
> /validate-thesis skew_consensus_v21
```

The orchestrator detects results/ already exists → Mode B → skips data
fetch and code generation, runs validation directly. Faster path to
seeing your existing strategy survive the 9-criteria gauntlet.

═══════════════════════════════════════════════════════════════

## Troubleshooting

### `python` not found
On Windows, try `py` instead. Or add Python to PATH via the installer.

### `git` not found
Install Git for Windows from git-scm.com. Restart your shell after install.

### `claude` not found
Install Claude Code per docs.claude.com. Restart your shell after install.

### Subagents not appearing in `/agents`
Confirm `.claude/agents/` is at the REPO ROOT, not inside another folder.
Run `ls -la .claude/agents/`. If empty or missing, re-extract the zip
(Step 3).

### Slash commands not appearing in `/help`
Confirm `.claude/commands/` exists with the two .md files.
Run `ls -la .claude/commands/`. Re-extract if missing.

### Verification script: "ModuleNotFoundError: No module named 'quant_validator'"
You're running from the wrong directory. Run from the REPO ROOT:
```bash
cd "/c/Users/stanw/Dropbox/PC (2)/Desktop/stan-trading-tools/ai-quant-researcher"
python scripts/verify_install.py
```

### Verification script: Deribit live ping WARNED
Network or firewall is blocking the public Deribit endpoint. This is a
warning not a failure — the overlay code is fine. You can use the system
normally; only crypto adapter calls will fail until network is fixed.

### Path issues with parentheses in `PC (2)`
ALWAYS quote the path:
```bash
cd "/c/Users/stanw/Dropbox/PC (2)/Desktop/stan-trading-tools/"
```
Never:
```bash
cd /c/Users/stanw/Dropbox/PC (2)/Desktop/stan-trading-tools/   # WRONG — fails
```

### Adapter raises NotImplementedError when running /validate-thesis
Expected behavior for Massive, Alpha Vantage, Flash Alpha, ORATS adapters
in v0.1. These are Phase 1 implementation work. Mode B (validating
existing HTML tool exports) doesn't need these adapters — skip to Mode B.

### Memory.db conflicts after `git pull`
Don't commit `memory.db` to git. Ensure `.gitignore` includes it (Step 8).
If you accidentally committed it, remove from tracking:
```bash
git rm --cached memory.db
git commit -m "Stop tracking memory.db"
```

### Theses folder got committed to git
Same fix as memory.db. The `theses/` folder contains personal strategy
data and shouldn't be in version control:
```bash
git rm -r --cached theses/
git commit -m "Stop tracking theses folder"
```
