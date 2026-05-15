# NEXT_TASK: f-triple-barrier-activation

STATUS: DONE

## Goal

**Phase C of evidence-fidelity-architecture.** Add a scheduler job that
periodically calls `label_snapshots()` in
`triple_barrier_labeler.py:273`. The function is fully implemented but
has no production caller — `trading_triple_barrier_labels` is at 0
rows. Once populated, this unlocks a per-pattern meta-classifier that
filters false positives from existing alpha without inventing new alpha.

## Brief

`docs/STRATEGY/QUEUED/f-triple-barrier-activation.md`

Parent: `docs/STRATEGY/QUEUED/f-evidence-fidelity-architecture-2026-05-14.md`

Prior phases shipped:
- Phase A `ca1705f` — canonical outcome split
- Phase B `51da8cc` — execution-truth wiring

## Deliverables (per brief)

1. Scheduler job registration (4h cadence, single-instance, coalesce)
2. `scripts/triple-barrier-backfill.ps1` — historical backfill
3. `tests/test_triple_barrier_scheduler.py`
4. `docs/runbooks/TRIPLE_BARRIER_LABELING.md`
5. CC_REPORT

## Hard constraints

- `brain_triple_barrier_mode` stays `shadow` at merge (operator flips later)
- Scheduler job uses `max_instances=1` + `coalesce=True`
- No changes to `triple_barrier_labeler.py` itself
- Backfill `-DryRun` default + kill switch
- TEST_DATABASE_URL must end in `_test`

## Consult gate

Scheduler cadence — 4h vs 6h? Brief default 4h.

## After Phase C

Phase D (NetEdge live wiring) + Phase E (multiple-testing discipline)
briefs already written and queued.
