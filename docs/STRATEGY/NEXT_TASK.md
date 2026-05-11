# NEXT_TASK: f-brain-event-kind-unify

STATUS: DONE

Completed 2026-05-11 — see
`docs/STRATEGY/CC_REPORTS/2026-05-11_brain-event-kind-unify.md` for the
full report (4 consult-gated deviations applied, 17 tests passing,
migration 238 landed, flag default-off so merge produces zero behaviour
change).

## Goal

**Phase 1b of the adaptive-promotion-architecture initiative.** Fix the
architectural defect Phase 1a found: unify the brain_work event queue
so outcome-kind events are claimable and processed by handlers. Behind
a feature flag, default off, byte-identical behavior until the operator
flips it.

## Why this is next

Phase 1a (commit `4c1e46e`, memo
`docs/AUDITS/2026-05-11_dispatcher_silence.md`) found:

- The dispatcher was NOT silent (Phase 0 grep used colon; dispatcher
  declares its prefix with underscore — `[brain_work_dispatch]`).
- The real defect: `enqueue_outcome_event` (`ledger.py:103`) writes
  `event_kind='outcome', status='done', processed_at=now()` in one
  INSERT, but `claim_work_batch` (`ledger.py:184`) filters
  `event_kind='work'`. Result: rows born terminal, never claimed.
- 7 of 9 handler-targeted event types are affected. ~4,000 events sit
  as pure audit trail across `backtest_completed` (1055),
  `breakout_alert_resolved` (2659), `broker_fill_closed` (131),
  `market_snapshots_batch` (179), and others. The cpcv_gate, mine,
  promote, demote, regime_ledger, pattern_stats, breakout_outcomes,
  live_drift, and execution_robustness handlers have never fired
  against production traffic.

Operator architect-call: wire the unified queue. Both `event_kind`
become claimable through the same lifecycle; `claim_work_batch` filters
by event_type + status (not kind). Flag-gated for safety.

## Brief

`docs/STRATEGY/QUEUED/f-brain-event-kind-unify.md`

Parent architectural brief:
`docs/STRATEGY/QUEUED/f-adaptive-promotion-architecture-2026-05-11.md`

Phase 1a memo:
`docs/AUDITS/2026-05-11_dispatcher_silence.md`

Phase 0 memo:
`docs/AUDITS/2026-05-11_cpcv_gate_coverage.md`

## Deliverables

1. **app/services/trading/brain_work/ledger.py** — gated changes to
   `enqueue_outcome_event` (`status='pending'`, `processed_at=NULL`)
   and `claim_work_batch` (drop `event_kind='work'` filter). All
   behind `chili_brain_outcome_claimable_enabled`.
2. **app/config.py** — new pydantic Settings field (default False).
3. **app/migrations.py** — new migration N adding partial index
   `ix_brain_work_events_claim_v2` (no data mutation, no column ops).
4. **tests/test_brain_work_event_kind_unify.py** — flag-off parity,
   flag-on claim, flag-on backward-compat-for-historical-rows.
5. **tests/test_brain_work_handler_idempotency.py** — each of 9
   handlers called twice with same payload, no duplicate
   side-effects. **Hard gate for Phase 1c.**
6. **docs/runbooks/BRAIN_WORK_EVENT_KIND.md** — operator runbook.
7. **docs/STRATEGY/CC_REPORTS/2026-05-11_brain-event-kind-unify.md**

## Hard constraints

- Flag defaults `False`. Merge produces zero behavior change.
- Reversible. Flag-off restores byte-identical previous behavior.
- Historical `status='done'` rows stay ineligible to claim (Phase 1c
  is the controlled mechanism to bring them forward).
- No changes to `dispatcher.py`, any handler, or
  `backtest_queue_worker.py`. Pure ledger + flag + tests.
- No autotrader / venue / broker touched.
- Migration is index-only.
- Handlers must be proven idempotent (test_brain_work_handler_idempotency
  is a hard gate for Phase 1c).

## Open question requiring consult

When the flag is True and the dispatcher processes an outcome event
successfully, should `mark_work_done` update `processed_at` (Option 1,
recommended) or leave it at the original outcome timestamp (Option 2)?
The brief assumes Option 1 — CC should surface the choice in consult.

## Next in queue

- **Phase 1c** (`f-brain-event-kind-backfill.md`) — controlled
  resurrection of 4,000 historical orphans after Phase 1b is stable
  in prod for 24h. Already queued.
- **Phase 2** (the original adaptive CPCV gate redesign) — proceeds
  in parallel once Phase 1b prod flip is clean for 24h.

## Side-shipped earlier this session

- `f-cowork-watcher-truncation-fix` (commit `e13c7d9`) — operator
  override.
- Supervisor parameterization (commit `f71fdf1`) — `-Mode session`
  added.
- Phase 0 audit (commit `738a72d`).
- Phase 1a audit (commit `4c1e46e`).
