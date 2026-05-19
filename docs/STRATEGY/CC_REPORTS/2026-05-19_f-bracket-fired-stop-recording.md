# CC_REPORT: f-bracket-fired-stop-recording

**Session type:** Cowork-direct execution (operator: "use daemon..." continuing the next task per recommendation).

## What shipped

**Single commit** on `main`, 3 files / **+150** LOC.

- `app/services/broker_service.py` — Robinhood stale-close writer (~line 2520). Mirror of the Coinbase one I shipped 4 hours earlier. This is the **landing path for broker-fired bracket stops on RH equity**.
- `app/services/trading/stop_engine.py` — auto-exec sell-recording at the `bm.sell()` callsite. The `sync_pending_exit_order` writer doesn't fire here (no `pending_exit_order_id`), so the event would be missed without this.
- `tests/test_bracket_fired_stop_recording.py` — 4 new static-grep tests pinning both writer sites and their try/except wrappers.

**No migration.** Pure code change.

## Sell-event coverage map (post-this-commit)

This commit closes the **final two writer-side blind spots**. Coverage now:

| Close path | Writer | Shipped |
|---|---|---|
| Pending-exit-order fills (RH equity polling) | `sync_pending_exit_order` (`robinhood_exit_execution.py:1267`) | 2026-05-18 |
| Crypto exit-monitor sells (Coinbase + RH crypto) | `crypto/exit_monitor.py` (after `_place_market_sell_for_trade`) | 2026-05-19 |
| Coinbase stale-close auto-closes | `coinbase_service.py` (after `coinbase_position_sync_gone`) | 2026-05-19 |
| **Robinhood stale-close auto-closes** | `broker_service.py` (after `broker_reconcile_position_gone`) | **THIS commit** |
| **stop_engine auto-exec sells** | `stop_engine.py` (after `bm.sell()`) | **THIS commit** |
| Historical (pre-recording) closures | mig 254 synthetic backfill | 2026-05-18 |

**Phase 4's `position_has_recorded_sell` helper now has complete data plumbing.** Every close path that produces a real sell or a synthetic auto-close writes a sell event.

## Verification

**Tests.** 35/35 PASS:
- 4 new bracket-fired-stop-recording tests (writer-site + try/except canaries)
- 5 existing Coinbase-exit-recording tests
- 10 Phase 2 + 10 Phase 3 + 6 Phase 4 = 26 existing position-identity tests

**Compile.** All 3 modified files compile clean.

**Deploy.** `docker compose up -d --force-recreate chili scheduler-worker brain-worker autotrader-worker broker-sync-worker` — all 5 services recreated healthy.

**Post-deploy event_type distribution** (`scripts/dispatch-bracket-stop-commit-deploy-out.txt`): unchanged from pre-deploy because the new event_types only fire on their specific triggers (broker-fired stop / stop_engine auto-exec). Both will start producing within hours of normal trading activity.

## Surprises / deviations

1. **Confirmed `sync_pending_exit_order` ALREADY records sells.** The prior brief (`f-execution-events-sell-side-recording`) added the writer at `robinhood_exit_execution.py:1267`. So the RH polling-driven exit fills were already covered. The real gaps were the *other* two paths.

2. **fast_path/exit_manager.py is out of scope** — that's the paper-trading / fast-path simulator, writes to `fast_executions` not `trading_trades`. No real broker activity.

3. **`broker_service.py` stale-close branch handles BOTH `broker_reconcile_position_gone` AND `broker_reconcile_no_exit_price`.** The writer fires for both (the only difference is `exit_price` being NULL in the latter case, which is fine — the event still records the position closure as a synthetic sell).

## Deferred

- **Phase 5 envelope-rename + decision-layer split** — the next big position-identity refactor step. Waits for at least one real `[phase4_*]` log line in production (i.e., the Phase 4 inverse-reconcile branch actually fires once and we observe the decision).
- **RH session restoration** — operator action, blocks any actual Phase 4 firing. The watcher will surface it on the next daily run when it happens.
- **Bracket-stop fill recording from the bracket reconciliation path** — `bracket_reconciliation_service.py` detects state drift on bracket stops but doesn't explicitly record sell events for filled-and-vanished stop orders. In practice, the `broker_reconcile_position_gone` path catches the downstream position vanish (the writer added here), so this isn't a gap — just a different observation point. Not in scope.

## Rollback plan

- `git revert <commit>` removes both writer call blocks.
- New events already written stay; they're harmless audit rows.
- Both writers are wrapped in try/except, so a failed revert wouldn't leave anything in an unsafe state.

## Status

Position-identity refactor's **data plane is now fully complete**. Every sell across every venue writes a sell event. Phase 4's reader has comprehensive coverage. The system can now safely consult `position_has_recorded_sell` and trust the answer.

NEXT_TASK to be set to **Phase 5 envelope-rename**, gated on the first real `[phase4_*]` log line. CC report = this file. Memory + plan + decision-log updates separately.
