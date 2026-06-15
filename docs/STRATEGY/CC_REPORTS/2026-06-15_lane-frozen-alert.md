# CC_REPORT: lane-frozen-alert

**Date:** 2026-06-15
**Branch:** `chili/lane-frozen-alert` (branched from `origin/main` @ `4ae059c`, which
includes #727 per-broker daily-loss).

> **Protocol deviation (flagged).** `NEXT_TASK.md` was the stale Phase 5I post-rename
> soak. The operator gave a direct, detailed task in chat (a loud FROZEN alert for the
> momentum lane). Per CLAUDE.md "flag conflicts, don't veto," I executed the operator's
> explicit task. Worked in an isolated worktree off latest `origin/main` because the
> primary working tree was dirty with a parallel codex agent's changes
> ([[feedback_sync_before_change]]) **and** because main carried the per-broker helpers
> the task depends on (the branch I was on did not).

## Why

2026-06-15: the global daily-loss kill switch tripped at 05:18 ET (a Coinbase-sized
$60 cap — the equity-basis bug, since fixed in #727) and the momentum lane sat empty
**~8h** before the operator caught it. A tripped safety breaker is **silent**: the
auto-arm pass short-circuits (`skipped="kill_switch"` / a per-broker block) and the
only trace is a 6ms `phase=ok` tick with no `[scheduler] auto_arm:` line. CHILI needed
a loud, unmissable FROZEN signal. (See [[project_per_broker_daily_loss]].)

## What shipped

New module `app/services/trading/momentum_neural/lane_health.py`:

* `evaluate_lane_health(db)` — pure, read-only, never raises. Returns
  `{frozen, severity, headline, detail, conditions[], grace_seconds}`. Detects:
  * **(a)** global kill switch held past the grace window (`get_kill_switch_status`);
  * **(b)** a per-broker daily-loss block held past grace
    (`is_broker_daily_loss_blocked` + new `get_broker_daily_loss_block`);
  * **(c)** lane enabled + expected-to-trade but the pass/scheduler is **not executing**
    — scheduler-worker heartbeat stale (durable, cross-process) OR the auto-arm pass
    heartbeat stale (in-process). Deliberately distinct from a quiet market: a healthy
    lane keeps both heartbeats fresh, so it never false-positives on "no setup."
* `run_lane_health_check(db)` — the periodic hook. When frozen: `logger.critical(
  "[lane_health] FROZEN …")` **and** a durable audit row in `trading_alerts`
  (`alert_type='lane_health_frozen'`, `sent_via='cockpit'`). Change-only with a
  re-remind cooldown (= the grace window) so an 8h freeze keeps nagging without
  spamming every 30s. Logs a `RECOVERED` line and resets when the lane un-freezes.

Wiring:

* `trading_scheduler.py` — new `_run_lane_health_check_job` registered alongside
  auto-arm (same lane-on condition, same cadence, gated on its own flag). The existing
  auto-arm job now stamps `record_auto_arm_run()` each pass (the (c2) heartbeat).
* `automation_query.automation_pnl_rollup` — embeds read-only `lane_health` so the
  **autopilot P&L band** (the cockpit's primary surface — [[project_autopilot_money_cockpit]])
  shows it live.
* Cockpit UI — `_autopilot_pnl_band.html` + `autopilot-pnl.js` + `autopilot.css`: a
  full-width, danger-red, pulsing **FROZEN banner** above the sticky P&L band (hidden
  unless `lane_health.frozen`). Static-asset cache-buster `v=6 → v=7`.
* `governance.py` — added `get_broker_daily_loss_block(family)` (cheap read-only view of
  the sticky registry).
* `config.py` — two settings (see Adaptive / reversible below).

**No migration** — `trading_alerts` (AlertHistory) already exists. No trading behavior
changed: the feature only READS safety state and emits alerts; it cannot block trades
or alter sizing.

### Adaptive threshold (no magic number) + reversible kill-switch

* `chili_lane_health_freeze_alert_seconds` default **0 = ADAPTIVE**: derived from the
  lane's own watch cadence (`auto_arm max_watch + watch_extend` = 300 + 600 = 900s). A
  breaker held longer than the lane would wait on a single candidate = a skipped arming
  cycle = frozen. A positive value overrides. The same value is reused as the re-remind
  cooldown (no second number). ([[feedback_adaptive_no_magic]])
* `chili_lane_health_alert_enabled` default **True** (ship live + on,
  [[feedback_no_dark_flags]]); `=0` fully reverts to the prior silent behaviour.

## Verification

* **Tests:** `tests/test_lane_health_alert.py` — **12/12 PASS** (184s; the runtime is
  conda+app import, not the tests). Covers: frozen-after-grace for (a)/(b), not-frozen
  within grace, flag-off, scheduler-down frozen, **quiet-market NOT frozen** (the
  anti-false-positive case), auto-arm-stalled frozen, lane-disabled no-alert, and for
  `run_lane_health_check`: emits critical + writes exactly one audit row, change-only
  (no spam on the 2nd tick), and RECOVERED reset. `test_run_emits_critical_and_writes_
  audit_row` exercises the real governance + lane_health + AlertHistory path — this IS
  "it fires when the kill switch is active."
* **Regression:** re-ran `test_per_broker_daily_loss.py` + `test_governance_daily_loss.py`
  alongside — see run `b8caqeotg`.

## Surprises / deviations

* The task's stated (c) heuristic — "no momentum session created in > N min" — would
  **false-positive constantly** in quiet markets (the crypto lane legitimately idles for
  long stretches with no breaks). I implemented the robust equivalent: "the auto-arm
  pass / scheduler is not *executing*" (heartbeats), which a quiet-but-healthy lane keeps
  fresh. Same intent, no false alarms.
* `trading_alerts` is **not currently read by any router** — so the in-app surface the
  operator sees is the cockpit band (via the rollup); the row is the durable audit log.

## Deploy (COMPLETED + verified live)

Merged to `main` as `90176f1` (PR #729). Built `chili-app:main-clean-90176f1` and
recreated BOTH affected containers (old kept as `-pre90176f1` for instant rollback;
env copied verbatim from the known-good running container → `DATABASE_URL` count = 1,
the env-file-duplicate-keys defense):

* **scheduler** (`chili-clean-recovery-scheduler`, role `cron_only`) — `Lane-health
  FROZEN watch (every 30s)` + `Momentum auto-arm-live` jobs registered; 0 boot
  tracebacks; auto-arm running (arming XLM-USD); lane-health correctly **silent** (real
  kill switch off → no false positive).
* **web** (`chili-clean-recovery-web`, 8001→8000) — uvicorn up, 0 tracebacks; new band
  JS/CSS served; `pnl-rollup` returns `lane_health` (frozen=False, enabled=True).

**Fires-when-frozen verified on the deployed image** via an *ephemeral* `docker exec -i`
process that simulated a held kill switch in process-local memory only (no DB persist,
no `activate_kill_switch` → live scheduler + real trading untouched, re-confirmed
`kill_switch_active=False` after):
`EVALUATE frozen=True severity=critical` → `HEADLINE MOMENTUM LANE FROZEN — global kill
switch active 8h` → `logger.critical [lane_health] FROZEN …` → audit row written
(id 77387, `sent_via=cockpit`) → the simulated row was deleted afterwards. I did NOT
trip the real kill switch (that would halt live trading).

## Deferred

* **Push/SMS on freeze.** The audit row does NOT itself dispatch SMS (separate delivery
  path). A push would be the most effective "8h → minutes" win; left out to avoid an
  unrequested external send. Easy follow-up: route `lane_health_frozen` through the
  existing alert delivery.
* **Pre-existing test drift** — `test_governance_daily_loss.py::…partial_option_leg_
  without_db` (stale `_FakeQuery` missing `.join` at governance.py:680, from the
  2026-06-12 alpaca-exclusion change) fails on `main`; spun off as a separate task chip.

## Open questions for Cowork

* `NEXT_TASK.md` is still the stale Phase 5I post-rename soak. Please re-queue or mark
  it so the next run isn't ambiguous.
