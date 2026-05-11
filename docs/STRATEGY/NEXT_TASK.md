# NEXT_TASK: f-brain-event-kind-backfill

STATUS: PENDING

## Goal

**Phase 1c of the adaptive-promotion-architecture initiative.**
Controlled backfill of the ~4,000 historical outcome/done orphans
through the unified queue so handlers process them.

## Why this is next

Phase 0, 1a, 1b code, Phase 1b prod flag flip + verification, and
Phase 2 (adaptive gate) all shipped. Handlers are firing in prod
(verified: 20 pending rows + dispatcher claimed market_snapshots_batch
+ `[brain_work:mine] ev_id=4335`). Phase 1c is the controlled mechanism
to bring the 4,000 historical orphans forward so the cpcv_gate produces
verdicts for the 1,055 historical `backtest_completed` events — that's
the actual drought relief payload.

## Brief

`docs/STRATEGY/QUEUED/f-brain-event-kind-backfill.md`

Parent: `docs/STRATEGY/QUEUED/f-adaptive-promotion-architecture-2026-05-11.md`
Phase 1a memo: `docs/AUDITS/2026-05-11_dispatcher_silence.md`
Phase 1b CC_REPORT: `docs/STRATEGY/CC_REPORTS/2026-05-11_brain-event-kind-unify.md`

## Deliverables (per brief)

1. `scripts/brain-event-backfill.ps1` — operator-controlled script,
   per-event-type, dry-run default, 30s inter-batch sleep, kill switch
2. Per-event-type pre-flight memos under `docs/AUDITS/` for the two
   large event types (backtest_completed, breakout_alert_resolved)
3. `docs/runbooks/BRAIN_EVENT_BACKFILL.md`
4. `docs/STRATEGY/CC_REPORTS/2026-05-11_brain-event-kind-backfill.md`

## Recommended execution order (per brief)

Smallest blast radius first:
1. paper_trade_closed (1 row) — smoke test
2. live_trade_closed (4 rows) — confidence builder
3. broker_fill_closed (131 rows) — post-hoc execution audit
4. market_snapshots_batch (179 rows) — populates regime_ledger
5. **backtest_completed (1055 rows) — the actual drought relief**
6. breakout_alert_resolved (2659 rows) — last, largest

CC should ship the script + memos + runbook + report. The actual
backfill UPDATE runs are operator-controlled via the script.

## Hard constraints

- No `app/` code changes. Pure script + docs.
- No new migrations / schema changes.
- Per-event-type scoping required (no `-EventType` default).
- DryRun default.
- 30s inter-batch sleep hardcoded.
- Mining handler `mine_patterns` has no event-level dedupe — DO NOT
  enable retroactive replay of `market_snapshots_batch` rows without
  inner-contract verification first.
- TEST_DATABASE_URL must end in _test if any tests are added.

## Side-shipped this session

- Phase 0 (`738a72d`)
- Phase 1a (`4c1e46e`)
- Phase 1b code (`2e9365c`) + prod flag flip + verified
- Phase 2 adaptive gate (`fd2e687`)
- Watcher truncation fix (`e13c7d9`)
- Supervisor parameterization (`f71fdf1`)
