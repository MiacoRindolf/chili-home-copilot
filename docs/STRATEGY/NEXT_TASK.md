# NEXT_TASK: f-position-identity-phase-5j-selective-reader-cleanup-slice-2

STATUS: PENDING

## Goal

Continue Phase 5J by migrating another small batch of read-only analytics from
the legacy `trading_trades` compatibility view to the semantic physical table
`trading_management_envelopes`.

Keep the compatibility view and the Python `Trade` ORM class in place.

## Current Gate State

- Phase 5H physical rename: applied as
  `283_position_identity_phase5h_physical_rename`
- Physical base table: `trading_management_envelopes` (`relkind='r'`)
- Legacy compatibility view: `trading_trades` (`relkind='v'`)
- Phase 5I closeout: `COMPLETE_POSITIVE`
- Phase 5J slice 1 shipped:
  - `app/routers/brain.py`
  - `app/services/trading/management_envelopes.py`
  - `scripts/d-cb-phase6-soak-probe.py`
  - `scripts/d-maker-only-tca-probe.py`
  - `scripts/d-imminent-silence-audit.py`
- Latest post-slice verification:
  - Phase 5I probe: `COMPLETE_POSITIVE`
  - schema-specific log hits: 0
  - brain KPI smoke: `ok=True`

## Candidate Slice 2 Targets

Prefer read-only analytics/reporting code:

- `app/services/trading/decision_packet_coverage.py`
- `app/services/trading/divergence_service.py`
- `scripts/analyze_trade_quality_funnel.py` if its current local edits are
  understood and safe to build on
- read-only dashboard/reporting scripts

Defer live writer paths:

- broker/order management
- reconciliation close paths
- stop execution
- autotrader placement paths
- migrations that intentionally preserve historical compatibility

## Tasks

1. Audit the candidate files and classify each reference as convert/keep/defer.
2. Convert only one small read-only group.
3. Add or extend guard tests.
4. Verify:

   ```powershell
   python -m pytest tests\test_phase5j_reader_cleanup.py tests\test_phase5i_post_rename_probe.py
   python scripts\d-phase5i-post-rename-soak-probe.py
   powershell -ExecutionPolicy Bypass -File scripts\dispatch-phase5i-post-rename-soak-probe.ps1
   ```

## Guardrails

- Do not drop the `trading_trades` compatibility view.
- Do not rename the Python `Trade` ORM class.
- Do not touch live writer/order-placement paths.
- Do not edit files with unrelated dirty work unless the current local diff is
  inspected and deliberately preserved.

## Acceptance

- Converted readers use `trading_management_envelopes`.
- Phase 5I remains `COMPLETE_POSITIVE`.
- Tests for converted readers pass.
- No schema-specific worker errors.

## References

- `docs/STRATEGY/CC_REPORTS/2026-05-30_f-position-identity-phase-5j-selective-reader-cleanup-slice-1.md`
- `docs/STRATEGY/CC_REPORTS/2026-05-30_f-position-identity-phase-5i-post-rename-soak-closeout.md`
- `scripts/d-phase5i-post-rename-soak-probe.py`
