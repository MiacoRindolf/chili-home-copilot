# NEXT_TASK: f-cpcv-gate-dispatcher-silence-audit

STATUS: PENDING

## Goal

**Phase 1a of the adaptive-promotion-architecture initiative.**
Read-only audit to find why `run_brain_work_dispatch_round` has logged
zero `[brain_work:dispatch]` lines in full brain-worker history, AND
identify the writer marking 205 `backtest_completed` events/24h as
`done` without invoking the cpcv_gate handler.

## Why this is next

Phase 0 (commit `738a72d`, memo
`docs/AUDITS/2026-05-11_cpcv_gate_coverage.md`) found two stacked
breaks in the promotion funnel:

1. **Dispatcher silence (100% of sampled patterns).** The handler that
   should produce CPCV verdicts has never logged a single verdict in
   container history. Yet `brain_work_events` shows 205 events/24h
   marked `done` — some other writer is silently marking them done.
2. **Ensemble pre-gate.** Force-eval against patterns 731 + 1212 (both
   with 7K+ PTR rows) shows `check_promotion_ready` short-circuits at
   `mining_validation.py:341` before CPCV runs, leaving
   `cpcv_n_paths` NULL even when the handler IS reached.

The original Phase 1 (synthetic-event backfill) is unsafe to ship
until we identify the rogue done-writer. If the new events get
silently marked done without handler invocation, the backfill is a
no-op that looks successful.

## Brief

`docs/STRATEGY/QUEUED/f-cpcv-gate-dispatcher-silence-audit.md`

Parent architectural brief:
`docs/STRATEGY/QUEUED/f-adaptive-promotion-architecture-2026-05-11.md`

Phase 0 memo (read first):
`docs/AUDITS/2026-05-11_cpcv_gate_coverage.md`

## Deliverables (all in scripts/ or docs/ — NO code under app/)

1. `scripts/audit-dispatcher-silence.ps1` — tests 6 hypotheses
   (H1–H6) about why the dispatcher is silent + locates the rogue
   done-writer by file:line.
2. `scripts/audit-dispatcher-silence-out.txt` — committed run output.
3. `docs/AUDITS/2026-05-11_dispatcher_silence.md` — one-page memo
   with H1–H6 verdicts + rogue-writer identification + Phase 1b
   safety recommendation.
4. `docs/STRATEGY/CC_REPORTS/2026-05-11_cpcv-gate-dispatcher-silence-audit.md`

## Hard constraints

- **READ-ONLY.** No DB writes. No `app/` code changes. No restarts.
  No new migrations / tables / columns. No env edits.
- `psql -c` SELECT-only; any `docker exec python -c` must `rollback()`
  in finally.
- No changes to `dispatcher.py`, any handler, or
  `backtest_queue_worker.py` — read them, don't edit them.
- Memo D2 must name the rogue done-writer by file:line. If
  inconclusive, recommend additional probe — don't speculate.

## Next in queue

- `f-cpcv-gate-event-backfill` (Phase 1b) — written after Phase 1a
  lands, calibrated to whether the dispatcher needs to be restored
  first.
- `f-supervisor-auto-relaunch-investigation` (priority 220) —
  partially relieved by commit `f71fdf1` (supervisor parameterized
  for `-Mode session`); brief still useful for documenting the
  expected operational pattern.

## Side-shipped this session

- `f-cowork-watcher-truncation-fix` (commit `e13c7d9`) — operator
  override.
- Supervisor parameterization (commit `f71fdf1`) — `-Mode session`
  added; operator runs the same supervisor in a second window for
  the session daemon.
