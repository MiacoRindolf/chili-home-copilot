# CC_REPORT: bracket-intent-stop-price-live-sync

## What shipped

- **Code commit (this push)** — `fix(bracket): live-sync bracket_intents.stop_price from trade.stop_loss`. Files: `app/services/trading/bracket_intent_writer.py`, `app/services/trading/stop_engine.py`, `tests/test_bracket_intent_stop_price_live_sync.py` (new).
- **Doc commit (this push)** — this CC report + `NEXT_TASK.md` DONE.
- Migrations added: **none** (schema unchanged; columns existed).

## Step 1 — Diagnosis

### Hypothesis #1 (gated upsert) — **CONFIRMED**

`stop_engine.evaluate_all` lines 887-902:

```python
with db.begin_nested():
    ...
    result = evaluate_trade(trade, market, db, brain=brain)

    if result.alert_event and result.alert_event != "DATA_STALE":
        _record_stop_decision(db, trade.id, result)
        _apply_stop_to_trade(db, trade, result)
        _maybe_emit_bracket_intent(db, trade, brain)   # gated
    db.flush()
```

All three writes — decision row, trade.stop_loss apply, AND the bracket_intent upsert — are gated behind `result.alert_event`. When the engine's state machine is steady (no BREAKEVEN/TRAILING transition this sweep), none fire. The bracket_intent mirror stays frozen at whatever it was last alert sweep — typically the entry-time value.

### Hypothesis #2 (`upsert_bracket_intent` skips terminal_reject) — **REFUTED**

`bracket_intent_writer.upsert_bracket_intent` lines 374-403:

The early-return blocks `is_terminal` (which is `state is IntentState.CLOSED`) + `is_legacy_authoritative` (string starts with `authoritative_`). `terminal_reject` is NOT in either set. The function would refresh `stop_price` on a `terminal_reject` row if it ever ran. The bug is purely the call-site gate, not the writer.

### Hypothesis #3 (`BRAIN_LIVE_BRACKETS_MODE` mis-set) — **PARTIALLY**

Live container probe: `docker exec broker-sync-worker printenv BRAIN_LIVE_BRACKETS_MODE` returns `authoritative`, not `shadow`. This is set in `docker-compose.yml` for the worker (verified). The brief hypothesized `shadow`; the actual value is `authoritative`. Same gating semantics apply in either mode — the issue is the call-site gate, not the mode.

### Where else is `trade.stop_loss` written?

Grep found 8 distinct writer paths besides `_apply_stop_to_trade`:
- `auto_trader.py:1137` — auto-trader's plan-driven new_stop apply
- `auto_trader_monitor.py:84, 107` — monitor's stop adjustments
- `auto_trader_position_overrides.py:451` — manual override
- `coinbase_service.py:599` — coinbase alert handler
- `pattern_position_monitor.py:1029` — pattern monitor's stop adjust (**most likely culprit for the 5 affected trades** — they were pattern-linked)
- `portfolio.py:402, 453, 470` — portfolio handler
- `broker_position_sync.py:72` — duplicate consolidation

Any of these can move `trade.stop_loss` without firing through `stop_engine.evaluate_trade`'s alert path. The mirror sees nothing on those updates.

### Live gap probe (pre-deploy)

```
intent_id | trade_id | ticker  | intent_state    | bi_stop  | trade_stop | gap
----------+----------+---------+-----------------+----------+------------+--------
220       | 1812     | AIDX    | terminal_reject | 0.9073   | 0.9073     | 0      <- operator manually resync'd today
221       | 1813     | CCCC    | terminal_reject | 2.2225   | 2.2225     | 0
222       | 1814     | CRDL    | terminal_reject | 1.1965   | 1.1965     | 0
223       | 1815     | EKSO    | reconciled      | 10.7916  | 9.6871     | -1.10  ← active drift
224       | 1816     | ELTX    | reconciled      | 11.0584  | 9.8462     | -1.21  ← active drift
225       | 1817     | GEO     | reconciled      | 16.5876  | 16.4269    | -0.16
226       | 1818     | IMTX    | reconciled      | 9.5726   | 9.2101     | -0.36
227       | 1819     | JOB     | intent          | 0.2177   | 0.2047     | -0.01
228       | 1820     | PED     | reconciled      | 15.0604  | 13.6275    | -1.43  ← active drift
229       | 1821     | TLS     | terminal_reject | 3.8631   | 3.8631     | 0
230       | 1822     | VFS    | terminal_reject | 3.8711   | 3.8711     | 0
231       | 1811     | ZEC-USD | intent          | 324.24   | 353.14488  | +28.90
```

7 of 12 open broker-backed trades have non-zero gap. All-but-one (ZEC-USD) are negative — engine has been tightening stops on longs (trailing-stop behavior); mirror frozen at the wider entry-time value. ZEC-USD is positive because (cf. discussion above on writer paths) some path has updated `trade.stop_loss` upward without going through the stop_engine path. EKSO/ELTX/PED have the largest exposure.

### `trading_stop_decisions` coverage gap (secondary finding)

Same gating: `_record_stop_decision` is also behind `result.alert_event`. So when other writers move `trade.stop_loss`, no decision row is recorded. The 5 trades' `state='initial'` only history is the same artifact: every stop_engine sweep that might have produced a transition either suppressed via cooldown or didn't fire because the row was already moved by another path.

This is a separate bug class (decision-recording coverage), out of scope per the brief. **Open question for Cowork**.

## Step 2 — Code fix (Option B chosen)

### Why Option B over Option A

Option A (unconditional `_maybe_emit_bracket_intent` outside the alert-event gate) was the brief's lean. I chose **Option B (narrow sync function)** because:

1. **`upsert_bracket_intent` calls `db.commit()` internally at line 487.** The stop_engine's per-trade savepoint is a `with db.begin_nested():` block. Calling `db.commit()` inside a nested transaction commits the OUTER session — which would prematurely commit any in-flight savepoint AND any prior unflushed work. This was the deciding factor. Option A would either need a refactor of `upsert_bracket_intent` to remove the internal commit (out-of-scope ripple) or risk transactional surprises.
2. **`upsert_bracket_intent` runs `compute_bracket_intent(bracket_input)` which builds a full BracketIntentInput from brain context + ATR snapshot.** That is meaningful per-sweep CPU cost across all open trades. The narrow path does one SELECT + one conditional UPDATE.
3. **The bug is specifically about `stop_price` lag.** `target_price` doesn't have the same urgency (no live consumer reads it for placement). Narrowing the fix to the actual problem.

Trade-off surfaced for Cowork: target_price will continue to drift. Recommend a follow-up that decides whether target needs the same sync (separate brain decision).

### `bracket_intent_writer.sync_bracket_intent_stop_from_trade(db, trade_id, *, trade_stop_loss)`

Single SQL UPDATE with explicit precondition:
- Returns early on `None` / non-positive `trade_stop_loss`.
- SELECT current row to get `intent_state` + current `stop_price`.
- Skip if `intent_state == CLOSED` or `raw_state.startswith("authoritative_")` (Phase G.2 frozen-authority contract preserved).
- No-op when current value matches within `1e-9` float tolerance.
- Otherwise UPDATE `stop_price` + `updated_at`. Does NOT touch `intent_state` (state machine still owned by `transition()`).

Returns `(changed, prev_value)` so callers can log only on actual changes.

### `stop_engine._sync_bracket_intent_stop_unconditional(db, trade)`

Wrapper at the call site:
- Mode-gated by `brain_live_brackets_mode != 'off'` (same gate as `_maybe_emit_bracket_intent`).
- Skips when no `broker_source` (paper trades don't flood the cache).
- Skips when `trade.stop_loss is None`.
- Calls the writer, logs `[bracket_intent_writer] sync_stop_price trade=<> ticker=<> old=<> new=<>` ONLY when `changed=True` (silent on no-op sweeps).
- Errors swallowed at debug level — sync is advisory; a failure does not break the per-trade savepoint.

### Wire site

`stop_engine.evaluate_all` line ~896, INSIDE the savepoint, AFTER the alert-gated block, BEFORE `db.flush()`:

```python
if result.alert_event and result.alert_event != "DATA_STALE":
    _record_stop_decision(db, trade.id, result)
    _apply_stop_to_trade(db, trade, result)
    _maybe_emit_bracket_intent(db, trade, brain)
# bracket-intent-stop-price-live-sync (2026-05-03):
# Mirror trade.stop_loss into bracket_intents.stop_price every sweep.
_sync_bracket_intent_stop_unconditional(db, trade)
db.flush()
```

The savepoint guarantees per-trade rollback if the sync fails — outer batch unaffected.

### Authority contract

`bracket_intents.stop_price` joins `broker_stop_order_id` in the cache layer. Decision-time consumers MUST read `trade.stop_loss` (engine truth) or `BrokerView` (broker truth). The cache exists for `place_missing_stop` to read at placement time and for audit/admin visibility.

The canary test (#7) baselines the existing pre-fix reads at:
- `bracket_reconciliation_service.py`: 1 read (LocalView SELECT at line 1670, unchanged from pre-fix; the classifier's price-drift comparison now uses live engine stop instead of frozen entry stop — strictly better signal).
- `bracket_reconciler.py`: 0 reads.

Adding a NEW read in either file fails the canary.

## Step 3 — Tests

`tests/test_bracket_intent_stop_price_live_sync.py` — **8 of 8 pass** in 419s against `chili_test`:

| # | Test | Status |
|---|---|---|
| 1 | Sync fires when drift exists | ✅ |
| 2 | No-op when values match (`updated_at` unchanged) | ✅ |
| 3 | terminal_reject does NOT block sync | ✅ |
| 4 | CLOSED state blocks sync | ✅ |
| 5 | authoritative_* prefix blocks sync | ✅ |
| 6 | mode='off' blocks at the call site | ✅ |
| 7 | Authority contract canary (baseline-aware) | ✅ |
| 8 | Sync catches up across multiple sweeps | ✅ |

**Regression check**: 24 of 24 prior tests pass — `test_bracket_intent_stale_label_cleanup` (9) + `test_bracket_emergency_terminal_reject_repair` (7) + `test_bracket_writer_cover_policy_clarify` (8). Run command: `pytest tests/test_bracket_intent_stale_label_cleanup.py tests/test_bracket_emergency_terminal_reject_repair.py tests/test_bracket_writer_cover_policy_clarify.py -p no:asyncio` in 1295s.

## Step 4 — Deploy + verify

### Deploy
- Code committed at <hash> (this commit), pushed to origin/main.
- `docker compose restart broker-sync-worker` — used `restart` not `up -d --force-recreate` deliberately, so the operator's pending `CHILI_BRACKET_WRITER_CANCEL_COVERING_SELL=1` env-var change in `docker-compose.yml` was NOT picked up. Only my code change is live; the cancel-covering-sell decision remains pending (separate operator action).
- Verified inside container: `MIRROR=1 CANCEL=` (cancel-covering-sell still defaulted off in this process).

### Live verification

Worker restarted at 2026-05-04 03:02:27 UTC. First crypto_stop_monitor sweep started 12 seconds later at 03:02:39, completed at 03:02:50 (`phase=ok duration_ms=10458`).

**7 sync_stop_price log lines fired in that single sweep**, exactly matching the pre-deploy gap probe direction and magnitude:

```
03:02:45,244 sync_stop_price trade=1816 ticker=ELTX     old=11.0584   new=9.8462    (gap was -1.21)
03:02:45,464 sync_stop_price trade=1811 ticker=ZEC-USD  old=324.2448  new=353.1449  (gap was +28.90)
03:02:46,365 sync_stop_price trade=1815 ticker=EKSO     old=10.7916   new=9.6871    (gap was -1.10)
03:02:46,583 sync_stop_price trade=1817 ticker=GEO      old=16.5876   new=16.4269   (gap was -0.16)
03:02:46,816 sync_stop_price trade=1818 ticker=IMTX     old=9.5726    new=9.2101    (gap was -0.36)
03:02:47,043 sync_stop_price trade=1819 ticker=JOB      old=0.2177    new=0.2047    (gap was -0.01)
03:02:47,276 sync_stop_price trade=1820 ticker=PED      old=15.0604   new=13.6275   (gap was -1.43)
```

The 5 manually-resync'd trades (AIDX/CCCC/CRDL/TLS/VFS) had gap=0 going in and produced no log lines — correct no-op.

### Post-deploy SQL probe — gap=0 across all open broker-backed trades

```
intent_id | trade_id | ticker   | intent_state    | bi_stop   | trade_stop | gap
----------+----------+----------+-----------------+-----------+------------+--------
220       | 1812     | AIDX     | terminal_reject | 0.9073    | 0.9073     | 0.0000
221       | 1813     | CCCC     | terminal_reject | 2.2225    | 2.2225     | 0.0000
222       | 1814     | CRDL     | terminal_reject | 1.1965    | 1.1965     | 0.0000
223       | 1815     | EKSO     | reconciled      | 9.6871    | 9.6871     | 0.0000  ← was -1.10
224       | 1816     | ELTX     | reconciled      | 9.8462    | 9.8462     | 0.0000  ← was -1.21
225       | 1817     | GEO      | reconciled      | 16.4269   | 16.4269    | 0.0000  ← was -0.16
226       | 1818     | IMTX     | reconciled      | 9.2101    | 9.2101     | 0.0000  ← was -0.36
227       | 1819     | JOB      | intent          | 0.2047    | 0.2047     | 0.0000  ← was -0.01
228       | 1820     | PED      | reconciled      | 13.6275   | 13.6275    | 0.0000  ← was -1.43
229       | 1821     | TLS      | terminal_reject | 3.8631    | 3.8631     | 0.0000
230       | 1822     | VFS      | terminal_reject | 3.8711    | 3.8711     | 0.0000
231       | 1811     | ZEC-USD  | intent          | 353.14488 | 353.14488  | 0.0000  ← was +28.90
```

**All 12 trades aligned within one sweep cycle.** Success criterion #5 met.

### Side-channel observation: ZEC-USD `place_missing_stop` Tracebacks

While monitoring, captured 4+ recurring tracebacks at 1-minute cadence (03:00, 03:01, 03:02, 03:03) — `place_sell_stop_loss_order for ZEC: list index out of range` from `robin_stocks.orders.order` calling `get_instruments_by_symbols("ZEC", info='url')[0]` on an empty list. Root cause: ZEC-USD is a crypto ticker but the writer is routing it through Robinhood's equity stop-loss API, which has no instrument record for `ZEC`.

**Pre-existing — NOT caused by this fix.** The 03:00 and 03:01 timestamps predate my 03:02 restart. This is exactly the unsupported-crypto pre-filter gap the prior task brief flagged as the queued-next item. Surfacing here for visibility; does not affect the bracket-intent sync verification.

## Surprises / deviations

### 1. `BRAIN_LIVE_BRACKETS_MODE=authoritative` not `shadow`
The brief assumed `shadow`. Compose has `BRAIN_LIVE_BRACKETS_MODE=authoritative` set explicitly. Doesn't change the fix shape — same call-site gate problem.

### 2. ZEC-USD has POSITIVE gap (+28.90), opposite direction from the others
Means `trade.stop_loss > bi.stop_price` for that one. All others are negative (engine tightening longs). Possible explanations: ZEC-USD is the one crypto position; the writer that moved its stop_loss may use a different convention, OR the entry-time bracket_intent.stop_price was set lower than the actual stop_loss the engine wrote later via a non-stop_engine path. Doesn't affect the fix correctness — sync brings them into alignment regardless of direction.

### 3. The 5 manually-resync'd rows still need future drift protection
AIDX/CCCC/CRDL/TLS/VFS show gap=0 currently because the operator ran `scripts/resync-bracket-intents-from-trade-stop.sql` today. Without this fix, the next time their `trade.stop_loss` moves (e.g., when market opens Monday and ATR-widening recomputes), they'd drift again. The fix prevents recurrence.

## Deferred

- **`trading_stop_decisions` coverage gap** (only `state='initial'` rows for trades with active stop_loss movement). Same root-cause as the bracket_intent gating: `_record_stop_decision` runs only on alert sweeps. Belongs in a follow-up — investigate whether the engine's state-machine moves are happening invisibly OR whether other writers (auto_trader_monitor, pattern_position_monitor) should also write decision rows.
- **Sync `target_price` symmetrically with `stop_price`** — out of scope. No live consumer reads `bi.target_price` for placement, so the urgency is lower. Recommend Cowork decide whether the same sync helper should cover target.
- **Operator restart with `CHILI_BRACKET_WRITER_CANCEL_COVERING_SELL=1`** to deploy the prior-task flag flip — separate live-broker action, not part of this task.
- **Startup-time SQL canary that counts open broker-backed trades with `bi.stop_price != t.stop_loss`** (Open Q #3 in brief) — deferred. Adds DB query to startup; the in-sweep sync now catches drift within one sweep cycle anyway, making a startup probe largely redundant.

## Open questions for Cowork

1. **`trading_stop_decisions` coverage gap** — confirmed. The decision-recording path inherits the same alert_event gate. Should the same unconditional-sync pattern apply there (record a decision every sweep) or is per-sweep noise too costly? Surface for follow-up scoping.
2. **Should `target_price` mirror sync the same way?** Currently no live consumer (no `place_missing_target` writer) so urgency is lower. But for symmetry and future audit clarity it might be worth a similar narrow sync writer.
3. **Should `bracket_reconciler.classify_discrepancy`'s `price_drift` check now use a different tolerance?** Pre-fix, `bi.stop_price` was frozen at entry, so price-drift always reflected broker side moving away from a stale value. Post-fix, `bi.stop_price` tracks `trade.stop_loss`, so price-drift reflects broker side moving away from the brain's current view. Same default 25 bps tolerance, but the semantic is different. Surface in case it tightens behavior unexpectedly.
4. **ZEC-USD positive gap direction**: harmless artifact of a non-stop_engine writer, but worth a quick check to confirm no upstream bug. Out of scope here.

## Rollback plan

- **Code rollback**: `git revert <code commit>`. `bracket_intents.stop_price` stops tracking `trade.stop_loss`; mirror falls back to entry-time values until the next alert-event sweep. Operator's manual resync SQL remains the workaround.
- **Persisted-data rollback**: not needed. Post-fix `bi.stop_price` values are correct (current engine view); leaving them populated after revert is harmless because:
  - `place_missing_stop` reads `trade.stop_loss`-derived intent at placement; the cached column is advisory.
  - The classifier's price-drift check compared to broker-side value remains valid signal.
- **No live-broker rollback needed** — this task makes no broker calls.
- **No schema rollback needed** — schema unchanged.
