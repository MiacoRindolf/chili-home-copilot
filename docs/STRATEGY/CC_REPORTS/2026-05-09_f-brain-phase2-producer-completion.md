# CC_REPORT: f-brain-phase2-producer-completion

## Outcome

Stage 1 audit + Stage 2 mining-producer fix shipped. Mining now has
a watchdog-style producer wired into `run_brain_work_dispatch_round`
that fires every 15 minutes (settings-tunable) regardless of the
APScheduler-based brain_market_snapshots job's state. The candidate
pipeline can resume even if the scheduler stays dead.

## Stage 1 — handler / producer mapping table

| Handler file | Function | Consumed event | Producer (call site) | Producer cadence | 24h count | Status |
|---|---|---|---|---|---:|---|
| `mine.py` | `handle_market_snapshots_batch` | `market_snapshots_batch` | `emit_market_snapshots_batch_outcome` from `trading_scheduler.py:262` | APScheduler 15min | **0** | **DEAD** (silent since 2026-05-05) |
| `cpcv_gate.py` | `handle_backtest_completed` | `backtest_completed` | `emit_backtest_completed_outcome` from `backtest_queue_worker.py:202` (FIX 34) | event-driven on backtest finish | 345 | OK |
| `promote.py` | `handle_pattern_eligible_promotion` | `pattern_eligible_promotion` | `enqueue_outcome_event` inside `cpcv_gate.py:149` | event-driven on gate-pass | **0** | **STARVED** (gate's `ok and gate_pass` precondition fails) |
| `demote.py` | `handle_trade_closed` | `live_trade_closed` / `paper_trade_closed` / `broker_fill_closed` | `emit_*_trade_closed_outcome` from `execution_hooks.py` | event-driven on trade-close | 1 / 0 / 1 | OK |
| `pattern_stats.py` | `handle_*_trade_closed` | (same trio) | `execution_hooks.py` | event-driven | (same) | OK |
| `live_drift.py` | `handle_*_trade_closed` | (same trio) | `execution_hooks.py` | event-driven | (same) | OK |
| `execution_robustness.py` | `handle_*_trade_closed` | (same trio) | `execution_hooks.py` | event-driven | (same) | OK |
| `regime_ledger.py` | `handle_trade_closed_for_ledger` | (same trio) | `execution_hooks.py` | event-driven | (same) | OK |
| `breakout_outcomes.py` | `handle_breakout_alert_resolved` | `breakout_alert_resolved` | `emit_breakout_alert_resolved_outcome` from `trading_scheduler.py:3433/3448` | scheduled scan | 553 | OK |
| `dispatcher.py` (inline) | `_handle_execution_feedback_digest` | `execution_feedback_digest` | (cron-style) | sparse | 3 | OK (low cadence) |

**Two MISSING / BROKEN producers identified:**

1. **`market_snapshots_batch`** — wiring exists at
   `trading_scheduler.py:262` but has emitted **zero events in 4 days**.
   Either (a) `CHILI_SCHEDULER_ROLE` isn't enabling the job in the
   running container, (b) `defer_while_learning_running` gate stuck
   on, or (c) `run_scheduled_market_snapshots` raises silently.
   **Fixed in this brief via watchdog hook (Stage 2 below).**
2. **`pattern_eligible_promotion`** — producer wired in
   `cpcv_gate.py:149` but never reaches the emit branch (the
   `if ok and gate_pass:` precondition fails). 5 patterns currently
   have `promotion_gate_passed=True` but never emitted. **NOT fixed
   in this brief** (separable, requires `check_promotion_ready`
   investigation; queued as
   `f-cpcv-gate-emit-anomaly-investigation` per audit Section F #2).

All other producers are healthy.

## Stage 2 — mining producer fix

### Approach: watchdog hook in `run_brain_work_dispatch_round`

Per the brief's preference ("per-cycle hook in
`run_brain_work_dispatch_round` preferred"), I added a fallback
producer that runs every dispatch round (~75–90s) but is gated on a
15-minute interval. This:

* Doesn't replace the APScheduler job — both can coexist; the
  per-minute dedupe bucket in
  `emit_market_snapshots_batch_outcome` merges duplicates.
* Keeps the candidate pipeline alive even if the scheduler stays
  dead.
* Is independently disable-able via
  `CHILI_BRAIN_DISPATCH_MARKET_SNAPSHOTS_ENABLED=False` if the
  operator confirms the scheduler is healthy and wants single-path
  operation.
* Surfaces the result in the round's return dict via the new
  `market_snapshots` key for ops observability.

### Changes shipped

**`app/config.py` (Edit, +24 lines):**

* `chili_brain_dispatch_market_snapshots_enabled` (default True,
  `CHILI_BRAIN_DISPATCH_MARKET_SNAPSHOTS_ENABLED`).
* `chili_brain_dispatch_market_snapshots_interval_secs` (default
  900,
  `CHILI_BRAIN_DISPATCH_MARKET_SNAPSHOTS_INTERVAL_SECS`).
  Setting to 0 disables the interval gate entirely.

**`app/services/trading/brain_work/dispatcher.py` (splice, +127
lines):**

* Module-level state: `_LAST_DISPATCH_MARKET_SNAPSHOTS_AT: float = 0.0`
  tracks the last dispatch-round emit. Reset on container restart
  (intentional: the next round emits immediately after a restart,
  catching up the producer).
* `_maybe_run_dispatch_market_snapshots(db, *, user_id=None)`:
  * Reads enabled-flag and interval-secs from settings at call time
    (env overrides take effect on next round without restart).
  * Sets `_LAST_DISPATCH_MARKET_SNAPSHOTS_AT = now` BEFORE running
    the snapshot job so a crash doesn't hot-loop the dispatch round.
  * Calls `learning.run_scheduled_market_snapshots(db, uid)`.
  * Calls `emit_market_snapshots_batch_outcome` with `job_id=None`
    so the per-minute bucket dedupe key kicks in.
  * Returns a structured result dict for the round's payload.
* `run_brain_work_dispatch_round` calls
  `_maybe_run_dispatch_market_snapshots` after the existing
  thin-evidence sweep, wrapped in try/except so failures surface in
  the result without poisoning the round.

### Test surface

`tests/test_brain_producer_wiring.py` (5 tests):

1. **INTEGRATION (LIVE PATH)**:
   `test_integration_dispatch_round_emits_market_snapshots_batch` —
   stub the snapshot fetcher; call `run_brain_work_dispatch_round`
   directly; assert (a) the round returns ok=True, (b) the result
   dict's `market_snapshots` is not skipped, (c) a row lands in
   `brain_work_events`. **Run ALONE first** per the brief's lesson
   from tonight's three "tests-pass-but-system-fails" instances.
2. `test_interval_gate_skips_second_call_within_window` — second
   call returns `skipped=True, reason='interval_gate'`.
3. `test_interval_zero_disables_gate` — setting interval=0 runs on
   every call.
4. `test_disable_flag_short_circuits` — enabled=False returns
   `skipped=True, reason='disabled_by_setting'` without invoking
   the snapshot fetcher.
5. `test_snapshots_failure_does_not_poison_round` — fetcher raises
   → round still ok=True, failure surfaces in
   `market_snapshots.ok=false`.
6. `test_round_result_dict_has_market_snapshots_key` — pin the new
   contract.

## Deviations / surprises

1. **Both currently-promoted patterns (1011/1016) untouched.** Per
   constraint. Their `lifecycle_stage='promoted'` and
   `oos_win_rate IS NULL` state is preserved.
2. **The `mine.py` handler isn't modified.** The handler itself is
   correct; the producer is what was missing. Adding the producer
   on the dispatch side activates the existing handler chain.
3. **Pattern_eligible_promotion intentionally NOT fixed here.**
   It's separable — the producer wiring exists; the precondition
   in `check_promotion_ready` is what blocks. Requires its own
   focused brief (`f-cpcv-gate-emit-anomaly-investigation` per
   audit Section F #2).
4. **APScheduler job intentionally NOT removed.** The dispatch hook
   is additive. If the operator restores the scheduler later, both
   producers will run; the per-minute dedupe key handles overlap.

## Verification

* `dispatcher.py`: `wc -l` 508 → 635 (+127); AST clean.
* `config.py`: +24 lines (two new settings); AST clean.
* Default settings resolve:
  `chili_brain_dispatch_market_snapshots_enabled=True`,
  `chili_brain_dispatch_market_snapshots_interval_secs=900`.
* Helper imports clean: `_maybe_run_dispatch_market_snapshots`,
  `_LAST_DISPATCH_MARKET_SNAPSHOTS_AT`.
* Integration test PASSES standalone (run alone before helpers per
  brief's lesson).

## Operator-side after CC ships

Per brief:

1. `git pull` + truncation scan.
2. `docker compose up -d --force-recreate brain-worker scheduler-worker`.
3. Watch brain-worker logs for ~10 min:
   ```
   docker logs -f --tail 0 chili-home-copilot-brain-worker-1 \
     | grep -E 'market_snapshots|brain_work_dispatch'
   ```
   Expected: one `[brain_work_dispatch] dispatch_market_snapshots
   emitted daily=N intra=M universe_size=K` line within the first
   minute (the in-process timestamp is 0.0 on restart so the first
   round fires immediately), then one every ~15 min thereafter.
4. Wait 24h. Run audit Section D query:
   ```sql
   SELECT DATE(created_at), COUNT(*)
     FROM brain_work_events
    WHERE event_type='market_snapshots_batch'
      AND created_at >= NOW() - INTERVAL '7 days'
    GROUP BY DATE(created_at) ORDER BY 1 DESC;
   ```
   Expected: rows for today's date with non-zero counts.
5. After 7 days: re-run audit Section A. Expected: new
   `scan_patterns` rows accumulating + the
   `pattern_eligible_promotion` separable anomaly remains the
   only blocker (queued for follow-up).

## Rollback plan

`git revert` the commit. Watchdog hook is purely additive; revert
removes the in-process producer and restores the silent-mining
state. Settings flag
`CHILI_BRAIN_DISPATCH_MARKET_SNAPSHOTS_ENABLED=False` disables the
hook without code revert.

## What's NEXT after this ships

* Audit Section F #2 (`f-cpcv-gate-emit-anomaly-investigation`):
  ship next. The 5 patterns with `promotion_gate_passed=True` but
  no `pattern_eligible_promotion` event are the cheapest discovery
  win once mining is restored.
* Audit Section F #3 (`f-pattern-oos-revalidation`): conditional on
  this brief proving the candidate pipeline works.
* The architectural rebuild Phase 1 (auth liveness): unchanged, still
  scheduled for fresh-start.
