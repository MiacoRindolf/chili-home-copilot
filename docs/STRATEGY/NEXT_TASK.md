# NEXT_TASK: f-position-identity-phase-5e-reporting-reader-soak

STATUS: PENDING

## Goal

Soak the Phase 5B/5C reporting reader after the Phase 5D attribution repair,
then decide whether the physical rename is safe to plan.

Do not rename `trading_trades` yet. This task is observation plus one more
fresh-data parity gate.

## Current Gate State

- Phase 5B hard linkage issues: 0
- Open broker trades missing `position_id`: 0
- Orphan decisions: 0
- Phase 5C opt-in reporting reader: live
- Phase 5D attribution repair: live
- Current 30d Phase 5C compare:
  - 320 closed envelopes
  - 25 envelope pattern groups
  - 25 decision pattern groups
  - 0 mismatched closed envelopes
  - $0.0000 attribution drift

## Tasks

1. Wait for fresh entries and at least a few fresh closes after mig 275.
2. Rerun:
   `/api/trading/attribution/live-vs-research?phase5b_compare=true`
3. Verify:
   - `mismatched_closed_envelopes = 0`
   - `absolute_group_pnl_delta = 0`
   - no new hard linkage issues in `trading_phase5b_decision_envelope_position`
   - default endpoint output is still legacy-compatible when
     `phase5b_compare=false`
4. If clean, write the Phase 5E soak report and prepare a rename-design brief.

## Acceptance

- At least one post-mig-275 entry/close cycle is represented in the read model.
- Reporting compare remains clean with fresh data.
- No writer path was changed.

## Rollback

No data rollback is expected. If drift reappears, keep the physical rename
blocked and write a targeted Phase 5F repair brief.

## References

- `docs/STRATEGY/CC_REPORTS/2026-05-26_f-position-identity-phase-5c-reporting-reader-adoption.md`
- `docs/STRATEGY/CC_REPORTS/2026-05-26_f-position-identity-phase-5d-decision-pattern-attribution-repair.md`
- Migrations: 274, 275
