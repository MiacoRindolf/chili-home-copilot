# NEXT_TASK: bracket-intent-stale-label-cleanup

STATUS: DONE

## Goal

Close the structural gap surfaced by `audit-missing-stop-emergency-repair` (CC report 2026-05-03): `bracket_intents.broker_stop_order_id` is never UPDATEd by any code in the tree, so the local mirror column is dead and `intent_state='terminal_reject'` rows persist indefinitely on positions the broker has already protected. This produced the audit's $2,107→$276 false-alarm pattern.

This task closes both halves of the loop in one commit:

1. **Mirror writer for `broker_stop_order_id`.** On every sweep where `BrokerView.available is True`, sync `bracket_intents.broker_stop_order_id` from `BrokerView.stop_order_id`. Treat the column as advisory cache, not authority. Broker truth stays load-bearing at decision time; the mirror exists for diagnosis, audit, and admin display.
2. **Auto-transition `intent_state='terminal_reject' → 'reconciled'`** when classifier returns `kind=agree` on a subsequent sweep. Closes the stale-label loop for the 6 surviving false-alarm rows from today's sweep + any future ones.

Success means:

- The 6 stale-label rows from today (AIDX 1812 / CCCC 1813 / CRDL 1814 / IMTX 1818 / TLS 1821 / VFS 1822) transition to `intent_state='reconciled'` and get their `broker_stop_order_id` populated from broker truth on the next sweep after deploy.
- ELTX 1816 (which got a real broker stop placed today) gets its NULL mirror filled with the actual `69f7c5b8-…` order ID.
- After deploy + flag flip, the SQL probe `SELECT count(*) FROM trading_bracket_intents WHERE state='terminal_reject' AND broker_stop_order_id IS NULL AND id IN (220,221,222,224,226,229,230)` returns 0.
- Future audits reading `broker_stop_order_id IS NULL` get accurate signal instead of ~85% false-alarm noise.

This task ships **code + verification observations**, not analysis. Deliverable is `docs/STRATEGY/CC_REPORTS/<date>_bracket-intent-stale-label-cleanup.md`.

## Why now

Pulled directly from `audit-missing-stop-emergency-repair` Open Questions #1 and #2 (see `docs/STRATEGY/CC_REPORTS/2026-05-03_audit-missing-stop-emergency-repair.md`). Cowork review (`docs/STRATEGY/COWORK_REVIEWS/2026-05-03_audit-missing-stop-emergency-repair.md`) recommended both as YES.

Three reasons to do this next:

1. **Closes the false-alarm root cause while context is fresh.** The diagnostic pattern that misclassified $2,107 of "exposure" as risk was the never-UPDATEd mirror column. Every future audit will hit the same misread until we fix it.
2. **Stops the 6h-throttle log noise.** After the throttle expires for the 5 `covered_by_existing_sell` positions, every sweep will fire `state_gated_skip` for them indefinitely. The auto-transition closes that loop.
3. **Today's sweep generated perfect verification data.** We can confirm the mirror write and auto-transition produce the expected steady state on the very next sweep after deploy — same 7 trades, same intents, broker state unchanged, observable diff.

`f8b-verification-soak-3` (preserved at `docs/STRATEGY/QUEUED/`) remains scheduled for re-promotion on/after 2026-05-04 16:30 UTC. Today's task does not affect it.

## Step 1 — Code: mirror writer + auto-transition

### Where the change lives

- `app/services/trading/bracket_reconciliation_service.py` — the sweep / classifier loop. Find the call site where `BrokerView` is constructed per-intent and the classifier returns its `ReconciliationDecision`. Add the mirror-write + auto-transition there.
- `app/services/trading/bracket_intent_writer.py` — if there is a canonical writer module for `bracket_intents` mutations (the audit-missing-stop-emergency-repair task confirmed mutations live at lines 266, 413, 453, 641 in this file), add the new writer functions there. Reuse the single-writer ownership pattern. Do NOT bypass it.
- `app/migrations.py` — no schema change needed. `broker_stop_order_id` already exists as a nullable column.

### Mirror-writer behavior

For every intent the sweep classifies (i.e., where `BrokerView.available is True`):

- If `BrokerView.stop_order_id` is non-null AND differs from local `bracket_intents.broker_stop_order_id` (NULL or stale value): UPDATE local to broker value. Emit an info log: `[bracket_reconciliation] mirror_write trade=<id> intent=<id> ticker=<> old=<NULL|prev> new=<broker_id>`.
- If `BrokerView.stop_order_id` is NULL AND local `broker_stop_order_id` is non-null: UPDATE local to NULL (the broker order has been canceled / filled / orphaned). Emit info log: `[bracket_reconciliation] mirror_clear trade=<id> intent=<id> ticker=<> old=<prev>`.
- If `BrokerView.available is False`: NO mirror write this sweep. Broker-down means we have no observation.
- If both sides agree: no-op, no log.

The mirror writer is called from inside the sweep loop, AFTER the classifier returns its decision, BEFORE `_invoke_writer_for_decision` runs. The order matters: the classifier's `kind=agree` decision was made against the current `BrokerView`, so writing the mirror first means downstream `_invoke_writer_for_decision` calls operate on a freshly-synced row. (In practice this matters mostly for tests; the live sweep doesn't re-read the row mid-loop.)

### Auto-transition behavior

Inside the same sweep loop, AFTER the classifier returns:

- If `decision.kind == 'agree'` AND `local.intent_state == 'terminal_reject'`: UPDATE `bracket_intents.state = 'reconciled'`, set `last_diff_reason = 'auto_reconciled_after_terminal_reject'`. Emit a CRITICAL log (this is operationally important — a position previously labeled in failure has been confirmed healthy): `[bracket_reconciliation] auto_reconcile trade=<id> intent=<id> ticker=<> from=terminal_reject to=reconciled`.
- Audit emit via existing `_g2_event(writer="auto_reconcile_terminal_reject", ...)` so the transition shows up in `trading_execution_events`.
- Idempotent: if `intent_state` is already `reconciled`, no-op.

### Authority contract preservation

Critical: the local column is **advisory cache**, not authority. Document this at the writer site with a comment, and:

- `_invoke_writer_for_decision` and all reconciler decision paths MUST continue reading from `BrokerView`, not from `bracket_intents.broker_stop_order_id`. The mirror is a *consequence* of the sweep, not an *input*.
- A grep for new `broker_stop_order_id` reads in any decision-making code path is a regression. Audit-table reads, admin-UI reads, debugging reads — fine. Decision-time reads — not fine.
- The classifier's `kind=agree` path should not change behavior. The new transition is a side effect of `kind=agree`, not a new branch in the classifier.

### Feature flag

`CHILI_BRACKET_INTENT_MIRROR_ENABLED`, defaulting `False` in `app/config.py` (mirror Field pattern from `chili_bracket_missing_stop_repair_enabled`). Default OFF in compose too — the operator flips manually after reading the deploy. Same operational pattern as the emergency-repair task.

When the flag is OFF, neither the mirror-write nor the auto-transition fires. Pre-existing `state_gated_skip` and `kind=agree` no-op behavior preserved.

## Step 2 — Regression tests

Add `tests/test_bracket_intent_stale_label_cleanup.py` covering:

1. **Mirror write on `kind=agree` when local NULL, broker has order.** Seed: terminal_reject intent, broker_stop_order_id NULL, BrokerView.stop_order_id='abc'. Assert: post-sweep local broker_stop_order_id='abc', state='reconciled', audit event written, both log lines emitted.
2. **Mirror write on `kind=agree` when local stale, broker has different order.** Seed: local broker_stop_order_id='old', BrokerView.stop_order_id='new'. Assert: local updated to 'new', state transitions to reconciled.
3. **Mirror clear on `kind=missing_stop` when local has order, broker NULL.** Seed: local broker_stop_order_id='dead', BrokerView.stop_order_id=None, BrokerView.position_quantity > 0. Assert: local cleared to NULL, mirror_clear log emitted, state stays terminal_reject (auto-transition doesn't fire because kind != agree).
4. **No mirror write when broker unavailable.** Seed: BrokerView.available=False. Assert: local row unchanged, no log lines, no audit event.
5. **No-op when both sides agree.** Seed: local broker_stop_order_id='abc', BrokerView.stop_order_id='abc', state='reconciled'. Assert: no UPDATE issued, no audit event, no log lines.
6. **Auto-transition idempotency.** Run twice on a `state='reconciled'` row with `kind=agree`. Assert: only first run produces the audit event; second is no-op.
7. **Flag OFF preserves pre-existing behavior.** Seed: terminal_reject intent, kind=agree on broker truth, flag False. Assert: local row unchanged (broker_stop_order_id stays NULL, state stays terminal_reject), no logs, no audit events.
8. **Authority contract canary.** Static check: grep `app/services/trading/bracket_reconciliation_service.py` and `app/services/trading/bracket_writer_g2.py` for reads of `bracket_intents.broker_stop_order_id` in decision-making code paths (anything called from `_invoke_writer_for_decision` or its descendants). Fail the test if any new read is detected. Implement as a static analysis test using `ast` or a literal grep.
9. **No-op for `kind=agree` rows that were never `terminal_reject`.** Seed: state='reconciled', kind=agree. Assert: no auto-transition fires (already reconciled). Mirror still writes if applicable (covered by tests 1+2).

All tests use `chili_test` per the conftest guard.

## Step 3 — Deploy + verify

1. Land the code on a clean commit. Run `scripts/verify-migration-ids.ps1` (no schema change, but standard hygiene).
2. **Run the regression tests against `chili_test` and report results in the CC_REPORT.**
3. Pre-deploy SQL probe (capture in CC_REPORT):

```sql
SELECT bi.id AS intent_id, t.id AS trade_id, t.ticker,
       bi.state AS intent_state, bi.broker_stop_order_id, bi.last_diff_reason
FROM trading_bracket_intents bi
JOIN trading_trades t ON t.id = bi.trade_id
WHERE bi.id IN (220, 221, 222, 224, 226, 229, 230)
ORDER BY bi.id;
```

Expected pre-deploy state: all `state='terminal_reject'`, all `broker_stop_order_id IS NULL` (per CC_REPORT for audit-missing-stop-emergency-repair).

4. Restart `broker-sync-worker` to pick up the new code with flag OFF. Verify behavior unchanged: same `state_gated_skip` / `state='terminal_reject'` lines (after the 6h emergency-repair throttle expires).
5. Operator flips `CHILI_BRACKET_INTENT_MIRROR_ENABLED=1` in `docker-compose.yml` `broker-sync-worker.environment`. `docker compose up -d --force-recreate --no-deps broker-sync-worker`.
6. Watch one full sweep cycle (~2 minutes). Capture `mirror_write` and `auto_reconcile` log lines in the CC_REPORT.
7. Post-flip SQL probe (same query as step 3). Expected:
   - All 7 rows: `state='reconciled'`, `last_diff_reason='auto_reconciled_after_terminal_reject'`.
   - 1816 (ELTX): `broker_stop_order_id='69f7c5b8-7e15-4176-a31f-1544696055d5'`.
   - 220, 221, 222, 226, 229, 230 (AIDX, CCCC, CRDL, IMTX, TLS, VFS): `broker_stop_order_id` populated with whatever the broker reports for the existing protective sell orders.
8. **Critical post-deploy canary:** the `state_gated_skip` log lines for those 7 trades should stop appearing on subsequent sweeps. Capture log diff (last sweep before flag flip vs. first 3 sweeps after) in the CC_REPORT.

## Brain integration (reuse, don't rewrite)

- `BrokerView.stop_order_id` — already populated by the broker truth path; just read it.
- `BrokerView.available` — gates whether to mirror at all.
- `bracket_intent_writer.py` UPDATE site at line 266/453/641 — extend or reuse for the new mutations. Single-writer ownership preserved.
- `_g2_event(writer=...)` audit emission — reuse for `auto_reconcile_terminal_reject` writer name.
- `_invoke_writer_for_decision` entry point in `bracket_reconciliation_service.py` — the new code is upstream of this; do not modify the function itself.

## Constraints / do not touch

- **Do not modify the live-fast-path safety belts.** PROTOCOL Hard Rule 1.
- **Do not promote `bracket_intents.broker_stop_order_id` to authority.** Decision-time consumers must continue reading `BrokerView`. Test #8 enforces this.
- **Do not change the emergency-repair branch** shipped in `ef50d3f` or its 6h throttle.
- **Do not flip `CHILI_BRACKET_INTENT_MIRROR_ENABLED` to default True in code.** Default OFF; operator flips manually.
- **Do not bundle this with the unsupported-crypto pre-filter task** (audit HIGH #4). One logical change per commit.
- **Tests use `_test`-suffixed DB.** PROTOCOL Hard Rule 5.
- **No magic numbers.** No new constants needed; the mirror writer reads `BrokerView` and writes back, no thresholds involved.

## Out of scope

- `covered_by_existing_sell` provenance investigation (Open Q #3 — were the original "rejected" SELL_STOPs the ones now covering?). Separate task.
- 6h emergency-repair throttle tuning (Open Q #4). Separate task if soak shows mistuning.
- Unsupported-crypto pre-filter (audit HIGH #4). Next task after this.
- Venue-truth shadow-log wiring (audit HIGH #2). Queued behind soak-3.
- Pullback-exit signal-specific cold-start hold (audit HIGH #3). Queued.
- Investigation into why `terminal_reject` was set on these 7 in the first place. Out of scope.
- Any change to the classifier's `kind=` enum. The auto-transition is a *consumer* of `kind=agree`, not a new kind.
- Migrating the existing 7 rows via a one-shot migration. The new code path will resolve them on the next sweep; no migration needed.

## Success criteria

1. New code lives in `bracket_reconciliation_service.py` (sweep loop hook) and `bracket_intent_writer.py` (writer functions). Existing `_invoke_writer_for_decision` and the emergency-repair branch untouched.
2. Feature flag `CHILI_BRACKET_INTENT_MIRROR_ENABLED` exists in `app/config.py`, defaults False, documented in CC_REPORT.
3. `tests/test_bracket_intent_stale_label_cleanup.py` exists, all 9 scenarios pass against `chili_test`. Test #8 specifically asserts the authority contract holds.
4. After flag flip + worker restart, the SQL probe in Step 3.7 returns the expected post-state (all 7 rows reconciled with mirror populated). CC_REPORT shows pre/post diff.
5. CC_REPORT log-diff section shows `state_gated_skip` for these 7 trades stops appearing after deploy.
6. CC_REPORT written at `docs/STRATEGY/CC_REPORTS/<date>_bracket-intent-stale-label-cleanup.md` per PROTOCOL format. One commit (or tight series), pushed.

## Open questions for Cowork (surface in your report only if relevant)

1. **Is there a canonical writer module to extend, or should the new writer live alongside the sweep code?** The audit-missing-stop-emergency-repair task identified mutations in `bracket_intent_writer.py`. If that's the single-writer module, extend it. If the sweep code already has its own narrow writer (e.g., for `last_seen_at`), surface the boundary so we don't duplicate.
2. **Should `mirror_write` log lines be downgraded from info to debug?** Each sweep touches up to ~50 intents; a steady-state cluster of "no-op" or "mirror_write to same value" logs could become noise. Recommend: keep info for cases where the mirror actually changed (added, cleared, updated), debug for no-ops. Surface if the implementation reveals a different threshold makes more sense.
3. **Does any consumer outside the reconciler currently read `bracket_intents.broker_stop_order_id`?** The CC_REPORT for audit-missing-stop-emergency-repair confirmed no UPDATE site exists, but didn't grep for READ sites. If the admin UI or any audit query reads it expecting NULL-or-real semantics, populating it changes their visible state. Surface the grep results in the CC_REPORT so any consumer behavior change is documented.
4. **What's the right value for `last_diff_reason` on the auto-reconcile transition?** I proposed `'auto_reconciled_after_terminal_reject'`. If the codebase has an enum or convention, surface it.

## Rollback plan

- **Code rollback:** Revert the commit. No schema change; nothing to undo at the DB layer beyond the rows that got their `broker_stop_order_id` populated and `state` transitioned. Those are correct values; leaving them in the rolled-back-but-populated state is safe.
- **Flag rollback:** Set `CHILI_BRACKET_INTENT_MIRROR_ENABLED=0` and restart `broker-sync-worker`. The new code becomes a no-op. Pre-existing classifier behavior resumes. Already-mirrored rows stay populated (which is fine — they're advisory).
- **If a downstream consumer breaks** because it relied on `broker_stop_order_id` being NULL: that's a contract bug we exposed, not a regression of this task. Fix the consumer.
- **No live-broker rollback needed.** This task makes no broker calls.
