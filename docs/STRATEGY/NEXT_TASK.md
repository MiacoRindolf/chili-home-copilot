# NEXT_TASK: f-position-identity-phase-5d-decision-pattern-attribution-repair

STATUS: PENDING

## Goal

Repair the small semantic attribution drift surfaced by Phase 5C before any
physical rename of `trading_trades`.

Phase 5C proved hard linkage is green, but the reporting compare found four
closed envelopes where `trading_decisions.scan_pattern_id` is NULL while the
linked management envelope has a populated `scan_pattern_id`.

## Current Gate State

- Phase 5B hard linkage issues: 0
- Open broker trades missing `position_id`: 0
- Orphan decisions: 0
- Phase 5C reporting reader: shipped behind
  `/api/trading/attribution/live-vs-research?phase5b_compare=true`
- 30d Phase 5C drift:
  - 320 closed envelopes
  - 4 mismatched closed envelopes
  - 3 envelope pattern groups
  - net mismatch PnL: $21.2641
  - absolute group drift: $42.5282
  - all mismatches have `decision_scan_pattern_id=NULL`

## Phase 5D Tasks

1. Add an idempotent migration that backfills
   `trading_decisions.scan_pattern_id` from the linked envelope only when:
   - `trading_decisions.scan_pattern_id IS NULL`
   - `trading_decisions.source_trade_id = trading_trades.id`
   - `trading_trades.scan_pattern_id IS NOT NULL`
2. Preserve provenance. Prefer a small metadata marker if an existing decisions
   JSON/notes field exists; otherwise document the migration and keep the
   operation tightly scoped.
3. Rerun the Phase 5C compare. Acceptance target:
   - `mismatched_closed_envelopes = 0`, or every residual mismatch has a
     documented reason.
4. Do not physically rename `trading_trades`.

## Acceptance

- The Phase 5C compare has no unexplained decision-vs-envelope pattern drift.
- The live-vs-research endpoint still defaults to legacy output unless
  `phase5b_compare=true` is passed.
- Tests pin the backfill predicate so it cannot overwrite non-null decisions.

## Rollback

Revert the migration if needed, or manually set the repaired decisions back to
NULL using the IDs captured in the migration verification report. This task is
semantic reporting repair only; it must not affect live trading writers.

## References

- `docs/STRATEGY/CC_REPORTS/2026-05-26_f-position-identity-phase-5c-reporting-reader-adoption.md`
- `docs/STRATEGY/CC_REPORTS/2026-05-26_f-position-identity-phase-5b-coinbase-linkage-repair.md`
- Migration: 274
