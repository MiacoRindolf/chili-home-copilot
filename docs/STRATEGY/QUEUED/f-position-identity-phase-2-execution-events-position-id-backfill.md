# f-position-identity-phase-2-execution-events-position-id-backfill

> **STATUS: SHIPPED 2026-05-18.** Operator chose Option A. Mig 248 fired clean; 168 historical closed positions seeded (33 → 201 in trading_positions); 8,358 of 8,358 with_trade_id execution_events resolved (100%); 6,797 null_trade_id events sit in quarantine (expected per brief). 11/11 tests pass. CC_REPORT at `docs/STRATEGY/CC_REPORTS/2026-05-18_f-position-identity-phase-2.md`. Phase 3 (authority flip + bracket_intent.position_id) is next.

> **Type:** Schema migration + backfill + double-write (no reader change)
> **Parent initiative:** `docs/STRATEGY/CURRENT_PLAN.md` — Position Identity Refactor
> **Design doc § 8.2:** `docs/DESIGN/POSITION_IDENTITY.md`
> **Phase 1 soak review (this gate):** `docs/STRATEGY/COWORK_REVIEWS/2026-05-11_position-identity-phase-1-soak.md`

## STATUS

Queued (not yet promoted to NEXT_TASK). Operator action to start: copy this
brief's body into `docs/STRATEGY/NEXT_TASK.md`, set `STATUS: PENDING`, run
`claude`. The active NEXT_TASK at queue-time was `f-brain-event-kind-unify`
(Phase 1b of the parallel adaptive-promotion-architecture initiative); Phase
2 of position-identity waits behind it.

## Goal

Add `trading_execution_events.position_id` (nullable initially). Resolve
every fill event in history to a position via the
`(user_id, broker_source, ticker, direction)` natural key. Double-write
the column at the two `record_execution_event` call sites so every new
event lands with `position_id` populated. **No read-side change** —
read-side stays on `trade_id`. Phase 2 is purely the foundation for the
authority flip that lands in Phase 3.

## Why this is next (after the Phase 1 soak)

The Phase 1 soak demonstrated exactly what the design doc § 1 enumerated:
trade rows die and are reborn while broker positions persist. Pos=15
(GRT-USD) is the marquee case — 13 close→re_opened cycles across 6 days
with the current state ending `open` while the most recent
`trading_trades` row stayed `closed` (exit_reason=`broker_reconcile_no_exit_price`).
Any fills against that prior trade_id are orphaned from the live position.
Phase 2 is the mechanism that re-links them via `position_id`.

## What the Phase 1 soak surfaced for Phase 2 scope (must read before drafting)

Cardinality probe run 2026-05-11 against `trading_execution_events`
(11,999 rows total):

| Event cohort | Count | Phase 1's coverage | Phase 2 implication |
|---|---:|---|---|
| `trade_id` resolves to an OPEN position (Phase 1 backfill walked these) | 66 | covered | trivial natural-key join backfill |
| `trade_id` references a CLOSED `trading_trades` row with no current `trading_positions` row | 5,305 | NOT covered (Phase 1 walked `WHERE status='open'`) | **scope decision required** — see below |
| `trade_id` IS NULL (event has no trade-row linkage at all) | 6,626 | NOT covered | quarantine view per § 11.2 Decision C |
| Misc (cancelled-trade events, etc.) | 2 | — | quarantine |

The 5,305-row tier is the load-bearing decision. Two options:

- **A. Phase 2 extends the Phase 1 backfill to walk historical closed
  trades.** Creates `trading_positions` rows with `state='closed'` for
  every unique `(user_id, broker_source, ticker, direction)` that appears
  in `trading_trades` but not yet in `trading_positions`. Then the natural-
  key join resolves 5,305 of those 5,305 events. Cost: positions table
  grows from ~27 rows to ~few hundred (one per unique historical
  ticker-cohort tuple). Benefit: future Phase 4 inverse-reconcile and
  Phase 5 reporting paths can ask "was there ever a position on this
  ticker?" against a single source of truth.
- **B. Phase 2 leaves them as `position_id=NULL` and the quarantine view
  records the cohort.** Cost: 5,305 events stay structurally
  orphaned-from-position until a separate historical-backfill brief
  lands later. Benefit: Phase 2 scope is tighter; Phase 4/5 work happens
  on cleaner data.

**Recommendation in this brief: Option A.** The closed-position rows are
cheap to create, have an exact natural key, and unlock Phase 4's
precise-history inverse-reconcile. Phase 1's backfill was scoped to
`status='open'` only because at that time the design intent was "live
positions first, history later" — the soak surfaced that "later" is
Phase 2's natural home. **Operator decision required before
implementation starts** — surface in the Open Questions consult.

The 6,626 NULL-trade_id events go to the quarantine view regardless.

## Brain integration (reuse, don't rewrite)

- `app/services/trading/execution_audit.py::record_execution_event` —
  every new event flows through here. Add a `position_id` resolution
  step that joins through `trade_id → trade.(user, broker, ticker) →
  position.id` (filtered by `direction` if available; `'long'` by
  default per design § 4.1). Wrap in try/except so a resolution miss
  never breaks the existing event write (shadow-mode posture continues).
- `scripts/backfill_position_rows.py` — existing Phase 1 backfill.
  Extend with an `--include-closed-trades` flag (or a new
  `backfill_closed_position_rows.py` if cleaner) that walks
  `trading_trades` WHERE `status` IN (`closed`, `cancelled`, `rejected`)
  and inserts `trading_positions` rows at `state='closed'` for any new
  natural-key tuples.
- `scripts/backfill_execution_event_position_id.py` — NEW. Walks
  `trading_execution_events` and writes `position_id` via the join.
  Idempotent (skip rows where `position_id IS NOT NULL`).
- View: `trading_execution_events_quarantine` — NEW. Selects rows where
  `position_id IS NULL` after backfill ran. Operator triages.

## Schema changes (single migration, ID 238)

```sql
-- mig 238: position-identity-phase-2: trading_execution_events.position_id

ALTER TABLE trading_execution_events
    ADD COLUMN IF NOT EXISTS position_id BIGINT NULL REFERENCES trading_positions(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS ix_trading_execution_events_position_id
    ON trading_execution_events (position_id) WHERE position_id IS NOT NULL;

-- Quarantine view (one-shot inspection):
CREATE OR REPLACE VIEW trading_execution_events_quarantine AS
SELECT
    e.id, e.trade_id, e.user_id, e.ticker, e.broker_source,
    e.event_type, e.event_at,
    t.status AS trade_status,
    CASE
        WHEN e.trade_id IS NULL THEN 'null_trade_id'
        WHEN t.id IS NULL THEN 'orphan_trade_id'
        WHEN NOT EXISTS (
            SELECT 1 FROM trading_positions p
            WHERE p.user_id = t.user_id
              AND p.broker_source = t.broker_source
              AND p.ticker = t.ticker
        ) THEN 'no_matching_position'
        ELSE 'resolution_failed_other'
    END AS quarantine_reason
FROM trading_execution_events e
LEFT JOIN trading_trades t ON t.id = e.trade_id
WHERE e.position_id IS NULL;
```

NOTE: the index is partial — only indexes events that have been resolved.
Phase 4's reader paths (when they land) consult the partial index.

## Constraints / do not touch

- **No reader path consults `position_id` yet.** Phase 2 is double-write
  + backfill only. The natural-key join continues to drive any read that
  needs position context until Phase 3.
- **`trade_id` is NOT removed in Phase 2.** It stays as the primary
  linkage in `trading_execution_events`. Both columns coexist through
  Phase 4. Removal lands in Phase 5 per § 6.2 timeline.
- **Live-money behavior unchanged.** Like Phase 1, the new column is
  read-only-by-no-one. If the resolution helper throws, the event still
  writes (with `position_id=NULL`).
- **Migration is idempotent.** `ADD COLUMN IF NOT EXISTS`, `CREATE INDEX
  IF NOT EXISTS`, `CREATE OR REPLACE VIEW`. Reruns are no-ops.
- **No magic numbers.** Direction default `'long'` is categorical, not a
  threshold. Backfill batch sizes (if used) cite their justification or
  use the existing convention in `scripts/backfill_position_rows.py`.
- **Hard rule from CLAUDE.md:** tests use `_test`-suffixed DB. The
  fixture truncates.
- **Brief constraint:** if Option B (no closed-trade extension) is
  selected, the quarantine view must still produce the same cohort
  cardinality — the goal is auditability, not coverage.

## Out of scope

- The Phase 3 reader-flip and the `bracket_intent.position_id` retarget
  (§ 8.3). Phase 2 only touches `trading_execution_events`.
- Inverse-reconcile rewrite (§ 8.4). The conservative `event_count == 0`
  check stays in place; Phase 2 just gives it a future home to migrate
  to.
- Transition-reason structured close paths (§ 8.5). Strings still rule.
- Removing the `trade_id` column anywhere. Not until Phase 5.

## Success criteria

1. Mig 238 applied cleanly on staging + production.
2. `trading_execution_events.position_id` populated for **100% of new
   events** since deploy (probe: `SELECT count(*) FROM
   trading_execution_events WHERE event_at > '<deploy_ts>' AND
   position_id IS NULL` returns 0).
3. Backfill complete:
   - If **Option A** chosen: 5,305 historical events resolve to a
     position (after closed-position backfill seeds the missing rows).
     The quarantine view shows only the ~6,626 null-trade_id cohort
     plus any genuine resolution failures.
   - If **Option B** chosen: only the 66 open-position events resolve;
     ~11,931 events sit in quarantine.
4. NO READER path consults `position_id` (verified with the same
   static-grep canary pattern Phase 1 used — see
   `tests/test_position_identity_phase1.py` Test J).
5. The shadow-mode invariant holds: no `[phase2_position_id_resolve]
   failed` lines blocking event writes; resolution misses become
   quarantine rows, not exceptions.

## Rollback plan

- Code revert: removes the resolution helper + the double-write line in
  `record_execution_event`. New events stop populating `position_id`;
  pre-revert rows retain whatever they had. No readers care.
- Migration: mig 238 is additive (`ADD COLUMN IF NOT EXISTS` + index +
  view). Standard CHILI practice: additive migrations stay forward.
- If a corrupted backfill writes wrong `position_id` values: `UPDATE
  trading_execution_events SET position_id = NULL` resets the cohort;
  rerun backfill after fixing the resolver.

## Open questions for operator

1. **Option A vs Option B on the 5,305 closed-trade events.** Brief
   recommends A — extend Phase 1 backfill to walk historical closed
   trades and seed `trading_positions` rows at `state='closed'`. Cost:
   modest table growth (~few hundred rows). Benefit: 5,305 events
   resolve cleanly; Phase 4 has a fully-indexed history. Operator
   decides at brief promotion.
2. **`direction='long'` default for the resolver.** Phase 1 used the
   same default. When perps come online (Hyperliquid/dYdX) the
   resolver needs broker-payload-aware short detection. Brief assumes
   long-only for now; flag for operator.
3. **Quarantine triage policy.** ~6,626 null-trade_id events lack
   resolution. Phase 2 surfaces them; the operator decides whether to
   tag them with synthetic positions, accept them as legacy
   audit-only, or run a separate brief to resolve them via order-history
   lookup.
4. **Broker-truth audit script.** The scheduled-task that produced the
   Phase 1 soak review could not run
   `scripts/audit_position_layer_parity.py` from outside docker. Phase
   2's exit criteria should add an explicit operator-run step:
   `docker compose exec -T scheduler-worker python
   /app/scripts/audit_position_layer_parity.py` before Phase 2's flip.

## Estimated effort (revised against Phase 1 actuals)

| Item | Pre-revision est | Revised est (post-Phase-1 actuals) |
|---|---|---|
| LOC | ~400 | ~500 (Option A adds the closed-trade backfill helper) |
| Tests | ~8 | ~10 (resolver, double-write, backfill, quarantine view, idempotency) |
| Soak | 1 week | 1 week |
| Operator-hours | ~2h | ~3h (quarantine triage + Option A/B decision) |

## Next in queue (after Phase 2 ships clean)

- **Phase 3** (`f-position-identity-phase-3-bracket-intent-position-id-retarget`)
  — ADD COLUMN `trading_bracket_intents.position_id`, backfill, swap
  reader paths under a feature flag. See design doc § 8.3.
- **Phase 4** (`f-position-identity-phase-4-inverse-reconcile-position-history`)
  — rewrite the `event_count == 0` workaround to consult position-level
  fill history. See design doc § 8.4.
