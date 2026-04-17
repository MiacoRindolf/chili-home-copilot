# Trading Brain - Phase K - Divergence Panel + Ops Health Rollout

**Status:** shadow-ready (K.1 complete). Authoritative cutover (K.2) is
**not opened**; any attempt to move the divergence scorer to
`authoritative` is blocked by the release-blocker script and by
explicit refusal inside `divergence_service.evaluate_pattern` /
`run_sweep`.

## Summary

Phase K ships two read-only capabilities that consolidate the
observability added by Phases A - J:

1. **Divergence panel** - a daily APScheduler sweep (06:15 server time
   by default) gathers the most-recent signal for each promoted / live
   pattern from the five existing divergence-bearing log tables
   (Phase A ledger, Phase B exit, Phase F venue, Phase G bracket
   reconciliation, Phase H position sizer), feeds them into the pure
   `compute_divergence` model, and appends one row to
   `trading_pattern_divergence_log` per pattern per sweep with per-layer
   severities and an overall hysteresis severity in
   `{green, yellow, red}`. The pure model is in
   `app/services/trading/divergence_model.py`; persistence is in
   `app/services/trading/divergence_service.py`.

2. **Ops health endpoint** - `GET /api/trading/brain/ops/health` returns
   a single read-only snapshot of every substrate phase (A - K) plus
   scheduler and governance state. Each phase appears once, in a stable
   order, with `present`, `mode`, `red_count`, `yellow_count`, and
   free-form notes. Defensive: any single broken phase summary is
   logged and surfaces as `present=False` rather than crashing the
   endpoint. The pure aggregator is in
   `app/services/trading/ops_health_model.py`; the service shim is
   `ops_health_service.build_health_snapshot`.

## Scope (K.1)

- Observational only: append-only writes to
  `trading_pattern_divergence_log`. Read-only aggregation for
  `/ops/health`.
- No lifecycle transitions. `scan_patterns.lifecycle_stage`,
  `promotion_status`, and `active` are **not** mutated.
- No governance actions. Kill switch and approval queue are surfaced
  in the snapshot but never driven by divergence outcomes in K.1.
- No quarantine. Auto-quarantine of cross-layer red patterns is K.2.

## Forbidden changes (K.1)

- Flipping `BRAIN_DIVERGENCE_SCORER_MODE=authoritative` in any
  environment before Phase K.2 is explicitly opened.
- Using divergence rows to auto-quarantine, auto-demote, or auto-kill
  patterns in K.1.
- Writing to `scan_patterns` from the divergence or ops-health
  services.
- Calling `evaluate_pattern`, `run_sweep`, or `build_health_snapshot`
  from a hot-path code path (scanner, alerts, paper trading, stop
  engine). The only allowed call-sites are the scheduled sweep job
  (`divergence_sweep_daily`) and the two diagnostics endpoints.
- Mutating the `/api/trading/scan/status` wire shape. The ops-health
  endpoint is a **new** endpoint, never a fork of `scan/status`.

## Rollout ladder

**K.1 (this ships):**

1. Deploy image with migration `137_divergence_panel` applied
   (creates `trading_pattern_divergence_log` with indexes on
   `(scan_pattern_id, sweep_at DESC)`, `(severity, sweep_at DESC)`,
   and `(divergence_id)`).
2. Set `BRAIN_DIVERGENCE_SCORER_MODE=shadow` and
   `BRAIN_OPS_HEALTH_ENABLED=true` in `.env`.
3. Recreate `chili`, `brain-worker`, `scheduler-worker` so the new env
   is visible and APScheduler picks up the daily sweep job.
4. Verify:
   - `GET /api/trading/brain/divergence/diagnostics` returns
     `{"ok": true, "divergence": {"mode": "shadow", ...}}`.
   - `GET /api/trading/brain/ops/health` returns
     `{"ok": true, "ops_health": {"overall_severity": ...,
     "phases": [...15 entries in stable order...], ...}}`.
   - Scheduler logs: "Added job `Divergence panel daily (06:15;
     mode=shadow)` to job store default".
5. Monitor for 2 - 4 weeks. The panel is **observational** only;
   divergence rows do not feed back into lifecycle or governance.

**K.2 (deferred, requires user-approved plan):**

- Auto-quarantine red patterns (move to `challenged` or `decayed`,
  set `active=false`) after N consecutive `red` sweeps.
- Wire divergence `red` into governance approval requirements.
- Flip `BRAIN_DIVERGENCE_SCORER_MODE=authoritative` in staging first,
  then production.

## Environment variables

All with sensible shadow defaults; see `app/config.py` for the full
list.

| Var | Default | Purpose |
| --- | --- | --- |
| `BRAIN_DIVERGENCE_SCORER_MODE` | `off` | One of `off`, `shadow`, `compare`, `authoritative`. K.1 only permits `off` / `shadow` / `compare`; authoritative raises at runtime. |
| `BRAIN_DIVERGENCE_SCORER_OPS_LOG_ENABLED` | `true` | Emit `[divergence_ops]` one-line ops logs on persist / refuse / skip. |
| `BRAIN_DIVERGENCE_SCORER_MIN_LAYERS_SAMPLED` | `1` | Hysteresis: patterns with fewer sampled layers are clamped to `green`. |
| `BRAIN_DIVERGENCE_SCORER_YELLOW_THRESHOLD` | `0.9` | Weighted score >= this but < `red` -> `yellow`. |
| `BRAIN_DIVERGENCE_SCORER_RED_THRESHOLD` | `1.8` | Weighted score >= this -> `red`. |
| `BRAIN_DIVERGENCE_SCORER_LOOKBACK_DAYS` | `7` | Signal gathering lookback window per layer. |
| `BRAIN_DIVERGENCE_SCORER_CRON_HOUR` / `_MINUTE` | `6` / `15` | Daily sweep schedule (server TZ). |
| `BRAIN_DIVERGENCE_SCORER_LAYER_WEIGHT_*` | `1.0` / `0.8` | Per-layer weights for the weighted-max severity score (venue defaults to 0.8 to damp microstructure noise). |
| `BRAIN_OPS_HEALTH_ENABLED` | `true` | Master toggle exposed in `ops_health.enabled`. |
| `BRAIN_OPS_HEALTH_LOOKBACK_DAYS` | `14` | Default window for the aggregated summary. |

## Severity rules

For each pattern, each sampled layer contributes
`severity_rank * layer_weight` to a weighted score, where
`severity_rank` is `{green: 0, yellow: 1, red: 2}`. The final severity
is the maximum score compared against the `yellow` / `red` thresholds,
with a `min_layers_sampled` floor that clamps under-sampled patterns
to `green`. This intentionally behaves like a **max** operator (one
severely red layer is enough to flag a pattern) rather than an average
(which would wash out isolated breakages).

Per-layer severity derivation in `divergence_service`:

- **ledger** (Phase A): `agree_bool=false` -> `yellow`.
- **exit** (Phase B): `agree_bool=false` -> `yellow`.
- **venue** (Phase F): `|realized_bps - expected_bps| >= 25 -> yellow`;
  `>= 100 -> red`.
- **bracket** (Phase G): `kind in {qty_drift, price_drift, state_drift}
  -> yellow`; `{orphan_stop, missing_stop, broker_down} -> red`.
- **sizer** (Phase H): `|divergence_bps| >= 50 -> yellow`;
  `>= 200 -> red`.

Thresholds are configurable via settings but the mapping above is the
K.1 default and is what all release gates assume.

## Release-blocker scripts

Two scripts live in `scripts/`:

- `check_divergence_release_blocker.ps1` - fails if any log line
  matches `[divergence_ops]` with either
  `event=divergence_persisted mode=authoritative` or
  `event=divergence_refused_authoritative`. Accepts `-Path` (file) or
  pipeline input. Optional JSON-based gates (`-MinDivergenceEvents`,
  `-MaxPatternsRed`, `-MaxPatternsYellow`) let ops wire the script into
  a diagnostics-dump CI step.
- `check_ops_health_release_blocker.ps1` - JSON-shape gate for the
  `/ops/health` wire shape: verifies required top-level keys
  (`overall_severity`, `lookback_days`, `scheduler`, `governance`,
  `phases`), scheduler / governance sub-shapes, and all 15 expected
  phase keys. Optional `-FailOnRedOverall` flips the script into a
  hard operational gate (defaults off).

Exit codes (both scripts):

- `0` - clean / contract intact.
- `1` - release blocker (authoritative divergence event or shape
  violation).
- `2` - file not found.
- `3` - malformed JSON.

## Diagnostics endpoints

All read-only, all require the standard identity cookie.

- `GET /api/trading/brain/divergence/diagnostics?lookback_days=N`
  returns:

  ```json
  {
    "ok": true,
    "divergence": {
      "mode": "shadow",
      "lookback_days": 14,
      "divergence_events_total": 0,
      "by_severity": {"green": 0, "yellow": 0, "red": 0},
      "patterns_red": 0,
      "patterns_yellow": 0,
      "mean_score": 0.0,
      "layers_tracked": ["ledger", "exit", "venue", "bracket", "sizer"],
      "latest_divergence": null
    }
  }
  ```

- `GET /api/trading/brain/ops/health?lookback_days=N` returns:

  ```json
  {
    "ok": true,
    "ops_health": {
      "overall_severity": "green",
      "lookback_days": 14,
      "scheduler": {"running": true, "job_count": 0},
      "governance": {"kill_switch_engaged": false, "pending_approvals": 0},
      "phases": [
        {"key": "ledger", "present": false, "mode": null,
         "red_count": 0, "yellow_count": 0, "notes": []},
        ...15 entries in stable order: ledger, exit_engine, net_edge,
        pit, triple_barrier, execution_cost, venue_truth,
        bracket_intent, bracket_reconciliation, position_sizer,
        risk_dial, capital_reweight, drift_monitor, recert_queue,
        divergence...
      ],
      "enabled": true
    }
  }
  ```

Both endpoints clamp `lookback_days` to `[1, 180]`.

## Rollback

If a divergence-panel issue is observed in shadow:

1. Set `BRAIN_DIVERGENCE_SCORER_MODE=off` in `.env`.
2. `docker compose up -d --force-recreate scheduler-worker`.
3. Verify the diagnostics endpoint reports `mode=off` and the
   scheduler logs no longer show `divergence_sweep_daily` firing.
4. Outstanding `trading_pattern_divergence_log` rows can be truncated
   via SQL if needed; no downstream consumer reads them in K.1.

Rolling back `/ops/health` is safe at any time: the endpoint is
additive and has no consumers writing back into the trading brain.

## Test matrix

- Pure model unit tests:
  - `tests/test_divergence_model.py` - 20 tests (id determinism,
    severity classification, hysteresis, weighting, input validation,
    payload structure).
  - `tests/test_ops_health_model.py` - 15 tests (empty / full
    summaries, severity extraction variants, authoritative-mode
    guard, scheduler / governance extraction, wire-shape stability).
- API smoke tests: `tests/test_phase_k_diagnostics.py`.
- Docker soak: `scripts/phase_k_soak.py` - 45 checks including
  schema presence, mode gating, append-only writes, determinism,
  authoritative refusal, diagnostics frozen shape, and ops-health
  15-phase stable order.
- Release-blocker smoke: PowerShell smoke tests for both scripts
  (clean / blocker / refused / missing-key / red-with-gate).
- Regression: `tests/test_scan_status_brain_runtime.py` unchanged;
  `/api/trading/scan/status` wire shape verified live (root keys
  `[ok, brain_runtime, prescreen, learning]`, `release = {}`,
  no top-level `release` / `work_ledger` / `scheduler` / `scan`).
