# NEXT_TASK: f-position-identity-phase-5j-selective-reader-cleanup-slice-5

STATUS: PENDING

## Goal

Continue Phase 5J by migrating clean read-only scripts or analytics modules from
the legacy `trading_trades` compatibility view to `trading_management_envelopes`.

Keep the compatibility view and the Python `Trade` ORM class in place.

## Current Gate State

- Phase 5H physical rename: applied as
  `283_position_identity_phase5h_physical_rename`
- Physical base table: `trading_management_envelopes` (`relkind='r'`)
- Legacy compatibility view: `trading_trades` (`relkind='v'`)
- Phase 5I closeout: `COMPLETE_POSITIVE`
- Phase 5J slices shipped:
  - slice 1: brain KPI, management-envelope helper, Coinbase/maker/imminent probes
  - slice 2: decision-packet coverage and divergence analytics
  - slice 3: dynamic priors, ticker-scope autotune, pattern-stats recompute
  - slice 4: realized stats sync and HRP active-position reader
- Latest post-slice verification:
  - Phase 5I probe: `COMPLETE_POSITIVE`
  - schema-specific log hits: 0

## Candidate Slice 5 Targets

Prefer clean read-only scripts/modules:

- `scripts/analyze_trade_quality_funnel.py` only after inspecting its current
  local edits
- `app/services/trading/net_edge_ranker.py` only after unrelated local edits are
  resolved or deliberately included
- other read-only reporting scripts without dirty local edits

Defer live writer paths:

- broker/order management
- reconciliation close paths
- stop execution
- autotrader placement paths
- migrations preserving historical compatibility
- broad mechanical rewrites

## Tasks

1. Audit candidate files and classify each reference as convert/keep/defer.
2. Convert one small read-only group.
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

- `docs/STRATEGY/CC_REPORTS/2026-05-30_f-position-identity-phase-5j-selective-reader-cleanup-slice-4.md`
- `docs/STRATEGY/CC_REPORTS/2026-05-30_f-position-identity-phase-5j-selective-reader-cleanup-slice-3.md`
- `docs/STRATEGY/CC_REPORTS/2026-05-30_f-position-identity-phase-5i-post-rename-soak-closeout.md`
