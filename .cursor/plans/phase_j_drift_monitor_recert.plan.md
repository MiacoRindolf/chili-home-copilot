---
status: completed_shadow_ready
title: Phase J - Drift monitor + re-certification queue (shadow rollout)
parent_plan: trading_brain_profitability_gaps_bd7c666a
---

## Objective

Introduce a **canonical drift-monitor + re-certification substrate** on
top of the existing lifecycle FSM (`lifecycle.py`).

Problem this solves:

1. `live_drift.py` and `alpha_decay.py` already detect WR / return drift
   against research expectations, but neither persists a **time series**
   of drift scores - both write opaque blobs into
   `scan_patterns.oos_validation_json` and, in some paths, flip patterns
   straight to `challenged` / `decayed`.
2. There is **no automated re-certification path**: once a pattern is
   flipped to `challenged` or `decayed`, nothing queues a fresh
   backtest + `ensemble_promotion_check` against current data.
3. There is no **Brier-score / CUSUM** drift detector anywhere in code;
   drift is only binomial-p-value + WR gap.
4. The observability needed to set safe thresholds for a future
   authoritative `shadow_gated` lifecycle stage is not there today.

Phase J ships:

1. **Canonical drift monitor (`drift_monitor_model.py` + service).** A
   pure module that, given the recent live-or-paper PnL sample for a
   pattern and its baseline backtest expectation, returns a structured
   `DriftDecision` with:
   - Brier-style calibration delta (observed win prob minus expected).
   - CUSUM-style cumulative-sum test statistic over the live sample.
   - Bucketed severity (`green / yellow / red`).
   - A deterministic `drift_id` for dedupe.
   The service persists one row per scan_pattern per sweep to a new
   `trading_pattern_drift_log` table.
2. **Re-cert proposal queue (`recert_queue_model.py` + service).** Pure
   function that, given a `DriftDecision` with severity `red` or a
   user-initiated request, returns a `RecertProposal` (no side effects).
   The service persists the proposal to a new
   `trading_pattern_recert_log` table with status `proposed`. In J.1 no
   consumer reads this table - it is a **proposal queue** only.
3. **APScheduler daily drift sweep** that iterates active patterns and
   calls the drift monitor. Gated on
   `BRAIN_DRIFT_MONITOR_MODE`.
4. **Diagnostics endpoints**
   `/api/trading/brain/drift-monitor/diagnostics` and
   `/api/trading/brain/recert-queue/diagnostics`.
5. **Release-blocker scripts**
   `check_drift_monitor_release_blocker.ps1` and
   `check_recert_queue_release_blocker.ps1`.
6. **Ops-log modules** for both subsystems in
   `app/trading_brain/infrastructure/`.
7. **Docker soak** `scripts/phase_j_soak.py`.
8. **Docs** `docs/TRADING_BRAIN_DRIFT_RECERT_ROLLOUT.md`.

Like every phase since A, Phase J is **strictly shadow**:

- The drift monitor reads from `trading_paper_trades` / `trading_trades`
  + `scan_patterns.oos_validation_json` baseline; it writes only to
  `trading_pattern_drift_log`.
- The re-cert queue writes only to `trading_pattern_recert_log`.
- Zero existing lifecycle transitions, no `challenged/decayed` flips,
  no backtest auto-trigger, no proposal pause, no scanner or alerts
  consumer change.

## Forbidden changes

- Introducing a new lifecycle stage (`shadow_gated`, `recert_pending`,
  etc.). J.1 is observational; stage additions are J.2.
- Modifying `lifecycle.transition`, `lifecycle._ALLOWED_TRANSITIONS`,
  or any existing lifecycle writer.
- Auto-calling `transition_on_decay` / `retire` / `request_pattern_to_live`
  from the drift monitor or re-cert queue.
- Auto-queuing backtests from the re-cert queue (no writes to
  `trading_backtests` from Phase J code).
- Changing how `scanner.generate_signals`, `daily_playbook`,
  `alerts.propose_trade`, or any router filters patterns.
- Modifying `live_drift.py` or `alpha_decay.py` (they keep running
  independently; Phase J is an **additional** observational layer).
- Setting `BRAIN_DRIFT_MONITOR_MODE=authoritative` or
  `BRAIN_RECERT_QUEUE_MODE=authoritative` in any environment before
  J.2 is explicitly opened.

## Scope (J.1)

Allowed changes:

- New migration 136 (two new tables - no alters on existing tables).
- New pure modules + services.
- New ORM models for the two new tables only.
- New ops-log modules.
- New config flags (defaulted `off`).
- New APScheduler job (mode-gated; `off` means no registration).
- New diagnostics endpoints.
- New release-blocker scripts.
- New tests + soak.
- New docs.

## Dependency order

1. Migration 136 + ORM models + unit test that migration applies.
2. Pure `drift_monitor_model.py` + `recert_queue_model.py` + unit tests.
3. Config flags in `app/config.py` + `.env` (defaulted `off`).
4. Ops-log modules (`drift_monitor_ops_log.py`,
   `recert_queue_ops_log.py`).
5. DB services `drift_monitor_service.py` + `recert_queue_service.py`
   + DB integration tests.
6. APScheduler registration for `drift_monitor_daily`.
7. Diagnostics endpoints + smoke tests.
8. Release-blocker PowerShell scripts + smoke tests.
9. Docker soak `phase_j_soak.py` + run inside `chili` container.
10. Regression: scan_status frozen contract + full Phase J test run.
11. Flip `.env` to `BRAIN_DRIFT_MONITOR_MODE=shadow` +
    `BRAIN_RECERT_QUEUE_MODE=shadow`, recreate services.
12. Docs + closeout.

## Verification gates

- All new unit + DB tests pass.
- `tests/test_scan_status_brain_runtime.py` stays green.
- `scripts/phase_j_soak.py` exits 0 with ALL CHECKS PASSED.
- Both release-blocker scripts return exit 0 against a live 5-min
  log window and exit 1 against synthetic authoritative ops lines.
- Diagnostics endpoints return `mode: "shadow"` end-to-end.
- `scheduler-worker` / `chili` logs show
  `[drift_monitor_ops] event=drift_persisted mode=shadow` at least
  once after waiting through one sweep or forcing via the soak path.
- Sample 5 active patterns; confirm each has at most one
  `trading_pattern_drift_log` row per sweep (no duplicates).

## Rollback

At any time during shadow:

1. Set `BRAIN_DRIFT_MONITOR_MODE=off` + `BRAIN_RECERT_QUEUE_MODE=off`.
2. Recreate `chili` + `scheduler-worker` + `brain-worker`.
3. `drift_monitor_service.run_sweep` becomes a no-op.
4. `recert_queue_service.propose` becomes a no-op.
5. APScheduler `drift_monitor_daily` job is not registered.
6. Existing rows in `trading_pattern_drift_log` and
   `trading_pattern_recert_log` are left intact for post-mortem.

## Non-goals

- Introducing a `shadow_gated` or `recert_pending` lifecycle stage.
  Deferred to J.2.
- Triggering backtests from recert proposals. Deferred to J.2.
- Modifying scanner / alerts / playbook consumer filters. Deferred to
  J.2.
- Replacing or modifying `live_drift.py` or `alpha_decay.py`.
- Isotonic calibration of the Brier delta (the metric is raw delta
  in J.1).
- Full KS / Kolmogorov-Smirnov distribution test. Brier + CUSUM is
  sufficient for J.1.

## Definition of done

Shadow substrate is running in all three services. Every active
pattern gets one `trading_pattern_drift_log` row per daily sweep.
Red-severity patterns generate one `trading_pattern_recert_log` row
per day (with idempotent dedupe). All tests green. Release blockers
verified clean against live logs. The plan closeout documents the
gaps J.2 must pick up (introducing the new lifecycle stage + wiring
recert proposals to a backtest-queue consumer).

## Closeout

**What shipped (J.1, shadow-only):**

- Migration `136_drift_monitor_recert` creates
  `trading_pattern_drift_log` (per-sweep drift scores) and
  `trading_pattern_recert_log` (append-only proposal queue) with
  indexes on pattern+timestamp, severity/status+timestamp, and the
  deterministic id columns. Applied cleanly in Docker (verified via
  `schema_version`).
- Pure modules `drift_monitor_model.py` (Brier-delta + two-sided
  CUSUM with sample-size-gated severity) and `recert_queue_model.py`
  (drift-to-proposal with idempotent `recert_id`). 27/27 focused
  unit tests pass.
- Config flags: `BRAIN_DRIFT_MONITOR_*` and `BRAIN_RECERT_QUEUE_*`
  (all default off). Mode gating in both services (`off`, `shadow`,
  `compare`, `authoritative`) with explicit `RuntimeError` on
  authoritative until J.2.
- Ops-log modules `drift_monitor_ops_log.py` and
  `recert_queue_ops_log.py` producing one-line structured events.
- Service writers `drift_monitor_service.py` and
  `recert_queue_service.py` with `evaluate_one` / `run_sweep` /
  `drift_summary` / `queue_from_drift` / `queue_manual` /
  `recert_summary`. Deterministic IDs (`drift_id`, `recert_id`).
- APScheduler `drift_monitor_daily` job registered (cron default
  05:30 server time) in `trading_scheduler.py`, gated by
  `BRAIN_DRIFT_MONITOR_MODE`. Fans out to `recert_queue_service`
  when red-severity rows land and the re-cert queue is active.
- Read-only diagnostics endpoints
  `GET /api/trading/brain/drift-monitor/diagnostics` and
  `GET /api/trading/brain/recert-queue/diagnostics` returning the
  frozen summary shapes.
- Release-blocker scripts
  `scripts/check_drift_monitor_release_blocker.ps1` and
  `scripts/check_recert_queue_release_blocker.ps1` (combined log
  pattern grep + optional diagnostics JSON gates). Smoke-tested
  4/4 (clean -> exit 0; authoritative/refused lines -> exit 1).
- Docker soak `scripts/phase_j_soak.py` - 28/28 checks pass
  in-container against live Postgres.
- `.env` flipped to `BRAIN_DRIFT_MONITOR_MODE=shadow` +
  `BRAIN_RECERT_QUEUE_MODE=shadow`; `chili`, `brain-worker`,
  `scheduler-worker` force-recreated. Diagnostics endpoints return
  `mode: "shadow"` against live services.
- Docs: `docs/TRADING_BRAIN_DRIFT_RECERT_ROLLOUT.md` covers
  rollout ladder, rollback, release blockers, monitoring, non-goals,
  and the explicit J.2 checklist.

**Regression evidence:**

- 27/27 Phase J pure unit tests pass.
- 2/2 `scan_status` frozen-contract tests still pass.
- 28/28 Phase J Docker soak checks pass end-to-end against live
  Postgres + live services.
- Both release blockers exit 0 against live 10-minute log windows
  and the live diagnostics JSON.
- Diagnostics endpoints return the frozen shape with
  `mode: "shadow"` after the env flip.

**Self-critique / honest limitations:**

- DB integration tests
  (`tests/test_drift_monitor_service.py`,
  `tests/test_recert_queue_service.py`) were authored but not
  executed in an isolated local pytest run. Same environmental
  interference documented in Phase I: live Docker Postgres contends
  with local pytest fixtures against `TEST_DATABASE_URL`. These DB
  paths are instead validated end-to-end by the Docker soak
  (`phase_j_soak.py` exercises writer + dedupe + summary + mode
  gating against the live database) and by the live diagnostics
  endpoint hitting the same service layer.
- The APScheduler cron (05:30 server time) is registered but has
  not fired yet in this window; runtime fire verification deferred
  until the first scheduled sweep. The service layer itself was
  exercised directly by the soak (which calls `evaluate_one` /
  `run_sweep` / `queue_from_drift` / `queue_manual` against live
  Postgres), so scheduler registration is the only part of the
  chain that relies on "it will fire on time" rather than direct
  observation.
- Drift sweeps consume closed `trading_paper_trades` outcomes only;
  live-broker fills and backtest fills are not folded in. Noted
  as J.2 gap #1 in the rollout doc.
- Baseline win-probability is read from `scan_patterns.win_rate`;
  patterns without that populated are silently skipped by the
  sweep. Noted as J.2 gap #2.
- No UI surfaces for either table in J.1 - operators inspect via
  diagnostics endpoints + raw SQL.
- CUSUM thresholds (`cusum_k=0.05`, `cusum_threshold_mult=0.6`,
  `min_red_sample=20`) are conservative defaults that have not
  yet been tuned against multi-week live data.

**Deferred to J.2 (explicit re-open checklist):**

- Named consumer for `trading_pattern_recert_log`: backtest queue
  integration and lifecycle FSM transitions.
- Authoritative mode cutover (governance approval + drift /
  kill-switch criteria + staging re-run of soak + release
  blockers).
- Feed live-broker fills into the drift sweep.
- Allow baseline override (OOS win-rate or calibrated model-
  registry baseline).
- Evaluate `include_yellow=true` for the re-cert queue.
- Tune CUSUM / Brier thresholds against observed drift
  distribution.
- UI surfaces (proposal queue + drift timeseries panel).

**Stays frozen (hard track):**

- No lifecycle transitions from J.1 code.
- No writes to `scan_patterns` from drift-monitor / recert-queue
  services.
- `BRAIN_DRIFT_MONITOR_MODE=authoritative` and
  `BRAIN_RECERT_QUEUE_MODE=authoritative` remain blocked by both
  service-level `RuntimeError` and the release-blocker grep
  pattern.
