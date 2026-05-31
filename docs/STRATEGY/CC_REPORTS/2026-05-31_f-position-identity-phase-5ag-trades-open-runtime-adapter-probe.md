# Phase 5AG - Trades Open-Row Runtime Adapter Probe

Date: 2026-05-31

## Verdict

SHIPPED as a read-only parity gate.

The public `/api/trading/trades` route still keeps open/all cutover guarded by
the Phase 5AF flag fallback. This slice proves whether open management-envelope
runtime objects can pass through the same broker-truth overlay and stale-open
suppression chain as the current `Trade` ORM objects.

## What changed

- Added `scripts/d-phase5ag-trades-open-runtime-adapter-probe.py`.
- The probe compares:
  - current `Trade` ORM open-row path
  - candidate runtime objects loaded from physical `trading_management_envelopes`
- Both sides run through:
  - `filter_broker_stale_open_trades(...)`
  - `broker_position_display_metrics(...)`
  - the public `/trades` open-row field renderer
- The probe is read-only and defaults to `_test` databases unless
  `PHASE5AG_ALLOW_LIVE_PROBE=true` is set.

## Live result

```text
PHASE5AG_ALLOW_LIVE_PROBE=true \
DATABASE_URL=postgresql://chili:chili@localhost:5433/chili \
python scripts/d-phase5ag-trades-open-runtime-adapter-probe.py

VERDICT_STATUS=COMPLETE_POSITIVE
old_trades=5
new_trades=5
old_suppressed=0
new_suppressed=0
matched=true
relation_kinds={'trading_management_envelopes': 'r', 'trading_trades': 'v'}
```

## Supporting gates

```text
python -m py_compile scripts/d-phase5ag-trades-open-runtime-adapter-probe.py
PASS

TEST_DATABASE_URL=postgresql://chili:chili@localhost:5433/chili_test \
  pytest -q tests/test_phase5ag_trades_open_runtime_adapter_probe.py \
            tests/test_trades_api_shadow_compare.py
13 passed

python scripts/d-phase5ae-trades-api-parity-probe.py
VERDICT_STATUS=COMPLETE_POSITIVE
CHECKS=3
MISMATCHES=0

DATABASE_URL=postgresql://chili:chili@localhost:5433/chili \
  python scripts/d-phase5k-live-path-parity-probe.py
VERDICT_STATUS=COMPLETE_POSITIVE
PARITY_CHECKS=6
PARITY_MISMATCHES=0

python scripts/d-phase5i-post-rename-soak-probe.py
VERDICT_STATUS=COMPLETE_POSITIVE
FRESH_DECISIONS=20
FRESH_ENVELOPES=20
FRESH_CLOSES=10
FRESH_CLOSE_MISMATCHES=0
```

## Architect call

The prior caution around open rows was correct: do not cut them over without
broker-truth parity evidence. We now have that evidence for the current live
shape. The next safe slice is to expand the existing default-off
`CHILI_PHASE5AF_TRADES_API_USE_ENVELOPES` flag so, when explicitly enabled, it
uses the proven runtime-object path for open/all responses instead of falling
back solely because open rows are present.

That still should not rename `/trades`, `Trade`, `trade_id`, schema classes, UI
labels, or the compatibility view.
