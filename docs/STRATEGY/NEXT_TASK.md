# NEXT_TASK: f-brain-runtime-tab-redesign

STATUS: DONE

## DONE — 2026-05-23

Shipped as four phase commits on `main`:

1. `9846455` — `feat(brain-runtime): unified sticky header + thesis line (phase A)`
2. `f3968fd` — `feat(brain-runtime): edge + activity cards above the fold (phase B)`
3. `d163b06` — `feat(brain-runtime): unified drilldown tabstrip (phase C)`
4. `0c2d10a` — `chore(brain-runtime): remove deprecated runtime selectors (phase D)`

The Runtime tab now renders the single hierarchy the brief asked for: one sticky `bx-runtime-header` (status dot, Start/Stop/Wake/Pause, regime + breaker chips, last-cycle, scheduler info, queue pill, help button), one `bx-thesis-line`, a two-column `bx-above-fold` grid (Today's Edge | Brain Activity), one `bx-drilldown-tabs` strip (Patterns/Cycles/Operations/Research/Analytics/Debug), and a slide-in `bx-diagnostics-drawer` reachable from the queue pill / pattern-gate summary / Open diagnostics button. The operator-desk-hero, summary-bar, runtime-gates section header, and the three-panel Overview/Opportunities/Deep-Dive nav are all gone; `git grep` is zero for `brain-summary-bar`, `brain-section-nav`, `brain-content-panel`, `operator-desk-hero`, `desk-runtime-details`, `bsb-`, `odh-`, `brain-status-bar`, `brain-runtime-gates-row`, `brain-runtime-gates-tab`. Smoke (HTTP 200 after each phase restart) and the 6-new-id curl check pass. Five of six `tests/test_brain_runtime_endpoints.py` pass — the one failure is a pre-existing `_insert_ptr_rows` data race on CURRENT_TIMESTAMP, untouched by this redesign. Report at `docs/STRATEGY/CC_REPORTS/2026-05-23_f-brain-runtime-tab-redesign.md`; screenshots in the sibling `2026-05-23_runtime-tab-redesign-screens/` directory.

---

## Goal

Redesign the **Runtime** sub-tab of the Trading Brain page (`/brain` → trading domain → "Runtime" tab) so that an operator opening it sees, without overlap or redundancy:

1. **One** runtime-health header (worker status, Start/Stop/Wake controls, regime, breaker, last cycle, queue health) — currently shown in 3 different places.
2. **One** thesis line — the brain's current call in plain English.
3. **Two columns above the fold**: "Today's Edge" (compact opportunity tiers + spec movers) on the left, "Brain Activity" (current step + dispatch queue health + pattern-gate diagnostics) on the right.
4. **One** drill-down tab strip below the fold: `Patterns / Cycles / Operations / Research / Analytics / Debug`. Everything that is not above-the-fold lives here.

The current layout stacks four navigational layers (Network/Runtime tabstrip → sticky summary bar → Overview/Opportunities/Deep-Dive section nav → bdd-tabs) and shows the same worker/regime/last-cycle data in three places. This task collapses that into the single hierarchy above without losing any data binding.

## Why now

Operator is asking for it. He has been working all session inside the runtime tab driving the position-identity refactor and the trading-brain audits; the duplicated worker-status rows, the buried Start/Stop/Wake controls inside `<details>`, and the always-on "runtime gates" header above every panel are slowing him down. None of the underlying brain machinery needs to change — this is purely a UX/IA pass on the templates, CSS, and the tab-switch JS.

## Brain integration (reuse, don't rewrite)

All data endpoints stay as-is. **Do not** touch any router code. The redesign rewires DOM only.

Existing endpoints already wired and used (keep them):
- `/api/brain/patterns/ptr-ready-but-ungated`
- `/api/brain/patterns/cpcv-verdict-diff`
- `/api/brain/dispatch-queue-depth`
- Worker control endpoints used by `startBrainWorker / stopBrainWorker / wakeBrainWorker / pauseBrainWorker`
- Opportunity board, playbook, perf, shadow-promoted, predictions, tradeable, research-edge loaders
- All `loadXxx()` functions inside `brain-trading-desk.js`, `brain-trading-deep-dive.js`, `brain-core.js`

Existing render functions to preserve (call from the new DOM ids):
- `updateBrainSummaryBar(data)` in `brain-core.js:947` — repoint to the new header element ids; keep the signature
- `switchDeepDiveTab(tab)` in `brain-trading-deep-dive.js:5` — repoint to the new unified drill-down tabs
- `brainRuntimeGates.refreshPatterns()` and `brainRuntimeGates.start()` in `_trading_runtime_gates.html` — keep; they now feed cards inside Brain Activity instead of standalone section
- `setTradingBrainSubtab('runtime'|'network')` in `brain-trading-graph.js:1` — unchanged (Network/Runtime tabstrip stays)

Functions / interactions to **remove** (replaced):
- `switchBrainPanel(panelId)` in `brain-core.js:934` — overview/opportunities/deep-dive panels are collapsing into one continuous view. Delete the function and its `data-panel` driven button keyboard nav at `brain-components.js:166`.
- `brainEnsurePanelLoaded(panelId)` lazy-load gates in `brain-core.js:978` — replace with one boot routine that fires the loaders for the above-the-fold cards on Runtime activation, plus per-drill-down-tab loaders on first click.

## Target structure (DOM contract)

Below is the new structure inside `<div id="trading-brain-pane-output">`. DOM ids in **bold** are NEW; everything else either keeps its id or has a 1:1 rename noted. Render functions read these ids — do not deviate.

```
trading-brain-pane-output
├── bx-runtime-header                         (sticky, was brain-summary-bar + bsb-row)
│   ├── bx-status-dot          (was bsb-summary-dot + bw-status-dot — keep ONE)
│   ├── bx-status-label        (was bsb-summary-worker + bw-status — keep ONE)
│   ├── bx-controls            (was bw-controls — promoted out of <details>)
│   │   ├── bw-start-btn       (id unchanged; same onclick)
│   │   ├── bw-wake-btn        (id unchanged)
│   │   ├── bw-pause-btn       (id unchanged)
│   │   └── bw-stop-btn        (id unchanged)
│   ├── bx-regime-chip         (was bsb-summary-regime + bsb-regime — keep ONE)
│   ├── bx-breaker-chip        (was bsb-breaker)
│   ├── bx-last-cycle          (was bsb-summary-cycle + bsb-last-cycle — keep ONE)
│   ├── bx-queue-pill          (NEW: compact dispatch-queue health, click → opens drawer)
│   └── bx-help-btn            (NEW: replaces the "How to read this desk" <details>; opens a modal with that copy)
│
├── bx-thesis-line             (was odh-thesis-line — kept verbatim, one line, no badge stack)
│
├── bx-above-fold (2-col grid)
│   ├── bx-edge-card (left col)
│   │   ├── Tier A list        (id: opp-tier-a — unchanged)
│   │   ├── Tier B list        (id: opp-tier-b — unchanged, collapsible)
│   │   ├── Tier C list        (id: opp-tier-c — unchanged, collapsible)
│   │   ├── Tier D list        (id: opp-tier-d — unchanged, hidden by default)
│   │   ├── opp-stale-banner   (unchanged, but lives inside this card)
│   │   ├── opp-refresh-failed-banner (unchanged)
│   │   └── spec-movers-section (unchanged, but rendered as a collapsed accordion at the bottom of this card)
│   │
│   └── bx-activity-card (right col)
│       ├── bx-current-step    (was bw-current-step / brain-learning-step — merged)
│       ├── bx-progress-bar    (was brain-progress-bar — kept; same data binding)
│       ├── brain-work-ledger-strip (unchanged id, kept; promoted out of nested <details>)
│       ├── bx-queue-summary   (NEW: pending / processing / retry / dead pills feeding from same endpoint)
│       ├── bx-pattern-gate-summary (NEW: "3 stuck · 1 disagree" headline; click → drawer)
│       └── bx-open-diagnostics-btn (NEW: opens diagnostics drawer)
│
├── bx-drilldown-tabs          (was bdd-tabs — promoted to top-level; section-nav and panel divs removed)
│   ├── Patterns  (id: bx-tab-patterns,  data-bx="patterns")
│   ├── Cycles    (id: bx-tab-cycles,    data-bx="cycles")
│   ├── Operations(id: bx-tab-ops,       data-bx="ops")
│   ├── Research  (id: bx-tab-research,  data-bx="research")
│   ├── Analytics (id: bx-tab-analytics, data-bx="analytics")
│   └── Debug     (id: bx-tab-debug,     data-bx="debug")
│
├── bx-drilldown-content       (was bdd-content; the existing bdd-panel divs move under here verbatim — keep ids brain-patterns, bw-stats-grid, brain-activity, brain-thesis-card, etc.)
│
├── bx-research-extras         (NEW container under the Research tab — re-home from the deleted "Opportunities" panel:)
│   ├── brain-playbook-section
│   ├── brain-perf-section
│   ├── brain-shadow-promoted-section
│   ├── brain-predictions (Live Predictions)
│   ├── brain-tradeable-section
│   └── brain-research-edge-section
│
└── bx-diagnostics-drawer      (NEW slide-in panel from right; hidden by default)
    ├── Pattern Gate Status — Stuck patterns sub-view  (existing tbody id brain-pattern-gate-stuck-tbody)
    ├── Pattern Gate Status — Verdict diff sub-view    (existing tbody id brain-pattern-gate-diff-tbody)
    └── Dispatch Queue Health — full bucket table      (existing tbody id brain-dispatch-queue-tbody)
```

## Files to touch (exhaustive)

Templates (rewrite scope; preserve all referenced ids unless noted):

1. `app/templates/brain.html` — replace the inner Runtime pane from line 33 (`<div id="trading-brain-pane-output">`) through line 95 (`</div><!-- .trading-brain-body-row -->`) with the new structure above. Keep the Network pane, the Reasoning domain, the Context domain, and the modal include exactly as they are.
2. `app/templates/brain/_trading_runtime_gates.html` — DELETE the section element wrapper; KEEP the `<script>` IIFE (`brainRuntimeGates` global). Move the markup tables into `_trading_diagnostics_drawer.html` (new file). The script should keep working unchanged because it targets ids by getElementById.
3. `app/templates/brain/_trading_operator_desk.html` — DELETE entirely. Its useful content goes:
   - `odh-thesis-line` → `bx-thesis-line` (one element only, no surrounding hero)
   - Worker controls + status row → `bx-runtime-header` (controls already include `bw-start-btn` etc.; preserve)
   - `brain-work-ledger-strip` → into `bx-activity-card`
   - `brain-reconcile-pipeline-details` learning-step span + progress bar → into `bx-activity-card`
   - `desk-help-details` "How to read this desk" copy → into a help modal triggered by `bx-help-btn` (new file: `_runtime_help_modal.html`)
   - `tb-momentum-neural-strip` → drop. It's display:none in the default state and unused on Runtime (the network tab has its own equivalent at `tbn-momentum-desk-panel`).
   - `odh-badges-row`, `odh-governance-hint`, `odh-fresh-row`, `odh-top3`, `odh-evidence-strip`, `odh-trust-panel`, `bsb-opp-summary-row` → drop. These are stale or redundant with the new edge/activity cards.
   - `brain-reflection` (AI Learning Report floating box) → keep but include from `brain.html` directly (move out of operator desk).
4. `app/templates/brain/_trading_opportunities.html` — refactor: the edge tiers move into `bx-edge-card`. `brain-edge-grid` (playbook + perf) + `brain-shadow-promoted-section` move out into `bx-research-extras` under the Research drill-down tab. `speculative-movers-section` becomes a collapsed accordion inside `bx-edge-card`.
5. `app/templates/brain/_trading_sections.html` — relocate all three sections (`brain-predictions`, `brain-tradeable-section`, `brain-research-edge-section`) into `bx-research-extras`. This file can be deleted after the move; update the include in `brain.html`.
6. `app/templates/brain/_trading_deep_dive.html` — rename the outer container ids: `bdd-tabs` → `bx-drilldown-tabs`, `bdd-content` → `bx-drilldown-content`, `bdd-tab` class → `bx-drilldown-tab`, `bdd-panel` class → `bx-drilldown-panel`. The inner panel ids (`bdd-patterns`, `bdd-cycles`, `bdd-ops`, `bdd-research`, `bdd-analytics`, `bdd-debug`) stay. Add a new `bx-research-extras` block at the top of the Research panel containing the relocated playbook/perf/shadow/predictions/tradeable/research-edge sections.
7. `app/templates/brain/_trading_diagnostics_drawer.html` — NEW. Contains the three diagnostic tables (Pattern Gate stuck / Pattern Gate diff / Dispatch Queue buckets). Slide-in from the right; closes on Escape or backdrop click. Include from `brain.html`.
8. `app/templates/brain/_runtime_help_modal.html` — NEW. Contains the "How to read this desk" copy reformatted as a small modal with sections: Worker controls, Today's Edge tiers (A/B/C/D meaning), Brain Activity card, Diagnostics drawer. Include from `brain.html`.

CSS:

9. `app/static/css/brain-trading.css` — add a new "Runtime tab redesign 2026-05-23" section near the top of the file with the new classes (`.bx-runtime-header`, `.bx-status-dot`, `.bx-controls`, `.bx-regime-chip`, `.bx-breaker-chip`, `.bx-queue-pill`, `.bx-help-btn`, `.bx-thesis-line`, `.bx-above-fold`, `.bx-edge-card`, `.bx-activity-card`, `.bx-drilldown-tabs`, `.bx-drilldown-tab`, `.bx-drilldown-panel`, `.bx-diagnostics-drawer`). Use existing tokens from `brain-tokens.css` — no new color literals. Mark `.brain-summary-bar`, `.brain-section-nav`, `.brain-content-panel`, `.brain-runtime-gates-row`, `.brain-runtime-gates-tab`, `.operator-desk-hero`, `.odh-*`, `.desk-runtime-details`, `.brain-status-bar`, `.bsb-row`, `.bsb-center`, `.bsb-right`, `.bsb-left`, `.bsb-chip`, `.bsb-progress`, `.bsb-meta`, `.bsb-dot`, `.bsb-item`, `.bsb-divider`, `.bsb-item-label`, `.bsb-item-value` and any other now-unused selectors as `/* DEPRECATED 2026-05-23 — runtime tab redesign */` then DELETE them at end of the file once you've confirmed no remaining references. Same for the `.bdd-*` class aliases that now have `.bx-drilldown-*` replacements.
10. Mobile rules at the bottom of the file: update the `@media (max-width: 768px)` block to target the new `.bx-runtime-header`, `.bx-above-fold` (single column on mobile), `.bx-drilldown-tabs`, etc. Preserve the 44px tap-target sizing.

JS:

11. `app/static/js/brain-core.js` — DELETE `switchBrainPanel`, `brainEnsurePanelLoaded`, and the `brainApplyInitialView()` overview/opportunities/deep-dive branching. Replace with a single `brainBootRuntime()` that:
    - Fires `loadOpportunityBoard()`, `loadShadowPromotedPatterns()` (now under Research tab — defer), `loadBrainWorkerStatus()`, `brainRuntimeGates.start()`, `brainRuntimeGates.refreshPatterns()` on Runtime activation
    - Lazy-loads per drill-down tab inside a new `_brainDrilldownLoaded` map (`patterns/cycles/ops/research/analytics/debug` — same content as the existing `switchDeepDiveTab` triggers but extended with the relocated Research-tab loaders: `loadPlaybook`, `loadPerfDashboard`, `loadBrainPredictions`, `loadTradeablePatterns`, `loadResearchEdgePatterns`)
    - Repoint `updateBrainSummaryBar(data)` to read the new ids: `bx-status-dot`, `bx-status-label`, `bx-regime-chip`, `bx-last-cycle`. Keep the signature; existing pollers call this and need to keep working.
12. `app/static/js/brain-components.js` — update the keyboard nav at line 167 to target `bx-drilldown-tabs` instead of `brain-section-nav`. Behavior identical.
13. `app/static/js/brain-trading-desk.js` — find every place that writes to `bsb-*` or `bw-status` ids; repoint to the new `bx-*` ids. Find every place that writes to `desk-runtime-details` or operator-desk-hero ids and either delete (badges row, evidence strip, trust panel, top3) or repoint (thesis line → `bx-thesis-line`). Keep the worker-status polling cadence unchanged.
14. `app/static/js/brain-trading-deep-dive.js` — update `switchDeepDiveTab(tab)` to target the new tab elements (`#bx-tab-<tab>` and `#bdd-<tab>` panel ids stay). Preserve the per-tab `if (!_bddLoaded[tab])` lazy-load gates. Add new gate entries for the Research-tab relocated sections.
15. NEW: `app/static/js/brain-diagnostics-drawer.js` — small module: open/close the drawer, populate by reusing `brainRuntimeGates.refreshPatterns()` + the queue-depth polling already running. Bind to `bx-open-diagnostics-btn` click and `bx-queue-pill` click and `bx-pattern-gate-summary` click. Include in `brain.html`.

## Constraints / do not touch

- **No router or service changes.** This is template + CSS + JS only.
- **No endpoint signatures change.** All `/api/brain/*` and worker-control endpoints stay.
- **Preserve every render function's read/write contract.** If a function writes to `#brain-patterns`, that id must still exist somewhere visible inside the drill-down. Rule of thumb: rename containers, never rename ids that JS reads. The audit ids that must be preserved verbatim: `bw-start-btn`, `bw-wake-btn`, `bw-pause-btn`, `bw-stop-btn`, `brain-work-ledger-text`, `brain-work-ledger-strip`, `brain-progress-bar`, `brain-learning-step`, `brain-learning-progress`, `brain-patterns`, `bw-stats-grid`, all `bw-stat-*`, `bw-cpcv-shadow-body`, `bw-regime-heatmap-body`, `bw-queue-*`, `bw-cycle-chart`, `bw-activity`, `brain-activity`, `brain-pipeline`, `brain-kpis`, `brain-thesis-card`, `thesis-stance`, `thesis-text`, `thesis-gauge`, `thesis-meta`, `opp-tier-a` through `opp-tier-d`, `opp-tier-a-wrap` through `opp-tier-d-wrap`, `opp-more-a` through `opp-more-c`, `opp-stale-banner`, `opp-refresh-failed-banner`, `opp-no-trade`, `opp-operator-strip`, `opp-op-sum`, `spec-movers-body`, `spec-movers-methodology`, `brain-playbook-content`, `brain-perf-content`, `brain-shadow-promoted-patterns`, `brain-predictions`, `brain-tradeable-patterns`, `brain-research-edge-patterns`, `brain-pattern-gate-stuck-tbody`, `brain-pattern-gate-diff-tbody`, `brain-pattern-gate-status`, `brain-pattern-gate-diff-summary`, `brain-dispatch-queue-tbody`, `brain-dispatch-queue-health-dot`, `brain-dispatch-queue-oldest`, `brain-dispatch-queue-updated`, `brain-dq-total-pending`, `brain-dq-total-processing`, `brain-dq-total-retry`, `brain-dq-total-dead`, `brain-reflection`, `brain-reflection-content`, `bdd-patterns` through `bdd-debug` panel ids, `cycle-report-*`, `pat-toolbar`, `pat-sort`, `pat-search`, `pat-chip`, `pat-count`, `bf-progress-bar`, `bf-progress-text`, `backfill-banner`, `brain-near-tradeable-candidates`, `brain-cycle-digest`, `brain-filter-bar`, `brain-stop-decisions`, `ops-profile-select`, `ops-profile-status`, `bw-help`, `bw-queue-debug-link`, `bw-uptime`, `bw-cycles`, `bw-current`, `bw-current-step`, `bw-current-progress`, `bw-step-timings`, `bw-last-cycle-timings`, `bw-cycle-chart-legend`, `bdd-inspect-health-body`, `bdd-debug-opp-json`, `brain-research-funnel`, `brain-proposal-skips`, `brain-research-kpi-benchmarks`, `brain-pipeline-near`, `brain-chart`.
- **Do not flip any feature flags.** This is a UI change, not a behavior change.
- **Do not change the Network sub-tab.** The `tb-tabstrip` Network/Runtime toggle and the neural mesh SVG stay exactly as they are.
- **Do not change the Reasoning or Context domains.** Same file, different `domain-*` divs — leave them alone.
- **Do not touch the brain-project-domain** include or its JS.

## Out of scope

- Any behavior change to the worker, dispatch, gates, or opportunity board logic.
- Any new API endpoints. The diagnostics drawer reuses what `brainRuntimeGates` already fetches.
- Dark/light theme work — brain-tokens.css already handles both; reuse tokens.
- Mobile redesign beyond the existing breakpoints. Make sure the new layout *works* at ≤768px; do not redesign mobile from scratch.
- Replacing chart libraries.
- Touching the Patterns/Cycles/Ops/Research/Analytics/Debug **content** — only their container shell moves. The tables, charts, and filter bars inside each drill-down panel stay verbatim.

## Success criteria

Bundle into one commit per phase below. After each phase: server restart, manual smoke, commit.

**Phase A — header + thesis (commit 1):**
1. Open `/brain` → trading domain → Runtime tab. The sticky header at top shows: status dot, "Running"/"Stopped"/"Idle" label, Start/Stop/Wake/Pause buttons visible by default, regime chip, breaker chip when tripped, last-cycle text, queue health pill. ZERO duplicate worker status anywhere on the page below the header.
2. Below the header: a single thesis line (no badges row, no narrative wall, no top3, no evidence strip, no trust panel).
3. `desk-runtime-details` is gone. Start/Stop/Wake work from the header.
4. `brain-summary-bar` is gone. `updateBrainSummaryBar()` writes to the new header ids; calls succeed silently when polled.

**Phase B — above-the-fold cards (commit 2):**
1. Two-column grid below the thesis: left = Today's Edge with Tier A/B/C/D and spec-movers collapsed accordion; right = Brain Activity with current step, work ledger, queue summary (pending/processing/retry/dead pills), and pattern-gate summary.
2. The runtime-gates section header is gone. Pattern Gate + Dispatch Queue still load (script preserved); their tables now live in the diagnostics drawer.
3. `bx-open-diagnostics-btn` opens a right-side drawer with the Pattern Gate stuck/diff tables and the Dispatch Queue buckets table. Backdrop click + Escape close.

**Phase C — drill-down tabs (commit 3):**
1. ONE tab strip below the above-the-fold: Patterns / Cycles / Operations / Research / Analytics / Debug.
2. The Overview/Opportunities/Deep-Dive section nav is gone. `switchBrainPanel` is deleted; no console errors.
3. Research tab now contains, in this order: Daily Playbook, P&L Performance, Shadow-promoted patterns, Live Predictions, Tradeable Patterns, Edge research lane — followed by the original Research panel content (pipeline / funnel / proposal skips / KPI benchmarks / pipeline-near / kpis / confidence history chart).
4. All other drill-down tabs render identically to before (Patterns toolbar + chips + list; Cycles activity timeline + cycle reports flip + stop engine decisions; Ops worker controls help + stats grid + CPCV shadow + regime heatmap + queue + chart; Analytics market thesis + content; Debug inspect-health + raw JSON).

**Phase D — CSS cleanup (commit 4):**
1. All deprecated selectors removed from `brain-trading.css`. `git grep` finds zero references to: `brain-summary-bar`, `brain-section-nav`, `brain-content-panel`, `operator-desk-hero`, `desk-runtime-details`, `odh-*`, `bsb-*`, `brain-status-bar`, `brain-runtime-gates-row`, `brain-runtime-gates-tab`.
2. New `bx-*` classes use only tokens from `brain-tokens.css` (run `grep -E 'rgb\(|rgba\(|#[0-9a-fA-F]{3,8}' app/static/css/brain-trading.css` and confirm no NEW literal colors were introduced in the `bx-*` rules — token usage only).
3. Page works at 1440px desktop, 1024px laptop, 768px tablet, 380px mobile (single-column collapse).

**Phase E — verification (no commit, just smoke + screenshots):**
1. `conda activate chili-env && .\scripts\start-https.ps1` brings the app up clean.
2. Hit `https://localhost:8000/brain`. Trading domain → Runtime tab loads with no console errors.
3. Worker Start button starts the brain worker (verify `docker ps | findstr brain-worker` shows Running).
4. Open diagnostics drawer; tables populate.
5. Click each drill-down tab; first-click triggers the lazy load (network tab in devtools shows the corresponding fetches).
6. `pytest tests/test_brain_runtime_endpoints.py -v` still passes (no test should reference removed DOM ids; if any does, mark it for follow-up but do not let it fail the run).
7. Take 4 screenshots into `docs/STRATEGY/CC_REPORTS/2026-05-23_runtime-tab-redesign-screens/`:
   - `01_header_and_thesis.png` — sticky header + thesis
   - `02_above_fold.png` — full above-the-fold (header + thesis + edge card + activity card)
   - `03_diagnostics_drawer.png` — drawer open over the above-the-fold
   - `04_drilldown_research.png` — Research tab expanded showing the relocated sections
8. Write the CC report at `docs/STRATEGY/CC_REPORTS/2026-05-23_f-brain-runtime-tab-redesign.md`.

## Rollback plan

- Each phase ships as its own commit. To revert: `git revert <commit>` in reverse order (D → C → B → A).
- No DB migrations, no flag flips, no broker behavior change. Rollback is purely a frontend revert.
- The deprecated CSS classes can be restored from git history if a downstream template I didn't catch was using them.
- All preserved DOM ids mean the existing JS continues to find its targets; if something looks wrong post-deploy, restart the browser hard (Ctrl+Shift+R) to bypass cached assets before reverting.

## Reference

- Old runtime gates report: `docs/STRATEGY/CC_REPORTS/2026-05-11_runtime-tab-surfacing.md` (Phase 4 of adaptive-promotion-architecture — added the runtime gates section the redesign now folds in).
- Operator desk template that's getting deleted: `app/templates/brain/_trading_operator_desk.html` (92 lines). Its useful pieces are listed in "Files to touch" §3 above.
- Main page: `app/templates/brain.html`. Trading domain block runs lines 31–96.
- The `brainRuntimeGates` IIFE inside `_trading_runtime_gates.html` (lines 126–346 of the current file) is the polling loop you keep; just relocate the markup it reads.

## Operator notes

- Run everything via the dispatch daemon. The redesign is server-restart-heavy (one restart per phase commit). Do not defer restarts to the operator — `docker compose up -d --force-recreate chili` is daemon-runnable.
- If a removed `<details>` collapsed copy of a status row was load-bearing for some accessibility or test path that I missed, leave it commented with `<!-- TODO 2026-05-23 verify no a11y / test impact -->` rather than deleting, and flag it in the CC report's Open Questions.
