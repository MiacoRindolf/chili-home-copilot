# NEXT_TASK: f-position-identity-phase-5ae-trades-api-shadow-soak-review

STATUS: IN_FLIGHT

## Goal

Review the passive `/api/trading/trades` management-envelope shadow canary
before any feature-flagged read-route cutover.

## Current State

Phase 5AE added a canary, not a behavior change:

- public `/trades` response still comes from the legacy `Trade` compatibility
  mapper
- `load_trades_api_envelope_rows(...)` reads the stable database-backed fields
  from `trading_management_envelopes`
- the route logs `[phase5v] /trades envelope shadow mismatch` only if current
  response rows drift from management-envelope rows
- broker-truth display overlays are excluded by comparing `local_entry_price`
  and `local_quantity`

Latest verification:

```text
Phase 5AE /trades parity probe: COMPLETE_POSITIVE, 3 checks, 0 mismatches
Phase 5K-A live-path parity:    COMPLETE_POSITIVE
Phase 5I post-rename soak:      COMPLETE_POSITIVE
Runtime observation rollup:     available via d-phase5-runtime-observation-probe.py
Focused tests:                  34 passed
Classifier:                     0 unexpected readers, 0 unexpected mutations
```

## Recommended Work Shape

1. Inspect runtime logs after normal UI/API use for:
   - `[phase5v] /trades envelope shadow mismatch`
   - relation/query errors involving `trading_trades`
   - relation/query errors involving `trading_management_envelopes`
2. Exercise:
   - `/api/trading/trades`
   - `/api/trading/trades?status=open`
   - `/api/trading/trades?status=closed`
3. Re-run:
   - `python scripts\d-phase5ae-trades-api-parity-probe.py`
   - `python scripts\d-phase5k-live-path-parity-probe.py`
   - `python scripts\d-phase5i-post-rename-soak-probe.py`
4. If all evidence stays clean, draft a feature-flagged `/trades` read-route
   cutover plan. Do not public-rename.
5. If mismatches appear, classify them as broker-truth overlay, stale-open
   suppression, null/date formatting, or true envelope divergence.

## Guardrails

- Do not drop or rename the `trading_trades` compatibility view.
- Do not rename `Trade`, `/trades`, `trade_id`, schema classes, UI labels, or
  public response field names.
- Do not touch sell/close, monitor-run, stop execution, broker/order/reconcile,
  PDT, cash, capital, portfolio, or promotion gates.
- No route cutover without clean shadow evidence and a reversible flag.

## Architect Verdict

The useful move now is observation. Phase 5AE gives us a cheap warning system
for `/trades` divergence while preserving the compatibility boundary that keeps
live trading safe.
