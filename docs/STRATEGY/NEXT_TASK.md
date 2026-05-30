# NEXT_TASK: f-position-identity-phase-5j-selective-reader-cleanup

STATUS: PENDING

## Goal

Begin the low-risk reader cleanup after the Phase 5H physical rename and Phase
5I soak.

This is not a destructive rename phase. Keep the `trading_trades` compatibility
view and the Python `Trade` ORM class in place. The objective is to move
analytics/reporting/read-only SQL toward the semantic name
`trading_management_envelopes` while preserving live writer compatibility.

## Current Gate State

- Phase 5H physical rename: applied as
  `283_position_identity_phase5h_physical_rename`
- Physical base table: `trading_management_envelopes` (`relkind='r'`)
- Legacy compatibility view: `trading_trades` (`relkind='v'`)
- Phase 5I closeout: `COMPLETE_POSITIVE`
  - fresh decisions: 20
  - fresh envelopes: 20
  - fresh closes: 10
  - fresh close mismatches: 0
  - hard linkage issues: 0
  - 30d mismatched rows: 0
  - 30d mismatched PnL: $0.0000
  - schema-specific log hits: 0
- Mig 288 installed: delayed envelope `scan_pattern_id` updates now backfill
  missing decision attribution.

## Tasks

1. Audit runtime `trading_trades` references and classify them:
   - Keep: ORM model/table-name compatibility, writes, old API contracts.
   - Convert: reports, dashboards, audit scripts, read-only analytics.
   - Defer: anything touching live broker/order management.
2. Convert a small first slice of read-only SQL to
   `trading_management_envelopes`.
3. Add/adjust tests around converted readers.
4. Rerun Phase 5I probe after the cleanup:

   ```powershell
   python scripts\d-phase5i-post-rename-soak-probe.py
   powershell -ExecutionPolicy Bypass -File scripts\dispatch-phase5i-post-rename-soak-probe.ps1
   ```

## Guardrails

- Do not drop the `trading_trades` compatibility view.
- Do not rename the Python `Trade` ORM class.
- Do not touch live writer/order-placement paths unless a reader is impossible
  to isolate.
- Do not rewrite broad swaths mechanically; migrate reader references in small,
  reviewable groups.

## Acceptance

- Converted readers use `trading_management_envelopes`.
- Live writer paths still work through the compatibility view.
- Phase 5I probe remains `COMPLETE_POSITIVE`.
- Tests for the converted readers pass.

## References

- `docs/STRATEGY/CC_REPORTS/2026-05-30_f-position-identity-phase-5i-post-rename-soak-closeout.md`
- `docs/STRATEGY/CC_REPORTS/2026-05-28_f-position-identity-phase-5h-production-physical-rename.md`
- `scripts/d-phase5i-post-rename-soak-probe.py`
