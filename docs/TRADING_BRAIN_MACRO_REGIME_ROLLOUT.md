# Trading Brain - Phase L.17 - Macro Regime Expansion Rollout

**Status:** shadow-ready (L.17.1 complete). Authoritative cutover
(L.17.2) is **not opened**; any attempt to move the macro regime panel
to `authoritative` is blocked by the release-blocker script and by
explicit refusal inside `macro_regime_service.compute_and_persist`.

## Summary

Phase L.17 is the first Frontier-Alpha (P4) slice in the master plan
`trading_brain_profitability_gaps_bd7c666a`. It ships a shadow-only
persisted macro regime snapshot that expands the existing SPY/VIX
equity composite in `market_data.get_market_regime()` with three
additional sub-regimes:

1. **Rates** - derived from IEF (7-10y Treasuries), SHY (1-3y), and
   TLT (20y+). Categorizes the long-duration trend and a naive yield
   curve slope proxy (`momentum_20d(TLT) - momentum_20d(SHY)`).
2. **Credit** - derived from HYG (high yield) and LQD (investment
   grade). Computes a spread proxy (`momentum_20d(HYG) - momentum_20d(LQD)`):
   positive -> credit tightening / risk-on, negative -> widening /
   risk-off.
3. **USD** - derived from UUP (dollar index ETF). Strong USD -> risk-off
   historically.

The pure classifier is in `app/services/trading/macro_regime_model.py`;
persistence lives in `app/services/trading/macro_regime_service.py`
and writes one row per successful sweep to
`trading_macro_regime_snapshots` (migration `138_macro_regime_snapshot`).
A daily APScheduler job (`macro_regime_daily`, default 06:30 server
time) runs the sweep in shadow mode only.

The existing `market_data.get_market_regime()` return shape is
**bit-for-bit unchanged**; no existing regime consumer reads from
the new table in L.17.1. Authoritative consumption (regime gating,
regime-aware position sizing) is deferred to L.17.2.

## Scope (L.17.1)

- Observational only: append-only writes to
  `trading_macro_regime_snapshots`. Read-only aggregation for
  `/api/trading/brain/macro-regime/diagnostics`.
- No consumer changes. Phases A-K keep using the existing SPY/VIX
  regime surface.
- No cross-regime quarantine. Auto-degrade of strategies when
  `macro_label = risk_off` is L.17.2.

## Forbidden changes (L.17.1)

- Flipping `BRAIN_MACRO_REGIME_MODE=authoritative` in any environment
  before Phase L.17.2 is explicitly opened.
- Using macro-regime rows to gate sizing, route selection, or scan
  admission in L.17.1.
- Mutating the return shape of `market_data.get_market_regime()`.
  The L.17 diagnostics endpoint is a **new** endpoint, never a fork
  of the existing regime surface.
- Writing to `scan_patterns`, `trading_paper_trades`, or
  `trading_risk_state` from the macro-regime service.
- Calling `compute_and_persist` from a hot-path code path (scanner,
  alerts, paper trading, stop engine). The only allowed call-sites
  are the scheduled sweep job (`macro_regime_daily`) and the
  diagnostics endpoint (read-only).

## Rollout ladder

**L.17.1 (this ships):**

1. Deploy image with migration `138_macro_regime_snapshot` applied
   (creates `trading_macro_regime_snapshots` with indexes on
   `(as_of_date DESC)`, `(regime_id)`, and
   `(macro_label, computed_at DESC)`).
2. Set `BRAIN_MACRO_REGIME_MODE=shadow` in `.env`.
3. Recreate `chili`, `brain-worker`, and `scheduler-worker` so the
   new env is visible and APScheduler picks up the daily sweep job.
4. Verify:
   - `GET /api/trading/brain/macro-regime/diagnostics` returns
     `{"ok": true, "macro_regime": {"mode": "shadow", ...}}`.
   - Scheduler logs: `"Added job 'Macro regime daily (06:30; mode=shadow)' to job store"`.
   - Release-blocker script exits 0 against the live log window:
     ```powershell
     (docker compose logs chili brain-worker scheduler-worker --since 30m 2>&1) |
       .\scripts\check_macro_regime_release_blocker.ps1
     ```
   - Docker soak exits 0:
     ```powershell
     docker compose exec -T chili python /app/scripts/phase_l17_soak.py
     ```
5. Monitor for 1-2 weeks: `snapshots_total` climbs by ~1/day,
   `mean_coverage_score` stays >= `BRAIN_MACRO_REGIME_MIN_COVERAGE_SCORE`
   (0.5 default), label distribution is stable over macro regimes.

**L.17.2 (future, not opened here):**

- Backfill regime history from prior bars.
- Wire `macro_label` into authoritative consumers (regime filter for
  promotion, regime-aware Kelly scaling in the risk dial).
- Flip mode to `authoritative` only after L.17.2 plan is explicitly
  frozen and soak is green for the extended consumer surface.

## Rollback

1. Set `BRAIN_MACRO_REGIME_MODE=off` in `.env`.
2. Recreate the three services (`chili`, `brain-worker`,
   `scheduler-worker`) so APScheduler de-registers the daily job.
3. The snapshot table remains in place (append-only + cheap); no
   reverse migration is required to kill-switch L.17. Prior rows are
   retained for inspection.

## Release blockers

A release is **blocked** if any log line in the last 30 minutes
contains either of:

- `[macro_regime_ops] event=macro_regime_persisted mode=authoritative`
- `[macro_regime_ops] event=macro_regime_refused_authoritative`

Check with:

```powershell
(docker compose logs chili brain-worker scheduler-worker --since 30m 2>&1) |
  .\scripts\check_macro_regime_release_blocker.ps1
```

Optional gate against `/macro-regime/diagnostics` JSON:

```powershell
curl -sk https://localhost:8000/api/trading/brain/macro-regime/diagnostics -o mr.json
.\scripts\check_macro_regime_release_blocker.ps1 -DiagnosticsJson .\mr.json -MinCoverageScore 0.5
```

## Configuration flags

| Flag | Default | Purpose |
|---|---|---|
| `BRAIN_MACRO_REGIME_MODE` | `off` | `off` \| `shadow` \| `compare` \| `authoritative`. L.17.1 allows only `off`, `shadow`, `compare`. `authoritative` is hard-refused. |
| `BRAIN_MACRO_REGIME_OPS_LOG_ENABLED` | `true` | Emit `[macro_regime_ops]` one-line events. |
| `BRAIN_MACRO_REGIME_CRON_HOUR` | `6` | Daily sweep cron hour. |
| `BRAIN_MACRO_REGIME_CRON_MINUTE` | `30` | Daily sweep cron minute. |
| `BRAIN_MACRO_REGIME_MIN_COVERAGE_SCORE` | `0.5` | Skip persistence if fewer than this fraction of ETFs returned usable bars. |
| `BRAIN_MACRO_REGIME_TREND_UP_THRESHOLD` | `0.01` | 20d momentum threshold for `trend_up` / `trend_down`. |
| `BRAIN_MACRO_REGIME_STRONG_TREND_THRESHOLD` | `0.03` | 20d momentum threshold for "strong" (composite score amplification). |
| `BRAIN_MACRO_REGIME_PROMOTE_THRESHOLD` | `0.35` | Composite score above which the sub-regimes collapse to `risk_on`; below negation -> `risk_off`; otherwise `cautious`. |
| `BRAIN_MACRO_REGIME_WEIGHT_RATES` | `0.45` | Weight of the rates score in the composite. |
| `BRAIN_MACRO_REGIME_WEIGHT_CREDIT` | `0.35` | Weight of the credit score in the composite. |
| `BRAIN_MACRO_REGIME_WEIGHT_USD` | `0.20` | Weight of the USD score in the composite. |
| `BRAIN_MACRO_REGIME_LOOKBACK_DAYS` | `14` | Default lookback for the diagnostics endpoint. |

## Operations

- **Daily sweep** - `scheduler-worker` runs `macro_regime_daily` at
  the configured cron time. Failures are logged (not raised) to avoid
  killing the scheduler.
- **Diagnostics** - `GET /api/trading/brain/macro-regime/diagnostics?lookback_days=14`
  returns the frozen summary shape:
  - `mode`, `lookback_days`, `snapshots_total`
  - `by_label` (risk_on / cautious / risk_off counts)
  - `by_rates_regime`, `by_credit_regime`, `by_usd_regime`
  - `mean_coverage_score`, `latest_snapshot`
- **Ops logging** - every compute, persist, skip, and refuse event is
  emitted as a single `[macro_regime_ops] event=...` line.

## Tests

- **Pure unit tests** - `tests/test_macro_regime_model.py` (17 tests:
  regime_id determinism, classify_trend thresholds, composite labels,
  partial coverage, missing readings, duplicate symbols, payload
  stability, equity fields echo).
- **API smoke** - `tests/test_phase_l17_diagnostics.py` (frozen shape
  + lookback clamp 422).
- **Docker soak** - `scripts/phase_l17_soak.py` (44 checks covering
  schema, settings, pure model green/yellow/red, persistence, off
  no-op, authoritative refusal, coverage gate, determinism,
  append-only, summary shape, additive-only guard on
  `get_market_regime()`).
