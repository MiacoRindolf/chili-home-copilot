# Phase 5AE - Trades API Shadow Canary

**Date:** 2026-05-30
**Status:** SHIPPED
**Scope:** Passive canary only; no public `/trades` behavior changed

## Summary

Added a passive `/api/trading/trades` shadow compare against the physical
`trading_management_envelopes` table while keeping the current public response
path on the legacy `Trade` compatibility mapper.

The route still returns the same rows and field names. The new code loads the
same stable database-backed fields from management envelopes, compares them to
the current response's local values, and logs a `[phase5v] /trades envelope
shadow mismatch` warning only if the two shapes drift.

Broker-truth display overlays are intentionally excluded from the comparison:
`entry_price` and `quantity` in the public payload may be broker-adjusted, so
the shadow compare uses `local_entry_price` and `local_quantity` for parity.

## What Changed

- Added `load_trades_api_envelope_rows(...)` in `management_envelopes.py`.
- Added `_stable_trades_shadow_mismatches(...)` and passive route shadow
  logging in `app/routers/trading_sub/trades.py`.
- Added `scripts/d-phase5ae-trades-api-parity-probe.py`, a read-only old-vs-new
  row-shape parity probe for the stable `/trades` database fields.
- Added focused tests for the shadow comparator and helper contract.

## Guardrails Preserved

- No public rename.
- No route cutover.
- No change to `/trades` response fields.
- No change to `trade_id`, schema names, UI labels, or the `Trade` ORM mapper.
- No broker/order/close/reconcile/PDT/capital-gate behavior changed.

## Verification

```text
python -m py_compile app\routers\trading_sub\trades.py app\services\trading\management_envelopes.py scripts\d-phase5ae-trades-api-parity-probe.py

python -m pytest tests\test_trades_api_shadow_compare.py tests\test_phase5t_audit_export_helper.py tests\test_management_envelopes.py tests\test_phase5_remaining_trade_refs.py tests\test_phase5l_reader_allowlist.py -q
# 34 passed

python scripts\d-phase5ae-trades-api-parity-probe.py
# VERDICT_STATUS=COMPLETE_POSITIVE
# CHECKS=3
# MISMATCHES=0

python scripts\d-phase5k-live-path-parity-probe.py
# VERDICT_STATUS=COMPLETE_POSITIVE

python scripts\d-phase5i-post-rename-soak-probe.py
# VERDICT_STATUS=COMPLETE_POSITIVE

python scripts\analyze_phase5_remaining_trade_refs.py --fail-on-unexpected-runtime --json
# unexpected_runtime_readers=[]
# unexpected_runtime_mutations=[]
# unclassified=[]
```

## Architect Verdict

This is the right amount of movement after Phase 5AD: a canary, not a rename.
Keep observing for `[phase5v] /trades envelope shadow mismatch` logs before
considering any feature-flagged `/trades` read-route cutover.
