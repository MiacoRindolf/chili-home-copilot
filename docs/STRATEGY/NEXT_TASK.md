# NEXT_TASK: f-position-identity-phase-4-inverse-reconcile-position-history

STATUS: PENDING

## Goal

With Phase 2 + Phase 3 shipped (mig 248 + 249), every fill in `trading_execution_events` and every bracket in `trading_bracket_intents` has a populated `position_id` FK to `trading_positions`. Phase 4 is the first **reader-side** consumer: rewrite the conservative `event_count == 0` inverse-reconcile workaround at `broker_service.py:1944` (the "broker-truth-self-heal" inverse-reconcile path that re-opens dead Trade rows when the broker still holds the position) to consult position-level fill history instead.

After Phase 4:
1. The inverse-reconcile decision is precise instead of conservative. Today: "if Trade row has zero fill events on the current trade_id, the close was bookkeeping-only — reopen." Phase 4: "if the position has no SELL fill in its full history (across all Trade row generations linked to this position_id), the close was bookkeeping-only — reopen."
2. The five `*_position_gone` exit_reason strings (`broker_reconcile_position_gone`, `coinbase_position_sync_gone`, `phantom_after_terminal_reject`, `emergency_price_monitor_guardrail`, `zombie_reconcile_orphan`) can begin to be replaced by a single position-level state-machine check. (Phase 4 just unblocks this; the full retirement is Phase 5 per design § 8.5.)

## Why this is next

Phase 2 + Phase 3 just shipped tonight. The Phase 4 brief in the design doc has been the gating consumer the whole time. With both `trading_execution_events.position_id` and `trading_bracket_intents.position_id` populated (100% on their with_trade_id cohorts), the inverse-reconcile rewrite has the foundation it needs.

The Phase 1 soak surfaced this as the marquee problem (GRT-USD's 13 close/reopen cycles with `event_count==0` workaround firing each time). Today's TCA finding (102 bps avg entry slippage) is separate — that's a Tier B item being handled in parallel briefs.

## Brain integration (reuse, don't rewrite)

- `app/services/broker_service.py:1944-2050` — the inverse-reconcile branch in `sync_positions_to_db`. This is the rewrite target.
- `app/services/trading/position_resolver.resolve_position_id` — already imported across the codebase; reuse for any position lookup.
- `app/services/trading/execution_audit.record_execution_event` — Phase 2's writer. Phase 4 is read-side; this is the data source.
- `trading_position_events` — Phase 1's authoritative position-level event log. Use for the "did this position ever close" question.

## Constraints / do not touch

- **Feature-flagged behind a new setting** `chili_position_identity_phase4_authority_enabled` (default `False`). The current `event_count == 0` path stays as-is when the flag is off. Operator flips the flag after a paper-soak window.
- **No schema change.** All inputs already exist (`position_id` on events; `trading_position_events` from Phase 1).
- **Position-identity reader canary canaries must be updated.** Both Phase 2's and Phase 3's `test_no_reader_consults_position_id` tests need to be updated to allow the new reader in `broker_service.py`. Update them explicitly with a comment pointing at the Phase 4 commit.
- **Live-money behavior unchanged when the flag is off.** Inverse-reconcile continues to use the old workaround until operator opts in.
- **Tests use `_test`-suffixed DB.** Standard PROTOCOL Hard Rule.

## Out of scope

- Retiring any of the five `*_position_gone` exit_reason strings. That's Phase 5.
- Renaming `trading_trades` to `trading_management_envelopes`. Phase 5.
- TCA-driven entry-price gating, maker-only Coinbase routing, reference-price re-snap. Separate briefs.
- The Phase 1 soak `event_count==0` cross-check itself (Phase 4 replaces it, doesn't preserve it).

## Success criteria

1. New setting `chili_position_identity_phase4_authority_enabled` (default `False`).
2. New helper in `position_resolver.py` (or sibling): `position_has_recorded_sell(db, position_id) -> bool` consulting `trading_execution_events` (filtered on `event_type` representing a sell/fill).
3. Inverse-reconcile path at `broker_service.py:~1944` routes through the new helper when the flag is on; old path when off.
4. Pytest cases (≥6): flag-off keeps old behavior; flag-on uses position history; position with no recorded sell across multiple Trade row generations correctly re-opens the current row; position with a recorded sell does NOT re-open (broker truth says position is gone for a real reason).
5. Reader canaries updated. Static-grep allowlist extended to include the new reader site.

## Rollback plan

- Disable: `CHILI_POSITION_IDENTITY_PHASE4_AUTHORITY_ENABLED=false` in `.env`, `docker compose up -d --force-recreate chili broker-sync-worker`. The old path takes over.
- Code revert: `git revert <phase4 commit>`. Helper stays in `position_resolver.py` (no harm); the inverse-reconcile reverts.

## Reference

- Design doc §8.4: `docs/DESIGN/POSITION_IDENTITY.md`
- Phase 2 CC report: `docs/STRATEGY/CC_REPORTS/2026-05-18_f-position-identity-phase-2.md`
- Phase 3 + TCA CC report: `docs/STRATEGY/CC_REPORTS/2026-05-18_f-position-identity-phase-3-and-tca-and-account-type.md`
- Memory: `project_2026_05_18_phase2_shipped`, `project_2026_05_18_phase3_and_tca_shipped` (this session's entry)
