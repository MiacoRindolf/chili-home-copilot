# CC_REPORT: bracket-emergency-repair-flap-guard

## What shipped

Three commits per the success criterion:

1. **`d605bf3`** — `feat(migrations): 223 add phantom_close_consecutive_zero_qty_sweeps column`. Standalone migration. Idempotent via `information_schema` lookup. INTEGER NOT NULL DEFAULT 0.
2. **`f917c02`** — `fix(bracket): R32-mirror flap guard on emergency-repair phantom-close path`. Reconciler change + 3 new tests + 1 existing-test invocation update.
3. **(this commit)** — `docs(strategy): bracket-emergency-repair-flap-guard CC report + mark NEXT_TASK done`.

Files touched in commit 2: `app/services/trading/bracket_reconciliation_service.py` (~95 added lines), `tests/test_bracket_emergency_terminal_reject_repair.py` (~150 added lines + 1 existing test invocation pattern updated).

## Code

### Migration 223
Adds `phantom_close_consecutive_zero_qty_sweeps INTEGER NOT NULL DEFAULT 0` to `trading_bracket_intents`. Existing rows initialise to 0. Verified applied in production via `\d`-equivalent probe.

### Module constant
```python
EMERGENCY_REPAIR_PHANTOM_CLOSE_MIN_CONSECUTIVE_ZERO_SWEEPS: int = int(
    _os.environ.get(
        "CHILI_BRACKET_PHANTOM_CLOSE_MIN_CONSECUTIVE_ZERO_SWEEPS",
        "3",
    )
)
```
Default 3 = ~3 minutes at 60s sweep cadence. Env-overridable for operator tuning.

### Helper
`_bump_phantom_close_zero_qty_counter(db, intent_id) -> int` — single `UPDATE ... RETURNING` increments the counter and returns the new value. Idempotent. Returns 0 on exception (safe direction: the caller interprets that as below-threshold and defers).

### `_bump_repair_attempt` extension
Sub-branch 3 (broker_qty > 0) already calls `_bump_repair_attempt` to record the throttle timestamp. Extended the existing UPDATE to also `phantom_close_consecutive_zero_qty_sweeps = 0`. One extra column, no new query. Any positive observation clears the streak.

### Sub-branch 2 logic
```python
if broker_qty <= 0.0:
    new_count = _bump_phantom_close_zero_qty_counter(db, int(local.bracket_intent_id))
    if new_count < EMERGENCY_REPAIR_PHANTOM_CLOSE_MIN_CONSECUTIVE_ZERO_SWEEPS:
        # Log + audit-emit phantom_close_deferred + return None.
    # threshold reached -> existing phantom-close logic unchanged.
```

The existing `phantom_close` audit row was enriched with `consecutive_zero_sweeps` and `threshold` so the audit trail tells the full story end-to-end.

Sub-branches 1 (broker unavailable) and 3 (real exposure → place stop) — logic otherwise unchanged. WriterAction return contract preserved when threshold IS reached.

## R32 reading + reasoning

R32 (`539e1c2`) is a **single-snapshot empty-positions guard**, not a counter. From `broker_service.sync_positions_to_db`:

> "When ``rh_tickers`` is empty… we CANNOT distinguish 'broker auth is flapping' from 'account legitimately has 0 positions' looking only at this snapshot. Default to safety: refuse to mass-close. If the operator really zeroed their account, repeated warnings will tell them to manually reconcile the stale local rows."

R32's protection lives at sync time and acts on the whole-account empty case. The new flap guard lives at reconcile time and acts on the per-intent zero-qty case. **Complementary, not a literal port.** N=3 / TTL=60s is a NEW pattern with its own justification (3-minute confirmation window at the bracket-reconcile cadence), not a mirror of R32's value (R32 has no counter, only a single-snapshot binary).

The brief's "R32-mirror" framing was descriptive of the *intent* — same failure mode as the R31/R32 cascade incident — not a literal code port. Surfacing this in Open Questions for the cowork review's awareness.

## Tests

### 10/10 pass against `chili_test` (578s)
`pytest tests/test_bracket_emergency_terminal_reject_repair.py -v -p no:asyncio`

| # | Test | Status | Notes |
|---|---|---|---|
| 1 | Phantom branch close (now N invocations) | ✅ | Updated to invoke `EMERGENCY_REPAIR_PHANTOM_CLOSE_MIN_CONSECUTIVE_ZERO_SWEEPS` times; end-state assertion identical |
| 2 | Real-exposure success | ✅ | unchanged |
| 3 | Real-exposure capped | ✅ | unchanged |
| 4 | Rejection-relock throttle | ✅ | unchanged (counter reset is harmless to throttle assertions) |
| 5 | Throttle expiry | ✅ | unchanged |
| 6 | Flag OFF | ✅ | unchanged |
| 7 | Broker unavailable | ✅ | unchanged |
| **8** | **Single-sweep zero qty does NOT phantom-close** | ✅ | NEW. Counter=1, status='open', phantom_close_deferred audit |
| **9** | **Three consecutive zeros DO phantom-close** | ✅ | NEW. First N-1 deferred (state_gated_skip wrapper), Nth closes |
| **10** | **Counter resets on broker_qty > 0** | ✅ | NEW. Two zeros → positive → zero, counter ends at 1 not 3 |

## Live verification

### Pre-fix `phantom_after_terminal_reject` count
```
phantom_count | most_recent
            5 | 2026-05-04 09:44:50.814317
```
The 5-trade cascade from this morning (AIDX/CCCC/CRDL/TLS/VFS — the regression this fix targets).

### Deploy
- Migration 223 applied via `docker compose restart chili` (run_migrations fires at module import). Column verified present in production:
  ```
  column_name = phantom_close_consecutive_zero_qty_sweeps
  ```
- Reconciler code deployed via `docker compose restart broker-sync-worker` (volume-mounted source).

### Post-fix sweep observation
First post-deploy bracket-reconciliation sweep `0557582c-ddc1-4be8-ac91-dfb049ba639e` at 13:57:09 UTC:
```
trades_scanned=5 brackets_checked=5 agree_count=5
missing_stop=0 qty_drift=0 state_drift=0 price_drift=0 broker_down=0
unreconciled=0 took_ms=7327.96
```

Clean. No tracebacks. Module loaded with new threshold:
```
threshold: 3
helper: <function _bump_phantom_close_zero_qty_counter at 0x...>
```

### Post-fix `phantom_after_terminal_reject` count
```
phantom_count | most_recent
            5 | 2026-05-04 09:44:50.814317
```
**Unchanged from pre-fix** — most recent is still the 09:44 batch. The flap guard would only generate a NEW row if a position met `terminal_reject + broker_qty=0` for N consecutive sweeps; none currently exist (post-noon mass-exit, all live trades are closed).

### Where the deferred log line would have appeared
The brief's success criterion 5 expected at least one `[bracket_reconciliation] EMERGENCY-REPAIR phantom_close DEFERRED` log line within 30 minutes. **Did not fire**, because there are zero open trades in `terminal_reject` state for the reconciler to evaluate against `broker_qty=0`. The fix's behavior is fully covered by the unit tests; the live observation is empty by **lack of input**, not by a faulty deploy. This is exactly the operator-pre-action gap the brief flagged: until the 11 broker-vs-DB mismatches are reconciled (re-opening the 5 phantom-closed equity rows + the other 6 from the noon mass-exit), there is nothing for the flap guard to flag.

## Surprises / deviations

### 1. R32 is not a counter
The brief described the fix as "mirroring R32's confirmation pattern" — but R32 is a single-snapshot binary guard, not a counter-with-threshold. The new flap guard implements the threshold-counter pattern as specified by the brief; it is *behaviourally similar* to R32 (both refuse to mass-close on a single suspicious sample) but uses different machinery. Surfaced for cowork review.

### 2. The `restart broker-sync-worker` step picked up the operator's pending compose env-var change
`CHILI_BRACKET_WRITER_CANCEL_COVERING_SELL=1` is now active in the running broker-sync-worker. Earlier in this session that env var was preserved as staged-but-not-deployed (per the f-leak-3 task's discipline). It went hot during this deploy — either compose v2 reconciled config across services on `up -d chili`, or some intermediate restart sequence in this session triggered it.

**Operationally contained right now**: 0 open trades, autotrader kill-switched from noon's `emergency_close_all`, no place_missing_stop calls flowing. But the flag IS hot; whenever the operator reconciles trades and unblocks the autotrader, `held_for_sells == broker_qty` cases will route through the cancel-and-place-stop branch instead of the upside-lock branch. Surfacing for operator awareness — the staged decision became deployed during this task without separate authorization.

### 3. The fix does not undo the 5 phantom-closed positions
Per the brief explicitly out-of-scope: "Do not auto-reopen Trade rows from inside this task." Those 5 rows (1812 AIDX / 1813 CCCC / 1814 CRDL / 1821 TLS / 1822 VFS) remain in DB as `status='closed', exit_reason='phantom_after_terminal_reject'` while the broker still holds the underlying shares. Operator reconciliation is the explicit follow-up.

### 4. No live signal observation for SC #5
As described above. Tests cover the behaviour; the live deferred-log signal can't fire without a terminal_reject+zero-qty position to evaluate against. Acknowledged in the report rather than papering over with a synthetic test row.

## Deferred

Everything called out in the brief as out-of-scope:
- **Operator reconciliation of the 11 broker-vs-DB mismatched positions.** Manual operator action; the dry-run script `scripts/dispatch-reopen-equity-trades-DRY-RUN-output.txt` already exists.
- **Bug 1 of the 2026-05-04 audit**: `is_disconnected()` weekend gap (the trigger for noon's mass exit).
- **Bug 2**: `emergency_close_all` writes false `exit_price` when quote fetch fails (6 trades currently have `exit_price == entry_price` lying in DB).
- **Bug 3**: redundant `activate_kill_switch` calls every 5 min.
- **Bug 4**: `emergency_close_all` does not submit broker SELL orders (the larger ticket).
- **Renaming `phantom_after_terminal_reject`** to clarify post-flap semantics.

## Open questions for Cowork

1. **R32 framing**: the brief described this as "R32-mirror" but R32 is a single-snapshot guard, not a threshold-counter. New pattern is complementary, not a port. If a future cascade mode emerges that also needs a counter at sync time, R32 itself could be ported to the same machinery — separate task class.
2. **Counter reset on broker_unavailable (sub-branch 1)**: brief said no reset there. Implementation respects that. Open question per brief: should it reset? My read: NO — broker_unavailable is "no signal," not "confirmed non-zero." Leaving the counter intact during transient broker-down windows means a real zero-qty regime that started before the disconnect can resume counting after reconnect, which is the right behaviour for the protection the guard provides.
3. **`restart` picking up env**: the operator's pending `CHILI_BRACKET_WRITER_CANCEL_COVERING_SELL=1` flag activated during this deploy. Either compose v2 behaviour changed under us, or a prior restart in this session already promoted it without my noticing. Worth a one-line verification in the next CC: `docker exec broker-sync-worker printenv` ahead of every deploy that uses `restart`, to make the staged-vs-deployed boundary visible.
4. **N=3 / TTL=60s tuning**: seed values. If post-reconciliation soak shows the deferred path firing too often (oscillating between deferred and closed), bump N. If it fires too rarely (real phantom positions taking too long to recognise), drop N. Not changing here.

## Rollback plan

- **Code rollback**: `git revert f917c02`. Reconciler change reverts; the new column stays in schema (harmless — NULL/0 default; no caller after revert). Sub-branch 2 returns to single-sweep close.
- **Migration rollback**: do not revert. Migration 223 is additive. Reverting orphans application references.
- **Flag-based rollback** (no commit needed): set `CHILI_BRACKET_PHANTOM_CLOSE_MIN_CONSECUTIVE_ZERO_SWEEPS=1` to restore single-sweep behaviour while keeping the new audit logging. Useful if N=3 turns out to be too lenient.
- **Hard-stop rollback**: flip `CHILI_BRACKET_MISSING_STOP_REPAIR_ENABLED=0` and recreate broker-sync-worker. Disables the entire emergency-repair branch; reverts to pre-`ef50d3f` `state_gated_skip` parking. Operator-only decision.

This task makes no broker calls.
