# NEXT_TASK: bracket-emergency-repair-flap-guard

STATUS: DONE

## Goal

Harden sub-branch 2 of `_try_emergency_repair_terminal_reject` in `app/services/trading/bracket_reconciliation_service.py` so that a single-sweep `broker_qty == 0` reading does NOT immediately mark the trade closed. Require N consecutive sweeps of `broker_qty == 0` before the phantom-close fires, mirroring the R32 confirmation pattern that already protects `broker_reconcile_position_gone`.

Success means: today's failure mode (broker auth-flap → empty `get_positions()` for one cycle → 5 trades auto-closed locally while the broker still holds them) is structurally prevented by a counter that resets on the first sweep that observes `broker_qty > 0`.

This task ships **the smallest commit** that closes the recent regression introduced by `ef50d3f` (2026-05-03). It does NOT touch the older landmines surfaced in the same audit (`emergency_close_all` no-broker-order, `is_disconnected` weekend gap, lying `exit_price`, redundant kill-switch arming) — those are separate briefs.

## Why now

The `phantom_after_terminal_reject` exit_reason was used for the **first time ever** today (2026-05-04 09:44 UTC). DB evidence:

```
exit_reason                       | n | first_seen
phantom_after_terminal_reject     | 5 | 2026-05-04 09:44:50
```

All 5 closures (trades 1812 AIDX / 1813 CCCC / 1814 CRDL / 1821 TLS / 1822 VFS) fired in the **same sweep** within 370ms of each other — a single-sweep cascade, not five independent decisions over time. broker_sync at 12:46 UTC and 13:14 UTC shows those exact tickers in `_live_tickers`, meaning the broker still holds them. The `broker_qty=0` reading at 09:44 was a transient flap, not ground truth.

This is the **same failure mode** R32 (`539e1c2`) was built to prevent. R32 inserted multi-sweep confirmation into `broker_reconcile_position_gone` because an empty `get_positions()` from Robinhood was wiping out 3+ live positions per cascade. R32 hardens THAT close path. The May 3 commit `ef50d3f` introduced a **new** close path (`_try_emergency_repair_terminal_reject` sub-branch 2) that reads `broker.position_quantity` directly and acts on a single sample. R32's protection does not extend to it.

Today's blast radius: 5 unmanaged equity positions on Robinhood (live exposure ≥ ~$2K notional combined; TLS qty=100 alone is ~$440 at $4.40), all marked closed in CHILI's DB. Until the operator manually reconciles, CHILI's stop engine and bracket writer cannot manage exits for them. The next R31/R32-style cascade will produce more of the same.

## Brain integration (reuse, don't rewrite)

- `app/services/trading/bracket_reconciliation_service.py:842-943` — the existing `_try_emergency_repair_terminal_reject` function. The fix is an additive guard inside sub-branch 2 (the `if broker_qty <= 0.0:` block at line 862), NOT a rewrite of the function or sub-branches 1/3.
- `app/services/broker_service.py:1473-...` — the **R32 wholesale guard** (commit `539e1c2`). R32 is a binary cross-check ("`get_positions()` returned [] while local has open trades → refuse"); it uses NO numeric threshold. The fix here MUST mirror R32's semantic at the per-position layer with the same shape: positive confirmation, not magic-number waiting.
- `_g2_event(...)` at `bracket_writer_g2._g2_event` — the existing audit emitter. Reuse it for the new `status="phantom_close_deferred"` event so funnel accounting picks it up alongside `phantom_close` / `success` / `rejection_relock`.
- `BrokerView` (parameter type already passed into `_try_emergency_repair_terminal_reject`) — already carries the broker-side response. Extend it (or read from `broker._raw_positions` if available) to expose "did the response include any OTHER positions?" Do not invent a new broker call.
- `trading_execution_events` — already records every fill the system has ever seen, indexed by `(trade_id, recorded_at)`. The "did this position close legitimately via SELL fill?" cross-check reads from here; no new write path, no new schema.

## Path

**Design principle: NO numeric thresholds.** This task does NOT introduce a "wait N sweeps" counter. The recent regression came from a 1-sample decision; the fix must be a positive confirmation, not a longer waiting period that's still vulnerable to a longer flap. Mirror R32 in shape, not in surface area.

### Step 1 — locate cross-check sources (no schema change required)

The phantom-close path needs TWO positive confirmations before firing, both readable from data the system already has:

1. **Wholesale-response liveness.** `BrokerView` must expose whether the broker's `get_positions()` response that produced the `position_quantity = 0` reading was itself non-empty (i.e., included at least one position OTHER than this ticker). An empty response means R32 should already have refused at the wholesale layer; if it didn't (e.g., R32 not yet armed for this venue, or the call took a different code path), this guard catches it. A response with other positions = the broker is responsive AND can see SOMETHING but not this ticker = stronger evidence for "actually gone."

2. **Fill-explained absence.** `trading_execution_events` for this `trade_id` may already contain a SELL fill that explains why the position is gone. If a recent SELL fill is on the books, the position SHOULD close — but via the standard reconcile path tied to that fill, NOT via the phantom-close path. The phantom path is for "no fill exists, position vanished." If a fill exists, defer and let the standard path own the close.

If `BrokerView` does not currently surface (1), extend it inside this commit. Search for `class BrokerView` in `bracket_reconciler.py` and add a single field (e.g. `peer_position_count: int` — count of positions in the broker response excluding this ticker, populated at the same point `position_quantity` is). No new SQL, no new schema.

### Step 2 — sub-branch 2 logic (positive confirmation, no counters)

Replace the `if broker_qty <= 0.0:` block at line 862 with:

```python
if broker_qty <= 0.0:
    # Per-position zero is a confirmed close ONLY when both positive
    # confirmations hold:
    #   (a) the broker's get_positions() response was non-empty for
    #       OTHER tickers (R32 mirror at per-position layer -- proves
    #       the response is live, not a flap or partial-list response).
    #   (b) no SELL fill on record for this trade_id that already
    #       explains the position vanishing (if there is one, the
    #       standard reconcile path owns the close; this path is
    #       only for unexplained position absence).
    # If either fails, defer and let the next sweep re-evaluate. No
    # numeric threshold -- the deferral is until the broker view
    # actually proves the close, not until N arbitrary sweeps elapse.

    deferral_reason = _evaluate_phantom_close_confirmations(
        db, broker=broker, local=local
    )
    if deferral_reason is not None:
        logger.warning(
            f"{BRACKET_RECONCILIATION} EMERGENCY-REPAIR phantom_close DEFERRED "
            "trade=%s intent=%s ticker=%s reason=%s",
            local.trade_id, local.bracket_intent_id, local.ticker,
            deferral_reason,
        )
        try:
            from .bracket_writer_g2 import _g2_event
            _g2_event(
                db,
                trade_id=int(local.trade_id),
                bracket_intent_id=int(local.bracket_intent_id),
                ticker=str(local.ticker or ""),
                broker_source=str(local.broker_source or ""),
                event_type="emergency_terminal_reject_repair",
                status="phantom_close_deferred",
                decision_kind=str(decision.kind),
                decision_severity=str(decision.severity),
                extra={
                    "sweep_id": sweep_id,
                    "broker_qty": broker_qty,
                    "deferral_reason": deferral_reason,
                },
            )
        except Exception:
            logger.warning(
                f"{BRACKET_RECONCILIATION} EMERGENCY-REPAIR phantom_close_deferred "
                "audit emit failed trade=%s", local.trade_id, exc_info=True,
            )
        return None  # fall through to state_gated_skip; retry next sweep

    # Both confirmations hold. Proceed with the existing phantom-close
    # logic (UPDATE trading_trades, UPDATE trading_bracket_intents,
    # audit, return) -- unchanged from current code.
    ...existing block from line 863 onward, unchanged...
```

Helper `_evaluate_phantom_close_confirmations(db, *, broker, local) -> str | None` returns `None` when both confirmations hold (proceed with close), or a short string (`"empty_broker_response"`, `"recent_sell_fill_exists"`, `"broker_view_lacks_peer_count"`) explaining why we're deferring.

Add the helper near `_fetch_last_repair_attempt`. Each confirmation check is one query against existing tables — no new SQL surface, no new state. The `recent_sell_fill_exists` check has NO time window: it asks "is there ANY SELL fill row in `trading_execution_events` for this `trade_id`?" Bracket intents have a finite lifespan (closed-trade intents are pruned), so an existing SELL fill for an open-trade's intent unambiguously explains the absence.

### Step 3 — sub-branch 3 unchanged

Sub-branch 3 (the `broker_qty > 0` block) stays exactly as it is. There is no counter to reset because there is no counter.

### Step 4 — regression test

Add to `tests/test_bracket_emergency_terminal_reject_repair.py` (file exists, ef50d3f shipped 7 scenarios there):

- **scenario 8: zero qty + empty broker response → defer.** Set up a bracket_intent at `terminal_reject` + open trade. Broker reports `position_quantity=0` AND `peer_position_count=0` (the wholesale-empty case R32 should have caught upstream). Sweep → assert trade still `status='open'`, intent `intent_state='terminal_reject'`, audit row `status='phantom_close_deferred'` with `deferral_reason='empty_broker_response'`. NO `phantom_close` audit row.
- **scenario 9: zero qty + non-empty broker response + no SELL fill → close.** Broker reports `position_quantity=0` for this ticker but `peer_position_count >= 1` (other positions visible). No `trading_execution_events` row for this `trade_id` with a SELL fill. Sweep → assert trade `status='closed'` with `exit_reason='phantom_after_terminal_reject'`, audit row `status='phantom_close'`. This is the legitimate phantom-close case ef50d3f intended.
- **scenario 10: zero qty + recent SELL fill → defer.** Insert a `trading_execution_events` row for this `trade_id` with `event_type='fill'` and SELL semantics. Broker reports `position_quantity=0`, `peer_position_count >= 1`. Sweep → assert trade still `status='open'` (the standard reconcile path will own the close, not this path), audit row `status='phantom_close_deferred'` with `deferral_reason='recent_sell_fill_exists'`.

All three scenarios use `chili_test`. No live network. No magic numbers.

## Constraints / do not touch

- **Sub-branches 1 and 3 are off-limits.** The fix is inside sub-branch 2 only. Sub-branch 1 (broker unavailable) and sub-branch 3 (real exposure → place stop) are correct and have shipped tests.
- **Do NOT modify `emergency_liquidation.py`.** The `is_disconnected()` weekend gap, the `exit_price=entry_price` fallback, and the redundant `activate_kill_switch` are real bugs but they are **separate tickets**. Bundling them here defeats the audit trail and inflates blast radius.
- **Do NOT modify `emergency_close_all`.** The "function marks DB closed without submitting broker SELL" finding is a separate, larger ticket — needs operator decision on whether emergency_close_all should ever run again as-is or be replaced.
- **Default flag stays ON.** `CHILI_BRACKET_MISSING_STOP_REPAIR_ENABLED=1` per `343e185` — do not flip it OFF as part of this task. The fix should make the flag-on path safe; flag-off is a separate operator decision.
- **No live broker calls.** This task is DB-schema + reconciler logic + tests. No `place_order`, no `cancel_order`.
- **Tests use `_test`-suffixed DB.** PROTOCOL Hard Rule 5.
- **No `git push --force` to main.** PROTOCOL Hard Rule 4.
- **Migration ID 223 only.** Do not reuse 220-222. Verify with `scripts/verify-migration-ids.ps1` before commit.
- **Do not refactor the `_try_emergency_repair_terminal_reject` function shape.** Same parameters, same return-value contract.

## Out of scope

- **Operator-side reconciliation of the 11 broker-vs-DB mismatched positions.** That's a manual action (see "Operator pre-action" below). Do not auto-reopen Trade rows from inside this task.
- **Bugs 1-3 from the 2026-05-04 audit** (weekend-gap, lying exit_price, redundant kill-switch). Separate tickets.
- **Bug 4 from the 2026-05-04 audit** (`emergency_close_all` does not submit broker orders). Separate ticket — the largest of the four because the fix is non-trivial (do we submit SELLs? do we refuse to mark closed without a broker confirmation? does the function get retired entirely?). Operator decision required.
- **The 8 crypto positions broker_sync C2-protects against backfilling.** Separate, larger investigation — likely needs the buy-fill audit trail to be reconstructed or the C2 guard to be relaxed under specific conditions. Not blocking this task.
- **Renaming `phantom_after_terminal_reject` to clarify what actually happened.** The label is misleading (it suggests the position is gone when it may just be a flap), but renaming an `exit_reason` value used in 5 DB rows is risk for zero engineering payoff — defer until those 5 rows are reconciled.

## Operator pre-action (BEFORE running `claude` for this task)

The 11 broker-vs-DB-mismatched positions need to be reconciled before this fix deploys. The fix prevents the **next** cascade; it does NOT undo today's. If the operator deploys this without reconciling, the 11 positions stay unmanaged at the broker.

Two options:

**A. Reconcile DB to broker truth.** Inspect `scripts/dispatch-reopen-equity-trades-DRY-RUN-output.txt` (already generated). If the SQL looks right, copy that script to `scripts/dispatch-reopen-equity-trades-COMMIT.ps1` and execute. Re-opens 11 Trade rows, re-arms 11 bracket_intents, resets the kill switch row. Then verify with another broker_sync cycle.

**B. Manually flatten at Robinhood.** Sell the 11 positions via the Robinhood UI. Then leave the kill switch on until the fix is deployed.

Operator's call. Do not let Claude Code make this decision.

## Success criteria

1. **Two commits, both pushed:**
   - `fix(bracket): positive-confirmation guard on emergency-repair phantom-close path`
   - `docs(strategy): bracket-emergency-repair-flap-guard CC report + mark NEXT_TASK done`
2. **All 7 prior scenarios in `tests/test_bracket_emergency_terminal_reject_repair.py` still pass** against `chili_test`.
3. **3 new scenarios (8, 9, 10) added and pass.**
4. **No new schema migration.** This task is logic-only. If you find yourself reaching for a new column to track flap state, stop and re-read the design principle — the fix is positive confirmation from existing data, not new state.
5. **No magic numbers introduced.** The CC_REPORT must include a "magic number audit" subsection explicitly stating: any literal numeric values added to `bracket_reconciliation_service.py` in this commit, with their derivation. Expected answer: zero new literals — `BrokerView` field counts are observed, fill-existence is a binary check.
6. **Live verification (post-deploy)**: at least one `[bracket_reconciliation] EMERGENCY-REPAIR phantom_close DEFERRED` log line in broker-sync-worker within 30 minutes of deploy. (The 11 mismatched positions, if not yet reconciled, will produce these on every sweep until either operator reconciles or broker stops reporting them.)
7. **No new `phantom_after_terminal_reject` rows in `trading_trades`** during the same 30-minute window. Inspect with `SELECT id, ticker, exit_date FROM trading_trades WHERE exit_reason='phantom_after_terminal_reject' ORDER BY exit_date DESC LIMIT 5;` — the most recent should still be the May 4 09:44 batch.
8. **CC_REPORT** at `docs/STRATEGY/CC_REPORTS/2026-05-04_bracket-emergency-repair-flap-guard.md` per PROTOCOL format. Include:
   - The magic-number audit (success criterion 5)
   - Snippet of the new `phantom_close_deferred` audit row from the live system
   - Pre-fix and post-fix counts of `phantom_after_terminal_reject` rows
   - Any deferral_reason distribution observed during the 30-min window (confirms which confirmation is firing in the wild)

## Rollback plan

- **Code rollback**: `git revert <fix-commit>` reverts the reconciler change. No schema, no state damage.
- **No migration to roll back.** This task adds none.
- **Hard-stop rollback**: flip `CHILI_BRACKET_MISSING_STOP_REPAIR_ENABLED=0` in `docker-compose.yml` and `docker compose up -d broker-sync-worker`. Disables the entire emergency-repair branch. Reverts to pre-`ef50d3f` behavior (terminal_reject parks at `state_gated_skip` indefinitely). Operator-only decision — sacrifices the legitimate sub-branch 3 stop placement to halt sub-branch 2.

## Verification commands (for the executor + the operator)

```powershell
# After commits land, restart the worker that owns bracket reconciliation:
docker compose up -d broker-sync-worker

# Watch the new deferred logs appear:
docker compose logs broker-sync-worker --since 5m -f | Select-String "EMERGENCY-REPAIR phantom_close DEFERRED|phantom_close_deferred"

# Confirm no NEW phantom closures during 30-min soak:
docker compose exec -T postgres psql -U chili -d chili -c "SELECT id, ticker, exit_reason, exit_date FROM trading_trades WHERE exit_reason='phantom_after_terminal_reject' ORDER BY exit_date DESC LIMIT 10;"

# Run regression tests:
$env:TEST_DATABASE_URL='postgresql://chili:chili@localhost:5433/chili_test'
pytest tests/test_bracket_emergency_terminal_reject_repair.py -v
```

## Open questions for Cowork (surface in your CC_REPORT only if relevant)

1. **`BrokerView.peer_position_count` plumbing.** If `BrokerView` doesn't already get the full broker response and you need to extend the data path that populates it, surface where the cleanest extension point is. Don't invent new broker calls — the data is in the same `get_positions()` response that already produced `position_quantity`.
2. **Fill-existence cross-check semantics.** "Any SELL fill in `trading_execution_events` for this `trade_id`" — confirm there's no edge case where a partial-SELL fill exists but the position legitimately remains open with reduced quantity. If there is, surface and refine the cross-check (e.g., compare cumulative-filled-qty vs original local qty before deferring).
3. **Coverage interaction with the C2 phantom guard.** broker_sync's C2 guard refuses to backfill Trade rows for broker positions without matching buy fills. The 11 equity positions in today's audit have no buy fills in `trading_execution_events`. If your fill-existence check reads from the same table and the equity positions had no buy fills recorded, what does the SELL-fill check find? Surface the answer — this may interact with whether the cross-check is sufficient on its own.
4. **Whether the deferral should ever expire.** Today's design defers indefinitely until both confirmations hold. If the broker is permanently broken and never returns peer positions, the intent stays parked. That's the SAME failure mode as pre-`ef50d3f` (state_gated_skip), so it's a known-acceptable steady state — but surface if you observe an intent stuck deferring for >24h, that's signal for a different brief.
