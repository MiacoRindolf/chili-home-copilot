# NEXT_TASK: f-runtime-tab-surfacing

STATUS: PENDING

## Goal

**Phase 4 of the adaptive-promotion-architecture initiative (FINAL).**
Surface the new gate machinery in the brain runtime tab: PTR-ready-but-ungated
patterns, adaptive vs legacy CPCV verdict diff, composite quality scores,
and brain_work_events queue depth.

## Brief

`docs/STRATEGY/QUEUED/f-runtime-tab-surfacing.md`

## Deliverables

1. 3 new read-only FastAPI endpoints in brain router
2. 2 new sections in the runtime tab template
3. Endpoint tests
4. CC_REPORT

## Hard constraints

- Read-only endpoints, no DB writes
- No autotrader / venue / broker / promotion_gate touched
- No new tables or migrations
- HTMX/vanilla JS only

## Consult gate

1. Confirm actual runtime-tab template path (brief assumes
   `app/templates/brain_runtime.html`)
2. Polling cadence (brief assumes 10s queue, on-demand for tables)

CC should surface in plan-gate consult.

## After this

Original architecture arc complete. Optional follow-ups (operator-directed):
- Run Phase 1c backfill (`scripts/brain-event-backfill.ps1`)
- Run Phase 3 backfill (`scripts/quality-score-backfill.ps1`)
- Flip `chili_cpcv_adaptive_gate_enabled=1`
- Dev-system reliability fixes (5 items I flagged separately)
