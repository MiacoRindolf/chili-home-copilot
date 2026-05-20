# f-composite-quality-event-driven (Phase 3 of adaptive-promotion-architecture)

> **Type:** New handler + backfill script + feature flag
> **Parent:** `docs/STRATEGY/QUEUED/f-adaptive-promotion-architecture-2026-05-11.md`
> **Status:** unblocked. Phase 1b/1c/2 all shipped; brain_work event queue
> is unified and outcomes flow through handlers correctly.

## Goal

Wire `quality_composite_score` as an event-driven node, not a once-per-cycle
batch. Today 584 of 586 patterns have NULL `quality_composite_score`
(Phase 0 finding). The score function exists; it just doesn't run because
the Phase 4 cohort-promote flow that triggers it is dormant.

Phase 3 makes the score event-driven so it recomputes on
`pattern_stats_updated`, `backtest_completed`, `live_trade_closed`,
`regime_evidence_updated`. The score becomes a live signal that feeds
Phase 2's adaptive gate (composite as a 4th Pareto-frontier dimension).

## Design

### Backfill (one-shot)

Script `scripts/quality-score-backfill.ps1` that computes
`pattern_quality_score.compute(pattern)` for every active pattern (586).
Establishes a baseline score distribution. Existing logic — no new model.

### Event-driven recompute (new Phase 2 handler)

New handler `app/services/trading/brain_work/handlers/quality_score.py`:

- Subscribes to: `pattern_stats_updated`, `backtest_completed`,
  `live_trade_closed`, `regime_evidence_updated`
- Reads the pattern, computes the score, writes
  `scan_patterns.quality_composite_score`
- Emits `pattern_quality_recomputed` outcome event for downstream
  consumers (Phase 2's adaptive gate will subscribe)
- Idempotent (re-runs produce same score given same inputs)
- Same import safety as other handlers — absolute imports per 2026-05-05 audit

Registered in `handlers/__init__.py`. Reuses existing
`pattern_quality_score.compute` logic (no new model).

### Wiring to Phase 2's adaptive gate

`cpcv_adaptive_gate.py` (shipped in commit `fd2e687`) currently treats
DSR, PBO, and median_sharpe as the Pareto axes. Phase 3 adds a 4th axis:
shrunken `quality_composite_score`. Behind the same
`chili_cpcv_adaptive_gate_enabled` flag (no new flag needed — once
adaptive gate is on, composite score is a free input).

### Deliverables

1. **`app/services/trading/brain_work/handlers/quality_score.py`** — new handler
2. **`app/services/trading/brain_work/handlers/__init__.py`** — register handler
3. **`scripts/quality-score-backfill.ps1`** — one-shot backfill (operator-controlled, `-DryRun` default)
4. **`app/services/trading/cpcv_adaptive_gate.py`** — wire composite as 4th Pareto axis (small edit, additive)
5. **`tests/test_handler_quality_score.py`** — idempotency + event-payload handling + missing-pattern handling
6. **`docs/runbooks/QUALITY_SCORE_HANDLER.md`** — operator runbook (how to read scores, backfill ops, rollback)
7. **`docs/STRATEGY/CC_REPORTS/2026-05-11_composite-quality-event-driven.md`**

## Hard constraints

- New handler must be **idempotent** (handler-idempotency hard gate from Phase 1b applies)
- No changes to existing handlers
- No changes to `promotion_gate.promotion_gate_passes` (legacy stays untouched)
- Adaptive gate edit is additive — when composite score is NULL for a pattern, treat as `pool_mean` (Bayesian-shrunken neutral) so the gate still works during the rollout window
- Backfill script: `-DryRun` default, per-pattern progress log, kill switch
- No autotrader / venue / broker touched

## Why this is next

Phase 1b/1c shipped means the event queue is healthy and handlers fire.
Phase 2 shipped means the adaptive gate is wrapped and shadow-loggable.
Phase 3 fills the data gap (584 NULL scores) AND enriches Phase 2 with
a 4th Pareto axis. Without Phase 3, Phase 2's adaptive gate is
3-dimensional and the composite-quality dimension stays dead.

Eventually enables `chili_cohort_promote_enabled=True` (currently OFF)
with confidence that the cohort flow has live data.

## Open questions for plan-gate consult

1. When composite score is NULL (pattern hasn't been backfilled yet),
   what does the adaptive gate do? Recommended: treat as `pool_mean`
   (Bayesian-shrunken neutral) so gate continues to work during rollout.
   Alternative: treat as "skip composite dimension entirely" (3-D
   Pareto until backfill completes).
2. Should `pattern_quality_recomputed` be `event_kind='outcome'` or
   `'work'`? Per Phase 1b unified queue: probably outcome — it's an
   audit-of-fact (the score got updated), not a task-to-do.

Brief defaults: pool_mean for NULL; event_kind='outcome'. CC should
surface in consult.

## Next in queue after Phase 3

- **Phase 4** (`f-runtime-tab-surfacing.md`) — UI changes to expose
  "PTR-ready but ungated" state + adaptive vs legacy verdict diff. To
  be written separately.
