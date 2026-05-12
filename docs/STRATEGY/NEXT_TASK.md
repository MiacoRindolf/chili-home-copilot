# NEXT_TASK: f-composite-quality-event-driven

STATUS: DONE

## Goal

**Phase 3 of the adaptive-promotion-architecture initiative.** Wire
`quality_composite_score` as an event-driven node + one-shot backfill
of the 584 NULL scores + add composite as a 4th Pareto axis in Phase 2's
adaptive gate.

## Brief

`docs/STRATEGY/QUEUED/f-composite-quality-event-driven.md`

## Why this is next

Phases 0, 1a, 1b, 1c, 2 all shipped. Composite quality score is dormant
(584/586 patterns NULL — Phase 0 finding). Phase 3 makes it live without
new model — just event-driven recompute + backfill + Phase 2 integration.

## Deliverables (per brief)

1. `app/services/trading/brain_work/handlers/quality_score.py` — new handler
2. Register in `handlers/__init__.py`
3. `scripts/quality-score-backfill.ps1` — one-shot, `-DryRun` default
4. Wire composite as 4th Pareto axis in `cpcv_adaptive_gate.py`
5. `tests/test_handler_quality_score.py`
6. `docs/runbooks/QUALITY_SCORE_HANDLER.md`
7. `docs/STRATEGY/CC_REPORTS/2026-05-11_composite-quality-event-driven.md`

## Hard constraints

- Handler must be idempotent (Phase 1b's hard-gate test pattern applies)
- No changes to existing handlers, promotion_gate, or autotrader/broker/venue
- Adaptive gate edit is additive (treat NULL composite as pool_mean during rollout)
- Backfill `-DryRun` default + kill switch + per-pattern progress log

## Consult gate (2 design questions)

1. NULL composite → pool_mean (default) vs skip dimension entirely?
2. `pattern_quality_recomputed` as outcome (default) vs work event kind?

CC should surface in plan-gate consult.
