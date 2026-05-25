---
name: report-rendering
description: >-
  How the Reporter (stage 10) builds and maintains ONE living, self-contained,
  browser-viewable HTML report per strategy — incrementally updated as the strategy
  moves through every agent (stages 1-10) and every phase (paper, live). The Reporter
  MUST use this whenever a stage completes and records a result, whenever a strategy
  report needs (re)building or refreshing, whenever a refinement iteration re-runs the
  pipeline, and whenever a phase (paper trading, broker feed) appends new state. Use it
  for EVERY strategy, not just the current one — the format and the update protocol are
  generic. Producing ad-hoc one-off reports instead of updating the canonical living
  report is the failure mode this prevents.
---

# Strategy Report Rendering (Reporter, stage 10)

You maintain a single source-of-truth report per strategy that anyone can double-click
open in a browser at ANY point in the pipeline and see exactly where things stand: what's
done, what's pending, what passed, what failed, and the full story from hypothesis to (when
it gets there) paper trading. It updates as the strategy is refined through the agents and
advances through the phases — it is not a one-shot end-of-run dump.

This restores the "open it and see the whole thing" experience of the old standalone HTML
tools, but as a clean, composable report fed by the pipeline rather than a monolithic tool.

## The model: data record + renderer (this is what makes it "living")

Two files per strategy, under `reports/<strategy>/`:

- **`report.json`** — the canonical record. Each stage writes/updates ONLY its own slice
  (plus a status and timestamp). This is append-and-update, never wipe.
- **`report.html`** — rebuilt from `report.json` by the renderer every time a slice changes.
  Self-contained; open it offline by double-click.

Because the HTML is always a pure function of `report.json`, you can rebuild it at any moment
and it reflects current state. A half-finished pipeline shows completed sections filled and
later sections marked pending — that is the point.

Keep history: bump a `version` integer on each full pipeline re-run (refinement iteration)
and snapshot `report_v<N>.html`. The current `report.html` always shows the latest; link the
snapshots so progress over refinements is auditable.

## `report.json` schema (generic — works for any strategy)

```
{
  "strategy": "<name>",
  "version": <int>,                      // bump on each pipeline re-run
  "run_mode": "A" | "B",
  "status": "<one-line headline status>",// e.g. "validated - sizing pending"
  "updated_at": "<iso>",
  "git_commit": "<short sha>",
  "stages": {
    "1_hypothesis":  { "status": <enum>, "updated_at": "...", ... },
    "2_pre_critic":  { ... },
    "3_code":        { ... },
    "4_backtest":    { ... },
    "5_stats":       { ... },
    "6_vs_random":   { ... },
    "7_validator":   { ... },
    "8_gates":       { ... },
    "9_risk":        { ... },
    "10_memory":     { ... }
  },
  "robustness": { "cost": {...}, "temporal": {...}, "regime": {...} },
  "phases":     { "4_paper": { "status": <enum>, ... }, "5_live": { "status": <enum>, ... } }
}
```

`status` enum (per stage and overall): `pending`, `running`, `done`, `pass`, `fail`,
`flagged`, `not_started`, plus `deployed` and `paused` for the live phases (Phase 4 paper /
Phase 5 live — `deployed` = actively trading; `paused` = intentionally halted, e.g. by the
kill-switch). Drive a colored chip off this in the HTML.

## What each section shows (map to the named agents)

Render a section per stage, in order, each with its status chip and `updated_at`:

1. **Hypothesis (Hypo-Refiner)** — the prose thesis and the refined JSON spec; spec-parity note if a reference exists.
2. **Pre-Critic** — kill/pass verdict + the key objections.
3. **Code (Coder Agent)** — plain-English summary of the generated `strategy()`, its params, the quarantined path, and fire-level parity vs any reference.
4. **Backtest** — headline run facts (n fires, span, universe).
5. **Stats** — performance metrics.
6. **VsRandom** — the verdict TABLE per horizon: increment, matched-random mean, gross/trade, z, p, beat-pool-median.
7. **Validator** — the 9 criteria + the placebo result.
8. **Gates** — DSR / correlation / PCA / vs_random pass/fail.
9. **Risk & Sizing** — the size recommendation (fractional Kelly level, kill-switch thresholds, caps), the sized equity curve, and tail behavior (worst month, worst year, drawdown).
10. **Memory / Final verdict** — accept/reject + logged reason, deploy/hold.

Plus a **Robustness** block (these are the gating analyses): cost-survival breakeven per
horizon; temporal stability per year (flag inversions); regime diagnosis (the conditioner
verdict). And a **Phases** block: Phase 4 paper-trade tracking (equity, fills, kill-switch
state) and Phase 5 live — `not_started` until they exist.

## Rendering conventions

- **Self-contained, offline-safe.** One `.html`, no external CSS/JS/CDN dependencies. It must
  open by double-click on Windows with no internet. (Stan's environment.)
- **Charts as inline SVG baked at render time** — no runtime JS, no CDN. Support at least:
  a line chart (sized equity curve), and bar charts (per-year stability; cost breakeven by
  horizon). Generate the SVG strings in the renderer from the data. Interactive (Chart.js via
  CDN) is an optional later upgrade, never the default.
- **Print/PDF-friendly.** Clean page flow so Ctrl-P -> "Save as PDF" yields a usable document
  (this gives browser-view AND PDF for free).
- **Aesthetic:** flat, clean, dark text on light, generous whitespace, a header band with the
  strategy name + overall status chip + version + timestamp + git commit + run mode. Status
  chips: green (pass/done), blue (deployed — live/paper active), amber (flagged/pending/paused),
  red (fail), gray (not_started). No heavy borders, no gradients. Tables for numbers; SVG for
  shapes.
- **Honest about incompleteness:** pending/not-started sections render visibly as such — never
  hide them. A reader must see what's left to do.

## Incremental update protocol (every contributing stage follows this)

1. Read `reports/<strategy>/report.json` (create it on stage 1 if absent).
2. Write/overwrite ONLY this stage's slice under `stages.<n>_<name>` (or `robustness.*` /
   `phases.*`), set its `status` and `updated_at`. **Never touch another stage's slice.**
3. Update the top-level `status`, `updated_at`, and `git_commit`.
4. Re-render `reports/<strategy>/report.html` from the full `report.json`.
5. On a full pipeline re-run (refinement), bump `version` and snapshot the previous
   `report.html` to `report_v<prev>.html` before overwriting.

This is why the report is "live": it is rebuilt from the record after every stage, so it is
always current without anyone assembling it by hand.

## File layout

```
reports/<strategy>/
├── report.json          # canonical record (source of truth)
├── report.html          # current rendered report (open this)
└── report_v<N>.html      # snapshots of prior refinement iterations
```

gitignore large data, but COMMIT `report.json` and `report.html` — they are the auditable
decision record (small, text). They belong in version control, like the memory log.

## Worked example — skew-consensus (current state)

Given what exists, the report would show: Hypothesis (done), Pre-Critic (pass), Code
(pending — Mode A not yet run), VsRandom (pass: 21d increment +18.3 bps, gross +106.5, z 9.12,
beat-median 51.6%), Robustness/cost (breakeven 21d 106.5 / 10d 33.3 / 5d 15.0 bps — 5d dies),
Robustness/temporal (per-year bars; 2020 flagged, -41.9 bps inversion), Robustness/regime
(verdict: accept + kill-switch, no gate), Risk & Sizing (pending), Phases (not_started).
Overall status chip: "validated - sizing pending."

## Checklist before returning

1. `report.html` is self-contained and opens offline (no CDN/external refs).
2. Only this stage's slice was written; all other slices preserved.
3. Pending / not-started sections are visibly marked, not hidden.
4. Header carries strategy, overall status chip, version, timestamp, git commit, run mode.
5. Numbers in tables; charts as inline SVG; page prints cleanly to PDF.
6. `report.json` + `report.html` committed.
