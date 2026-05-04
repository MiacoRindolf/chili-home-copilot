# Design: Position Identity Refactor

**Status:** draft for operator + Cowork review (2026-05-04). Doc-only at this stage; no code lands until the questions in **§ Open Questions for operator** are answered.

**Forward pointer:** after this doc is reviewed and revised, the next NEXT_TASK is Phase 1 implementation (the `trading_positions` table + shadow-mode write path in `broker_sync`).

---

## 1. Architectural problem

Today's CHILI model collapses three distinct concepts into a single ephemeral row, `trading_trades`:

1. **The decision to enter** — entry pattern, scan_pattern_id, related_alert_id, mesh_entry_correlation_id, strategy_proposal_id, asset_kind, indicator_snapshot.
2. **The management envelope** — stop_loss, take_profit, trail_stop, high_watermark, stop_model, exit_reason, pending_exit_*, scale_in_count, auto_trader_version.
3. **The broker position** — broker_source, broker_order_id, broker_status, last_broker_sync, filled_*, avg_fill_price.

When any one of these dies, all three die together. Today (2026-05-04) shipped four coordinated patches that worked but exposed the ceiling:

### 1.1 broker-truth-self-heal: `event_count == 0` is a workaround, not a fix
The inverse-reconcile branch in `app/services/broker_service.py::sync_positions_to_db` re-opens a closed Trade row when broker still reports the position AND `trading_execution_events` count is 0 for that `trade_id`. This relies on `trade_id` being stable across the position's lifetime. It isn't. If a Trade row dies and is recreated (e.g., by C2 phantom-guard fallback), the new row's `trade_id` has zero events even though the position has a long fill history. Inverse-reconcile would refuse to heal — even though the underlying broker position is the same physical position. See `app/services/broker_service.py:1444-1538` for the inverse-reconcile + GGG-revive + C2 cascade.

### 1.2 bracket-writer-respect-upside-targets: pending decisions live on a dying envelope
The pending_decision row in `trading_bracket_intents.payload_json` is keyed via `bracket_intent.trade_id` FK. If the envelope dies before the operator answers, the pending decision dies with it — the operator-input-pending state is lost even though the position is still alive at the broker. See `app/services/trading/bracket_writer_g2.py::record_pending_bracket_decision` and the FK path at `app/services/trading/bracket_intent_writer.py::upsert_bracket_intent`.

### 1.3 bracket-emergency-repair-flap-guard: orphan column from working-around envelope identity
Migration 223 added `phantom_close_consecutive_zero_qty_sweeps INTEGER NOT NULL DEFAULT 0` to `trading_bracket_intents` to track auth-flap retries against an envelope. The next task retired the entire path that wrote that column. The column is now dead code at the schema layer — a symptom of patching at the envelope layer when the right layer is the position. See `app/migrations.py:14671` for the orphan column.

### 1.4 The 5 cancelled covering limit-sells (AIDX/CCCC/CRDL/TLS/VFS, 2026-05-04 19:14)
The bracket writer cancelled operator-authored profit-targets to free shares for SELL_STOPs. The pending-decision surface fixes the *policy* gap, but the *deeper* gap is that the writer can't see "the position has both a target limit and wants a stop" as one coordinated state — it only sees per-Trade-row data. Position-level visibility into ALL active broker orders for the position is what enables the writer to coordinate with existing protection rather than racing to replace it. The one-sell-per-share constraint at Robinhood retail is a venue fact; coordinating around it requires position-level state.

### 1.5 The architectural ceiling
Every patch shipped today added complexity at the wrong layer (Trade row / bracket intent) to work around the missing position layer. The next dozen patches will keep doing the same thing. The structural fix is to introduce the position layer once and let the patches retire.

---

## 2. Operator-stated design answers (load-bearing inputs)

The operator answered five design questions in the prior chat turn. Each is reflected verbatim with paraphrased Cowork interpretation.

| # | Operator quote | Interpretation in this doc |
|---|---|---|
| 1 | *"I'm not sure, what can make the more profit without losing quality?"* (account granularity) | Aggregate by `(user_id, broker_source, ticker)`. Account-type column reserved on the schema for forward-compat (single-account-per-broker today; multi-account ready). See § 4.1. |
| 2 | *"for long trades, just use rh, for scalping just coinbase"* + *"autopilot settings page so I can enable/disable... per broker"* (cross-broker same-ticker) | Separate position rows per `broker_source`. Phase 7 sketch (§ 9) shows the autopilot settings page consuming `trading_positions` + a new `autopilot_routing_rules` table. |
| 3 | *"event-driven then"* (snapshot vs event-sourced) | Event-sourced. Snapshot fields on `trading_positions` are derived materializations of `trading_position_events`, not primary truth. State rebuildable from events at any time. See § 5. |
| 4 | *"I think separating them is a cleaner approach"* (three-layer split) | Three layers: `trading_decisions` (immutable entry intent) → `trading_management_envelopes` (today's `trading_trades` minus entry-decision fields) → `trading_positions` (broker-authoritative identity). See § 3. |
| 5 | *"I give you the decision for this basing on your investigation"* (mig 223 orphan column) | Bundle DROP into Phase 1's coordinated migration. Single schema change rather than a separate hygiene ticket. See § 8.1. |

The operator's framing on the broader initiative — *"I just vibe coded this so proper data structure and algorithms refactoring must be done"* — is the standing license for this redesign. The doc cites operator-stated intent rather than inventing it.

---

## 3. Three-layer model

### 3.1 Decision layer — `trading_decisions` (NEW)
**Owns:** the immutable "we decided to enter this trade for this reason" record. Never mutated after creation. Persists for as long as audit/reporting needs it (i.e., effectively forever).

**Columns currently on `trading_trades` that move here:**
- `scan_pattern_id`, `related_alert_id`, `strategy_proposal_id` (FKs to the source signals)
- `pattern_tags`, `mesh_entry_correlation_id`, `auto_trader_version` (provenance)
- `indicator_snapshot` (point-in-time market state at the moment of decision)
- `tca_reference_entry_price` (the limit/quote at decision time — TCA reference)
- `entry_date` (when we decided)

**Owned operations:** `INSERT` only. Read-by-id from envelope rows for backwards-compat reporting. Never UPDATE'd.

### 3.2 Management envelope layer — `trading_management_envelopes` (renamed `trading_trades`)
**Owns:** the dynamic "how we are managing this active interaction with the broker right now" state. Created when an entry decision becomes active broker-side; closed when the management cycle ends (full exit, manual close, broker reconciliation). A new envelope binds when a position re-opens after closing.

**Columns currently on `trading_trades` that stay here (mutable management state):**
- `stop_loss`, `take_profit`, `trail_stop`, `high_watermark`, `stop_model` (engine-managed brackets)
- `pending_exit_*` family (active exit-order tracking)
- `scale_in_count` (envelope-scoped)
- `tca_*_exit_*` (exit-time TCA)
- `exit_reason`, `exit_date`, `exit_price`, `pnl` (envelope's own close state)
- `quantity`, `direction`, `filled_quantity`, `remaining_quantity`, `filled_at`, `submitted_at`, `acknowledged_at`, `first_fill_at`, `last_fill_at`, `avg_fill_price` — envelope's own broker-interaction journal
- `broker_order_id`, `broker_status`, `last_broker_sync` (envelope's bound broker order)
- `tags`, `notes`

**New columns on this layer:**
- `decision_id BIGINT NOT NULL` FK → `trading_decisions.id` (every envelope traces to its decision)
- `position_id BIGINT NOT NULL` FK → `trading_positions.id` (every envelope binds to a position)

**Owned operations:** Created when a decision becomes active. Mutated as the envelope manages the position. Set to `status='closed'` when this management cycle ends. Multiple envelope rows can exist over time for the same `position_id` (one per re-open).

### 3.3 Position layer — `trading_positions` (NEW)
**Owns:** the broker-authoritative "do we hold this thing?" identity. One row per `(user_id, broker_source, ticker)` natural key (account_type included for forward-compat per § 4.1). Persists for the lifetime of the position-as-a-concept; outlives any individual envelope.

**Columns:**
- `id BIGINT PRIMARY KEY`
- `user_id INT FK` (nullable per existing `trading_trades.user_id` semantics)
- `broker_source VARCHAR(20) NOT NULL`
- `account_type VARCHAR(20) NOT NULL DEFAULT 'cash'` (forward-compat for cash/margin/IRA/spot/portfolio)
- `ticker VARCHAR(20) NOT NULL`
- `current_quantity DOUBLE PRECISION` (derived from event stream — see § 5)
- `current_avg_price DOUBLE PRECISION` (derived)
- `state VARCHAR(20) NOT NULL DEFAULT 'unknown'` (`open` | `closed` | `unknown`)
- `current_envelope_id BIGINT NULL` FK → `trading_management_envelopes.id` (nullable; the active envelope managing this position right now, if any)
- `last_observed_at TIMESTAMP` (when broker_sync last confirmed)
- `last_state_transition_at TIMESTAMP`
- `created_at TIMESTAMP NOT NULL DEFAULT NOW()`
- `updated_at TIMESTAMP NOT NULL DEFAULT NOW()`

**Natural key uniqueness:** `UNIQUE(user_id, broker_source, account_type, ticker)`.

**Owned operations:** Created on first broker observation of a position. Snapshot fields (`current_quantity`, `current_avg_price`, `state`) updated by the event-stream materialization. Closed (`state='closed'`) when broker no longer reports the position AND the `position_state` event derivation confirms closure. Re-opened (back to `state='open'`) when broker reports the position again after a closed state — same `id` kept (the position-as-a-concept survives the gap; this is the operator-promised "buy back the same ticker = same position id" semantic).

### 3.4 Why three layers, not two — concrete examples

**Example A: trade re-entry on the same ticker.** Operator buys 100 AAPL via autotrader on Mon (decision_1 → envelope_1 → position_id=42). Tue, broker fills sell at stop, envelope_1 closes. Wed, autotrader re-enters 100 AAPL (decision_2 → envelope_2 → position_id=42 — same row, state goes closed→open). Reporting can show: "position 42 had two management cycles, two distinct entry decisions, lifetime P/L = sum of envelopes."

In the two-layer model (no decision separate from envelope), envelope_1's exit_reason and pnl are gone the moment a new envelope binds. Reporting would have to walk fragile join paths.

**Example B: stop-engine evaluation when the envelope is mid-rebind.** Today, if a Trade row is briefly closed (e.g., during inverse-reconcile's cycle), the stop_engine sees `trade.status='closed'` and stops managing it. With the three-layer model, the stop_engine reads `trading_positions WHERE state='open'` regardless of envelope state — never loses sight of an open position.

**Example C: bracket writer with full position context.** Today the writer sees `trade.stop_loss` (envelope-scoped) but not "all active broker orders for this position." With position-level state including the active broker-order list, the writer recognizes that an existing covering limit is part of the operator's intended bracket pair, not a competitor for the share allocation. See § 1.4 for why this matters.

The two-layer model (decision merged into envelope OR position merged into envelope) doesn't solve any of these without re-introducing today's "row dies → state dies" cascade.

---

## 4. Schema-key choices

### 4.1 Account-type granularity
**Decision (per operator answer #1):** include `account_type VARCHAR(20) NOT NULL DEFAULT 'cash'` in the `trading_positions` natural key from day one. Today's operator runs a single cash-equivalent account per broker; the column defaults to `'cash'` for Robinhood and `'spot'` for Coinbase (resolved at insert via the existing `_compute_trade_snapshot` and ticker-based asset class inference at `app/models/trading.py:151`).

**Why include now:** schema migrations to add a new column to a high-cardinality natural-key constraint later are expensive (lock the table, rebuild the unique index). Adding to the schema now with a sensible default costs nothing now and saves a high-blast-radius migration later. The column is forward-compat-only; nothing reads it for routing in Phase 1.

### 4.2 Cross-broker same-ticker
**Decision (per operator answer #2):** broker_source is a first-class column in the position natural key. BTC-USD on Robinhood and BTC-USD on Coinbase are two distinct positions with two distinct `id`s. The autopilot routing layer (Phase 7) can layer on top: rules like *"long BTC-USD goes to Robinhood, scalp BTC-USD goes to Coinbase"* read `trading_positions` to decide which broker has an open management envelope.

### 4.3 Snapshot vs event-sourced
**Decision (per operator answer #3):** event-sourced. `trading_position_events` is the truth; `trading_positions.current_*` columns are derived materializations rebuilt from events on every broker_sync write. Rebuild from events is a one-shot script (e.g., `scripts/rebuild_position_snapshots.py`).

**Why:** snapshot-only would force every "did this position ever do X?" query to walk fragile `exit_reason` strings. Event-sourcing is the operator-stated intent and the natural fit for the broker-truth-driven workflow. Snapshot fields exist for query convenience (e.g., `WHERE current_quantity > 0`).

---

## 5. Event-sourced position state

### 5.1 Event taxonomy
`trading_position_events` (full DDL in § 7.2) records every observation of every position. Event types form a closed enum:

| `event_type` | Source | Effect on snapshot |
|---|---|---|
| `opened` | First broker-sync observation of a position | `state='open', current_quantity=X, current_avg_price=Y, last_observed_at=now` |
| `qty_change` | Subsequent broker-sync where quantity differs | `current_quantity=NEW, current_avg_price` updated; state unchanged |
| `closed` | Broker no longer reports the position | `state='closed', last_state_transition_at=now`; snapshot quantity → 0 |
| `re_opened` | Broker reports a previously-closed position again | `state='open'`; quantity from new observation |
| `suspect` | broker_sync sees a 0-position response on a non-empty account (R32 wholesale-empty-positions guard) | No state mutation; flag the position for operator review |
| `corrected` | Manual operator override (admin endpoint) | Sets state explicitly + carries operator note |

**No magic numbers in event taxonomy.** Each event carries `transition_reason VARCHAR(64)` (see § 8 for value mapping). Each event carries the broker observation payload as JSONB (positions endpoint response, fill records) for full auditability without re-fetching.

### 5.2 State derivation
A small Python helper `derive_position_state(events: list[PositionEvent]) -> dict` scans events in chronological order and returns the final snapshot state. Called:
- On every broker_sync write (after recording new event(s) for the cycle, derive and update snapshot fields).
- On startup if `trading_positions.last_observed_at` is older than configurable threshold (operator review).
- By a one-shot rebuild script for backfill (see § 5.3).

The derivation is pure: same input event list always produces same output snapshot. No magic numbers; the only "thresholds" are operator-state-derived (e.g., "current_avg_price = volume-weighted average of all fills since last `opened`/`re_opened` event").

### 5.3 Backfill strategy
At Phase 1 cutover, no events exist yet. Backfill walks all existing `trading_trades` rows for `(user_id, broker_source, ticker)` distinct keys, opens a position row for each unique key, and writes a single synthetic `opened` event per active position with `transition_reason='backfill_initial'` and the trade row's current state baked in.

For closed trades, the backfill writes a synthetic `closed` event with `transition_reason='backfill_pre_refactor:<original_exit_reason>'` so reporting that filters on transition_reason can still distinguish causes.

The backfill is idempotent: re-running it skips positions already created. See § 8.3 for backfill phasing.

---

## 6. How existing tables map (column-by-column)

### 6.1 `trading_trades` → split

| Today's column | Post-refactor home | Notes |
|---|---|---|
| `id` | `trading_management_envelopes.id` | Renamed table; column same |
| `user_id` | envelope.user_id + decisions.user_id + positions.user_id | Replicated for query convenience; positions.user_id is authoritative for "who owns this position" |
| `ticker` | positions.ticker (authoritative); envelope.ticker (denormalized for queries) | Envelope's denormalized ticker MUST equal positions.ticker (FK + assertion in writer) |
| `direction` | envelope.direction | Position layer is direction-agnostic at the schema level; "short positions" use a separate position row with quantity convention or a `direction` column on positions (open question — see § 11) |
| `entry_price` | envelope.entry_price | Average entry price for THIS envelope; positions.current_avg_price is the running cross-envelope average |
| `exit_price`, `exit_date`, `exit_reason`, `pnl` | envelope.exit_* | Envelope-scoped exit; position-level transition_reason in `trading_position_events.transition_reason` |
| `quantity` | envelope.quantity | This envelope's intended size; positions.current_quantity is broker truth |
| `entry_date` | decision.entry_date + envelope.created_at | Decision's entry_date is the immutable timestamp; envelope.created_at is when management started |
| `status` | envelope.status | Same enum (open/working/closed/cancelled/rejected) — envelope-scoped |
| `tags`, `notes` | envelope (mutable) | |
| `indicator_snapshot` | decision.indicator_snapshot | Immutable snapshot at decision time |
| `broker_source` | positions.broker_source (authoritative) + envelope.broker_source (denormalized) | |
| `broker_order_id`, `broker_status`, `last_broker_sync`, `filled_*`, `avg_fill_price`, `submitted_at`, `acknowledged_at`, `first_fill_at`, `last_fill_at` | envelope (envelope's bound broker-order journal) | |
| `tca_reference_entry_price`, `tca_entry_slippage_bps` | decision.tca_reference_entry_price + envelope.tca_entry_slippage_bps | Reference is decision-time; slippage is observed-time on the envelope |
| `tca_reference_exit_price`, `tca_exit_slippage_bps` | envelope (exit is envelope-scoped) | |
| `strategy_proposal_id`, `scan_pattern_id`, `pattern_tags`, `related_alert_id`, `mesh_entry_correlation_id`, `auto_trader_version` | decision.* | All entry-attribution is decision-scoped |
| `stop_loss`, `take_profit`, `trail_stop`, `high_watermark`, `stop_model` | envelope (engine-managed bracket state) | |
| `trade_type`, `management_scope` | envelope.* | |
| `scale_in_count` | envelope.scale_in_count | Envelope-scoped; new envelopes start at 0 |
| `pending_exit_*` (5 columns) | envelope (envelope's exit-order tracking) | |
| `asset_kind` | positions.asset_kind (authoritative; same auto-derive logic moves to positions writer) | |

### 6.2 `trading_bracket_intents` → retarget FK

| Today | Post-refactor |
|---|---|
| `trade_id BIGINT FK trading_trades.id` | `position_id BIGINT FK trading_positions.id` (Phase 3 swap) |
| `payload_json.pending_decision` | unchanged shape; now keyed on position (survives envelope churn — § 1.2 fix) |
| `intent_state`, `stop_price`, `target_price`, `direction`, `quantity`, `entry_price`, `last_diff_reason`, all other fields | unchanged |
| `phantom_close_consecutive_zero_qty_sweeps` (mig 223 orphan) | DROPPED in Phase 1 (per operator answer #5) |

The retarget happens in Phase 3. During Phases 1-2, both columns coexist (`trade_id` stays for backwards-compat; `position_id` added and backfilled). Phase 3 makes `position_id` NOT NULL; Phase 5 drops `trade_id`.

### 6.3 `trading_execution_events` → add `position_id`

| Today | Post-refactor |
|---|---|
| `trade_id INT NOT NULL` | unchanged (still tracks "which envelope was active when this fill landed") |
| (new column) | `position_id BIGINT FK trading_positions.id NOT NULL` after Phase 2 backfill completes |

Backfill: for every existing event, look up the trade_id's `(user_id, broker_source, ticker)` and resolve to the position row. Orphaned trade_ids (the trade row was deleted somehow) get an explicit `position_id=NULL` initially, then a one-shot script either resolves them via order-history lookup or marks them in a quarantine table for operator review (see § 11.2 — Open Question on backfill atomicity).

---

## 7. Schema specifics — full DDL

### 7.1 `trading_positions`

```sql
CREATE TABLE trading_positions (
    id              BIGSERIAL PRIMARY KEY,
    user_id         INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
    broker_source   VARCHAR(20) NOT NULL,
    account_type    VARCHAR(20) NOT NULL DEFAULT 'cash',
    ticker          VARCHAR(20) NOT NULL,
    asset_kind      VARCHAR(20) NULL,            -- 'equity' | 'crypto' | 'option'
    current_quantity         DOUBLE PRECISION NULL,
    current_avg_price        DOUBLE PRECISION NULL,
    state                    VARCHAR(20) NOT NULL DEFAULT 'unknown',
    current_envelope_id      BIGINT NULL REFERENCES trading_management_envelopes(id) ON DELETE SET NULL,
    last_observed_at         TIMESTAMP NULL,
    last_state_transition_at TIMESTAMP NULL,
    created_at  TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMP NOT NULL DEFAULT NOW(),
    CONSTRAINT trading_positions_state_check
        CHECK (state IN ('unknown', 'open', 'closed', 'suspect')),
    CONSTRAINT uq_trading_positions_natural_key
        UNIQUE (user_id, broker_source, account_type, ticker)
);

CREATE INDEX ix_trading_positions_state_open
    ON trading_positions (broker_source, ticker)
    WHERE state = 'open';

CREATE INDEX ix_trading_positions_user_broker
    ON trading_positions (user_id, broker_source);
```

Why the partial index `WHERE state = 'open'`: the dominant query is "what positions am I currently holding?" — answered by `state='open'` rows. The partial index keeps that query fast without indexing the entire history of closed positions.

### 7.2 `trading_position_events`

```sql
CREATE TABLE trading_position_events (
    id                BIGSERIAL PRIMARY KEY,
    position_id       BIGINT NOT NULL REFERENCES trading_positions(id) ON DELETE CASCADE,
    event_type        VARCHAR(20) NOT NULL,        -- opened | qty_change | closed | re_opened | suspect | corrected
    transition_reason VARCHAR(64) NOT NULL,        -- structured cause; see § 8
    quantity          DOUBLE PRECISION NULL,        -- post-event broker-reported qty (NULL for 'suspect')
    avg_price         DOUBLE PRECISION NULL,
    broker_payload    JSONB NULL,                   -- full broker-side response captured at observation time
    envelope_id       BIGINT NULL REFERENCES trading_management_envelopes(id) ON DELETE SET NULL,
                                                    -- which envelope was active at observation, advisory
    observed_at       TIMESTAMP NOT NULL DEFAULT NOW(),
    recorded_at       TIMESTAMP NOT NULL DEFAULT NOW(),
    CONSTRAINT trading_position_events_event_type_check
        CHECK (event_type IN ('opened', 'qty_change', 'closed', 're_opened', 'suspect', 'corrected'))
);

CREATE INDEX ix_position_events_position_observed
    ON trading_position_events (position_id, observed_at DESC);

CREATE INDEX ix_position_events_event_type_observed
    ON trading_position_events (event_type, observed_at DESC);
```

Append-only. No UPDATEs. A `corrected` event with operator note explains any "we got the original observation wrong" case.

### 7.3 `trading_management_envelopes` (renamed `trading_trades`)

The DDL is today's `trading_trades` schema with two new FK columns:

```sql
-- Migration 1 (Phase 1): rename + add columns
ALTER TABLE trading_trades RENAME TO trading_management_envelopes;
ALTER TABLE trading_management_envelopes
    ADD COLUMN decision_id BIGINT NULL REFERENCES trading_decisions(id) ON DELETE SET NULL,
    ADD COLUMN position_id BIGINT NULL REFERENCES trading_positions(id) ON DELETE SET NULL;

CREATE INDEX ix_envelopes_position ON trading_management_envelopes (position_id);
CREATE INDEX ix_envelopes_decision ON trading_management_envelopes (decision_id);
```

Phase 4 backfill populates both columns. Phase 5 (after soak) makes them NOT NULL. The rename can be done as a view alias for backwards-compat with existing reporting queries that still reference `trading_trades` (a `CREATE VIEW trading_trades AS SELECT * FROM trading_management_envelopes;` covers most read-only consumers).

### 7.4 `trading_decisions`

```sql
CREATE TABLE trading_decisions (
    id                       BIGSERIAL PRIMARY KEY,
    user_id                  INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
    ticker                   VARCHAR(20) NOT NULL,
    direction                VARCHAR(10) NOT NULL DEFAULT 'long',
    entry_date               TIMESTAMP NOT NULL DEFAULT NOW(),
    indicator_snapshot       JSONB NULL,
    tca_reference_entry_price DOUBLE PRECISION NULL,
    scan_pattern_id          INTEGER NULL REFERENCES scan_patterns(id) ON DELETE SET NULL,
    related_alert_id         INTEGER NULL REFERENCES trading_breakout_alerts(id) ON DELETE SET NULL,
    strategy_proposal_id     INTEGER NULL REFERENCES trading_proposals(id) ON DELETE SET NULL,
    pattern_tags             VARCHAR(500) NULL,
    mesh_entry_correlation_id VARCHAR(64) NULL,
    auto_trader_version      VARCHAR(32) NULL,
    asset_kind               VARCHAR(20) NULL,
    notes                    TEXT NULL,
    created_at               TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX ix_decisions_scan_pattern ON trading_decisions (scan_pattern_id);
CREATE INDEX ix_decisions_related_alert ON trading_decisions (related_alert_id);
CREATE INDEX ix_decisions_mesh_correlation ON trading_decisions (mesh_entry_correlation_id);
CREATE INDEX ix_decisions_entry_date ON trading_decisions (entry_date DESC);
```

INSERT-only at the application layer (no ORM relationships that allow UPDATE; `validates` hooks reject mutations).

---

## 8. Migration plan — 6 phases with explicit exit criteria

Each phase is small enough to ship as a single NEXT_TASK. Each phase's exit criteria are concrete enough that the implementer doesn't need to invent semantics.

### 8.1 Phase 1 — `trading_positions` table + shadow-mode write
**Scope:** add `trading_positions` table (DDL § 7.1) + `trading_position_events` table (DDL § 7.2). DROP `trading_bracket_intents.phantom_close_consecutive_zero_qty_sweeps` in the same migration (per operator answer #5).

**Code changes:** `broker_service.sync_positions_to_db` writes a `position_event` row for every observed position (event_type=`opened` for first observation, `qty_change` if quantity differs, etc.) and updates the `trading_positions` snapshot. NO READERS depend on the new tables for decisions in this phase.

**Soak duration:** 1 week of broker_sync cycles. Verify the position rows stay in sync with broker truth via an audit query that compares `trading_positions.current_quantity` to the most recent broker-sync snapshot for every active position.

**Exit criteria:**
1. Migration applies cleanly on staging + production.
2. After 1 week soak: zero discrepancies in the audit query for active positions.
3. Backfill script (`scripts/backfill_position_rows.py`) ran; every distinct `(user_id, broker_source, ticker)` from current `trading_trades` has a corresponding position row.
4. mig 223 column dropped + verified.

**Rollback:** revert the migration; the new tables drop; no live system reads them so rollback is purely additive-cleanup.

### 8.2 Phase 2 — `trading_execution_events.position_id` backfill
**Scope:** ADD COLUMN (nullable initially), backfill, switch new writes to populate it.

**Code changes:** `record_execution_event` populates `position_id` on every new write by joining through trade_id → position. Backfill script walks historical events and writes position_id; orphaned events go to a quarantine view (Open Question § 11.2).

**Soak duration:** 1 week.

**Exit criteria:**
1. 100% of new events have non-NULL position_id.
2. Backfill complete; quarantine view has fewer than `< some operator-stated threshold>` rows (operator decides — see § 11.2).
3. NO READERS use position_id for decisions yet.

**Rollback:** drop the column; reverts to today's trade_id-only state. Position_event stream is unaffected.

### 8.3 Phase 3 — `trading_bracket_intents.position_id` retarget
**Scope:** ADD COLUMN, backfill, swap reader paths.

**Code changes:** writers populate position_id on new intents. `bracket_reconciliation_service` and `bracket_writer_g2` continue reading via trade_id (compat); a feature flag `CHILI_BRACKET_INTENT_USE_POSITION_KEY` (default OFF) lets one writer at a time switch to position-key reads. Phase 3.5 (separate small task): flip the flag to ON after soak.

**Soak duration:** 2 weeks (writers swap independently; soak the slowest path).

**Exit criteria:**
1. All bracket-related code reads via position_id when flag ON.
2. Behavior parity verified: same bracket actions on the same positions whether flag is ON or OFF.
3. Today's pending_decision rows (post-deploy) survive an envelope rebind without losing the operator-input-pending state.

**Rollback:** flip the flag OFF. The position_id column stays populated for future re-attempt.

### 8.4 Phase 4 — Inverse-reconcile uses position-level history
**Scope:** rewrite `sync_positions_to_db` inverse-reconcile branch to use position-level event history.

**Code changes:** the conservative `event_count == 0` check (today's workaround at `app/services/broker_service.py:1444-1538`) becomes "for this position, is there a SELL fill in `trading_execution_events.position_id IN (this position's ids)` since the most recent `opened` or `re_opened` event?" That's the precise check; the count workaround retires.

**Soak duration:** 2 weeks (live-broker behavior change; needs slow ramp).

**Exit criteria:**
1. New incidents that match the May 4 09:44 cascade pattern self-heal cleanly.
2. The `event_count == 0` workaround code is removed (not just bypassed).
3. Existing `test_broker_sync_inverse_reconcile.py` tests pass with the new check substituted.

**Rollback:** code revert. Position-state machinery stays; reverts to the previous workaround.

### 8.5 Phase 5 — Close paths consolidate
**Scope:** the five close-reason strings (§ 9 mapping table) move to `trading_position_events.transition_reason`. `trading_management_envelopes.exit_reason` stays for backwards-compat reporting.

**Code changes:** every site that today sets `Trade.exit_reason` ALSO writes a `trading_position_events` row with the structured transition_reason. Old reporting queries continue to work; new queries use the position-event stream as the authoritative source.

**Soak duration:** 1 quarter (reporting consumers need time to migrate; see § 11.4 Open Question on deprecation timeline).

**Exit criteria:**
1. Every close path writes both the legacy exit_reason AND a position event.
2. New reporting queries that use transition_reason produce parity-equivalent results to legacy queries that use exit_reason.
3. Operator-side reporting dashboards confirmed compatible (no broken charts).

**Rollback:** remove the position-event writes from close paths; legacy exit_reason continues as the only source. No data loss (position events are additive).

### 8.6 Phase 6 — Bracket writer has full position context
**Scope:** writer reads "all active broker orders for this position" + brain's bracket intent + operator's pending decision in one coordinated state.

**Code changes:** `bracket_writer_g2.place_missing_stop` and the future trailing-stop placement helper (when added) read the position's full active-orders list and decide based on the coordinated state. The CANCEL_COVERING_SELL flag becomes a tactical preference, not a load-bearing necessity. The pending-decision surface from today's task continues to be the operator-input mechanism.

**Soak duration:** 2 weeks.

**Exit criteria:**
1. The 5-cancelled-limits failure mode (§ 1.4) cannot recur structurally.
2. Bracket writer can place stop AND target as a coordinated bracket without cancelling either side (within the venue's one-sell-per-share constraint, by routing the bracket as a multi-leg order where supported).
3. Today's pending-decision surface routes operator choices without per-envelope churn.

**Rollback:** code revert. Position-level reads stay; writer reverts to today's per-envelope-only logic.

---

## 9. Close-reason mapping table

The five string-named close reasons and their post-refactor `transition_reason` values:

| Today's `exit_reason` string | Phase-5 `transition_reason` | Source code site (today) |
|---|---|---|
| `broker_reconcile_position_gone` | `automatic_broker_observation:position_gone` | `app/services/broker_service.py:1751-1810` (the stale-trade close loop) |
| `phantom_after_terminal_reject` | retired (path deleted in `broker-truth-self-heal`); historical rows keep their string | n/a (path retired) |
| `emergency_price_monitor_guardrail` | `automatic_emergency_response:freeze_at_<reason>` (e.g. `…:disconnected`) | `app/services/trading/emergency_liquidation.py::emergency_close_all` (now operator-only invocation) |
| `zombie_reconcile_orphan` | `automatic_orphan_cleanup` | broker_sync orphan-trade close path |
| `broker_reconcile_no_exit_price` | `automatic_broker_observation:position_gone:no_quote` | `broker_service.sync_positions_to_db` no-quote close path |
| `broker_stop_filled_outside_chili` (suggested in operator pre-action SQL) | `manual_operator:fill_outside_chili` | manual SQL only; no code path writes this today |
| Real envelope closes (target hit, stop hit, manual close, etc.) | `automatic_broker_observation:fill_observed` + envelope.exit_reason='target'/'stop'/etc. | `_finalize_filled_exit` at `robinhood_exit_execution.py:381` |

No string is orphaned. Every existing reason has a structured `transition_reason` cousin or is mapped to retired-path semantics.

---

## 10. Phase 7 — autopilot settings UI (sketch only; out of scope to build here)

The data-model-only sketch of what the UI consumes:

**`autopilot_routing_rules`** (NEW table; design only):
```sql
CREATE TABLE autopilot_routing_rules (
    id           BIGSERIAL PRIMARY KEY,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    rule_kind    VARCHAR(40) NOT NULL,    -- 'broker_enable' | 'strategy_toggle' | 'trade_kind_allowlist' | 'ticker_blocklist'
    broker_source VARCHAR(20) NULL,         -- when scoped to a broker
    asset_kind    VARCHAR(20) NULL,         -- when scoped to an asset class
    strategy_name VARCHAR(64) NULL,         -- when scoped to a strategy
    enabled       BOOLEAN NOT NULL DEFAULT true,
    config_json   JSONB NULL,
    created_at    TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMP NOT NULL DEFAULT NOW()
);
```

**Operator's stated UI requirements (verbatim):** *"autopilot settings page so I can enable/disable there the flags and checkboxes for the auto trading system including enabling and disable kind of trades per broker"*.

The UI consumes:
- `trading_positions` (current holdings) for the "what's currently active" view.
- `autopilot_routing_rules` (the operator's preferences) for the toggles.
- `trading_position_events` (history) for "what has the system done with this position?"

The autopilot decision-maker (today: `auto_trader.py`) reads `autopilot_routing_rules` BEFORE deciding which broker to route a new trade to. Per operator answer #2, the default rule set is *"long → robinhood, scalp → coinbase"* expressed as two `strategy_toggle` rules.

**Out of scope here:** the UI itself. The data contract above is what the future UI integrates against.

---

## 11. Open Questions for operator

These emerged from the source-code read in Step 1 of the brief. Operator answers in the review pass; the doc gets revised.

### 11.1 Stale event tolerance window
**Framing:** when `broker_sync` misses a sync cycle (auth flap, network blip), the event stream has a gap. Three options:

- **A — best-guess continuity:** assume the position state didn't change during the gap; no event written for the missed window.
- **B — explicit `sync_gap` event:** emit a `suspect` event with `transition_reason='sync_gap'` covering the gap; downstream consumers know there's missing data.
- **C — alert + halt:** any gap > N minutes raises a CRITICAL log + activates the kill switch.

**Cowork's recommendation:** **B**. Explicit gap markers preserve the audit trail without requiring operator intervention on every transient blip. Aligns with the "no signal ≠ negative signal" principle from `bracket-writer-respect-upside-targets`.

**Affects:** Phase 1's broker_sync code; the `derive_position_state` helper's interpretation of gaps; the audit dashboard.

### 11.2 Backfill atomicity for `trading_execution_events.position_id`
**Framing:** Phase 2 needs to backfill position_id on every historical event row. Three options:

- **A — single bulk update:** SET position_id = lookup(trade_id, ticker, broker_source) for all rows in one transaction. Risk: locks the table for the duration.
- **B — online-rolling:** chunked update by id range; each chunk in its own transaction. Slower but no long lock.
- **C — quarantine ambiguous + bulk for clean cases:** rows where the trade_id has zero matching position_id (orphans) go to a quarantine view; the rest are bulk-updated. Operator reviews the quarantine.

**Cowork's recommendation:** **C**. The orphan count is presumably small (a few hundred at most given today's data volume); operator review of those specifically is cheap and prevents silent corruption. Bulk for clean cases keeps the migration window short.

**Affects:** Phase 2 migration scripting; the quarantine table schema if it's needed.

### 11.3 Reporting deprecation timeline
**Framing:** existing dashboards grep `exit_reason` strings (the legacy values in § 9). New `transition_reason` values are different. Three options:

- **A — maintain both for one quarter:** legacy exit_reason continues to be written; transition_reason added in parallel. Reporting queries opt into either side.
- **B — shim view:** create a `legacy_exit_reasons` view that maps transition_reason values back to legacy strings; legacy queries continue working unchanged.
- **C — hard-cut at Phase 5:** legacy exit_reason removed; reporting queries must update.

**Cowork's recommendation:** **A** with a side of **B** (add the shim view in Phase 5; deprecate the dual-write at the end of Q3).

**Affects:** Phase 5 exit criteria; downstream reporting consumers.

### 11.4 Fast-path subsystem dependency
**Framing:** fast-path code in `chili-brain/` and `app/services/trading/fast_path/` writes to `fast_executions`, `fast_orderbook`, `fast_alerts`, etc. Two options:

- **A — migrate fast-path to position-keying in this initiative.** Big scope expansion; touches the fast-path team's stack.
- **B — leave fast-path decoupled in v1, integrate after Phase 4 lands.** Fast-path stack writes to its own tables; doesn't depend on `trading_positions` for decisions; integrates later when the model is proven.

**Cowork's recommendation:** **B**. Fast-path is paper-mode by default and its volumes are large enough that backfill atomicity (§ 11.2) is harder there than for the live trading stack. Integrate after Phase 4 lands and the position-event derivation is battle-tested.

**Affects:** scope boundary of this initiative; whether Phase 7 includes fast-path settings.

---

## 12. Brain context flow

### 12.1 `compute_bracket_intent` — unchanged interface, position-keyed call site
Brain bracket derivation at `app/services/trading/bracket_intent.py::compute_bracket_intent` takes `BracketIntentInput` and returns target/stop. Today the caller is the bracket writer with envelope-scoped data. Post-refactor: caller takes data from the position layer (qty, avg_price) + decision layer (regime, lifecycle, pattern_win_rate). The brain function itself doesn't change; the input source does.

### 12.2 Stop engine
`app/services/trading/stop_engine.py::evaluate_all` reads `trading_trades` rows today. Post-refactor: reads `trading_management_envelopes` JOIN `trading_positions WHERE state='open'`. The state derivation ensures we only evaluate envelopes that are currently bound to live positions. Stale envelopes (envelope.status='open' but position.state='closed') get caught and marked.

### 12.3 Stop monitor
The monitor reads bracket_intents to decide what stops are expected at the broker. Post-Phase-3: reads via `bracket_intent.position_id` join — the intent stays alive across envelope rebinds, so the monitor's view is continuous.

### 12.4 Brain inputs vs outputs
The brain's role doesn't change. It reads market state + position state + decision context, returns target/stop/regime/lifecycle decisions. The data plumbing changes; the brain function signatures don't.

---

## 13. What this initiative does NOT change

- **Order placement code.** `place_market_order`, `place_stop_loss_sell_order`, etc. stay as-is. Position layer is a read-side mirror; broker is still the truth.
- **Broker auth, session restoration.** `broker_service.is_connected`, `try_restore_session` stay.
- **TP/SL primitives at the venue.** Robinhood / Coinbase API surfaces are not redesigned.
- **Pattern engine, scan_patterns, signal generation.** Decisions still flow from the same upstream pipeline.
- **Fast-path subsystem (per § 11.4 Open Question).**
- **Existing dashboards short-term (per § 11.3 timeline).**
- **Live-money behavior in Phase 1.** Phase 1 is read-only / shadow-mode.

---

## 14. Glossary

- **Position** — broker-authoritative identity; one row per `(user_id, broker_source, account_type, ticker)` natural key. Persists across closing-and-reopening of the underlying broker holding. Owns the "do we hold this?" question.
- **Position event** — append-only record of an observation about a position. Event types: opened / qty_change / closed / re_opened / suspect / corrected. Source of truth for the position's state derivation.
- **Decision** — immutable record of "we decided to enter this trade for these reasons at this time." Owns scan_pattern_id, indicator_snapshot, attribution.
- **Management envelope** — the dynamic "how we are managing the broker interaction right now" record. Bound to a position via `position_id`; one envelope binds at a time. New envelope on re-open. Today's `trading_trades` row, with entry-decision fields removed.
- **Transition reason** — structured value on `trading_position_events`. Replaces the loose `exit_reason` string convention with a constrained vocabulary (§ 9).
- **Snapshot fields** — `trading_positions.current_quantity`, `current_avg_price`, `state`. Derived from the event stream; rebuildable from events at any time. Not authoritative on their own.

---

## 15. Estimated cost (pre-implementation)

| Phase | LOC est | Test count est | Soak | Operator-hours |
|---|---|---|---|---|
| 1 | ~600 | ~12 | 1 week | ~3h (review migration + audit query) |
| 2 | ~400 | ~8 | 1 week | ~2h (backfill review + quarantine triage) |
| 3 | ~700 | ~15 | 2 weeks | ~4h (flag-flip + parity verification) |
| 4 | ~500 | ~10 | 2 weeks | ~3h (live-incident review window) |
| 5 | ~400 | ~10 | 1 quarter | ~6h (reporting consumer migration) |
| 6 | ~600 | ~12 | 2 weeks | ~3h (writer behavior review) |
| **Total** | **~3200** | **~67** | **~2.25 quarters** | **~21h** |

These are doc-time estimates only. Each phase ships with its own NEXT_TASK that re-estimates against actual code state at that point.

---

## 16. References

Source files cited in this design (`file:line` form):

- `app/models/trading.py:39-188` — `Trade` ORM (split target)
- `app/models/trading.py:345-410` — `TradingExecutionEvent` ORM (Phase 2 target)
- `app/models/trading.py:2015-2059` — `BracketIntent` ORM (Phase 3 target)
- `app/models/trading.py:1022-1060` — `PaperTrade` ORM (parallel; future-phase consideration)
- `app/services/broker_service.py:1372-1907` — `sync_positions_to_db` (Phase 1 + 4 site)
- `app/services/broker_service.py:1444-1538` — inverse-reconcile branch (Phase 4 rewrite target)
- `app/services/broker_service.py:1473-1515` — R32 wholesale-empty-positions guard (preserved through refactor)
- `app/services/trading/bracket_reconciliation_service.py` — sweep loop + `_invoke_writer_for_decision` (Phase 3 reader-swap site)
- `app/services/trading/bracket_writer_g2.py::place_missing_stop` — covered-by-existing-sell branch (Phase 6 site)
- `app/services/trading/bracket_writer_g2.py::record_pending_bracket_decision` — pending-decision surface (Phase 3 retarget target)
- `app/services/trading/bracket_intent.py:110-180` — `compute_bracket_intent` (brain integration point)
- `app/services/trading/stop_engine.py::evaluate_all` — stop engine reader site (post-Phase-3 retarget)
- `app/services/trading/emergency_liquidation.py::emergency_close_all` — close path mapped in § 9 (Phase 5)
- `app/services/trading/robinhood_exit_execution.py:381` — `_finalize_filled_exit` close path mapped in § 9
- `app/migrations.py:14671` — orphan column from mig 223 (DROP in Phase 1)
- `app/services/trading/execution_audit.py:198` — `record_execution_event` (Phase 2 column-fill site)
- `docs/STRATEGY/CURRENT_PLAN.md:33-95` — initiative-level shape this doc concretises
- `docs/STRATEGY/COWORK_REVIEWS/2026-05-04_*.md` — today's review files captured the architectural pain
- `docs/STAGING_DATABASE.md`, `docs/PHASE_ROLLBACK_RUNBOOK.md` — protocol docs for migration discipline (referenced in each phase's rollback plan)
