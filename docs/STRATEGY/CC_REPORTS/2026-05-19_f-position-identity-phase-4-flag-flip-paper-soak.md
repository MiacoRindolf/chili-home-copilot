# CC_REPORT: f-position-identity-phase-4-flag-flip-paper-soak

**Session type:** Cowork-direct execution (operator: "go do it" after the brief was updated to assume the sell-side recording fix had landed).

## Step 1 — Pre-flip audit

Confirmed the brief's premise:

| Metric | Value | Brief expectation |
|---|---|---|
| sells_recorded | **450** | 450+ ✓ |
| buys_recorded | 8,352 | (context) |
| positions_with_sell | **112** | ~112 ✓ |
| schema_version tip | `254_synthetic_exit_fill_events` | (the prerequisite mig) ✓ |
| `CHILI_POSITION_IDENTITY_PHASE4_AUTHORITY_ENABLED` in `.env` | not set (default `False`) | ready to flip |

The 450 sell events are 100% synthetic (`event_type='backfill_exit_fill'`, populated by mig 254 from `trading_trades.exit_*`). Sample payload: `{"side": "sell", "source": "mig254_backfill", "trade_id": <id>, "exit_reason": <reason>}`. All have `payload_json->>'side' = 'sell'` (verified) — the Phase 4 helper sees them correctly.

## Step 2 — Flag flip + worker restart

- Added `CHILI_POSITION_IDENTITY_PHASE4_AUTHORITY_ENABLED=true` to `.env` (used `[System.IO.File]::WriteAllBytes` ASCII per memory `feedback_never_powershell_outfile_env`).
- `docker compose up -d --force-recreate broker-sync-worker` — lowest blast radius per brief.
- Worker recreated cleanly. Scheduler started. All 8 broker_sync_only jobs registered. No tracebacks.

## Step 3 — Soak audit (~6 min window post-flip)

**No regressions, no crashes, no `[phase4_*]` log lines.**

| Check | Result |
|---|---|
| Worker health | Up, healthy |
| Tracebacks / CRITICAL exceptions tied to Phase 4 | 0 |
| `INVERSE RECONCILE [phase4_no_sell]` log lines | 0 |
| `INVERSE RECONCILE [phase1_event_count_0]` log lines | 0 |
| `CONTRADICTION [phase4_has_sell]` log lines | 0 |
| Bracket reconciliation sweep | ran cleanly (7 trades scanned, 5 expected `missing_stop` warnings for known crypto-stop venue gap, 1 agree, 1 price_drift) |
| Crypto stop-loss monitor | fired, 1 stop hit suppressed (normal) |

**Why no Phase 4 log lines:** the `sync_positions_to_db` (Robinhood) inverse-reconcile branch only enters when the broker reports a position alive AND a local Trade row says closed. The Robinhood broker session is currently dead ("[broker] No valid session in DB — user must re-authenticate via web UI"). `get_positions()` returns empty, so the inverse-reconcile branch never fires. The Phase 4 path is loaded and ready; it has not yet been exercised in production.

This is **expected and benign**. The integration verification (Step 1 + the synthetic-sell verify probe) confirms the helper would return correct results when the branch fires.

## Step 4 — Promote

**Decision: PROMOTE.** Flag stays on.

Rationale:
1. Brief Step 2 success criterion ("worker recreates cleanly with flag on, no crashes") met.
2. Helper data is materialized correctly (112 distinct positions with recorded sells).
3. Branch hasn't fired yet but the code path is wired and the data is correct; first real exercise will be the next time RH session is restored AND broker_sync sees a closed-locally / alive-at-broker discrepancy.
4. Rollback is one env-var flip away if a future audit shows a false-positive re-open.

**This decision recorded in `docs/STRATEGY/COWORK_DECISIONS_LOG.md`.**

## Rollback plan (unchanged from brief)

```
# In .env:
CHILI_POSITION_IDENTITY_PHASE4_AUTHORITY_ENABLED=false

# Then:
docker compose up -d --force-recreate broker-sync-worker
```

Legacy `event_count == 0` path resumes in ~30s.

## Open follow-ups (queued, not in scope here)

- **Coinbase exit-path sell-side recording** (`f-coinbase-exit-side-recording`). The Phase 4 inverse-reconcile is currently only in the Robinhood `sync_positions_to_db`. Coinbase has its own sync path that doesn't share this code.
- **Bracket-fired stop event recording** — broker-fired stops aren't yet recorded as live sell events in `trading_execution_events`. Synthetic backfill (mig 254) covers historical closed trades but new bracket-fires would still be missing. Brief queued.
- **RH session restoration** — operator action, blocks any actual Phase 4 firing.
- **Watcher extension** — the daily `CHILI-pid537-watcher` doesn't yet check for Phase 4 firing or false-positive re-opens. Could add a check for "any new INVERSE RECONCILE [phase4_*] log line in the last 24h" and surface in the watcher report.

## Status

NEXT_TASK marked DONE. The position-identity refactor's reader plane (Phase 4) is now **live in production**. Phase 5 (envelope-rename + decision-layer split) becomes the natural follow-on once RH session is restored and Phase 4 has been exercised in real conditions.
