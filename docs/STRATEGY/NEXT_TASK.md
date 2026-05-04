# NEXT_TASK: broker-truth-self-heal

STATUS: DONE

## Goal

Make CHILI symmetric: today the system has multiple paths to mark a Trade row closed automatically (`broker_reconcile_position_gone`, `phantom_after_terminal_reject`, `emergency_price_monitor_guardrail`, `zombie_reconcile_orphan`) but **zero paths to un-mark a Trade row closed when the broker subsequently proves the position still exists.** The operator should never need to run an SQL script to reconcile broker truth into the database. The system should observe, cross-check, and self-heal.

This task ships four coordinated changes:

1. **Retire `_try_emergency_repair_terminal_reject` sub-branch 2.** The path is a hazard with no benefit — `broker_reconcile_position_gone` (with R32 protection) already owns the "broker says position gone" close path. Sub-branch 2 was a redundant, less-protected alternative. Delete it.

2. **Retire automated `emergency_close_all` callers.** When the system is disconnected or detects emergency conditions, the correct response is **freeze** (activate kill switch, refuse new entries, leave existing trades for the operator to manage), not **auto-liquidate** (especially when the auto-liquidate doesn't even submit broker orders — Bug 4). Replace `alerts.py:1232` with a freeze call. `emergency_close_all` stays in the module for explicit operator invocation only, with Bugs 2+3 fixed inside.

3. **Add inverse-reconcile in `broker_sync`.** When the broker reports a position for a (user, broker_source, ticker) and the most recent Trade row for that key is `status='closed'` AND has no SELL fill on record in `trading_execution_events`, the close was bookkeeping-only — re-open the row. Match qty and avg_price as a sanity cross-check. Single rule, no magic numbers, no allow-lists of close reasons.

4. **Fix the residual lies in the now-operator-only `emergency_close_all`.** Bug 2: `exit_price = entry_price` fallback → set NULL. Bug 3: `activate_kill_switch` non-idempotent → guard.

After deploy, the 11 broker-vs-DB mismatched positions from today self-heal on the first broker_sync cycle. No script. No human SQL. The system observes broker truth and corrects itself.

## Why now

Today (2026-05-04) two automated close paths fired for the first time ever and produced 11 unmanaged Robinhood positions whose Trade rows are wrongly marked closed in DB. The shipped flap guard prevents the *next* sub-branch-2 cascade; it does NOT undo today's, and it doesn't address the noon `emergency_close_all` trigger or the structural one-way-reconciliation gap.

The operator's stated principle: **CHILI must be smart and adaptive, not constant-and-static.** Every threshold should be derived from observable data; every potentially-destructive action should cross-check existing system state before firing. Today's 11-position lockup is a direct consequence of that principle being violated in three places (sub-branch 2's single-sample close, the static 10-min disconnect timer, the missing inverse-reconcile path).

The 11 stuck positions are real-money exposure right now. The inverse-reconcile design above is what shrinks that exposure window from "until operator runs script" to "until next 2-min broker_sync cycle."

## Brain integration (reuse, don't rewrite)

- `app/services/broker_service.py::sync_positions_to_db` (R32 lives here, lines ~1473-1515) — the inverse-reconcile lives in the same function. The R32 wholesale guard already runs first; if it refuses (broker returned `[]`), the inverse-reconcile doesn't run. Otherwise: for each broker position, look up the most recent Trade row, decide reopen-vs-create-vs-no-op.
- `trading_execution_events` — the single source of truth for "did a fill happen." The inverse-reconcile's only cross-check is "is there a SELL fill row for this trade_id." No new schema, no new state.
- `app/services/trading/governance.py::activate_kill_switch` — the freeze primitive. Idempotency guard goes in here as the Bug 3 fix; the freeze call from `alerts.py` reuses the same primitive.
- `app/services/trading/alerts.py::run_price_monitor` lines 1222-1242 — the call site that currently invokes `emergency_close_all`. Replace with `activate_kill_switch(reason=f"price_monitor_freeze: {emergency_reason}")` + early return. No more auto-liquidation from this surface.
- The C2 guard in `sync_positions_to_db` — keep. C2 still protects against backfilling Trade rows for positions that have NO history at all in CHILI (genuinely-new positions the system didn't open). The inverse-reconcile path runs BEFORE C2 reaches its refusal, handling the "we did open this, but our record was wrongly closed" case. C2 only reaches the refusal branch when there's no historical record whatsoever.

## Path

**Design principle: zero new magic numbers, zero new env-overridable hardcoded defaults, zero new auto-close paths.** Decisions derived from observable system state or binary cross-checks against existing data. If you find yourself typing a literal threshold or a frozen list of strings, stop and call it back to Cowork.

### Step 1 — Retire sub-branch 2 of `_try_emergency_repair_terminal_reject`

In `app/services/trading/bracket_reconciliation_service.py` around line 862-943, **delete** the entire `if broker_qty <= 0.0:` block (sub-branch 2). The function should only have sub-branches 1 (broker unavailable → fall through) and 3 (real exposure → place stop). When `broker_qty == 0`, fall through to the existing `state_gated_skip` outcome — the parent `broker_reconcile_position_gone` path (which has R32 protection at the wholesale layer) already owns this case.

The flap guard introduced in `f917c02` (today's commit) becomes dead code after this deletion. **Delete it too** — the helper `_bump_phantom_close_zero_qty_counter`, the constant `EMERGENCY_REPAIR_PHANTOM_CLOSE_MIN_CONSECUTIVE_ZERO_SWEEPS`, the env var `CHILI_BRACKET_PHANTOM_CLOSE_MIN_CONSECUTIVE_ZERO_SWEEPS`, the sub-branch-3 counter reset.

Migration 223's column `phantom_close_consecutive_zero_qty_sweeps` is now orphan. Leave it in place (don't write a drop-column migration in this task — schema-removal is a separate hygiene ticket). It will sit at 0 forever.

Update `tests/test_bracket_emergency_terminal_reject_repair.py`: scenarios 1 (existing phantom-close), 8 (single-sweep deferral), 9 (three-sweep close), 10 (counter reset) all assume sub-branch 2 exists. Replace them with **one** new scenario: "broker_qty == 0 falls through to state_gated_skip" — assert no trade-row mutation, no audit emission, return None. Existing scenarios 2-7 (broker unavailable, real-exposure variants) stay unchanged.

### Step 2 — Replace `emergency_close_all` auto-call with freeze

In `app/services/trading/alerts.py:1230-1236`, replace:

```python
if action == "emergency_close_all":
    out = emergency_close_all(db, user_id, reason="price_monitor_guardrail")
    results["emergency_action"] = "emergency_close_all"
    results["emergency_result"] = out
    logger.critical("[alerts] Emergency liquidation executed from price monitor: %s", out)
    return results
```

with:

```python
if action == "emergency_close_all":
    # Disconnect / drawdown-critical detected. Correct response is FREEZE,
    # not auto-liquidate: when disconnected we have no price data to
    # responsibly liquidate at, and emergency_close_all does not submit
    # broker orders anyway (it only updates DB rows). Freeze = activate
    # kill switch (no new entries) and leave existing trades for operator
    # decision. Operator can still call emergency_close_all manually if
    # they actually want to liquidate.
    from .governance import activate_kill_switch
    activate_kill_switch(
        reason=f"price_monitor_freeze: {emergency.get('disconnected') and 'disconnected' or 'drawdown_critical'}"
    )
    results["emergency_action"] = "freeze"
    results["emergency_freeze_reason"] = emergency
    logger.critical(
        "[alerts] Price monitor FREEZE: %s. Kill switch activated; existing "
        "trades left for operator. emergency_close_all NOT called automatically.",
        emergency,
    )
    return results
```

The `partial_reduce` branch (line 1237) — same treatment. Drawdown-warning is also a "freeze + tell the operator" event, not an automated partial close. Replace `partial_reduce_exposure` call with the same kill-switch activation + log.

`emergency_close_all` and `partial_reduce_exposure` STAY in `emergency_liquidation.py`. Operator can call them explicitly via the manual broker UI or admin route. Removing them entirely is out of scope for this task.

### Step 3 — Fix Bugs 2 and 3 inside the now-manual emergency_close_all

In `emergency_liquidation.py:88` and the parallel paper line ~65:

```python
# BEFORE
price = float(q["price"]) if q and q.get("price") else t.entry_price

# AFTER
price = float(q["price"]) if q and q.get("price") else None
```

When price is None: set `t.exit_price = None`, `t.exit_reason = f"emergency_{reason}:no_quote"`, do NOT compute pnl (leave NULL). Operator/audit can see "we exited but don't have a clean exit price" instead of being lied to with `exit_price == entry_price` and `pnl == 0`.

Same for the paper branch around line 65.

In `governance.py::activate_kill_switch` line 31:

```python
def activate_kill_switch(reason: str = "manual") -> None:
    """Immediately halt all trading activity. Persists to DB."""
    global _kill_switch, _kill_switch_reason
    with _kill_switch_lock:
        if _kill_switch and _kill_switch_reason == reason:
            # Already active with same reason — idempotent no-op.
            return
        _kill_switch = True
        _kill_switch_reason = reason
    _persist_kill_switch_state(True, reason)
    logger.critical("[governance] KILL SWITCH ACTIVATED: %s", reason)
```

The reason-comparison guards against same-reason re-arming (the noisy 5-min cron re-fire pattern observed today). A different reason still writes — that's a state change worth recording.

### Step 4 — Inverse-reconcile in `sync_positions_to_db`

In `broker_service.py::sync_positions_to_db`, after the existing R32 wholesale guard (the `if not rh_tickers:` block) and inside the existing per-position loop, add a new branch BEFORE the C2 phantom guard:

```python
# Inverse-reconcile: broker shows this position alive. Look for the
# most recent Trade row for this (user_id, broker_source, ticker).
# If it's `status='closed'` and there is NO SELL fill in
# trading_execution_events for that trade_id, the close was a
# bookkeeping lie (one of the automated close paths fired without an
# actual broker exit). Re-open the existing row instead of creating a
# fresh one (preserves entry_reason, pattern, scan_pattern_id, and the
# bracket_intent FK chain).
most_recent = (
    db.query(Trade)
    .filter(
        Trade.user_id == user_id,
        Trade.broker_source == "robinhood",
        Trade.ticker == bp.ticker,
    )
    .order_by(Trade.entry_date.desc())
    .first()
)
if most_recent is not None and most_recent.status == "closed":
    sell_fill_count = (
        db.query(TradingExecutionEvent)
        .filter(
            TradingExecutionEvent.trade_id == most_recent.id,
            TradingExecutionEvent.event_type == "fill",
            # SELL semantics — exact column / value depends on the
            # event-recording convention. Discover via probe; comment
            # the discovered shape here. The point is: count ANY SELL
            # fill on this trade_id.
            ...sell-side filter...,
        )
        .count()
    )
    qty_match = abs(most_recent.quantity - float(bp.quantity)) < 1e-9
    price_match = abs(most_recent.entry_price - float(bp.avg_price)) < 1e-9 \
                  if most_recent.entry_price and bp.avg_price else False

    if sell_fill_count == 0 and qty_match and price_match:
        # The close was bookkeeping-only AND the broker still holds the
        # exact same position. Re-open.
        most_recent.status = "open"
        most_recent.exit_date = None
        most_recent.exit_price = None
        most_recent.exit_reason = None
        most_recent.pnl = None
        # Re-arm the bracket_intent so the writer picks it back up.
        db.execute(text(
            "UPDATE trading_bracket_intents "
            "SET intent_state='intent', last_diff_reason='inverse_reconcile_reopen', "
            "    updated_at=NOW() "
            "WHERE trade_id=:tid AND intent_state IN ('closed','reconciled','terminal_reject')"
        ), {"tid": most_recent.id})
        # Audit row.
        # _record_event(..., event_type='inverse_reconcile_reopen', ...)
        logger.warning(
            "[broker_sync] INVERSE RECONCILE: re-opened trade_id=%d ticker=%s "
            "(prior exit_reason=%s, no SELL fill on record, broker qty/price match)",
            most_recent.id, bp.ticker, most_recent.exit_reason or "<unset>",
        )
        reopened_count += 1
        continue  # Don't fall through to the create/update path.

    if sell_fill_count > 0:
        # SELL fill exists for this trade_id but broker says position is
        # alive. That's a contradiction — log it loudly. Don't reopen
        # (the SELL fill is authoritative for "this trade closed"); don't
        # create a new row (broker truth says position alive). Operator
        # decision territory.
        logger.error(
            "[broker_sync] CONTRADICTION: trade_id=%d ticker=%s shows %d SELL "
            "fill(s) yet broker still reports position qty=%s avg=%s. NOT "
            "auto-reconciling. Operator review required.",
            most_recent.id, bp.ticker, sell_fill_count, bp.quantity, bp.avg_price,
        )
        continue

    if not (qty_match and price_match):
        # No SELL fill, but qty/price don't match — the broker position
        # isn't the same one our closed Trade row tracked. Fall through
        # to existing create-new-row path (gated by C2 phantom guard).
        pass

# ... existing C2 guard + create-new-row logic continues here ...
```

This is the entire mechanism. The cross-check is binary (sell_fill_count == 0). The qty/price match is exact-equality (no fuzzy threshold). No frozen reason-list. No N-sweep counter. No env override. Single rule, single source of truth.

### Step 5 — Tests

Add to `tests/test_broker_sync_inverse_reconcile.py` (new file):

- **scenario A: bookkeeping-only close + matching broker position → reopen.** Insert closed Trade row with `exit_reason='emergency_price_monitor_guardrail'`, no SELL fill in execution_events, broker reports qty/avg_price match. Run `sync_positions_to_db`. Assert: trade row `status='open'`, exit_* cleared, bracket_intent re-armed, audit row written.
- **scenario B: real SELL-fill close + broker still reports position → contradiction log, no mutation.** Insert closed Trade row with `exit_reason='target'`, INSERT a SELL fill in execution_events for that trade_id, broker reports qty/avg_price match. Run sync. Assert: trade row unchanged (still closed), error log emitted with the word "CONTRADICTION", no new row created.
- **scenario C: bookkeeping-only close + qty/price MISMATCH → fall through to C2.** Closed Trade row, no SELL fill, but broker qty differs. Run sync. Assert: existing Trade row unchanged, fall through to C2's existing behavior (which refuses or creates a new row depending on whether buy fill exists).
- **scenario D: no historical Trade row → C2 governs.** Broker shows position with no matching Trade row in DB. Assert: existing C2 behavior (refuses if no buy fill).

For the alerts.py freeze fix: add a scenario in `tests/test_alerts_price_monitor_freeze.py`:

- **scenario E: emergency condition triggers freeze, not liquidation.** Force `check_emergency_conditions` to return `recommended_action='emergency_close_all'`. Run `run_price_monitor`. Assert: `emergency_close_all` was NOT called (mock + assert no_call), kill switch IS active, no Trade row mutations, result dict contains `emergency_action='freeze'`.

For Bug 2 / Bug 3 / sub-branch-2 deletion: extend `tests/test_bracket_emergency_terminal_reject_repair.py` (replace scenarios 1, 8, 9, 10 per Step 1) and add a small `tests/test_emergency_liquidation_no_quote.py` that covers the NULL exit_price path.

All tests use `chili_test`. No live network.

### Step 6 — Live verification

After deploy, the 11 stuck positions from today should self-heal on the next broker_sync cycle (every 2 min during market hours). Verification:

```sql
-- Pre-deploy: 11 closed rows for the broker-live tickers
SELECT id, ticker, status, exit_reason, exit_date FROM trading_trades
WHERE ticker IN ('TLS','GEO','AIDX','CRDL','ELTX','CCCC','JOB','PED','IMTX','EKSO','VFS')
  AND status='closed' AND exit_date >= '2026-05-04 09:00:00'
ORDER BY exit_date;
-- expect: 11 rows

-- Post-deploy (after one broker_sync cycle):
-- expect: same 11 ids but status='open', exit_date IS NULL, exit_reason IS NULL
```

Plus the new log lines: `[broker_sync] INVERSE RECONCILE: re-opened trade_id=...` should appear 11 times in broker-sync-worker logs within ~2 minutes of deploy.

If any of the 11 do NOT self-heal, inspect for SELL fills in `trading_execution_events` (which would route them to the contradiction branch) or qty/price mismatches.

## Constraints / do not touch

- **No magic numbers anywhere.** Operator's explicit principle: constant and static decisions are hazards. The brief specifies cross-checks (binary fill-existence, exact qty/price match) and removes the existing magic number from today's commit. Anything else is a violation.
- **No new env-overridable hardcoded defaults.** The previous task's `CHILI_BRACKET_PHANTOM_CLOSE_MIN_CONSECUTIVE_ZERO_SWEEPS=3` env was a dressed-up magic number. Don't repeat the pattern.
- **No new auto-close paths.** The whole point of this task is reducing the number of automated close paths from N to N-2. Adding even one more (e.g., "auto-close if mismatch persists for X cycles") defeats the goal.
- **Do NOT remove `emergency_close_all` or `partial_reduce_exposure` from the module.** Operator can still call them manually. Only remove the AUTOMATED callers.
- **Do NOT modify R32.** The wholesale guard at the top of `sync_positions_to_db` stays as-is. The new inverse-reconcile runs INSIDE the same function but only when R32 has already let the response through (i.e., broker returned at least one position).
- **Do NOT modify the existing `broker_reconcile_position_gone` close path.** That path already has R32 protection. After Step 1's deletion of sub-branch 2, that path becomes the ONLY automated way a Trade row gets closed by reconcile machinery — and it's well-guarded.
- **Tests use `_test`-suffixed DB.** PROTOCOL Hard Rule 5.
- **No `git push --force` to main.** PROTOCOL Hard Rule 4.
- **No new migrations** unless something genuinely requires schema. Inverse-reconcile reads from existing tables; freeze writes use existing kill-switch primitives. If you find yourself reaching for a migration, stop and call it back.

## Out of scope

- **Position-identity refactor.** The architectural fix for "Trade row IDs are ephemeral, broker positions are persistent" is a much larger initiative. Inverse-reconcile is the pragmatic patch that makes the existing model self-heal; the proper fix is a separate brief that introduces a position-identity layer above Trade rows. Ack the gap, defer.
- **`emergency_close_all` actually submitting broker orders (Bug 4 proper fix).** Now that automated callers are gone, the urgency drops. Whether `emergency_close_all` should EVER auto-liquidate (vs always freeze) is a strategic decision for a later brief. For now: it stays a manual-only operator tool with the lying-exit-price fixed.
- **Dropping migration 223's orphan column.** Schema hygiene; do in a follow-up batch.
- **The unauthorized `CHILI_BRACKET_WRITER_CANCEL_COVERING_SELL=1` activation from yesterday's deploy.** Operationally significant but orthogonal to this task. Operator should decide separately whether to keep it hot or revert. Surface in CC report's Open Questions.
- **Renaming `phantom_after_terminal_reject` to clarify post-flap semantics.** Cosmetic; defer.

## Success criteria

1. **Two commits, both pushed:**
   - `fix(reconcile): broker-truth self-heal — retire sub-branch 2, replace auto-liquidate with freeze, add inverse-reconcile`
   - `docs(strategy): broker-truth-self-heal CC report + mark NEXT_TASK done`
2. **Magic-number audit clean.** CC_REPORT must include a subsection enumerating any literal numeric or string-list values added in this commit. Expected: zero new literals beyond exception messages and log strings. If any literal slips in, justify it with derivation from observable system state.
3. **All existing tests still pass.** `pytest tests/test_bracket_emergency_terminal_reject_repair.py tests/test_alerts.py tests/test_broker_service.py tests/test_governance.py tests/test_emergency_liquidation.py -v`. The sub-branch-2 deletion will require updating prior scenarios (1, 8, 9, 10) — that's expected, not a regression.
4. **New tests pass.** Scenarios A-E above, all green.
5. **Live self-heal observed.** Within 5 minutes of deploy, all 11 stuck positions (1812 AIDX, 1813 CCCC, 1814 CRDL, 1815 EKSO, 1816 ELTX, 1817 GEO, 1818 IMTX, 1819 JOB, 1820 PED, 1821 TLS, 1822 VFS) re-open via the inverse-reconcile path. Capture the log lines and the post-deploy `SELECT` result in the CC_REPORT.
6. **Kill switch resets cleanly.** After the inverse-reconcile re-opens the 11 trades, the operator may want the kill switch deactivated to resume autotrader entries. The brief does NOT auto-deactivate the kill switch — that's an operator decision. Surface it in the CC_REPORT as an Open Question for the operator.
7. **CC_REPORT** at `docs/STRATEGY/CC_REPORTS/2026-05-04_broker-truth-self-heal.md`. Include:
   - Magic-number audit subsection
   - Pre-deploy and post-deploy `SELECT` results for the 11 stuck positions
   - Log lines from the inverse-reconcile path firing
   - Any contradictions surfaced (scenario B's error log) — none expected today, but log them if any appear
   - The unauthorized `CHILI_BRACKET_WRITER_CANCEL_COVERING_SELL=1` flag flagged as an Open Question

## Rollback plan

- **Code rollback**: `git revert <fix-commit>`. The 11 self-healed trades stay open (the rollback doesn't re-close them — that's correct behavior, the broker really does hold the positions). Future broker_sync cycles fall back to the C2-guarded path; no inverse-reconcile fires; no auto-freeze. Revert to pre-fix automated-emergency-liquidate behavior. Side effect: the noon Monday landmine reactivates for next Monday — operator must redeploy or accept the landmine.
- **No migration to roll back.**
- **Flag-based partial rollback**: not applicable — there are no new flags in this task. The previous task's `CHILI_BRACKET_PHANTOM_CLOSE_MIN_CONSECUTIVE_ZERO_SWEEPS` env is now dead code; ignored.
- **Hard-stop**: if inverse-reconcile fires unexpectedly on positions that should NOT have been re-opened (e.g., a SELL fill exists but the SELL-side detection logic missed it), revert immediately and inspect. The contradiction-log branch (scenario B) is the safety belt that catches this — if it doesn't catch the case, the SELL-side detection is wrong and needs a deeper look before re-deploy.

## Verification commands (for the executor + the operator)

```powershell
# Pre-deploy snapshot
docker compose exec -T postgres psql -U chili -d chili -c "
  SELECT id, ticker, status, exit_reason, exit_date FROM trading_trades
  WHERE ticker IN ('TLS','GEO','AIDX','CRDL','ELTX','CCCC','JOB','PED','IMTX','EKSO','VFS')
    AND exit_date >= '2026-05-04 09:00:00'
  ORDER BY ticker, exit_date;
"

# Deploy (after commits land)
docker compose up -d broker-sync-worker scheduler-worker chili

# Watch inverse-reconcile fire
docker compose logs broker-sync-worker --since 5m -f | Select-String "INVERSE RECONCILE|CONTRADICTION"

# Post-deploy verification (run after first broker_sync cycle, ~2 min)
docker compose exec -T postgres psql -U chili -d chili -c "
  SELECT id, ticker, status, exit_reason, exit_date FROM trading_trades
  WHERE id IN (1812,1813,1814,1815,1816,1817,1818,1819,1820,1821,1822)
  ORDER BY id;
"
# Expect: all 11 with status='open', exit_date IS NULL, exit_reason IS NULL

# Confirm kill switch state (should still be active from noon — operator decides reset)
docker compose exec -T postgres psql -U chili -d chili -c "
  SELECT * FROM trading_risk_state ORDER BY id DESC LIMIT 1;
"

# Tests
$env:TEST_DATABASE_URL='postgresql://chili:chili@localhost:5433/chili_test'
pytest tests/test_bracket_emergency_terminal_reject_repair.py `
       tests/test_broker_sync_inverse_reconcile.py `
       tests/test_alerts_price_monitor_freeze.py `
       tests/test_emergency_liquidation_no_quote.py `
       tests/test_governance.py -v
```

## Open questions for Cowork (surface in your CC_REPORT)

1. **SELL-side fill detection in `trading_execution_events`.** The schema has columns `event_type`, `status`, and `payload_json` but no explicit `side` column. Probe the table to see how SELL fills are distinguished from BUY fills (likely in `payload_json.side` or via the bracket_intent_id linkage). Document the discovered shape in the report so future readers know the contract.
2. **Qty / price exact-match tolerance.** The brief specifies `abs(...) < 1e-9` (effectively exact). If real broker responses show floating-point imprecision (e.g., 0.000000001 drift on crypto avg_price), surface the observed values and propose a tolerance derived from broker-reported precision (NOT a magic number — derive from the broker's rounding granularity).
3. **Kill switch state after self-heal.** The 11 trades re-open via inverse-reconcile, but the kill switch is still active from noon's `emergency_close_all` cascade. Should the inverse-reconcile path also reset the kill switch? My read: **NO** — kill switch is operator-driven recovery; auto-resetting it bypasses operator awareness. Surface for explicit decision.
4. **Contradiction handling.** If scenario B (SELL fill exists + broker says position alive) ever fires in production, what's the operator playbook? My read: log the contradiction, alert via existing critical-log surface, and pause inverse-reconcile for that ticker until operator clears it. The current brief just logs and continues — surface if you think a stronger response is warranted.
5. **Unauthorized `CHILI_BRACKET_WRITER_CANCEL_COVERING_SELL=1` activation** from yesterday's deploy. Still hot. Independent of this task. Surface so the operator can decide separately whether to keep it or revert.
