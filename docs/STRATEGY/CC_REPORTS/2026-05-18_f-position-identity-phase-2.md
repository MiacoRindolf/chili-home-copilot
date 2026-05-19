# CC_REPORT: f-position-identity-phase-2-execution-events-position-id-backfill

**Session type:** Cowork-direct execution (operator approved 2026-05-18 with "ship the next one" after Tier A + watcher landed).

## What shipped

**Single commit on `main`** (hash visible in `git log` post-push), 4 files / **+512 / -0**:

- `app/migrations.py` (+216) — `_migration_248_execution_events_position_id`
- `app/models/trading.py` (+11) — `TradingExecutionEvent.position_id` BIGINT FK
- `app/services/trading/execution_audit.py` (+77) — `_resolve_position_id_for_event` + double-write integration in `record_execution_event`
- `tests/test_position_identity_phase2.py` (+208) — 11 pinned tests

**Migration 248** is a single coherent unit with five steps, each in its own try/except:

1. `ALTER TABLE trading_execution_events ADD COLUMN IF NOT EXISTS position_id BIGINT NULL` with FK to `trading_positions(id) ON DELETE SET NULL`.
2. `CREATE INDEX IF NOT EXISTS ix_trading_execution_events_position_id ... WHERE position_id IS NOT NULL` (partial).
3. `CREATE OR REPLACE VIEW trading_execution_events_quarantine` selecting unresolved rows with categorical `quarantine_reason`.
4. **Option A closed-position seed** — INSERT INTO trading_positions for every `(user, broker, 'cash', ticker, 'long')` natural-key tuple in `trading_trades` that didn't yet exist. Idempotent via `NOT EXISTS`.
5. **Execution events backfill** — UPDATE trading_execution_events SET position_id via natural-key join through trade_id → trading_trades → trading_positions. Prefers `state='open'` over `state='closed'` when both match (handles close/reopen cycles).

## Verification

**Tests.** `pytest tests/test_position_identity_phase2.py -v -p no:asyncio` → **11 passed in 1.33s**. Coverage: resolver match/miss/null-input behavior, broker lower-casing, direction default, DB-exception swallow, reader canary (static grep ensuring no production code reads `position_id` outside `execution_audit.py`).

**Deploy.** `docker compose up -d --force-recreate chili scheduler-worker brain-worker autotrader-worker broker-sync-worker` clean, exit 0.

**Post-deploy live DB observations** (probe in `scripts/dispatch-phase2-postdeploy-out.txt`):

| Check | Result |
|---|---|
| `schema_version` tip | `248_execution_events_position_id`, applied 2026-05-19 02:45:06 UTC ✓ |
| `position_id` column | BIGINT NULL, FK to trading_positions(id) ✓ |
| Partial index | `ix_trading_execution_events_position_id ... WHERE position_id IS NOT NULL` ✓ |
| `trading_execution_events_quarantine` view | created ✓ |
| `trading_positions` cardinality | **33 → 201** (168 historical closed-state positions seeded by Option A) |
| `trading_execution_events` total | 15,155 |
| Resolved (with position_id) | **8,358 = 55.15%** |
| Unresolved | 6,797 |
| With trade_id cohort resolution rate | **8,358 / 8,358 = 100.00%** |
| Null trade_id cohort resolution rate | 0 / 6,797 (expected per brief) |
| Quarantine breakdown | exclusively `null_trade_id` (6,797 rows); zero `orphan_trade_id`, zero `no_matching_position`, zero `resolution_failed_other` |

## Surprises / deviations

1. **Brief's cardinality estimates have grown.** The brief was drafted 2026-05-11 against an 11,999-row `trading_execution_events`. Today's table is 15,155 rows — 26% growth in a week. Option A still works perfectly; the resolution rate is the load-bearing number and it's 100% for the resolvable cohort.

2. **No new fill events observed in the 10-minute post-deploy window.** The worker restart was clean but no live broker activity hit `record_execution_event` during my probe. The double-write pathway is exercised by my unit tests (which mock the resolver) but not yet live-fired. **First live verification will come on the next real fill** — which the daily pid 537 watcher and any future audit query against the partial index will surface.

3. **Brief estimated ~5,305 closed-trade events to resolve under Option A; actual 8,358.** This is the same growth pattern as item 1.

4. **`account_type='cash'` used uniformly in the closed-position seed.** Matches Phase 1's `_resolve_account_type_for_position` convention. When Phase 7 (per design § 8.7) refines per-account-type rules, the historical rows will need an `UPDATE` script to retroactively set Coinbase rows to `'spot'`. Not in scope here.

## Deferred

- **Phase 3 — authority flip** (`f-position-identity-phase-3-bracket-intent-position-id-retarget` per CURRENT_PLAN). Adds `position_id` to `trading_bracket_intents`, swaps reader paths under a feature flag. Logical next step after this Phase 2.
- **Phase 4 — inverse-reconcile rewrite.** Replaces the conservative `event_count == 0` workaround with position-level history lookup. Phase 2 just landed the foundation.
- **TCA wiring** (`f-tca-writer-wiring`). Still queued; Phase 2 unblocked it but didn't touch it.
- **Pid 537 watcher** continues running daily; no change to its scope.
- **`git push`** — pending operator (PROTOCOL Hard Rule blocks daemon-driven push to main).

## Open questions for Cowork

1. **Phase 3 sequencing vs TCA wiring.** Both are now actionable. Phase 3 continues the position-identity arc (smaller blast radius, contains the slippage cost of orphan brackets). TCA wiring unlocks slippage measurement (different problem area, different code surface). My recommendation: Phase 3 next — same code surface, momentum on the refactor, the inverse-reconcile rewrite is what actually retires the `*_position_gone` strings. TCA wiring after.

2. **Historical `account_type` cleanup.** The 168 seeded rows all have `account_type='cash'`. Coinbase positions should arguably be `'spot'`. Cheap to fix with a follow-up SQL update; not blocking anything.

3. **Quarantine view triage policy** (operator decision from brief §11.2). 6,797 `null_trade_id` events sit in quarantine. Options: (a) tag with synthetic positions via order-history lookup, (b) accept as legacy audit-only, (c) separate brief. My read: (b) is fine; these are pre-`trade_id` events that have no clean upstream resolution.

## Rollback plan

1. `git revert <hash>` — removes resolver + double-write at `record_execution_event`. The column, index, view, seeded positions, and backfilled position_id values stay. New events lose double-write.
2. `ALTER TABLE trading_execution_events DROP COLUMN IF EXISTS position_id CASCADE` — drops the column and the partial index. Quarantine view would auto-fail; drop separately if needed: `DROP VIEW IF EXISTS trading_execution_events_quarantine`.
3. To purge backfilled position_id values without dropping the column: `UPDATE trading_execution_events SET position_id = NULL`.
4. To delete the 168 seeded closed positions: `DELETE FROM trading_positions WHERE state='closed' AND id NOT IN (<original 28 ids>)`. Not recommended; they're harmless.

## Status

NEXT_TASK marked DONE. Phase 2 brief in `docs/STRATEGY/QUEUED/` annotated as shipped. CURRENT_PLAN updated. Memory entry written.
