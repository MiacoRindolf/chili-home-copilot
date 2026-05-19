# NEXT_TASK: f-position-identity-phase-5-envelope-rename

STATUS: PENDING

## Goal

Position-identity refactor Phase 5 per `docs/DESIGN/POSITION_IDENTITY.md` § 6.2: rename `trading_trades` → `trading_management_envelopes`, split out a separate `trading_decisions` table for immutable entry intent, and remove `trade_id` from `trading_execution_events` / `trading_bracket_intents` in favor of the now-complete `position_id` FK.

This is the BIG refactor — schema rename, multi-table split, ORM updates, 2-week soak window. Per design doc it must wait for Phases 1-4 to be operationally enabled and exercised. Phase 4 is shipped + flag-flipped but has NOT been exercised in production yet (RH session is down → `sync_positions_to_db` inverse-reconcile branch hasn't entered).

**Gate this brief on at least one observed `[phase4_*]` log line in production.**

## Why this is next

Position-identity refactor's data plane is fully complete (Phase 1+2+3 = `trading_positions` + `*.position_id` populated; mig 254 backfilled + 5 writer hooks now cover every sell-event source). Reader plane shipped behind a flag. Phase 5 is the schema cleanup that retires the load-bearing workarounds in `trading_trades` / `trading_execution_events.trade_id` / `trading_bracket_intents.trade_id`.

## What "exercised in production" means (gating condition)

At least ONE of:

1. A `broker-sync-worker` log line containing `[phase4_no_sell]` or `[phase4_has_sell]` — i.e., the inverse-reconcile branch fires and uses the new helper.
2. A new sell event in `trading_execution_events` with one of the new event_types (`crypto_exit_submitted`, `coinbase_position_sync_gone_close`, `broker_reconcile_position_gone_close`, `stop_engine_auto_sell`, `exit_fill`) authored AFTER 2026-05-19 (post-deploy of the writers).
3. Operator-driven manual smoke: place a small crypto exit, confirm a `crypto_exit_submitted` event lands in the table.

Until one of these is observed, Phase 5 stays queued.

## Bridge briefs that don't require Phase 5

While waiting for Phase 4 exercise, the operator may instead pick from:

- **`f-coinbase-maker-only-routing`** (memory-queued, high-leverage per the 2026-05-18 TCA finding: avg +102 bps entry slippage). Maker-only routing on Coinbase would reduce taker fees + adverse fills materially. ~60% of pattern 585's gross edge would be reclaimed.
- **`f-stop-engine-payoff-ratio-gate`** (not yet written): use the payoff_ratio gate (Tier A #2 from 2026-05-18) to size-tier the autotrader entries. Smaller blast radius than Phase 5.
- **`f-pid-537-watcher-elevation-decision`** (gated on n=15 verdict from the daily watcher).

## Phase 5 scope (preview only; do not start until gated)

Per design doc § 6.2:

1. New `trading_decisions` table — immutable entry-intent snapshot.
2. Rename `trading_trades` → `trading_management_envelopes` (alembic-style via custom migration `_migration_NNN_envelope_rename`).
3. Migrate `trade_id` FKs on `trading_execution_events` and `trading_bracket_intents` to point at `trading_management_envelopes.id` (rename is mechanical; FK targets stay valid).
4. New `decision_id` FK column on envelopes pointing at `trading_decisions`.
5. ORM updates (ScanPattern → Decision → Envelope → Position chain).
6. 2-week soak per design doc.
7. Final phase: drop `trade_id` on `trading_execution_events` / `trading_bracket_intents`, use `position_id` exclusively.

## Brain integration (reuse, don't rewrite)

When Phase 5 is unblocked:
- `position_resolver.resolve_position_id` + `position_resolver.position_has_recorded_sell` — both stay unchanged; they already use `position_id`.
- All 5 sell-event writer call sites — unchanged; they go through `record_execution_event` which is downstream of the envelope-rename.
- The flag-gated Phase 4 reader at `broker_service.py:~1980` — needs updating to reference the renamed table.

## Constraints / do not touch

- **Don't start Phase 5 until the gate above is met.**
- When started: no live-money behavior change during the schema rename. The rename + FK retarget is data-model only; close-path strings + flag-gated reader stay the same.
- 2-week soak is non-negotiable per the design doc.

## Success criteria (when Phase 5 starts)

To be detailed in the Phase 5 brief. This file is just the gate.

## Rollback plan

The renames are reversible via inverse migration. The 2-week soak is the operational safety net.

## Reference

- `docs/DESIGN/POSITION_IDENTITY.md` § 6.2 — Phase 5 spec
- Coverage-map summary in `docs/STRATEGY/CC_REPORTS/2026-05-19_f-bracket-fired-stop-recording.md`
- All Phase 1-4 CC reports under `docs/STRATEGY/CC_REPORTS/`
- Memory: `project_2026_05_19_phase4_flag_flipped`, `project_2026_05_19_coinbase_exit_recording_shipped`, `project_2026_05_19_bracket_fired_stop_recording_shipped` (this session)
