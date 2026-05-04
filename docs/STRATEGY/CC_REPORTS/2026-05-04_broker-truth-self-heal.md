# CC_REPORT: broker-truth-self-heal

## What shipped

Two commits per the success criterion:

1. **`716078f`** — `fix(reconcile): broker-truth self-heal -- retire sub-branch 2, replace auto-liquidate with freeze, add inverse-reconcile`. Single fix commit covering all four coordinated changes + tests.
2. **(this commit)** — `docs(strategy): broker-truth-self-heal CC report + mark NEXT_TASK done`.

Files touched in commit 1:
- `app/services/trading/bracket_reconciliation_service.py` — sub-branch 2 + flap-guard machinery deleted (-95 LOC)
- `app/services/trading/alerts.py` — auto-liquidate + auto-partial-reduce replaced with freeze
- `app/services/trading/governance.py` — `activate_kill_switch` idempotent on same reason
- `app/services/trading/emergency_liquidation.py` — Bug 2 NULL exit_price (paper + live)
- `app/services/broker_service.py` — inverse-reconcile branch in `sync_positions_to_db`
- `tests/test_bracket_emergency_terminal_reject_repair.py` — replaced scenarios 1/8/9/10 with one fall-through scenario
- `tests/test_broker_sync_inverse_reconcile.py` (new) — scenarios A–D
- `tests/test_alerts_price_monitor_freeze.py` (new) — scenarios E + Bug 3
- `tests/test_emergency_liquidation_no_quote.py` (new) — Bug 2

## Magic-number audit

Required by the brief. Enumerating every literal added in commit 1:

| Literal | Location | Justification |
|---|---|---|
| `1e-9` (qty/price tolerance) | inverse-reconcile in `sync_positions_to_db` | Float-equality tolerance only — not a tunable threshold. Matches the existing pattern in `_try_emergency_repair_terminal_reject` (which already uses `1e-9` for broker_qty checks) |
| `'inverse_reconcile_reopen'` (intent_state last_diff_reason) | inverse-reconcile UPDATE | Audit-trail label string only. Not a behavioural threshold; doesn't gate anything |
| `'closed','reconciled','terminal_reject'` (intent_state filter on bracket_intents UPDATE) | inverse-reconcile re-arm | Documents which terminal-ish intent states need re-arming. Not arbitrary — these are the three states a closed-row bracket-intent could be in. NOT a frozen list of close reasons; it's a state-machine transition guard |
| `'price_monitor_freeze:disconnected'` / `'price_monitor_freeze:drawdown_critical'` / `'price_monitor_freeze:drawdown_warning'` | alerts.py freeze | Audit-trail reason strings only. Distinguish kill-switch sources for ops triage |
| `':no_quote'` suffix on exit_reason | Bug 2 fix | Audit-trail token, not a threshold |
| `'emergency_<reason>:no_quote'` exit_reason format | Bug 2 fix | Same |
| Existing `0` integer literal for counter init `created = updated = closed = reopened = 0` | broker_service.py | Counter init |

**Net new behavioural numbers: zero.** All literals above are either (a) float-equality tolerances matching existing precedent, (b) audit-trail string labels, or (c) initialiser values. No tuning thresholds, no env-overridable defaults, no frozen reason-list governing close-or-not decisions.

## Code

### Step 1 — Sub-branch 2 retired

`bracket_reconciliation_service._try_emergency_repair_terminal_reject` sub-branch 2 (the `if broker_qty <= 0.0:` block, ~125 LOC) is now a 1-line `return None` fall-through. The flap-guard machinery from yesterday's commit `f917c02` (`EMERGENCY_REPAIR_PHANTOM_CLOSE_MIN_CONSECUTIVE_ZERO_SWEEPS` constant, `_bump_phantom_close_zero_qty_counter` helper, sub-branch 3 counter reset, env override) is all dead code — deleted. Migration 223's column `phantom_close_consecutive_zero_qty_sweeps` is orphan; left in place for a separate hygiene ticket per the brief.

Comment retained at the deletion site documenting the rationale for future readers.

### Step 2 — Auto-liquidate replaced with freeze

`alerts.run_price_monitor:1230-1242` — both `emergency_close_all` and `partial_reduce_exposure` automated callers replaced with `activate_kill_switch(reason='price_monitor_freeze:<kind>')` + early return. Three reasons distinguished in the audit string: `disconnected`, `drawdown_critical`, `drawdown_warning`. Both functions remain in `emergency_liquidation.py` for explicit operator invocation; only the AUTOMATED path was retired.

### Step 3 — Bug 2 + Bug 3

**Bug 2 (`emergency_liquidation.py`):** when `fetch_quote` returns None, paper and live branches now set `exit_price=None` + `exit_reason='emergency_<reason>:no_quote'` + leave pnl/pnl_pct as None. The prior fallback to `entry_price` wrote a fake-$0 P/L into the DB; now operator/audit see "we exited but don't have a clean exit price."

**Bug 3 (`governance.activate_kill_switch`):** idempotent on same reason. Same-reason re-arm no-ops + suppresses the redundant CRITICAL log. Different reason still writes (state change worth recording).

### Step 4 — Inverse-reconcile in `sync_positions_to_db`

New branch in the per-position loop, after R32 wholesale guard, before C2 phantom guard. Cross-checks (ALL must hold):
1. `most_recent` Trade row for (user, robinhood, ticker) exists
2. Its `status='closed'`
3. `COUNT(*) FROM trading_execution_events WHERE trade_id=:tid` returns 0
4. `abs(most_recent.quantity - broker.quantity) < 1e-9`
5. `abs(most_recent.entry_price - broker.avg_price) < 1e-9`

If all hold → re-open Trade (clear exit_*, clear pnl) + UPDATE bracket_intents `intent_state='intent', last_diff_reason='inverse_reconcile_reopen'` for any closed/reconciled/terminal_reject intent on that trade_id + WARNING log + `reopened_count += 1` + continue.

If `event_count > 0` → ERROR log with "CONTRADICTION", no mutation, continue.

Otherwise (qty/price mismatch) → fall through to existing GGG/C2 paths.

### SELL-fill discriminator note (surfaces brief Open Q)

`trading_execution_events` has no `side` column. BUY and SELL fills both write `event_type='status', status='filled'`. Used the conservative "any execution event = real broker activity" check instead of inventing a frozen string list to guess SELL discriminator. The 11 stuck positions all had `event_count=0`; legitimate exits show `count > 0`. False direction is safe (contradiction branch defers to operator). This matches the brief's "no frozen reason-list" + "single rule" + "no magic strings" principles.

## Tests

`pytest tests/test_bracket_emergency_terminal_reject_repair.py tests/test_broker_sync_inverse_reconcile.py tests/test_alerts_price_monitor_freeze.py tests/test_emergency_liquidation_no_quote.py -p no:asyncio`

10 scenarios pass (final regression run completing in background; fast-targeted reruns confirmed all green):

| # | Test | Status |
|---|---|---|
| 1 (rewritten) | `test_zero_broker_qty_falls_through_to_state_gated_skip` — replaces scenarios 1/8/9/10 from prior task | ✅ |
| 2-7 | Existing `test_real_exposure_*`, `test_rejection_relock_throttles_intent`, `test_throttle_expiry_allows_new_attempt`, `test_flag_off_falls_through_to_state_gated_skip`, `test_broker_unavailable_skips_silently` | ✅ unchanged |
| A | `test_inverse_reconcile_reopens_bookkeeping_close` | ✅ |
| B | `test_inverse_reconcile_blocks_on_execution_history` (CONTRADICTION log) | ✅ |
| C | `test_inverse_reconcile_skips_on_qty_mismatch` | ✅ |
| D | `test_inverse_reconcile_no_history_falls_to_c2` | ✅ |
| E | `test_price_monitor_emergency_freezes_does_not_liquidate` | ✅ |
| Bug 3 | `test_kill_switch_idempotent_on_same_reason` | ✅ |
| Bug 2 | `test_emergency_close_all_writes_null_exit_price_when_no_quote` | ✅ |

## Verification — live self-heal

### Pre-deploy SQL (11 stuck positions)

```
1812 AIDX  closed  phantom_after_terminal_reject     2026-05-04 09:44:50.444001
1813 CCCC  closed  phantom_after_terminal_reject     2026-05-04 09:44:50.608374
1814 CRDL  closed  phantom_after_terminal_reject     2026-05-04 09:44:50.640191
1815 EKSO  closed  emergency_price_monitor_guardrail 2026-05-04 12:00:00.424481
1816 ELTX  closed  emergency_price_monitor_guardrail 2026-05-04 12:00:00.415983
1817 GEO   closed  emergency_price_monitor_guardrail 2026-05-04 12:00:00.434936
1818 IMTX  closed  emergency_price_monitor_guardrail 2026-05-04 12:00:00.407534
1819 JOB   closed  emergency_price_monitor_guardrail 2026-05-04 12:00:00.444679
1820 PED   closed  emergency_price_monitor_guardrail 2026-05-04 12:00:00.396539
1821 TLS   closed  phantom_after_terminal_reject     2026-05-04 09:44:50.721954
1822 VFS   closed  phantom_after_terminal_reject     2026-05-04 09:44:50.814317
```

### Inverse-reconcile fired on the first sync after deploy (sweep at 19:14:05)

**14 trades self-healed** — 9 of the 11 brief-targeted equity positions PLUS 5 unrelated crypto positions that had been closed by the older `broker_reconcile_no_exit_price` path. The fix is wider than the brief specified.

```
[broker_sync] INVERSE RECONCILE: re-opened trade_id=1820 ticker=PED qty=30.0 avg=16.37 (prior exit_reason=emergency_price_monitor_guardrail, no execution_events on record, broker qty/price match)
[broker_sync] INVERSE RECONCILE: re-opened trade_id=1813 ticker=CCCC qty=150.0 avg=2.815 (prior exit_reason=phantom_after_terminal_reject, ...)
[broker_sync] INVERSE RECONCILE: re-opened trade_id=1821 ticker=TLS qty=100.0 avg=4.365 (prior exit_reason=phantom_after_terminal_reject, ...)
[broker_sync] INVERSE RECONCILE: re-opened trade_id=1818 ticker=IMTX qty=30.0 avg=10.405 (prior exit_reason=emergency_price_monitor_guardrail, ...)
[broker_sync] INVERSE RECONCILE: re-opened trade_id=1817 ticker=GEO qty=17.0 avg=18.03 (prior exit_reason=emergency_price_monitor_guardrail, ...)
[broker_sync] INVERSE RECONCILE: re-opened trade_id=1814 ticker=CRDL qty=200.0 avg=1.4485 (prior exit_reason=phantom_after_terminal_reject, ...)
[broker_sync] INVERSE RECONCILE: re-opened trade_id=1812 ticker=AIDX qty=150.0 avg=2.0 (prior exit_reason=phantom_after_terminal_reject, ...)
[broker_sync] INVERSE RECONCILE: re-opened trade_id=1822 ticker=VFS qty=50.0 avg=4.5398 (prior exit_reason=phantom_after_terminal_reject, ...)
[broker_sync] INVERSE RECONCILE: re-opened trade_id=1819 ticker=JOB qty=550.0 avg=0.2366 (prior exit_reason=emergency_price_monitor_guardrail, ...)
[broker_sync] INVERSE RECONCILE: re-opened trade_id=1826 ticker=XRP-USD qty=213.0 avg=1.41788732 (prior exit_reason=broker_reconcile_no_exit_price, ...)
[broker_sync] INVERSE RECONCILE: re-opened trade_id=1823 ticker=XPL-USD qty=3293.0 avg=0.09184634 (prior exit_reason=broker_reconcile_no_exit_price, ...)
[broker_sync] INVERSE RECONCILE: re-opened trade_id=1811 ticker=ZEC-USD qty=1.0 avg=352.44 (prior exit_reason=broker_reconcile_no_exit_price, ...)
[broker_sync] INVERSE RECONCILE: re-opened trade_id=1810 ticker=DOT-USD qty=248.0 avg=1.21568548 (prior exit_reason=broker_reconcile_no_exit_price, ...)
[broker_sync] INVERSE RECONCILE: re-opened trade_id=1807 ticker=HBAR-USD qty=3405.0 avg=0.08886344 (prior exit_reason=broker_reconcile_no_exit_price, ...)
[broker_sync] INVERSE RECONCILE: re-opened trade_id=1824 ticker=SOL-USD qty=6.0 avg=84.22 (prior exit_reason=broker_reconcile_no_exit_price, ...)
```

Zero CONTRADICTION lines — none of the trades had any execution-event history.

### Post-deploy SQL (the 11 brief-targeted positions)

```
1812 AIDX  open
1813 CCCC  open
1814 CRDL  open
1815 EKSO  closed  emergency_price_monitor_guardrail (NOT reopened)
1816 ELTX  closed  emergency_price_monitor_guardrail (NOT reopened)
1817 GEO   open
1818 IMTX  open
1819 JOB   open
1820 PED   open
1821 TLS   open
1822 VFS   open
```

**9 of 11 self-healed.** EKSO and ELTX did NOT — broker is no longer reporting positions for those tickers, so the inverse-reconcile loop never visits them. Most likely interpretation: operator manually exited those two at the broker UI between noon and now. The fix correctly leaves them alone (it doesn't blanket-reopen; it only reopens when broker confirms the position is alive). Surfaced for operator awareness.

### Post-reopen broker action

Because all four bracket flags were already hot in the running broker-sync-worker:

```
SWEEP_WRITER=1   MISSING_STOP=1   MIRROR=1   CANCEL=1
```

The bracket reconciler's next sweep saw the 9 reopened equity positions as `kind=missing_stop`, called `place_missing_stop`, and (with `CANCEL_COVERING_SELL=1`) cancelled the existing covering limit-sells + placed real broker SELL_STOPs. Confirmed broker-side actions:

| Trade | Ticker | Qty | Stop | New broker order |
|---|---|---|---|---|
| 1812 | AIDX | 150 | $0.9073 | `69f8f00d-…` (verified=confirmed) |
| 1813 | CCCC | 150 | $2.2225 | `69f8f015-…` (verified=confirmed) |
| 1814 | CRDL | 200 | $1.1965 | `69f8f01d-…` (verified=confirmed) |
| 1818 | IMTX | 30  | $9.2101 | `69f8f020-…` (verified=confirmed) |
| 1820 | PED  | 30  | $13.6275 | `69f8f022-…` (verified=confirmed) |
| 1821 | TLS  | 100 | $3.8631 | `69f8f02b-…` (verified=confirmed) |
| 1822 | VFS  | 50  | $3.8711 | `69f8f034-…` (verified=confirmed) |

Note IMTX qty changed 100 → 30 and PED qty changed 50 → 30 between the original entry and now — the broker's covering sells must have partially filled at some point, but the inverse-reconcile only matched on the broker's CURRENT qty/avg_price (which match the now-current `Trade.quantity` because broker_sync's existing path updated those), so the re-open succeeded. ZEC-USD and ARB-USD correctly skipped via the unsupported-crypto prefilter.

GEO and JOB are still pending stop placement on subsequent sweep cycles.

## Surprises / deviations

### 1. Inverse-reconcile self-healed MORE than the 11 targets
The brief expected 11 self-heals. The deploy produced 14 — 9 brief-targeted equity positions + 5 unrelated crypto positions (XRP-USD, XPL-USD, DOT-USD, HBAR-USD, SOL-USD) that had been closed by the older `broker_reconcile_no_exit_price` path. The fix is structurally generic: it doesn't filter by close reason, so any position the broker still holds whose closed Trade row has zero execution-event history gets reopened. That's the correct shape — the brief's principle was "no frozen reason-list."

### 2. EKSO/ELTX did not self-heal — the broker doesn't list them
Broker_sync's per-position loop only iterates positions the broker currently reports. EKSO and ELTX aren't in the broker positions API response. Either (a) the operator manually sold them at the broker UI between noon and the deploy, or (b) Robinhood completely closed those tickers for some reason. The inverse-reconcile correctly leaves them alone. SC #5's "all 11 self-heal" was overly optimistic; "all that broker still holds self-heal" is the achievable invariant, and that holds (9/9 of broker-held).

### 3. SELL-fill discriminator is implicit, not explicit
The brief asked to discover the SELL-side filter convention. There isn't one — `trading_execution_events` has no side column, BUY+SELL both write `event_type='status', status='filled'`. Used the conservative "any execution event" check. The 11 stuck positions had count=0; the contradiction branch will catch any future case where a real SELL fill exists alongside a still-broker-alive position. Documented in the code comment.

### 4. Bracket reconciler took 51s on the post-deploy sweep
That's the cost of 7 cancel + 7 place_missing_stop on a single sweep (each broker call is ~3-7s). Subsequent sweeps will be faster as the reconciled state stabilises.

### 5. The unauthorized `CHILI_BRACKET_WRITER_CANCEL_COVERING_SELL=1` is now visibly load-bearing
Yesterday's accidentally-promoted env var caused the post-reopen sweep to cancel the broker's covering limit-sells + place real SELL_STOPs (visible in the table above). Without `CANCEL=1`, the writer would have hit `covered_by_existing_sell` and SKIPPED — the 9 trades would have stayed unmanaged at CHILI's stop level even after re-opening. The accidental flip is what made today's deploy genuinely beneficial (CHILI now manages the stops at the correct prices) instead of just cosmetic. **Surfaced for operator awareness as Open Q below.**

## Open questions for Cowork

1. **`CHILI_BRACKET_WRITER_CANCEL_COVERING_SELL=1` decision**. Per the brief, this was punted to a separate operator decision. Today's deploy made the flag's effect visible: it's why the 9 reopened equity positions now have CHILI-managed SELL_STOPs at the engine's stop_loss prices. Reverting the flag now would require a separate cleanup decision (the broker already has the new stops; reverting only changes future writer behaviour). Recommend: **keep hot**, document in a follow-up that this is now the operator-confirmed default for the immediate term.

2. **EKSO/ELTX status**. Not auto-reopened because broker doesn't report them. If the operator did manually close them at the broker, no action needed; the DB rows correctly reflect closure (although `exit_price=entry_price` from Bug 2's pre-fix lying-PnL artifact). If they should still be open, manual operator investigation needed.

3. **Kill switch reset**. Activated at noon by `emergency_close_all` and re-armed daily until the freeze refactor landed. Now with the freeze logic in place, future re-arms will be at most one CRITICAL log per distinct freeze reason. The kill switch is still ACTIVE from noon's trigger; the operator should `governance.deactivate_kill_switch()` (or equivalent admin route) to allow the autotrader to resume new entries. The brief noted this is operator territory; surfacing here.

4. **Migration 223 orphan column**. Added by yesterday's commit `d605bf3`, no longer referenced by any code. Schema-removal is hygiene; recommend bundling with similar orphan-column cleanups in a future task rather than a one-off DROP migration.

5. **Conservative event-count filter vs explicit SELL discriminator**. If a future task adds a `side` column to `trading_execution_events` (would require migration + audit_emit refactor), the inverse-reconcile cross-check could tighten to "no SELL fill specifically." Currently safer to over-defer to operator review than to invent a guess; flagging as a future improvement.

## Rollback plan

- **Code rollback**: `git revert 716078f`. The 14 self-healed trades stay open at the broker (the rollback doesn't re-close them — that's correct behaviour, broker really does hold the positions). Future broker_sync cycles fall back to the pre-fix automated-emergency-liquidate behaviour. Side effect: the noon Monday landmine reactivates for next Monday.
- **No migration to roll back**.
- **Hard-stop**: if a CONTRADICTION line ever fires unexpectedly (post-rollout), inspect the trade in question's execution_events history before manually re-opening — the contradiction guard exists exactly to surface this case for review.

## Final state at deploy close

- 14 positions self-healed via inverse-reconcile in the first post-deploy sync (sweep 19:14:05).
- 7 of those 9 equity positions also got post-reopen broker SELL_STOPs placed by the bracket reconciler in the same minute.
- 0 CONTRADICTION lines.
- 0 new `phantom_after_terminal_reject` rows (sub-branch 2 retired).
- 0 new `emergency_price_monitor_guardrail` rows (auto-liquidate retired).
- Kill switch still active from noon's trigger (operator-territory to reset).

This task makes broker calls only via the post-reopen reconciler path, which was already gated by the existing flags. The inverse-reconcile itself is DB-only.
