# NEXT_TASK: f-position-identity-phase-5ai-trades-api-flag-route-trial

STATUS: QUEUED

## Goal

Run a short, controlled `/api/trading/trades` route trial with
`CHILI_PHASE5AF_TRADES_API_USE_ENVELOPES=true`, then decide whether to leave the
flag on, roll it back, or add one more tie-order hardening slice.

## Current State

Phase 5AH added the default-off route path:

- `status=closed` uses the existing simple envelope renderer.
- `status=open` uses management-envelope runtime objects and preserves
  broker-truth overlays plus stale-open suppression.
- no status filter uses the same runtime-object serializer; the Phase 5AH probe
  shows identical row content with only tie-order drift among rows sharing the
  same `entry_date`.

Live probes are green:

```text
Phase 5AH cutover probe: COMPLETE_POSITIVE
Phase 5AG open runtime adapter probe: COMPLETE_POSITIVE
Phase 5AE /trades base parity probe: COMPLETE_POSITIVE
Phase 5K live-path parity probe: COMPLETE_POSITIVE
Phase 5I post-rename soak probe: COMPLETE_POSITIVE
```

## Recommended Work Shape

1. Before flipping, re-run:
   - `scripts/d-phase5ah-trades-api-cutover-probe.py`
   - `scripts/d-phase5ag-trades-open-runtime-adapter-probe.py`
   - `scripts/d-phase5ae-trades-api-parity-probe.py`
   - `scripts/d-phase5k-live-path-parity-probe.py`
   - `scripts/d-phase5i-post-rename-soak-probe.py`
2. If all gates are still green, set
   `CHILI_PHASE5AF_TRADES_API_USE_ENVELOPES=true` in `.env`.
3. Restart only the `chili` web container with `--no-deps`.
4. Probe `/api/trading/trades`, `/api/trading/trades?status=open`, and
   `/api/trading/trades?status=closed` via the live web route.
5. Watch logs for `[phase5af]` / `[phase5ah]` fallback or exception lines.
6. If clean, leave the flag on for a short soak. If any route/UI regression
   appears, flip the flag back to `false` and restart only `chili`.

## Guardrails

- Do not drop or rename the `trading_trades` compatibility view.
- Do not rename `Trade`, `/trades`, `trade_id`, schema classes, UI labels, or
  public response field names.
- Do not touch sell/close, monitor-run, stop execution, broker/order/reconcile,
  PDT, cash, capital, portfolio, or promotion gates.
- Do not restart Postgres for this trial.
