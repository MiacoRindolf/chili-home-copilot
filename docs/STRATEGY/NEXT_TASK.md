# NEXT_TASK: position-identity-phase-1

STATUS: DONE

## Goal

Implement Phase 1 of the position-identity refactor per the locked design at `docs/DESIGN/POSITION_IDENTITY.md` § 8.1. Ship the position layer in shadow mode: new tables exist, broker_sync writes to them, backfill seeds them, audit query verifies parity. **Zero readers depend on the new tables for decisions.** Live behavior is unchanged.

After this task ships and soaks for 1 week clean, Phase 2 becomes the next NEXT_TASK (`trading_execution_events.position_id` backfill).

## Why now

The design doc is locked (`Decisions closed: 2026-05-04`). § 7.1 + § 7.2 + § 8.1 are concrete enough that the migration writes from them directly. Today's COWORK_REVIEW flagged 3 small operator-confirmation items; this brief embeds them inline so they get answered on the PR review rather than blocking another chat round-trip.

## Scope boundary — Phase 1 only

This task ships ONLY the position layer (positions + events tables). Specifically:

- **Phase 1 ships:** `trading_positions` table, `trading_position_events` table, DROP of mig 223 orphan column, shadow-mode writes from `sync_positions_to_db`, backfill script (Trade + PaperTrade), audit query, tests.
- **Phase 1 does NOT ship:** rename of `trading_trades → trading_management_envelopes`, creation of `trading_decisions` table, retarget of `bracket_intents.trade_id → position_id`, modification of any reader path (stop_engine, bracket_writer, inverse-reconcile). All those land in later phases per the design doc § 8.

The `current_envelope_id` FK on `trading_positions` targets **`trading_trades(id)`** in Phase 1, not the future renamed `trading_management_envelopes`. The FK column rename happens when Phase 3 / 4 renames the table; Phase 1 leaves it pointing at today's name.

## Brain integration / source material the executor must read

Cite specific file:line throughout the implementation:

- `docs/DESIGN/POSITION_IDENTITY.md` — the locked design. § 7.1 (positions DDL), § 7.2 (events DDL), § 8.1 (Phase 1 scope + exit criteria), § 11 (decisions, especially 11.1 sync_gap and 11.2 backfill quarantine), § 6.1 + § 6.4 (column-by-column maps for Trade and PaperTrade), § 5.1 (event taxonomy)
- `app/migrations.py` — particularly the convention around `_migration_NNN_*` naming and idempotence. Last migration ID is 223 (`phantom_close_consecutive_zero_qty_sweeps`); this task adds 224 + drops 223's column in the same script. Read `_assert_migration_ids_unique` to confirm collision protection.
- `app/services/broker_service.py:1372-1907` — `sync_positions_to_db`. Phase 1 hooks the new write path inside this function, after R32's wholesale-empty-positions guard (line ~1473) and before C2's phantom check.
- `app/models/trading.py:39-188` — `Trade` ORM (entry shape for backfill).
- `app/models/trading.py:1022-1048` — `PaperTrade` ORM (per § 6.4 mapping).
- `app/services/trading/bracket_reconciliation_service.py` — confirms NO READS from the new tables in Phase 1 (the reader-swap is Phase 3).
- `docs/STAGING_DATABASE.md` — Phase 1 audit query runs against `chili_staging` for production-shaped verification before live deploy.
- `docs/PHASE_ROLLBACK_RUNBOOK.md` — Phase 1 rollback shape.

## Path

**Design principle: zero new magic numbers.** Every threshold derives from observable system state. Where the existing brief implies a derived threshold (e.g., sync_gap detection), this brief specifies the derivation explicitly.

### Step 1 — Migration `_migration_224_position_identity_phase_1`

Single migration covering:

1. **CREATE `trading_positions`** per design doc § 7.1, including the direction column added by the revision pass:

```sql
CREATE TABLE IF NOT EXISTS trading_positions (
    id              BIGSERIAL PRIMARY KEY,
    user_id         INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
    broker_source   VARCHAR(20) NOT NULL,
    account_type    VARCHAR(20) NOT NULL DEFAULT 'cash',  -- 'cash' | 'margin' | 'IRA' | 'spot' | 'portfolio' | 'paper'
    ticker          VARCHAR(20) NOT NULL,
    direction       VARCHAR(10) NOT NULL DEFAULT 'long',
    asset_kind      VARCHAR(20) NULL,                      -- 'equity' | 'crypto' | 'option'
    current_quantity         DOUBLE PRECISION NULL,
    current_avg_price        DOUBLE PRECISION NULL,
    state                    VARCHAR(20) NOT NULL DEFAULT 'unknown',
    current_envelope_id      BIGINT NULL REFERENCES trading_trades(id) ON DELETE SET NULL,
                                                          -- FK targets today's trading_trades; rename in a later phase
    last_observed_at         TIMESTAMP NULL,
    last_state_transition_at TIMESTAMP NULL,
    created_at  TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMP NOT NULL DEFAULT NOW(),
    CONSTRAINT trading_positions_state_check
        CHECK (state IN ('unknown', 'open', 'closed', 'suspect')),
    CONSTRAINT trading_positions_direction_check
        CHECK (direction IN ('long', 'short')),
    CONSTRAINT uq_trading_positions_natural_key
        UNIQUE (user_id, broker_source, account_type, ticker, direction)
);
CREATE INDEX IF NOT EXISTS ix_trading_positions_state_open
    ON trading_positions (broker_source, ticker)
    WHERE state = 'open';
CREATE INDEX IF NOT EXISTS ix_trading_positions_user_broker
    ON trading_positions (user_id, broker_source);
```

2. **CREATE `trading_position_events`** per § 7.2, including the `sync_gap` event type added by the revision pass:

```sql
CREATE TABLE IF NOT EXISTS trading_position_events (
    id                BIGSERIAL PRIMARY KEY,
    position_id       BIGINT NOT NULL REFERENCES trading_positions(id) ON DELETE CASCADE,
    event_type        VARCHAR(20) NOT NULL,        -- opened | qty_change | closed | re_opened | suspect | corrected | sync_gap
    transition_reason VARCHAR(64) NOT NULL,
    quantity          DOUBLE PRECISION NULL,
    avg_price         DOUBLE PRECISION NULL,
    broker_payload    JSONB NULL,
    envelope_id       BIGINT NULL REFERENCES trading_trades(id) ON DELETE SET NULL,
                                                    -- advisory; rename later
    observed_at       TIMESTAMP NOT NULL DEFAULT NOW(),
    recorded_at       TIMESTAMP NOT NULL DEFAULT NOW(),
    CONSTRAINT trading_position_events_event_type_check
        CHECK (event_type IN ('opened', 'qty_change', 'closed', 're_opened', 'suspect', 'corrected', 'sync_gap'))
);
CREATE INDEX IF NOT EXISTS ix_position_events_position_observed
    ON trading_position_events (position_id, observed_at DESC);
CREATE INDEX IF NOT EXISTS ix_position_events_event_type_observed
    ON trading_position_events (event_type, observed_at DESC);
```

3. **DROP `trading_bracket_intents.phantom_close_consecutive_zero_qty_sweeps`** — orphan column from mig 223:

```sql
ALTER TABLE trading_bracket_intents
    DROP COLUMN IF EXISTS phantom_close_consecutive_zero_qty_sweeps;
```

Idempotent (`DROP COLUMN IF EXISTS`). Safe to re-run on environments that already had the column dropped.

**Migration ID 224.** Verify via `_assert_migration_ids_unique`. Run `scripts/verify-migration-ids.ps1` ahead of merge.

### Step 2 — Shadow-mode write path in `sync_positions_to_db`

Modify `app/services/broker_service.py::sync_positions_to_db` to write to the new tables alongside today's behavior. The new code path is **additive** — it never modifies today's `trading_trades` writes, never blocks today's logic, never raises an exception that escapes the function (failures inside the new path log + continue).

Insert the new write path AFTER R32's wholesale-empty-positions guard (line ~1473) and BEFORE C2's phantom guard. For each broker position observed in the cycle:

```python
# Phase 1 shadow-mode position-layer write. NEVER raises; failures log + continue.
try:
    _phase1_record_position_observation(
        db,
        user_id=user_id,
        broker_source="robinhood",
        account_type=_resolve_account_type(bp),  # 'cash' for now; ready for 'margin'/'IRA' later
        ticker=bp.ticker,
        direction=_resolve_direction(bp),  # 'long' default; broker doesn't expose short for retail RH
        asset_kind=_infer_asset_kind(bp.ticker),
        broker_qty=float(bp.quantity),
        broker_avg=float(bp.avg_price) if bp.avg_price else None,
        broker_payload=bp.raw,  # the full broker response JSONB
    )
except Exception:
    logger.warning("[phase1_position_event] write failed; shadow-mode continues", exc_info=True)
```

The helper `_phase1_record_position_observation`:

1. Looks up the existing `trading_positions` row by natural key.
2. If not found → `INSERT` with `state='open'`, write a `trading_position_events` row with `event_type='opened'` and `transition_reason='broker_sync_first_observation'`.
3. If found and `state='closed'` → state becomes `open` again. Write `event_type='re_opened'` and `transition_reason='broker_sync_position_reappeared'`. Update snapshot fields.
4. If found and quantity differs → write `event_type='qty_change'` with `transition_reason='broker_sync_qty_observation'`. Update snapshot.
5. If found and identical → no event written. Just bump `last_observed_at`.
6. **Sync-gap detection:** if the prior event for this position was older than `2 × broker_sync_cron_interval_seconds`, write a `sync_gap` event BEFORE the current observation event. The 2× factor derives from `app/services/trading_scheduler.py`'s `broker_sync` cron (currently every 2 min → 4-min threshold). NO MAGIC NUMBER — read the cron interval from the scheduler config; the 2× multiplier is the next-cycle-plus-tolerance derivation noted in design doc § 11.1.

After the per-position loop, handle "broker dropped a position" (broker_sync's existing close path). For each position currently `state='open'` whose ticker is NOT in the broker's response this cycle, write `event_type='closed'` with `transition_reason='broker_sync_position_gone'`. State transitions to closed; snapshot quantity → 0.

**Critical invariant:** no READER changes in Phase 1. The new `trading_positions.state` is NOT consulted by stop_engine, bracket_writer, inverse-reconcile, or anywhere else. Other code paths still read `trading_trades.status`. The position layer accumulates data; downstream consumers ignore it until Phase 4.

### Step 3 — Backfill script `scripts/backfill_position_rows.py`

Idempotent script that walks `trading_trades` AND `trading_paper_trades` and seeds `trading_positions` rows + initial events.

```
Algorithm:
1. SELECT DISTINCT (user_id, broker_source, COALESCE(account_type, 'cash'), ticker, direction)
   FROM trading_trades
   WHERE status='open'
   UNION ALL
   SELECT DISTINCT (user_id, NULL, 'paper', ticker, direction)
   FROM trading_paper_trades
   WHERE status='open'

2. For each distinct key:
   - INSERT INTO trading_positions ON CONFLICT (natural_key) DO NOTHING
   - If newly inserted: write trading_position_events with event_type='opened', transition_reason='backfill_initial'

3. For closed Trade/PaperTrade rows (subset where the position-as-a-concept later was reopened):
   - Walk by entry_date order; for each closed row, write a synthetic 'closed' event with transition_reason='backfill_pre_refactor:<original_exit_reason>'

4. Print summary: positions created, events written, paper-mode positions, ambiguous rows skipped (if any)
```

Idempotent: re-runs use `ON CONFLICT DO NOTHING` for positions and a `WHERE NOT EXISTS` guard for events. Safe to run multiple times during Phase 1 soak.

### Step 4 — Audit query `scripts/audit_position_layer_parity.py`

Compares `trading_positions` snapshot against today's broker-reported truth. Runs at least once per soak day; ideally on every broker_sync cycle as a sanity check.

```
For each row in trading_positions WHERE state='open':
  Look up matching live broker position (rh.account.get_open_stock_positions, etc.)
  Assert: trading_positions.current_quantity == broker.qty (within 1e-9)
  Assert: trading_positions.current_avg_price ~ broker.avg (within 1e-6)
  Discrepancies: log + write to a quarantine view (per § 11.2 Decision C)
For each broker-reported position NOT in trading_positions WHERE state='open':
  Likely a backfill miss. Log + queue for operator review.
```

The 1e-9 and 1e-6 tolerances are float-equality conventions, NOT magic-number thresholds — they match the existing tolerance pattern used in `_try_emergency_repair_terminal_reject` and elsewhere in the bracket layer. No new behavioral threshold.

### Step 5 — Tests `tests/test_position_identity_phase_1.py`

At minimum:

- **A: migration applies cleanly.** Fresh `chili_test`, run migrations to head; assert `trading_positions` and `trading_position_events` exist with correct constraints; assert `trading_bracket_intents.phantom_close_consecutive_zero_qty_sweeps` does not exist.
- **B: opened event on first broker observation.** Mock broker_sync with one position; assert position row inserted, opened event written.
- **C: qty_change event on quantity diff.** Same mock with different qty on second cycle; assert qty_change event written, snapshot updated.
- **D: closed event on broker-drop.** Position present cycle 1, absent cycle 2; assert closed event + state transition.
- **E: re_opened event when broker reports previously-closed position.** State 'closed' + broker reports it again; assert re_opened event + state→open.
- **F: sync_gap event when prior event is stale.** Mock the cron interval; force prior event timestamp older than 2×; assert sync_gap event written before the current observation.
- **G: direction in natural key.** Long 100 + short 50 of same ticker = 2 distinct position rows.
- **H: account_type='paper'.** PaperTrade-style observation creates position with account_type='paper'.
- **I: backfill covers Trade and PaperTrade.** Seed both tables; run backfill; assert positions created for both kinds, opened events written.
- **J: shadow-mode no-readers.** No code in stop_engine, bracket_reconciliation, bracket_writer, inverse-reconcile reads from trading_positions for decisions in Phase 1. Verified by grep — assert no reads.
- **K: idempotent migration.** Re-run mig 224; assert no errors, no duplicate columns.

All tests against `chili_test` (PROTOCOL Hard Rule 5).

## Operator confirmation items (resolve on PR review, not blocking)

These three came from the prior COWORK_REVIEW. The brief proceeds with the recommendations; operator can flip any of them on PR review.

1. **`pnl_pct` column on the renamed envelope table** — Cowork recommended adding nullable `pnl_pct` to `trading_management_envelopes` for PaperTrade parity. This brief defers that to the rename phase (NOT Phase 1). PaperTrade's current `pnl_pct` column stays intact in `trading_paper_trades` until the rename happens. **Ack on PR review.**

2. **Sync-gap detection threshold derivation** — this brief uses `2 × broker_sync_cron_interval_seconds` (read from scheduler config; today = 2 min × 2 = 4 min). Operator can suggest a different multiplier on PR review (e.g., 1.5× for stricter, 3× for laxer). Default stays at 2× per design doc § 11.1.

3. **2-week Phase 5 soak feasibility** — confirmation of "no external dashboards / BI consumers that need longer migration runway." Not a Phase 1 concern; just an awareness item carried forward.

## Constraints / do not touch

- **No reader changes.** No code in stop_engine, bracket_writer, bracket_reconciliation_service, inverse-reconcile, or any decision-making path reads from `trading_positions` or `trading_position_events` in Phase 1. Verified by grep test.
- **No live behavior change.** Today's `trading_trades` writes continue exactly as today. The new path is purely additive shadow-mode.
- **No magic numbers.** Sync-gap threshold derives from the scheduler's broker_sync cron interval × 2; float-equality tolerances match existing conventions. Anything else surfaces as Open Question.
- **No `_migration_224_*` collisions.** Verify via `_assert_migration_ids_unique` and `scripts/verify-migration-ids.ps1`.
- **No rename of `trading_trades`.** That happens later. Phase 1's FK columns target `trading_trades(id)`.
- **No creation of `trading_decisions` table.** Same — later phase.
- **No `trading_execution_events.position_id` column.** That's Phase 2.
- **Tests use `_test`-suffixed DB.** PROTOCOL Hard Rule.
- **No `git push --force` to main.** PROTOCOL Hard Rule.
- **One logical commit per success criterion 1.** Don't bundle unrelated changes.

## Out of scope

- **Phase 2-6 work.** Phase 2 is the next NEXT_TASK once Phase 1 soaks 1 week clean.
- **Operator pre-actions** (kill switch reset, EKSO/ELTX P/L cleanup) — operator-driven, decoupled.
- **The Phase 6 multi-leg-order language tightening** in the design doc — flagged for a follow-up doc revision before Phase 6 lands. Not blocking Phase 1.
- **`trading_decisions` table creation, `trading_management_envelopes` rename, `bracket_intents.position_id` retarget** — all later phases.
- **`pnl_pct` column on envelope table** — deferred to the rename phase per Operator confirmation #1 above.

## Success criteria

1. **Three commits, all pushed:**
   - `feat(migrations): 224 position identity phase 1 — create trading_positions + trading_position_events + drop mig 223 orphan column`
   - `feat(broker_sync): position-layer shadow-mode write path + backfill script + audit query`
   - `docs(strategy): position-identity-phase-1 CC report + mark NEXT_TASK done`
2. **Migration applies cleanly** on staging + production. New tables exist with all constraints, indexes verified via `\d`. Orphan column dropped + verified.
3. **Audit query parity for at least 1 week.** After cutover, every broker_sync cycle's audit query reports zero discrepancies for active positions. CC_REPORT includes the audit-query results from at least the first 5 sweeps post-deploy.
4. **Backfill complete.** Every distinct (user_id, broker_source, account_type, ticker, direction) from current `trading_trades` AND `trading_paper_trades` has a corresponding `trading_positions` row + at least one event. Quarantine count < 5 (per design doc § 11.2 acceptable threshold; surfaces in CC report if higher).
5. **Magic-number audit clean.** CC report enumerates every literal added in Step 1-4. Expected count of new behavioral thresholds: zero.
6. **Tests A-K pass** against `chili_test`.
7. **Live verification (post-deploy):**
   - `docker compose exec -T postgres psql -U chili -d chili -c "\d trading_positions"` — table exists with all expected columns and indexes.
   - `docker compose exec -T postgres psql -U chili -d chili -c "\d trading_position_events"` — same.
   - `docker compose exec -T postgres psql -U chili -d chili -c "\d trading_bracket_intents"` — orphan column gone.
   - `docker compose logs broker-sync-worker --since 5m` — first 2-3 broker_sync cycles after deploy show position-layer write log lines (`[phase1_position_event]` or similar). No exceptions in the new write path.
   - At least one `sync_gap` event row appears within 24h post-deploy IF a broker_sync cycle was missed (e.g., due to broker auth flap); zero `sync_gap` events if all cycles ran clean.
8. **CC_REPORT** at `docs/STRATEGY/CC_REPORTS/2026-05-04_position-identity-phase-1.md` per PROTOCOL format. Include:
   - Migration ID confirmation (224).
   - Magic-number audit (per success criterion 5).
   - Backfill summary (positions created, events written, paper-mode count, quarantine count).
   - Audit-query results from at least the first 5 post-deploy sweeps.
   - Operator-confirmation items (3 items in this brief — answer each).
   - The `\d` output for the two new tables.
9. **Operator review and acknowledgement** of the 3 confirmation items in the section above.

## Rollback plan

- **Code rollback (broker_service.py):** `git revert <fix-commit>` reverts the shadow-mode write path. The new tables stay populated through the cycles that ran with the new code; subsequent cycles stop writing. No live system effect (no readers).
- **Migration rollback:** **DO NOT REVERT mig 224.** It's additive (new tables) plus a column DROP that's already been reasoned about. Reverting would orphan the application reference if any code-revert is partial. Standard CHILI practice per `docs/PHASE_ROLLBACK_RUNBOOK.md` — additive migrations stay forward.
- **Backfill rollback:** truncate `trading_positions` and `trading_position_events` if the backfill produced bad data; re-run the script (idempotent). Truncate is operator-only; in normal operation the data stays.
- **Hard-stop rollback:** if a bug in the shadow-mode write path is corrupting the new tables (despite the try/except), `TRUNCATE trading_position_events; TRUNCATE trading_positions;` clears the slate; subsequent cycles re-populate from broker truth.
- **No live broker rollback needed.** This task does NOT cancel or place broker orders.

## Verification commands (for the executor + the operator)

```powershell
# Pre-deploy: confirm migration ID is unique
.\scripts\verify-migration-ids.ps1

# Post-deploy: tables exist
docker compose exec -T postgres psql -U chili -d chili -c "\d trading_positions"
docker compose exec -T postgres psql -U chili -d chili -c "\d trading_position_events"
docker compose exec -T postgres psql -U chili -d chili -c "\d trading_bracket_intents" | Select-String "phantom_close_consecutive_zero_qty_sweeps"
# Expect: 0 hits (column dropped)

# Run backfill
docker compose exec -T scheduler-worker python /app/scripts/backfill_position_rows.py

# Verify backfill seeded both kinds
docker compose exec -T postgres psql -U chili -d chili -c "
  SELECT account_type, COUNT(*) FROM trading_positions GROUP BY account_type;
"
# Expect: 'cash' for live equities + 'paper' for paper-mode + others as applicable

# Watch the shadow-mode write path
docker compose logs broker-sync-worker --since 10m -f | Select-String -Pattern "phase1_position_event|trading_position_events|sync_gap"

# Run audit query
docker compose exec -T scheduler-worker python /app/scripts/audit_position_layer_parity.py

# Tests
$env:TEST_DATABASE_URL='postgresql://chili:chili@localhost:5433/chili_test'
pytest tests/test_position_identity_phase_1.py -v
```

## Open questions for Cowork (surface in your CC_REPORT)

1. **Direction default for crypto positions.** All Coinbase spot positions are `direction='long'` (no shorting). Confirm the default in `_resolve_direction` defaults to 'long' AND that perps venues (when they come online) signal differently. Surface for design-pass-2 if the perps integration shape changes the assumption.

2. **`account_type` resolution for crypto.** Robinhood crypto reports under the same account; Coinbase has distinct spot/portfolio surfaces. Phase 1 hardcodes `account_type='cash'` for everything; confirm this is okay for v1 and the autopilot routing (Phase 7) refines later.

3. **Backfill pruning of historical closed Trades.** Today's `trading_trades` table has 1800+ rows including many closed-then-recreated cycles. Backfill walks them all but Phase 1's exit criteria only check active positions. Surface the count of "synthetic events written from closed historical trades" — operator may want to cap or trim.

4. **The 3 operator-confirmation items in this brief.** Answer each in the CC report (or surface back to operator).

## Forward pointer

After Phase 1 ships and soaks 1 week clean, the next NEXT_TASK is **`position-identity-phase-2`** — `trading_execution_events.position_id` backfill per design doc § 8.2. Phase 2's exit criteria from the design doc become Phase 2's success criteria directly.
