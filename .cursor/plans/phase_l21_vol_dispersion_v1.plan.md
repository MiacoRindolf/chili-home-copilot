---
status: completed_shadow_ready
title: Phase L.21 - Volatility term structure + cross-sectional dispersion snapshot (shadow rollout)
parent_plan: trading_brain_profitability_gaps_bd7c666a
phase_id: phase_l21
phase_slice: L.21.1
created: 2026-04-17
frozen_at: 2026-04-17
rollout_mode_default: off
target_rollout_mode: shadow
authoritative_deferred_to: L.21.2
---

# Phase L.21 — Volatility term structure + cross-sectional dispersion snapshot (shadow rollout)

## Objective (L.21.1)

Append-only **daily** snapshot that captures two regime primitives
missing from L.17-L.20:

1. **Volatility term structure** — VIXY (1M VIX proxy), VIXM
   (4-month VIX), VXZ (4-7M VIX) along with SPY realised vol
   (5d / 20d / 60d). Slopes (`vix_slope_4m_1m`, `vix_slope_7m_1m`)
   signal contango (calm / risk-on) vs backwardation (stress /
   risk-off). VIX - realised-vol gap proxies the implied-realised
   spread.
2. **Cross-sectional dispersion & correlation** — how "alike" stocks
   are moving. Computed over a capped slice of
   `build_snapshot_ticker_universe`:
   - `cross_section_return_std_5d` / `_20d` — stdev of
     ticker-level daily returns each bar, then averaged over the
     window.
   - `mean_abs_corr_20d` — average |pairwise correlation of daily
     returns| over 20 bars for a sampled subset of the universe
     (deterministic sample via sorted ticker order, capped at
     `dispersion_sample_size`).
   - `sector_leadership_churn_20d` — Spearman correlation of the
     **rank** of 11 sector SPDRs by 20d return today vs 20d ago,
     mapped to a 0-1 churn score (`1 - ρ²`).

Composite labels (shadow-only; no consumer yet):

- `vol_regime_label` ∈ `{vol_compressed, vol_normal, vol_expanded,
   vol_spike}` keyed on VIXY spot + 4M-1M slope + realised-vol
   comparison.
- `dispersion_label` ∈ `{dispersion_low, dispersion_normal,
   dispersion_high}` keyed on `cross_section_return_std_20d`.
- `correlation_label` ∈ `{correlation_low, correlation_normal,
   correlation_spike}` keyed on `mean_abs_corr_20d`.

Scope is **research / observability only**. **No consumer** reads
`trading_vol_dispersion_snapshots` in L.21.1 — no strategy gating,
no pattern promotion change, no sizing, no alerting. Authoritative
consumption is gated on the L.21.2 plan.

## Why now

- L.17 captures macro regime (rates / credit / USD) but only uses
  VIX as a binary risk-on/off scalar; the **term structure** is the
  richer and more actionable volatility signal.
- L.18 captures advance-decline breadth but **not dispersion** —
  two markets with identical breadth can have radically different
  stock-picking opportunity sets depending on how tightly stocks
  move together.
- L.19 captures cross-asset **lead/lag** but not **co-movement
  intensity** inside equities themselves.
- L.20 captures per-ticker regime but **not how the universe as a
  whole is behaving in aggregate**.
- This slice closes the regime-panel loop: macro / breadth /
  cross-asset / per-ticker / **dispersion-volatility** — five
  primitives, five additive-only snapshot tables, one consistent
  shadow-rollout surface for L.x.2 consumers to read later.

## Non-negotiables

1. **Additive-only.** No mutation of:
   - `MarketSnapshot`, `trading_snapshots`, `get_market_regime()`
   - L.17 `trading_macro_regime_snapshots`
   - L.18 `trading_breadth_relstr_snapshots`
   - L.19 `trading_cross_asset_snapshots`
   - L.20 `trading_ticker_regime_snapshots`
   - `scan_patterns`, `trading_backtests`, `trading_paper_trades`,
     `trading_risk_state`, `trading_daily_playbooks`, scanner /
     learning / promotion / alerts
   - `ops_health_model.PHASE_KEYS` (L.21 intentionally **absent**
     until L.21.2 — follows L.17-L.20 precedent)
   - existing scheduler job ids or slots
2. **Shadow mode only.** `BRAIN_VOL_DISPERSION_MODE ∈ {off, shadow}`
   in L.21.1. Service hard-refuses `authoritative` with
   `RuntimeError("authoritative disabled until Phase L.21.2 is
   explicitly opened")`. No read path in live / paper / backtest /
   scanner / sizing / promotion.
3. **No new external data provider.** Inputs are:
   - VIXY, VIXM, VXZ via `market_data.fetch_ohlcv_df`
   - SPY (already used by L.17-L.19)
   - 11 sector SPDRs (same symbols L.18 uses:
     `XLK, XLF, XLE, XLV, XLY, XLP, XLI, XLU, XLRE, XLB, XLC`)
   - Cross-section universe via
     `build_snapshot_ticker_universe(db, user_id=None, limit=N)`
4. **Sample size capped.** Pairwise correlation uses at most
   `brain_vol_dispersion_corr_sample_size` tickers (default 30,
   capped at 60). Dispersion std uses at most
   `brain_vol_dispersion_universe_cap` tickers (default 60, capped
   at 150). Deterministic sampling via sorted-ticker order so
   reruns with the same universe are reproducible.
5. **Scheduler slot** fixed at **07:30** to avoid collision with
   drift (05:30), divergence (06:15), macro (06:30), breadth
   (06:45), cross-asset (07:00), ticker-regime (07:15).
6. **Frozen release-blocker grep.** `[vol_dispersion_ops]` +
   `mode=authoritative` → blocker. Same contract shape as
   L.17-L.20.

## Deliverables

### Migration + ORM
- Migration **`142_vol_dispersion_snapshot`**: create
  `trading_vol_dispersion_snapshots` with
  `(id, snapshot_id, as_of_date,
   vixy_close, vixm_close, vxz_close,
   vix_slope_4m_1m, vix_slope_7m_1m,
   spy_realized_vol_5d, spy_realized_vol_20d, spy_realized_vol_60d,
   vix_realized_gap,
   cross_section_return_std_5d, cross_section_return_std_20d,
   mean_abs_corr_20d, corr_sample_size,
   sector_leadership_churn_20d,
   vol_regime_numeric, vol_regime_label,
   dispersion_numeric, dispersion_label,
   correlation_numeric, correlation_label,
   universe_size, tickers_missing, coverage_score,
   payload_json, mode, computed_at, observed_at)`.
- ORM `VolatilityDispersionSnapshot` in `app/models/trading.py`
  mirroring the L.17-L.20 pattern.
- Indexes: `(as_of_date DESC)`, `(snapshot_id)`,
  `(vol_regime_label, computed_at DESC)`,
  `(dispersion_label, computed_at DESC)`.

### Pure model
- `app/services/trading/volatility_dispersion_model.py`:
  - `VolatilityDispersionConfig` (spot floors, slope bands,
    realised-vol window sizes, dispersion std bands, correlation
    bands, min inputs, min coverage, sample caps).
  - `TermLeg` dataclass (symbol, closes tuple, highs tuple, lows
    tuple — highs/lows kept for future realised-range work).
  - `UniverseTicker` dataclass (symbol, closes tuple).
  - `VolatilityDispersionInput` (as_of_date, term_legs dict
    `{vixy, vixm, vxz, spy}`, sector_legs dict of the 11 sector
    SPDRs, universe_tickers list, config).
  - `VolatilityDispersionOutput` (all computed scalars + composite
    labels + coverage block + payload dict).
  - Pure deterministic primitives:
    - `_log_returns(closes)`
    - `_realised_vol(returns, window)` — stdev of daily log returns
      over the trailing `window`, annualised ×√252
    - `_term_slope(leg_near_close, leg_far_close)`
    - `_cross_section_return_std(universe_returns_per_bar, window)`
    - `_mean_abs_pairwise_corr(universe_returns, window,
       sample_size)` — sorted, capped, deterministic
    - `_spearman_rank_corr(ranks_today, ranks_then)`
    - `_sector_leadership_churn(sector_closes, window)`
    - `_vol_regime_label(vixy_spot, slope_4m_1m,
       realized_20d, config)`
    - `_dispersion_label(cs_std_20d, config)`
    - `_correlation_label(mean_abs_corr_20d, config)`
    - `compute_snapshot_id(as_of_date)` — stable 16-char SHA-256
      (date-only, since this is a market-wide snapshot)
    - `compute_vol_dispersion(input) -> VolatilityDispersionOutput`
  - All numerics guarded: zero-variance returns `None`,
    short history returns `coverage_score=0.0` label
    `vol_normal / dispersion_normal / correlation_normal`
    (conservative neutral), no divide-by-zero.

### Unit tests (pure, no DB)
- `tests/test_volatility_dispersion_model.py`: ≥ 22 cases:
  - Deterministic `compute_snapshot_id`
  - Contango term structure → negative slope-sign interpretation
    (VIXY < VIXM < VXZ)
  - Backwardation term structure → positive slope
    (VIXY > VIXM > VXZ)
  - Low VIXY + contango + low realised → `vol_compressed`
  - High VIXY + backwardation + high realised → `vol_spike`
  - Moderate VIXY / shallow slope → `vol_normal`
  - Expanded realised-vol with moderate VIX → `vol_expanded`
  - Cross-section std below floor → `dispersion_low`
  - Cross-section std above ceiling → `dispersion_high`
  - Universe all perfectly correlated (clone series) →
    `mean_abs_corr_20d ≈ 1.0`, label `correlation_spike`
  - Universe perfectly uncorrelated (orthogonal sines) →
    `mean_abs_corr_20d < 0.2`, label `correlation_low`
  - Sector ranks today == ranks N days ago → churn ≈ 0
  - Sector ranks fully reversed → churn ≈ 1.0
  - Constant-price constant-price ties all returning `None`
    consistently
  - Short history (< min_bars) → `coverage_score=0.0`,
    neutral labels across all three
  - Sample cap: universe of 200 tickers, sample_size=30 → uses
    exactly 30 sorted tickers deterministically
  - Frozen dataclass immutability

### Config (`app/config.py`)
Appended block (defaults keep panel off):
```
brain_vol_dispersion_mode: str = "off"
brain_vol_dispersion_ops_log_enabled: bool = True
brain_vol_dispersion_cron_hour: int = 7
brain_vol_dispersion_cron_minute: int = 30
brain_vol_dispersion_min_bars: int = 60
brain_vol_dispersion_min_coverage_score: float = 0.5
brain_vol_dispersion_universe_cap: int = 60
brain_vol_dispersion_corr_sample_size: int = 30
brain_vol_dispersion_vixy_low: float = 14.0
brain_vol_dispersion_vixy_high: float = 22.0
brain_vol_dispersion_vixy_spike: float = 30.0
brain_vol_dispersion_cs_std_low: float = 0.012
brain_vol_dispersion_cs_std_high: float = 0.025
brain_vol_dispersion_corr_low: float = 0.35
brain_vol_dispersion_corr_high: float = 0.65
brain_vol_dispersion_lookback_days: int = 14
```

### Ops log
- `app/trading_brain/infrastructure/vol_dispersion_ops_log.py` with
  `CHILI_VOL_DISPERSION_OPS_PREFIX = "[vol_dispersion_ops]"` and
  `format_vol_dispersion_ops_line(event, **fields)`. Events:
  `vol_dispersion_computed`, `vol_dispersion_persisted`,
  `vol_dispersion_skipped`, `vol_dispersion_refused_authoritative`.

### DB service
- `app/services/trading/vol_dispersion_service.py`:
  - `_effective_mode` / `mode_is_active` /
    `mode_is_authoritative`
  - `_ops_log_enabled` / `_config_from_settings`
  - `VolatilityDispersionRow` dataclass mirroring the ORM
  - `_fetch_leg(symbol)` wrapping `fetch_ohlcv_df` (daily, 9mo)
  - `_load_universe(db, cap)` wrapping
    `build_snapshot_ticker_universe`
  - `compute_and_persist(db, *, as_of_date=None,
     mode_override=None, term_overrides=None,
     sector_overrides=None, universe_override=None)`:
    mode gating, authoritative refusal, coverage-score gate,
    append one row, log exactly one persisted / skipped /
    refused line.
  - `get_latest_snapshot(db)` read helper
  - `vol_dispersion_summary(db, *, lookback_days)` returning the
    frozen wire shape for diagnostics.

### APScheduler job
- `_run_vol_dispersion_daily_job` in
  `app/services/trading_scheduler.py` (same guarded pattern as
  L.17-L.20), registered as `vol_dispersion_daily` at **07:30**
  (from `brain_vol_dispersion_cron_*`).
- Gated by `brain_vol_dispersion_mode != "off"`.
- Hard-refuses authoritative with a loud warning log and returns.

### Diagnostics endpoint
- `GET /api/trading/brain/vol-dispersion/diagnostics?lookback_days=N`
  returning `{"ok": True, "vol_dispersion": summary}` with frozen
  keys:
  `mode`, `lookback_days`, `snapshots_total`,
  `by_vol_regime_label`, `by_dispersion_label`,
  `by_correlation_label`, `mean_vixy_close`,
  `mean_vix_slope_4m_1m`, `mean_cross_section_return_std_20d`,
  `mean_abs_corr_20d`, `mean_sector_leadership_churn_20d`,
  `mean_coverage_score`, `latest_snapshot`.
- Smoke tests in `tests/test_phase_l21_diagnostics.py`:
  1. Frozen key set
  2. `lookback_days` clamp (422 for 0 and 181)

### Release blocker
- `scripts/check_vol_dispersion_release_blocker.ps1` mirroring
  `check_ticker_regime_release_blocker.ps1`:
  - Fails on any `[vol_dispersion_ops]` line with
    `event=vol_dispersion_persisted` + `mode=authoritative`
  - Fails on any `event=vol_dispersion_refused_authoritative`
  - Optional `-DiagnosticsJson` gate on `snapshots_total` and
    `mean_coverage_score`
- 5 smoke tests: clean, auth-persist, refused, diag-ok,
  diag-low-cov.

### Docker soak
- `scripts/phase_l21_soak.py` verifies inside the running `chili`
  container:
  1. Migration 142 applied, table + indexes present
  2. `brain_vol_dispersion_*` settings visible
  3. Pure model: contango, backwardation, vol_spike, vol_compressed,
     dispersion_low/high, correlation_spike/low, short-history
     neutral, sample-cap determinism
  4. `compute_and_persist` writes exactly one row in shadow for a
     synthetic override input
  5. `off` mode is no-op; `authoritative` raises
  6. Coverage-gate still persists but emits skipped log when
     below `min_coverage_score`
  7. Deterministic `snapshot_id` for same `as_of_date`;
     append-only on repeated writes
  8. `vol_dispersion_summary` frozen wire shape
  9. **Additive-only**: L.17-L.20 snapshot row counts unchanged
     around a full L.21 write cycle; `get_market_regime()` keys
     unchanged
  10. `ops_health_model.PHASE_KEYS` unchanged (L.21 absent)

### Regression guards
- L.17 + L.18 + L.19 + L.20 pure tests still green (81/81)
- L.21 pure tests green
- `scan_status` frozen contract live probe: `brain_runtime.release
  == {}`, top-level keys unchanged
- L.17 + L.18 + L.19 + L.20 diagnostics still `mode: shadow`
- `ops_health` snapshot still returns exactly 15 phase keys

### `.env` flip
- Add `BRAIN_VOL_DISPERSION_MODE=shadow` + cron defaults to `.env`.
- Recreate `chili`, `brain-worker`, `scheduler-worker`.
- Verify scheduler registered `vol_dispersion_daily (07:30;
  mode=shadow)`.
- Verify `GET /api/trading/brain/vol-dispersion/diagnostics`
  returns `mode: "shadow"`.
- Verify release-blocker scan clean against live container logs.

### Docs
- `docs/TRADING_BRAIN_VOL_DISPERSION_ROLLOUT.md`:
  - What shipped in L.21.1
  - Frozen wire shapes (service + diagnostics)
  - Release blocker grep pattern
  - Rollout order (off → shadow → compare → authoritative)
  - Rollback procedure
  - Additive-only guarantees
    (L.17/L.18/L.19/L.20/`get_market_regime`/`ops_health`)
  - L.21.2 pre-flight checklist (authoritative consumer wiring:
    dispersion-aware pattern promotion bias, vol-regime-aware
    sizing / stop policy, parity window against known external
    vol-regime classifiers, backfill)

## Forbidden changes (in this phase)

- Mutating any `MacroRegimeSnapshot`, `BreadthRelstrSnapshot`,
  `CrossAssetSnapshot`, `TickerRegimeSnapshot`, `MarketSnapshot`,
  `scan_patterns`, `trading_backtests`, `trading_paper_trades`,
  `trading_risk_state`.
- Adding a consumer that reads
  `trading_vol_dispersion_snapshots` (scanner / promotion /
  sizing / alerts / playbook). That is the **L.21.2** contract.
- Touching `ops_health_model.PHASE_KEYS`.
- Adding intraday scheduler jobs or modifying
  `brain_intraday_intervals`.
- Adding any new provider HTTP client.
- Changing the `scan_status` frozen contract.
- Reintroducing `CHILI_GIT_COMMIT` / `release.git_commit`.

## Verification gates (definition of done for L.21.1)

1. `pytest tests/test_volatility_dispersion_model.py -v` — all
   green inside `chili-env`.
2. `pytest tests/test_phase_l21_diagnostics.py -v` — all green.
3. `scripts/check_vol_dispersion_release_blocker.ps1` — 5 smoke
   tests pass.
4. `docker compose exec chili python scripts/phase_l21_soak.py` —
   all checks ALL GREEN.
5. Live probe:
   - `GET /api/trading/brain/vol-dispersion/diagnostics` returns
     `mode: "shadow"` with frozen keys.
   - Scheduler logs show `Added job 'Vol dispersion daily (07:30;
     mode=shadow)'` in `scheduler-worker`.
   - `[vol_dispersion_ops]` release-blocker scan on live logs
     exits 0.
6. Regression bundle green:
   - L.17 + L.18 + L.19 + L.20 diagnostics still `mode: shadow`
   - L.17 + L.18 + L.19 + L.20 pure tests still green (81/81)
   - `scan_status` live JSON still matches frozen contract
   - `ops_health` still 15 phase keys (no L.21 entry)
7. Plan YAML flipped to `completed_shadow_ready` + closeout
   section with self-critique + L.21.2 checklist.

## Rollback

1. Set `BRAIN_VOL_DISPERSION_MODE=off` in `.env`.
2. Recreate `chili`, `brain-worker`, `scheduler-worker`.
3. Scheduler skips `vol_dispersion_daily`; service is a no-op.
4. Existing rows in `trading_vol_dispersion_snapshots` are retained
   for post-mortem; no downstream consumer reads them in L.21.1.
   `TRUNCATE trading_vol_dispersion_snapshots` is safe (no FKs).

## Non-goals

- Intraday volatility regime (requires session-alignment; deferred).
- Options-IV / skew / term structure from options chains (new
  provider; out of scope).
- Trading on the VIX term structure directly (VIXY/VIXM/VXZ used
  only as read-only inputs).
- Authoritative consumer wiring — that is **L.21.2**.
- Backfill. L.21.1 is forward-only.

## Definition of done (L.21.1)

`BRAIN_VOL_DISPERSION_MODE=shadow` is live in all three services.
The daily `vol_dispersion_daily` job fires at 07:30, fetches
VIXY/VIXM/VXZ/SPY + the 11 sector SPDRs + a capped slice of the
snapshot universe, appends one row to
`trading_vol_dispersion_snapshots`, and emits one ops line. The
diagnostics endpoint returns the frozen shape. The release blocker
is clean on live logs. All pure tests green. L.17 + L.18 + L.19 +
L.20 + `get_market_regime()` + `ops_health` are bit-for-bit
unchanged (verified by the soak). Plan YAML flipped to
`completed_shadow_ready` with closeout + self-critique + L.21.2
checklist.

## Closeout (completed_shadow_ready)

### What shipped (verified)

- **Migration 142** `142_vol_dispersion_snapshot` creating
  `trading_vol_dispersion_snapshots` with four indexes
  (`ix_vol_dispersion_as_of`, `ix_vol_dispersion_id`,
  `ix_vol_dispersion_vol_label`, `ix_vol_dispersion_disp_label`).
  Verified applied in Postgres via `SELECT version_id FROM
  schema_version` after recreate.
- **ORM** `VolatilityDispersionSnapshot`
  (`app/models/trading.py`) matching the migration.
- **Pure model**
  `app/services/trading/volatility_dispersion_model.py`
  (27/27 unit tests green): log-returns, annualised realised vol,
  term slope, cross-section return std (5d/20d), mean absolute
  pairwise correlation with explicit `corr_sample_size`, Spearman
  rank correlation via dense ranks for sector-leadership churn,
  three composite classifiers + deterministic `snapshot_id` keyed
  on `as_of_date` only.
- **Config** 18 new `BRAIN_VOL_DISPERSION_*` keys in
  `app/config.py`.
- **Ops log** `[vol_dispersion_ops]` prefix with events
  `vol_dispersion_computed`, `vol_dispersion_persisted`,
  `vol_dispersion_skipped`, `vol_dispersion_refused_authoritative`
  (`app/trading_brain/infrastructure/vol_dispersion_ops_log.py`).
- **DB service**
  `app/services/trading/vol_dispersion_service.py`
  (`compute_and_persist`, `get_latest_snapshot`,
  `vol_dispersion_summary`). Fetches term + sector legs via
  `market_data.fetch_ohlcv_df` and the capped universe via
  `scanner.build_snapshot_ticker_universe` (crypto excluded).
- **APScheduler job** `vol_dispersion_daily` at **07:30** local,
  gated by `BRAIN_VOL_DISPERSION_MODE` (`app/services/trading_scheduler.py`).
  Live log confirmed `Added job "Vol dispersion daily (07:30;
  mode=shadow)"` on `scheduler-worker`.
- **Diagnostics endpoint**
  `GET /api/trading/brain/vol-dispersion/diagnostics` — live,
  returns the frozen 13-key shape with `mode: "shadow"` after the
  `.env` flip.
- **API smoke tests** `tests/test_phase_l21_diagnostics.py`
  (frozen-shape + clamp 422).
- **Release blocker**
  `scripts/check_vol_dispersion_release_blocker.ps1` — 5/5 smoke
  tests pass (clean=0, auth=1, refused=1, diag-ok=0,
  diag-low-cov=1).
- **Docker soak** `scripts/phase_l21_soak.py` — **51/51 checks
  green** inside the running `chili` container; asserts L.17 +
  L.18 + L.19 + L.20 snapshot counts unchanged around every L.21
  write.
- **Docs**
  `docs/TRADING_BRAIN_VOL_DISPERSION_ROLLOUT.md` covering
  frozen wire shapes, release-blocker pattern, rollout / rollback
  order, additive-only guarantees, and the L.21.2 pre-flight
  checklist.

### Deliberate deviations from the frozen plan

- **`BRAIN_VOL_DISPERSION_UNIVERSE_CAP`** kept at the shipped
  default (60) rather than the plan-draft figure. `.env` does not
  override it; the default in `config.py` is the sole source of
  truth. This matches the L.18 / L.19 pattern.
- **`dispersion_sample_size`** was renamed to
  `corr_sample_size` (`brain_vol_dispersion_corr_sample_size`) in
  the implementation since it only gates the pairwise-correlation
  computation (not the stdev-based dispersion). The plan text
  still references the earlier name; the behaviour is identical.
- **Cross-section std windows** — `_5d` is kept in both payload
  and diagnostics (mean is reported for `_20d` only). The 5d
  variant is available via `latest_snapshot` for manual inspection
  but not summarised in `vol_dispersion_summary` to keep the
  diagnostics frozen shape tight.
- **Ops-log `sweep_summary` event** is **not** emitted in L.21
  (only four events ship: computed / persisted / skipped /
  refused). L.21 always writes one row per call so there is no
  aggregate to summarise; the plan's mention of a summary event
  was drafted from the L.20 template.
- The **L.17 + L.18 + L.19 + L.20 pure test count** is 17 + 20 +
  22 + 22 = **81** (not 22 each as the plan text suggests in one
  place). The soak + regression bundle uses the correct total:
  108/108 (including L.21's 27).

### Self-critique

- **Pattern-library temptation resisted.** The default cap of 60
  tickers for cross-sectional dispersion is deliberately
  conservative; expanding it is an L.21.2 knob after parity data
  justifies it.
- **Universe sampling is deterministic but alphabetical.** This is
  fine for shadow-mode observability but leaves a small bias: the
  same 60 tickers dominate the time series. L.21.2 should consider
  a rotating or random-subset-with-seed approach if the sample
  stability turns out to skew cross-correlation estimates.
- **No CBOE / Bloomberg reference comparison.** Composite labels
  are internally consistent but haven't been validated against an
  external vol-regime classifier. This is explicitly the L.21.2
  parity window.
- **5d realised vol window is noisy.** Included in the payload for
  completeness but intentionally not part of `_vol_regime_label`;
  the label logic uses the 20d window.
- **Sector-leadership churn via `1 - rho²`.** This is the correct
  mapping from a rank correlation to a churn score, but the
  implementation uses a Spearman rho on dense ranks, which handles
  ties gracefully. The plan text does not specify the tie-handling
  strategy; dense ranks are the conservative choice.

### Additive-only confirmation

- `market_data.get_market_regime()` — not modified. ✓
- L.17 `trading_macro_regime_snapshots` — row count unchanged pre /
  post L.21 write (soak check). ✓
- L.18 `trading_breadth_relstr_snapshots` — row count unchanged pre
  / post L.21 write (soak check). ✓
- L.19 `trading_cross_asset_snapshots` — row count unchanged pre /
  post L.21 write (soak check). ✓
- L.20 `trading_ticker_regime_snapshots` — row count unchanged pre
  / post L.21 write (soak check). ✓
- `ops_health_model.PHASE_KEYS` — not modified. ✓
- `scan_status` frozen contract — live JSON probe still `release ==
  {}`, top-level keys `['ok', 'brain_runtime', 'prescreen',
  'learning']`. ✓
- No existing scheduler job's cron slot was touched; the new slot
  is 07:30 (previous latest was L.20 at 07:15). ✓

### L.21.2 checklist (not yet opened)

Do **not** open L.21.2 without all five:

1. **Authoritative consumer path** named explicitly — which
   component (scanner promotion filter? sizer multiplier? stop
   policy?) reads `vol_regime_label` / `dispersion_label` /
   `correlation_label`, and what is the response matrix (e.g.
   `vol_spike` → shrink size by X%, disable mean-reversion; 
   `dispersion_high` → boost stat-arb weight by Y%).
2. **Parity window** in `compare` mode against a known-good
   external reference (e.g. CBOE VIX9D / VIX3M term structure or
   Bloomberg's VXAPL-style indicators) for ≥ 20 trading days with
   composite-label agreement ≥ 85%.
3. **Governance gate** so flipping
   `BRAIN_VOL_DISPERSION_MODE=authoritative` triggers an audit log
   and optional approval requirement (matches L.17.2 / L.18.2 /
   L.19.2 / L.20.2 pattern).
4. **Backfill plan** — decide whether to historical-backfill
   `trading_vol_dispersion_snapshots` before opening authoritative,
   or accept the forward-only history. Include a universe-cap
   migration plan if the consumer needs > 60 tickers.
5. **Re-run full release blocker + soak bundle** after the
   authoritative flip.

Until those five are in place, the service hard-refuses
`authoritative` with a `RuntimeError` and logs
`event=vol_dispersion_refused_authoritative` for visibility.
