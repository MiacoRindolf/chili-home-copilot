# NEXT_TASK: f-position-identity-phase-3-bracket-intent-position-id-retarget

STATUS: PENDING

## Goal

Extend position-identity from Phase 2's `trading_execution_events.position_id` to **`trading_bracket_intents.position_id`** — the second writer-side FK. After Phase 3:

1. `trading_bracket_intents` has a nullable `position_id BIGINT FK -> trading_positions(id) ON DELETE SET NULL`.
2. Every existing bracket intent has `position_id` backfilled via the same `(user, broker, ticker, direction)` natural-key resolver used in Phase 2.
3. Every NEW bracket intent written by `bracket_intent_writer` / `bracket_writer_g2` / `bracket_reconciliation_service` double-writes `position_id` alongside `trade_id`.
4. **Still NO reader path consults `position_id`** — Phase 3 is the second-and-final foundation phase. Phase 4 (`f-position-identity-phase-4-inverse-reconcile-position-history`) will flip readers under a feature flag.

## Why this is the right next step

Phase 2 just shipped (commit on 2026-05-18, mig 248). The Phase 2 brief and CC report both call out Phase 3 as the natural follow-on:

- Bracket intents are the second 1:1-trade_id-coupled table the design doc § 1 enumerated. Same orphan problem: when a Trade row dies + recreates, the bracket intent FK dies with it (e.g., trade 1815 EKSO on 2026-05-01).
- Phase 4's inverse-reconcile rewrite needs BOTH `execution_events.position_id` AND `bracket_intents.position_id` populated before it can replace the conservative `event_count == 0` workaround.
- The code surface is the same area touched in Phase 2 — momentum on the refactor, less context-switch.

## Brain integration (reuse, don't rewrite)

- `app/services/trading/execution_audit._resolve_position_id_for_event` — **THIS IS THE RESOLVER TO REUSE.** Phase 3 should NOT write a parallel resolver. Either import the existing one, or extract it to a shared module (e.g. `app/services/trading/position_resolver.py`) if Phase 3 needs it in places that can't import from `execution_audit`.
- `app/services/trading/bracket_intent_writer.py` (and `bracket_writer_g2.py`) — the writer paths. Add the position_id resolution call before the bracket_intent row is created.
- `app/services/trading/bracket_reconciliation_service.py` — also writes bracket intents; same pattern.
- `app/migrations.py` — add mig **249** with the same shape as mig 248: column + partial index + backfill via natural-key join through `trade_id → trading_trades → trading_positions`.

## Constraints / do not touch

- **No reader path consults `position_id` yet.** Phase 3 is still write-only. The static-grep reader-canary in `tests/test_position_identity_phase2.py` already covers `app/services/`; extend it (or add a Phase 3 companion) to also cover bracket-intent reads.
- **No removal of `trade_id` on `trading_bracket_intents`.** Stays as primary FK through Phase 5 per design doc § 6.2.
- **No live-money behavior change.** The new column is read by nobody. Resolution misses → `position_id=NULL`, never an exception.
- **Migration is idempotent.** `ADD COLUMN IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`. Backfill uses `WHERE position_id IS NULL`.
- **Tests use `_test`-suffixed DB.** Standard PROTOCOL Hard Rule.
- **No magic numbers.** Direction default `'long'`, account_type default `'cash'` — both match Phase 1/2's writers.

## Out of scope

- The Phase 4 reader-flip and inverse-reconcile rewrite (`event_count == 0` -> position-level history). Separate brief.
- TCA writer wiring (`f-tca-writer-wiring`) — independent code surface; can ship in parallel by a separate CC session if operator wants.
- `account_type='spot'` retrofit for Coinbase rows. Cheap one-line UPDATE, surfaces as `account_type_cleanup_coinbase_spot`. Not blocking.

## Success criteria

1. Mig 249 applied cleanly. `schema_version` tip advances to `'249_bracket_intents_position_id'`.
2. `trading_bracket_intents.position_id` populated for **100% of new intents** written since deploy (probe: `SELECT COUNT(*) FROM trading_bracket_intents WHERE created_at > '<deploy_ts>' AND position_id IS NULL` returns 0, allowing for intents where the trade row's natural key has no matching position — those should appear in a Phase 3 quarantine view).
3. Backfill resolves the historical bracket_intent rows. Acceptance target: >= 95% of rows with non-NULL `trade_id` resolved (mirrors Phase 2's 100% on the with_trade_id cohort, with some slack for any pre-Phase-2 anomalies).
4. Reader canary still passes — extend `tests/test_position_identity_phase2.py::test_no_reader_consults_position_id_in_app_services` (or add a Phase 3 sibling) to also catch reads of `trading_bracket_intents.position_id`.
5. 8+ new tests covering: resolver-reuse import, double-write at each of the 3 bracket-intent writer call sites, backfill idempotency, NULL handling on intent rows whose trade_id is itself NULL.

## Rollback plan

- `git revert <phase3 commit>` — removes the double-write. New intents stop populating `position_id`; pre-revert rows retain whatever they had.
- Column + index stay (additive migration); drop separately if absolutely needed.

## Reference

- Phase 2 CC report: `docs/STRATEGY/CC_REPORTS/2026-05-18_f-position-identity-phase-2.md`
- Design doc § 8.3 (Phase 3 spec): `docs/DESIGN/POSITION_IDENTITY.md`
- Phase 2 brief (lessons learned): `docs/STRATEGY/QUEUED/f-position-identity-phase-2-execution-events-position-id-backfill.md` (marked SHIPPED)
- Memory: `project_2026_05_18_phase2_shipped` (this session's entry)
