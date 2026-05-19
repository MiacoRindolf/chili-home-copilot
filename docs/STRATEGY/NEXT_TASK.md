# NEXT_TASK: f-position-identity-phase-4-flag-flip-paper-soak

STATUS: PENDING

## Goal

Operator-driven paper-soak of the Phase 4 inverse-reconcile flag (`CHILI_POSITION_IDENTITY_PHASE4_AUTHORITY_ENABLED`). The sell-side recording fix (mig 254 + writer hooks at `robinhood_exit_execution.py`) landed and populated 450 sell events covering 112 distinct positions. The Phase 4 reader can now safely consult position-level fill history.

This brief is a soak/audit/promote sequence, not a code change.

## Why this is next

Phases 1-4 of the position-identity refactor + the sell-side recording fix all shipped in this session. The remaining gate to retire the conservative `event_count == 0` workaround is operator validation that flipping the flag produces sensible inverse-reconcile decisions in flight.

## Procedure

### Step 1 — Pre-flip audit (5 min)

```sql
-- Confirm sell-side data is populated:
SELECT COUNT(*) AS sells_recorded
FROM trading_execution_events
WHERE status='filled' AND LOWER(payload_json->>'side')='sell';
-- Expected: 450+ (was 0 before mig 254)

-- Confirm position-level distribution looks healthy:
WITH cohort AS (
  SELECT p.id, p.state,
         EXISTS(SELECT 1 FROM trading_execution_events e
                WHERE e.position_id=p.id AND e.status='filled'
                  AND LOWER(e.payload_json->>'side')='sell') AS has_sell
  FROM trading_positions p
)
SELECT state, has_sell, COUNT(*) FROM cohort GROUP BY state, has_sell;
-- Expected: ~107 closed with sell, ~89 closed without, ~5 open with, 0 open without
```

### Step 2 — Flip to shadow comparison (15 min)

Briefly enable the flag and observe a single `broker_sync` cycle:

```bash
# In .env:
CHILI_POSITION_IDENTITY_PHASE4_AUTHORITY_ENABLED=true

# Restart broker_sync_worker only (lowest blast radius):
docker compose up -d --force-recreate broker-sync-worker

# Watch logs for one broker_sync cycle (~2min):
docker compose logs -f broker-sync-worker | grep -E "INVERSE RECONCILE|CONTRADICTION"
```

Expected: log lines tagged `[phase4_no_sell]` or `[phase4_has_sell]`. No re-opens that don't make sense.

### Step 3 — Compare against legacy

For every `[phase4_no_sell]` re-open log line, sanity-check:
- Does the position have a recent Trade row with `exit_date` set?
- If yes: there was a real sell history; Phase 4 should NOT have flagged it for re-open. Investigate.
- If no: Phase 4 correctly identified a bookkeeping-only close. ✓

For every `[phase4_has_sell]` CONTRADICTION log:
- Is broker reporting the position as alive RIGHT NOW?
- Is the qty/price matching the prior closed Trade?
- If yes: this is a "re-bought after sell" case; Phase 4 correctly refuses to re-open the dead row. Operator should investigate whether the brain wants to track this as a new entry.

### Step 4 — Promote or rollback

After ~24h of soak with no anomalies:

- **Promote:** leave the flag on; document the flip date in `docs/STRATEGY/COWORK_DECISIONS_LOG.md`.
- **Rollback:** set the flag back to `false` and `docker compose up -d --force-recreate broker-sync-worker`. The legacy `event_count == 0` path resumes immediately.

## Success criteria

1. Step 1 audit confirms sell-side data is populated.
2. Step 2 produces at least one `[phase4_*]` log line (any decision; just proves the new path is firing).
3. Step 3 inspection finds no false-positive re-opens.
4. After 24h, Phase 4 path is durably enabled OR the operator decides Phase 5 is higher priority and parks the flag off.

## Out of scope

- Any code changes. This is a soak/decision brief.
- Coinbase exit-path sell-side recording (separate brief `f-coinbase-exit-side-recording`).
- Bracket-fired stop event recording (separate brief).
- Phase 5 (envelope-rename) — separate refactor, waits for Phase 4 to be durably enabled OR explicitly deferred.

## Rollback plan

`CHILI_POSITION_IDENTITY_PHASE4_AUTHORITY_ENABLED=false` + `docker compose up -d --force-recreate broker-sync-worker`. Legacy path resumes in ~30s. Phase 4 code stays; just dormant again.

## Reference

- Phase 4 CC report: `docs/STRATEGY/CC_REPORTS/2026-05-18_f-position-identity-phase-4.md`
- Sell-side recording CC report: `docs/STRATEGY/CC_REPORTS/2026-05-18_f-execution-events-sell-side-recording.md`
- Helper: `app/services/trading/position_resolver.position_has_recorded_sell`
- Reader: `broker_service.sync_positions_to_db` inverse-reconcile branch (~line 1944)
