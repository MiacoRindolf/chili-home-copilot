# f-runtime-tab-surfacing (Phase 4 of adaptive-promotion-architecture)

> **Type:** UI / FastAPI surface changes
> **Parent:** `docs/STRATEGY/QUEUED/f-adaptive-promotion-architecture-2026-05-11.md`
> **Status:** unblocked. Phases 0/1a/1b/1c/2/3 all shipped.

## Goal

Surface the new gate machinery in the brain trading-domain runtime tab
so the operator can see:

1. **PTR-ready-but-ungated patterns** — patterns with ≥30 PTR rows whose
   CPCV verdict hasn't been produced yet (the residue of the dispatcher
   silence Phase 1a found). Distinct from "no data" patterns.
2. **Adaptive vs legacy CPCV verdict diff** — for the 39 patterns with
   CPCV data, show both gate verdicts side-by-side so operator can
   sanity-check before flipping `chili_cpcv_adaptive_gate_enabled`.
3. **Quality composite score** — for the 2 patterns with non-null
   composite scores (and any backfilled by Phase 3), expose the value
   + when it was last recomputed.
4. **Brain-worker dispatch queue depth** — per event_type counts of
   pending / processing / done. Critical for spotting Phase 1b
   regressions (e.g. handler stops claiming) without log-grepping.

## Design

### Backend additions (FastAPI)

Three new endpoints under `app/routers/brain.py` (or
`brain_project.py` — whichever currently serves the runtime tab):

1. `GET /api/brain/patterns/ptr-ready-but-ungated`
   - Returns: `[{pattern_id, name, ptr_rows, lifecycle_stage,
                  has_cpcv_data, ensemble_failed_at?, last_backtest_at}]`
   - Source: scan_patterns LEFT JOIN trading_pattern_trades aggregate

2. `GET /api/brain/patterns/cpcv-verdict-diff`
   - Returns: `[{pattern_id, legacy_pass, adaptive_pass, shrunken_dsr,
                  shrunken_pbo, shrunken_med_sharpe, composite_score,
                  pareto_dominant}]`
   - Source: `cpcv_adaptive_eval_log` (Phase 2 shadow log) joined with
     `scan_patterns.promotion_gate_passed`

3. `GET /api/brain/dispatch-queue-depth`
   - Returns: `[{event_kind, event_type, status, count, oldest_pending_age_seconds}]`
   - Source: `brain_work_events` aggregate WHERE
     `domain='trading' AND status IN ('pending','processing','retry_wait','dead')`

All three are read-only, no DB writes, no autotrader interaction.

### Frontend additions

Two new sections in the runtime tab (`app/templates/brain_runtime.html`
or equivalent — check the template path):

**Section A: "Pattern Gate Status"** — table with two sub-views toggled
via a tab control:
- "Stuck patterns" → ptr-ready-but-ungated endpoint
- "Verdict diff" → cpcv-verdict-diff endpoint with legacy/adaptive
  badge columns

**Section B: "Dispatch Queue Health"** — small dashboard panel:
- Total pending count
- Per-event_type breakdown (especially backtest_completed,
  pattern_eligible_promotion)
- Oldest pending age in human-readable form (e.g., "3m 12s")
- Color: green if oldest <2min, yellow <10min, red >10min

### Deliverables

1. **`app/routers/brain.py` (or brain_project.py)** — three new endpoints, read-only
2. **`app/templates/brain_runtime.html`** (or the actual template file) —
   two new sections with HTMX/vanilla JS to poll the endpoints
3. **`tests/test_brain_runtime_endpoints.py`** — read-only endpoint tests
4. **`docs/STRATEGY/CC_REPORTS/2026-05-11_runtime-tab-surfacing.md`**

## Hard constraints

- Read-only endpoints. No DB writes.
- No autotrader / venue / broker / promotion_gate touched.
- No new tables or migrations (use the existing
  `cpcv_adaptive_eval_log` from Phase 2 + `brain_work_events` +
  `scan_patterns`).
- HTMX-or-vanilla-JS only (no new frontend framework dependency).
- Endpoints must handle the case where flag-gated tables/columns are
  empty (e.g., `cpcv_adaptive_eval_log` empty until flag flips).
- Templates: check if `brain_runtime.html` exists; if not, the relevant
  template is wherever the trading-domain runtime tab is rendered.

## Why this is last

Phases 0–3 built the machinery. Phase 4 makes it observable. Without
Phase 4 the operator has to SQL the DB to know what the new gates are
doing. With Phase 4 the runtime tab tells the story.

## Open questions for plan-gate consult

1. Locate the actual runtime-tab template. Brief assumes
   `app/templates/brain_runtime.html` but CC should confirm and adjust.
2. Polling cadence. Brief assumes 10s for queue-depth and on-demand
   refresh for the pattern tables. Operator may want faster/slower.

Brief defaults: locate-template-first; 10s queue poll, on-demand refresh
for tables.
