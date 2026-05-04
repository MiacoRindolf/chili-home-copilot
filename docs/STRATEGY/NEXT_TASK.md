# NEXT_TASK: bracket-intent-stop-price-live-sync

STATUS: DONE

## Goal

Make `bracket_intents.stop_price` continuously track `trade.stop_loss` for live broker-backed trades, so the broker reflects the brain's current dynamic stop view instead of a frozen entry-time number. Today's diagnostic (see Why now) showed the two values have drifted apart by $0.14–$0.93 across the 5 unprotected positions over 2 days — and `place_missing_stop` reads from `bracket_intents.stop_price`, so without this fix, every broker stop CHILI places is at the stale value.

Success means:

1. **Diagnose** why the existing `_maybe_emit_bracket_intent` (called from `stop_engine.evaluate_all` at `app/services/trading/stop_engine.py:896`) is not refreshing `bracket_intents.stop_price` for the 5 affected rows. Hypotheses to test in order:
   - The function is gated behind `if result.alert_event` (line 893) — so when state is steady (no BREAKEVEN/TRAILING transition this sweep), the upsert never runs even though `trade.stop_loss` still reflects an earlier move.
   - `upsert_bracket_intent` may have a path that no-ops on `intent_state='terminal_reject'` rows (the function I read earlier only documented skips for `CLOSED` and authoritative-prefix states; verify behavior for terminal_reject).
   - `BRAIN_LIVE_BRACKETS_MODE` defaults to `"shadow"` in `app/config.py:339` and isn't overridden in compose. Shadow mode SHOULD upsert; verify the path runs end-to-end.
2. **Fix** so `bracket_intents.stop_price` is updated whenever `trade.stop_loss` changes for an open broker-backed trade, regardless of `intent_state`. The right shape is probably an unconditional sync at the end of each `evaluate_trade` (not gated on `result.alert_event`), or a separate periodic mirror sync.
3. **Preserve** the "broker is authoritative" contract. `bracket_intents.stop_price` joins `broker_stop_order_id` as advisory cache. Decision-time consumers (writer, classifier) MUST continue reading the source of truth (BrokerView for live broker state, `trade.stop_loss` for the engine's view) — not the cached `bracket_intents.stop_price`.
4. **Verify on the live system** post-deploy that `bracket_intents.stop_price` tracks `trade.stop_loss` within one sweep cycle for all open broker-backed trades, including any in `terminal_reject` state.

This task ships **diagnostic + structural fix + tests**. Deliverable: `docs/STRATEGY/CC_REPORTS/<date>_bracket-intent-stop-price-live-sync.md`.

## Why now

The 2026-05-03 stale-label-cleanup task closed the broker_stop_order_id mirror gap. The Cowork review for that task surfaced an adjacent gap when the operator asked "shouldn't stops be dynamic?":

- A read-only DB probe confirmed `trading_stop_decisions` has only `state='initial'` rows for trades 1812/1813/1814/1821/1822 (all dated 2026-05-01 14:37). No subsequent BREAKEVEN/TRAILING decisions logged.
- BUT `trade.stop_loss` HAS moved on all 5 — gaps of $0.14 to $0.93 versus `bracket_intents.stop_price`. So the engine has been updating `trade.stop_loss` somewhere; the `_record_stop_decision` path just isn't reflecting every move.
- `bracket_intents.stop_price` is frozen at 2026-05-01 14:37 entry-time values. `_maybe_emit_bracket_intent` has not refreshed the rows even though they're in shadow mode and not in CLOSED/authoritative state.

This means `place_missing_stop` (the writer that the operator's `CHILI_BRACKET_WRITER_CANCEL_COVERING_SELL=1` flip activates) would place stops at the old May-1 prices — wider than the brain's current view. The operator already paid the cost: had to run a manual one-shot resync SQL today (`scripts/resync-bracket-intents-from-trade-stop.sql`) before flipping the flag. This task prevents recurrence.

A secondary finding worth surfacing in the diagnosis: `trading_stop_decisions` having only `initial` rows AND `trade.stop_loss` having moved suggests the decision-recording path may also have a coverage gap. Investigate as part of step 1; flag in CC_REPORT if the cause turns out to be alert-cooldown suppression of decisions (which would mean the engine's state-machine moves are happening invisibly).

## Step 1 — Diagnostic

Before writing any fix, produce a written diagnosis in the CC_REPORT covering:

1. **Where does `trade.stop_loss` actually get UPDATEd?** Grep the codebase for assignments to `Trade.stop_loss`. Possibilities: stop_engine's `_apply_stop_to_trade`, position monitor, manual operator path, broker_sync, others. The 5 trades have moved by varying amounts ($0.14 to $0.93) — find the actual writer path.
2. **Does `_maybe_emit_bracket_intent` fire on every sweep, or only when `result.alert_event` is truthy?** Read the call site at `stop_engine.py:893-896`. Confirm or refute the hypothesis that gated upsert is why the bracket_intent stop_price stays frozen.
3. **What does `upsert_bracket_intent` actually do for an existing `terminal_reject` row?** Trace the function in `bracket_intent_writer.py`. The early-return at line 378-396 skips for CLOSED and authoritative-prefix states. Does the rest of the function update `stop_price` on terminal_reject rows, or does it short-circuit somewhere later?
4. **Verify `BRAIN_LIVE_BRACKETS_MODE` is in fact `shadow` in the running broker-sync-worker.** `docker exec broker-sync-worker printenv BRAIN_LIVE_BRACKETS_MODE`. If the result is anything other than `shadow` or empty (which falls back to default `shadow`), surface that.
5. **Confirm decision-recording coverage gap.** Cross-check: count of `trading_stop_decisions` rows for trades 1812/1813/1814/1821/1822 in the last 48h, vs. expected count given `trade.stop_loss` has moved. Surface the gap if real.

Write the diagnosis into the CC_REPORT BEFORE shipping any fix. The shape of the fix depends on what the diagnosis finds.

## Step 2 — Code fix

The exact shape depends on the diagnosis; suggested defaults:

### Default option A (simplest, if hypothesis #1 holds): unconditional sync inside the savepoint

In `stop_engine.evaluate_all` at lines 887-896 (the per-trade savepoint), call `_maybe_emit_bracket_intent` UNCONDITIONALLY whenever `_apply_stop_to_trade` produced a change OR whenever the trade has a non-NULL `stop_loss` and broker_source set. Currently:

```python
if result.alert_event and result.alert_event != "DATA_STALE":
    _record_stop_decision(db, trade.id, result)
    _apply_stop_to_trade(db, trade, result)
    _maybe_emit_bracket_intent(db, trade, brain)
```

becomes (illustrative — CC may refine):

```python
if result.alert_event and result.alert_event != "DATA_STALE":
    _record_stop_decision(db, trade.id, result)
    _apply_stop_to_trade(db, trade, result)
# Sync bracket_intents.stop_price every sweep, even when no alert fired.
# trade.stop_loss may have been updated by another path, OR _apply_stop_to_trade
# above. The shadow upsert is idempotent; calling it on no-op sweeps is cheap.
_maybe_emit_bracket_intent(db, trade, brain)
```

### Default option B (more invasive but cleaner): a dedicated mirror sync

Add a function `sync_bracket_intent_stop_from_trade(db, trade)` that runs unconditionally at the end of each per-trade savepoint. Just reads `trade.stop_loss` and writes `bracket_intents.stop_price` if they differ. Skips closed/authoritative rows. Bypasses the broader `upsert_bracket_intent` (which has additional concerns like compute_bracket_intent and target sync that don't need to run every sweep).

Pick the smaller of the two paths in CC's judgment; surface the trade-off in the CC_REPORT.

### Whatever path is chosen

- Must work for `intent_state='terminal_reject'` rows (this is the exact case that produced today's exposure).
- Must NOT touch CLOSED rows.
- Must NOT touch rows starting with `authoritative_` prefix (preserves Phase G.2 frozen authority).
- Must be idempotent — calling on a no-op sweep is a no-op write.
- Must emit an info log line ONLY when `stop_price` actually changes (silent on no-op sweeps to avoid noise).
- Must respect the same feature-flag gate as the existing path (`brain_live_brackets_mode != "off"`).

### Authority contract

The fix puts `bracket_intents.stop_price` firmly in the cache layer. Add a comment at the writer site documenting: "`bracket_intents.stop_price` is advisory cache, mirroring `trade.stop_loss`. Decision-time consumers must read `trade.stop_loss` (engine truth) or `BrokerView` (broker truth) directly. The cache exists for `place_missing_stop` to read at placement time and for audit visibility."

A static-grep canary test (similar to the stale-label-cleanup task's test #8) should fail if any new decision-time consumer starts reading `bracket_intents.stop_price` instead of trade.stop_loss / BrokerView.

## Step 3 — Tests

Add `tests/test_bracket_intent_stop_price_live_sync.py` covering:

1. **Sync fires on every sweep when trade.stop_loss != bi.stop_price.** Seed: open trade with `trade.stop_loss=2.0`, bracket_intent with `stop_price=1.5`. Run one sweep. Assert: post-sweep `bi.stop_price=2.0`, info log line emitted.
2. **No-op when values match.** Seed: both at 2.0. Run sweep. Assert: no UPDATE issued (check via session-event count or `updated_at` unchanged), no log line.
3. **terminal_reject does NOT block sync.** Seed: open trade, terminal_reject intent, drift exists. Run sweep. Assert: stop_price updated to trade.stop_loss; intent_state stays terminal_reject (auto-transition is a separate concern from the prior task).
4. **CLOSED state DOES block sync.** Seed: closed intent with stale stop_price. Run sweep. Assert: stop_price unchanged.
5. **Authoritative prefix DOES block sync.** Seed: `intent_state='authoritative_submitted'`. Run sweep. Assert: stop_price unchanged.
6. **`brain_live_brackets_mode='off'` blocks sync.** Seed: drift exists, mode=off. Run sweep. Assert: stop_price unchanged.
7. **Authority contract canary.** Static-grep test parallel to the prior task's test #8: fail if `_invoke_writer_for_decision` or its descendants read `bracket_intents.stop_price` to make a decision (other than `place_missing_stop` reading at placement time, which is the intended path).
8. **Sync continues to fire across multiple sweeps as trade.stop_loss moves.** Seed: drift exists, sync once → matches. Mutate `trade.stop_loss` to a new value. Run another sweep. Assert: stop_price catches up.
9. **No regression on stale-label-cleanup tests.** All 9 tests from `test_bracket_intent_stale_label_cleanup.py` still pass.
10. **No regression on emergency-repair tests.** All 7 tests from `test_bracket_emergency_terminal_reject_repair.py` still pass.

All tests use `chili_test`.

## Step 4 — Deploy + verify

1. Land the code on a clean commit. `verify-migration-ids.ps1` (no schema change expected, but standard hygiene).
2. **Run the full test suite for bracket_* tests against `chili_test`. Report results in CC_REPORT.**
3. Pre-deploy SQL probe (capture in CC_REPORT):
   ```sql
   SELECT bi.id AS intent_id, t.id AS trade_id, t.ticker,
          bi.intent_state, bi.stop_price AS bi_stop, t.stop_loss AS trade_stop,
          (t.stop_loss - bi.stop_price)::numeric(12,4) AS gap,
          bi.updated_at AS bi_updated, t.last_updated_at AS trade_updated
   FROM trading_bracket_intents bi
   JOIN trading_trades t ON t.id = bi.trade_id
   WHERE t.status = 'open' AND t.broker_source IS NOT NULL
   ORDER BY bi.id;
   ```
   Expected: many rows with non-zero `gap` (frozen mirror).
4. Restart `broker-sync-worker` (and any other worker that runs stop_engine — likely `scheduler-worker` per `trading_scheduler.py:1675`). Verify pickup.
5. Wait one stop_engine sweep cycle. Capture log lines: `[bracket_intent_writer] sync_stop_price intent=<> trade=<> ticker=<> old=<> new=<>`.
6. Post-deploy SQL probe (same query). Expected: `gap = 0` (or very close, modulo float precision) for all rows where `trade.stop_loss IS NOT NULL`. ELTX 1816 / IMTX 1818 (already reconciled) should also catch up to current `trade.stop_loss`.
7. **Live sanity check:** find a position whose stop_engine state is currently TRAILING. Note its `trade.stop_loss`. Wait for one sweep. Confirm the new value lands in `bracket_intents.stop_price` within a sweep cycle. (If no TRAILING trade is currently open, surface in CC_REPORT and skip.)

## Brain integration (reuse, don't rewrite)

- `stop_engine.evaluate_all` — the existing per-trade savepoint at lines 887-902. Hook the new sync inside the savepoint so per-trade failures roll back cleanly.
- `bracket_intent_writer.upsert_bracket_intent` — reuse if Default Option A; or write the new narrower function in the same file for Option B.
- `BracketIntentInput` / `compute_bracket_intent` — only needed for Option A's full upsert path. Skip for Option B.
- `_g2_event` / audit-emission plumbing — reuse for an optional audit row when stop_price actually changes.
- `trading_stop_decisions` table — out of scope for the fix, but flag in diagnosis if its coverage is incomplete.

## Constraints / do not touch

- **Do not modify the live-fast-path safety belts.** PROTOCOL Hard Rule 1.
- **Do not promote `bracket_intents.stop_price` to authority.** It joins the cache layer alongside `broker_stop_order_id`. Decision-time reads stay against `trade.stop_loss` / `BrokerView`.
- **Do not modify `_apply_stop_to_trade`** (the function that writes `trade.stop_loss`). The diagnosis may surface a separate concern around `_record_stop_decision` coverage; that's a follow-up task, not this one.
- **Do not touch CLOSED or `authoritative_*`-prefixed rows.** Phase G.2 contract preserved.
- **Do not flip `BRAIN_LIVE_BRACKETS_MODE` to anything other than `shadow` in code.** That's a deliberate operator choice with a separate rollout doc (`docs/TRADING_BRAIN_LIVE_BRACKETS_ROLLOUT.md`).
- **No magic numbers.** The 5-trade list is illustrative for verification; the fix must apply to all open broker-backed trades.
- **Tests use `_test`-suffixed DB.** PROTOCOL Hard Rule 5.
- **Do not bundle this with the queued `bracket-writer-cover-policy-clarify` task.** Separate logical change.

## Out of scope

- Auto-execution of stops on the broker. R30 (2026-04-30) deliberately removed `_try_auto_execute_stop`. Don't bring it back. The bracket_writer_g2 + reconciler chain is the path to broker.
- Investigating the `trading_stop_decisions` coverage gap (only `initial` rows for moved trades). Flag in CC_REPORT diagnosis; if confirmed, queue as a follow-up.
- Any change to the stop_engine's state machine logic, brain context, or alert dispatch.
- Backfilling `bracket_intents.stop_price` for closed trades. Mirror the live ones; let history rest.
- Migrating the 5 currently-affected rows. Operator already ran `scripts/resync-bracket-intents-from-trade-stop.sql` for those.
- Any change to `trade.stop_loss` write paths.

## Success criteria

1. Diagnosis section in CC_REPORT names the exact mechanism by which `bracket_intents.stop_price` was diverging from `trade.stop_loss` (which hypothesis was right).
2. Code fix lives in `stop_engine.py` and/or `bracket_intent_writer.py`. Existing call sites untouched except for the new unconditional sync.
3. Authority contract canary test (test #7) passes — no decision-time consumer reads `bracket_intents.stop_price`.
4. All 10 new tests pass against `chili_test`. Existing 9 (stale-label-cleanup) + 7 (emergency-repair) tests still pass — no regression.
5. Post-deploy SQL probe shows `gap = 0` for all open broker-backed trades within one sweep cycle. CC_REPORT shows pre/post diff.
6. CC_REPORT log-diff section shows the new sync log lines for trades that moved.
7. CC_REPORT written at `docs/STRATEGY/CC_REPORTS/<date>_bracket-intent-stop-price-live-sync.md`. One commit (or tight series), pushed.

## Open questions for Cowork (surface in your report only if relevant)

1. **Default Option A vs B?** The diagnosis may make one obviously right. If both are viable, surface the trade-off so we can decide. Lean toward Option A unless `upsert_bracket_intent` does enough extra work that running it every sweep is meaningfully more expensive than a narrow sync.
2. **`trading_stop_decisions` coverage gap.** If the diagnosis confirms that decisions aren't recorded on every state-machine move (only when alert_event fires), surface it as a follow-up task. Don't fix in this task; investigate why.
3. **Should there be a backstop that detects future drift?** A startup-time SQL canary that counts open broker-backed trades with `bi.stop_price != t.stop_loss` and logs a WARNING if any exist would catch a regression of this fix without waiting for an audit. Surface implementation cost; default to "yes if cheap, no if it adds startup latency."
4. **Is there any code path that reads `bracket_intents.stop_price` for decisions other than `place_missing_stop`?** The canary test (#7) enforces "no new readers"; the diagnosis should also confirm "no existing readers besides the writer." Surface findings.

## Rollback plan

- **Code rollback:** `git revert <this commit>`. The new sync becomes a no-op. `bracket_intents.stop_price` stops tracking `trade.stop_loss` again (returns to today's pre-fix behavior). Operator can run `scripts/resync-bracket-intents-from-trade-stop.sql` ad-hoc when needed.
- **Persisted-data rollback:** Not needed. The post-fix `bracket_intents.stop_price` values are correct (engine's current view). After revert, they stay correct until the next time `trade.stop_loss` moves and the cache goes stale again.
- **No live-broker rollback needed.** This task makes no broker calls. Whatever stops are currently at the broker stay there.
- **No schema rollback needed.** Schema is unchanged.
