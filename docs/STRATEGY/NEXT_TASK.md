# NEXT_TASK: position-identity-phase-5-runtime-observation

STATUS: IN_FLIGHT

## Goal

Let the Phase 5 compatibility boundary soak under normal runtime before any new rename work.

## Current Verdict

The Phase 5 rename/refactor pressure should stop here for now:

- runtime raw `trading_trades` readers are gone
- high-value display loaders are on management-envelope helpers
- `Trade` remains the deliberate compatibility ORM mapper
- `trading_trades` remains the deliberate compatibility relation
- broad rename now would add risk without improving alpha, execution, slippage, or capital control

Latest observation update, 2026-05-30 PT:

- Phase 5K live-path parity probe: `COMPLETE_POSITIVE`
- Phase 5I post-rename soak probe: `COMPLETE_POSITIVE`
- Phase 5 reader canary: clean, no unexpected runtime readers or mutations
- focused Phase 5 reader tests: passing
- app runtime logs: no Phase 5 relation/query errors observed
- Postgres `schema_version.version` errors were classified as one-shot probe/dashboard noise, not live trading code
- Added `scripts/d-phase5-runtime-observation-probe.py` so the next market-window closeout runs the full gate mechanically.

This is healthy weekend/crypto-window evidence, but not yet a full normal market-session closeout. Keep observing; do not begin a broad rename.

## Observation Checklist

1. Watch the existing live probes/canaries:
   - Phase 5K-A parity
   - Phase 5I post-rename probe
   - Phase 5L reader allowlist
   - Or run the rollup:
     `python scripts\d-phase5-runtime-observation-probe.py --since-minutes 390 --market-window-complete`
2. Watch runtime logs for relation/query errors involving:
   - `trading_trades`
   - `trading_management_envelopes`
   - `Trade`
   - `trade_id`
3. Keep the compatibility view intact.
4. Do not start another rename slice unless a concrete reader or production issue gives a reason.

## Safe Future Work, If Needed

Only these are acceptable without a fresh operator decision:

- add semantic helper APIs for a concrete reader still carrying decision risk
- add canaries that prevent drift back to raw `trading_trades` reads
- document public compatibility boundaries

## Hard Guardrails

- Do not drop or rename the `trading_trades` compatibility view.
- Do not broadly rename `Trade`.
- Do not rename public `/trades`, `trade_id`, schema classes, or UI labels.
- Do not touch monitor-run, close/sell, broker/order/reconcile, PDT, cash, capital, or portfolio gates as a rename cleanup.

## Exit Criteria

Observation is complete when production canaries remain green across a normal trading window and no relation/query errors appear. At that point, either leave Phase 5 parked or open a fresh, concrete brief for a non-rename trading improvement.
