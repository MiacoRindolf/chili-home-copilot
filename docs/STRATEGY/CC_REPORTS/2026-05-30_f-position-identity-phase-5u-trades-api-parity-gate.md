# Phase 5U - /trades API Parity Gate

Date: 2026-05-30
Status: SHIPPED

## What changed

- Added `load_trades_api_envelope_rows(...)` for the base public `/trades` row shape.
- Added `scripts/d-phase5u-trades-api-parity-probe.py`, a read-only live parity gate comparing `trading_trades` view rows against `trading_management_envelopes` table rows for all/open/closed samples.
- Did not change `/api/trading/trades` output, route behavior, broker-truth overlays, stale suppression, order/close/reconcile/PDT/capital-gate logic, or public naming.

## Verification

- `python -m py_compile app\services\trading\management_envelopes.py scripts\d-phase5u-trades-api-parity-probe.py`
- `python -m pytest tests\test_management_envelopes.py -q` -> 7 passed
- `python scripts\d-phase5u-trades-api-parity-probe.py` -> COMPLETE_POSITIVE
- `python -m pytest tests\test_phase5_remaining_trade_refs.py tests\test_phase5l_reader_allowlist.py -q` -> 9 passed
- `python scripts\analyze_phase5_remaining_trade_refs.py --json --include app --fail-on-unexpected-runtime` -> ok=true
- `python scripts\d-phase5k-live-path-parity-probe.py` -> COMPLETE_POSITIVE
- `python scripts\d-phase5i-post-rename-soak-probe.py` -> COMPLETE_POSITIVE

## Architect verdict

The base `/trades` database row shape is parity-clean. The route is still not ready for a blind cutover because open-trade broker-truth display overlays and stale suppression expect ORM objects. Next safe step is a shadow compare inside the route or adjacent service, not a public rename.