# NEXT_TASK: f-position-identity-phase-5af-trades-api-cutover-soak

STATUS: IN_FLIGHT

## Goal

Soak the default-off `/api/trading/trades` management-envelope cutover flag and
decide whether a narrow closed-row route trial is safe.

## Current State

Phase 5AF added a typed flag:

```text
CHILI_PHASE5AF_TRADES_API_USE_ENVELOPES=false
```

Default behavior is unchanged. If the flag is enabled, `/api/trading/trades`
can render rows from `trading_management_envelopes`, but it falls back to the
compatibility path whenever open rows are requested or present. This protects
broker-truth display overlays and stale-open suppression.

Latest evidence:

```text
Focused tests:                  38 passed
Phase 5AE /trades parity probe: COMPLETE_POSITIVE, 3 checks, 0 mismatches
Phase 5K live-path parity:      COMPLETE_POSITIVE, 6 checks, 0 mismatches
Phase 5I post-rename soak:      COMPLETE_POSITIVE
Classifier raw reader bucket:   0
```

## Recommended Work Shape

1. Keep live `CHILI_PHASE5AF_TRADES_API_USE_ENVELOPES=false` initially.
2. Exercise:
   - `/api/trading/trades`
   - `/api/trading/trades?status=open`
   - `/api/trading/trades?status=closed`
3. Check logs for:
   - `[phase5v] /trades envelope shadow mismatch`
   - `[phase5af] /trades envelope cutover fallback`
   - relation/query errors involving `trading_trades`
   - relation/query errors involving `trading_management_envelopes`
4. If clean, run a short flag-on trial only for closed-row confidence:
   - enable `CHILI_PHASE5AF_TRADES_API_USE_ENVELOPES=true`
   - restart only `chili`
   - call `/api/trading/trades?status=closed`
   - confirm response shape and logs
   - flip back off unless the operator explicitly wants it left on
5. Before any broad open/all cutover, build a read-only open-row runtime adapter
   parity probe that covers broker-truth overlays and stale-open suppression.

## Guardrails

- Do not drop or rename the `trading_trades` compatibility view.
- Do not rename `Trade`, `/trades`, `trade_id`, schema classes, UI labels, or
  public response field names.
- Do not touch sell/close, monitor-run, stop execution, broker/order/reconcile,
  PDT, cash, capital, portfolio, or promotion gates.
- Do not broad-enable the route flag for open/all responses until open-row
  broker-truth parity is proven.
