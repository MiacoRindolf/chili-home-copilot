# NEXT_TASK: f-position-identity-phase-5c-reporting-reader-adoption

STATUS: PENDING

## Goal

Adopt the Phase 5B read-only envelope layer in one reporting reader, compare
old `trading_trades` output against the new decision/envelope/position view,
and explicitly settle decision-pattern vs envelope-pattern attribution before
any physical table rename.

## Current Gate State

Phase 5B soak is green on hard linkage after mig 273:

- Fresh envelopes since mig 264: 23
- Fresh closes since mig 264: 17
- Hard Phase 5B linkage issues: 0
- Open broker trades missing `position_id`: 0
- Orphan decisions: 0
- Historical closed broker-envelope debt: 110 rows, intentionally non-blocking

Remaining report-parity wrinkle:

- Old-vs-new 30d PnL diff is ~4 groups / ~$42.53 absolute.
- Cause: a few bridge-created `trading_decisions.scan_pattern_id` values are
  NULL while their management envelopes have `scan_pattern_id` populated.
- This is semantic attribution, not broken linkage.

## Phase 5C Tasks

1. Pick one low-risk reporting reader that currently queries
   `trading_trades` for pattern/PnL reporting.
2. Add a Phase 5B implementation using
   `app.services.trading.management_envelopes` or
   `trading_phase5b_decision_envelope_position`.
3. Keep the old query live and compare outputs side by side.
4. For pattern attribution, report both fields where useful:
   `decision.scan_pattern_id` and `envelope.scan_pattern_id`.
5. Do not physically rename `trading_trades`.

## Acceptance

- One reporting reader can run old and Phase 5B paths side by side.
- The difference set is understood and documented.
- No live trading writer or close path changes.
- Tests pin the chosen reader's parity behavior.

## Rollback

Disable the Phase 5B reader path or revert the reporting-reader commit. The
underlying Phase 5B views are read-only and can remain installed.

## References

- `docs/STRATEGY/CC_REPORTS/2026-05-21_f-position-identity-phase-5b-read-models.md`
- `docs/STRATEGY/CC_REPORTS/2026-05-26_f-position-identity-phase-5b-coinbase-linkage-repair.md`
- Migrations: 264, 265, 273
