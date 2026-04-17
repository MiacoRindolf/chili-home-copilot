# Trading Brain - Phase J - Drift Monitor + Re-cert Queue Rollout

**Status:** shadow-ready (J.1 complete). Authoritative cutover (J.2) is
**not opened**; any attempt to move either knob to `authoritative` is
blocked by release-blocker scripts and by explicit refusal inside
`drift_monitor_service.evaluate_one` and
`recert_queue_service.queue_from_drift` / `queue_manual`.

## Summary

Phase J ships two shadow-only capabilities on top of the canonical
pattern lifecycle:

1. **Drift monitor** - a daily APScheduler sweep (05:30 server time by
   default) evaluates each promoted/live pattern against its backtest
   baseline win probability using (a) the Brier-style calibration delta
   `observed_p - baseline_p` and (b) a two-sided CUSUM statistic over
   the ordered recent closed outcomes. Each sweep appends one row per
   pattern to `trading_pattern_drift_log` with severity in
   `{green, yellow, red}`. The pure model is in
   `app/services/trading/drift_monitor_model.py`; the service writer
   is `drift_monitor_service`.

2. **Re-cert proposal queue** - when the daily sweep flags a
   `red`-severity pattern (and the re-cert queue is active), one row is
   appended to `trading_pattern_recert_log` with `source=drift_monitor`
   and `status=proposed`. Operators can also file manual proposals via
   `recert_queue_service.queue_manual`. `(pattern, as_of_date, source)`
   dedupe is enforced by the deterministic `recert_id` and an
   idempotent writer.

## Scope (J.1)

- Observational only: append-only writes to two new tables.
- No lifecycle transitions. `scan_patterns.lifecycle_stage`,
  `promotion_status`, and `active` are **not** mutated.
- No backtest triggers. The re-cert queue is a proposal log; no
  downstream consumer reads it in J.1.
- Diagnostics endpoints are read-only.

## Forbidden changes (J.1)

- Flipping `BRAIN_DRIFT_MONITOR_MODE=authoritative` or
  `BRAIN_RECERT_QUEUE_MODE=authoritative` in any environment before
  Phase J.2 is explicitly opened.
- Using drift rows to auto-demote, auto-retire, or auto-re-backtest
  patterns in J.1.
- Writing to `scan_patterns` from the drift-monitor or recert-queue
  services.
- Calling `queue_from_drift` or `queue_manual` from a hot-path code
  path (scanner, alerts, paper trading, stop engine). The only allowed
  call-site is the scheduled job.

## Rollout ladder

J.1 (this ships):

1. Deploy image with migration `136_drift_monitor_recert` applied
   (creates `trading_pattern_drift_log` and
   `trading_pattern_recert_log` with indexes).
2. Set `BRAIN_DRIFT_MONITOR_MODE=shadow` and
   `BRAIN_RECERT_QUEUE_MODE=shadow` in `.env`.
3. Recreate `chili`, `brain-worker`, and `scheduler-worker` with
   `docker compose up -d --force-recreate`.
4. Verify:
   - `GET /api/trading/brain/drift-monitor/diagnostics` returns
     `mode: "shadow"` and the frozen shape.
   - `GET /api/trading/brain/recert-queue/diagnostics` returns
     `mode: "shadow"` and the frozen shape.
   - `scripts/check_drift_monitor_release_blocker.ps1` passes.
   - `scripts/check_recert_queue_release_blocker.ps1` passes.
   - `scripts/phase_j_soak.py` (in-container) reports
     `ALL CHECKS PASSED`.
5. Monitor the following over a multi-week window before opening
   Phase J.2:
   - **Drift monitor:** `drift_events_total`, `by_severity`,
     `patterns_red`, `patterns_yellow`, `mean_brier_delta`,
     `mean_cusum_statistic`.
   - **Re-cert queue:** `recert_events_total`, `by_source`,
     `patterns_queued_distinct`, `by_status`.

J.2 (not opened):

- Reopen requires: named consumer for `trading_pattern_recert_log`
  (backtest queue + lifecycle FSM integration), explicit authority
  contract for automated lifecycle transitions, drift/kill-switch
  criteria, mandatory re-run of soak + release-blocker scripts in
  staging, and documented governance approval.

## Rollback

At any time during shadow:

1. Set `BRAIN_DRIFT_MONITOR_MODE=off` and
   `BRAIN_RECERT_QUEUE_MODE=off` in `.env`.
2. Recreate `chili`, `brain-worker`, `scheduler-worker`.
3. `evaluate_one` / `run_sweep` become no-ops (return `None` /
   empty list, write no rows).
4. `queue_from_drift` / `queue_manual` become no-ops.
5. The daily drift-monitor scheduler job is skipped by the
   scheduler's mode gate and is not registered.
6. Existing rows in `trading_pattern_drift_log` and
   `trading_pattern_recert_log` are left intact for post-mortem.

## Mandatory release blockers

Both scripts must exit **0** against a rolling log window **and** the
diagnostics JSON dumps before any subsequent change is merged. They
exit **1** if a forbidden line is observed or a diagnostics gate
fires.

```powershell
docker compose logs chili brain-worker scheduler-worker --since 30m |
  Out-File -FilePath dm.log -Encoding utf8
curl.exe -sk https://localhost:8000/api/trading/brain/drift-monitor/diagnostics -o dm.json
.\scripts\check_drift_monitor_release_blocker.ps1 -Path dm.log -DiagnosticsJson dm.json

docker compose logs chili brain-worker scheduler-worker --since 30m |
  Out-File -FilePath rq.log -Encoding utf8
curl.exe -sk https://localhost:8000/api/trading/brain/recert-queue/diagnostics -o rq.json
.\scripts\check_recert_queue_release_blocker.ps1 -Path rq.log -DiagnosticsJson rq.json
```

The drift-monitor blocker flags any `[drift_monitor_ops]` line with
`event=drift_persisted mode=authoritative` or
`event=drift_refused_authoritative`. The re-cert blocker flags any
`[recert_queue_ops]` line with
`event=recert_persisted mode=authoritative` or
`event=recert_refused_authoritative`.

## Monitoring

During shadow, watch:

- **Drift monitor**
  - `drift_events_total` scales with the number of eligible patterns
    (promoted/live with a baseline win-probability) times the number
    of daily sweeps.
  - `by_severity.red` should be rare; a sustained spike means a
    family of patterns are degrading and should be reviewed.
  - `patterns_red` over multiple sweeps identifies persistent
    offenders. A single red sweep for a given pattern may be noise;
    the CUSUM statistic is biased against it, but sample size still
    matters (see `min_red_sample` in `drift_monitor_model.py`).
  - `mean_brier_delta` should center near 0 in a healthy cohort; a
    persistent negative mean indicates systemic overconfidence in
    baselines.

- **Re-cert queue**
  - `recert_events_total` and `patterns_queued_distinct` track the
    fan-out from drift into proposals. In a healthy system they
    should match `patterns_red` closely (1 proposal per distinct
    red pattern per day).
  - `by_source` should be dominated by `drift_monitor` in J.1;
    `manual` entries are operator-initiated.
  - `by_status` stays at `{proposed: N, dispatched: 0, ...}` in
    J.1 because no consumer advances the status.

## Non-goals (explicit)

- Automated backtest scheduling from `trading_pattern_recert_log`.
  Deferred to J.2.
- Lifecycle FSM transitions (demote / retire) driven by drift.
  Deferred to J.2.
- Binomial p-value drift test (different regime; `live_drift.py`
  continues to own the legacy binomial test; canonical drift owns
  Brier + CUSUM).
- WR / return drift on alpha-decay axes (`alpha_decay.py` retains
  its own separate observability).
- Covariance-matrix drift correlation. Out of scope.
- UI surfaces for the proposal queue. Deferred to J.2.

## Known gaps / J.2 checklist

1. Drift is computed from closed `trading_paper_trades` outcomes
   only; live-broker fills are **not yet** folded in. Mapping live
   fills into the sweep bundle is a J.2 requirement.
2. Baseline win-probability comes from `scan_patterns.win_rate` when
   present; otherwise the pattern is skipped. J.2 should allow
   overriding this with `oos_win_rate` or a calibrated baseline from
   the model registry.
3. No feedback loop from `trading_pattern_recert_log` back into
   `scan_patterns`. Any lifecycle change is **explicit operator
   action** in J.1.
4. `include_yellow=false` by default: yellow drift does not propose a
   re-cert. J.2 can evaluate whether yellow-severity patterns benefit
   from automated re-cert or should stay observational.
5. CUSUM parameters (`cusum_k`, `cusum_threshold_mult`,
   `min_red_sample`, `min_yellow_sample`) are conservative defaults;
   tune after multi-week soak data is collected.
