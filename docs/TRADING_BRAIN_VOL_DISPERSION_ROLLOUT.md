# Phase L.21 — Volatility term structure + cross-sectional dispersion rollout

Status: **shadow-only (L.21.1)**. Phase L.21.2 opens the authoritative
consumer path explicitly, gated on governance approval + operator
review of the vol / dispersion / correlation composites vs realised
P&L by strategy family (trend-following vs mean-reversion vs
stat-arb / pair strategies).

## What L.21.1 ships

Additive, read-only observability of three market-wide regime
primitives that L.17 / L.18 / L.19 / L.20 don't cover:

1. **VIX term structure** via VIXY / VIXM / VXZ ETFs + SPY.
   Slopes (`vix_slope_4m_1m`, `vix_slope_7m_1m`) pick up
   contango vs backwardation; SPY realised vol (5 / 20 / 60 day)
   feeds the implied-realised gap.
2. **Cross-sectional return dispersion** over a capped slice of the
   snapshot universe (default cap 60, configurable). Standard
   deviation of daily returns across tickers — low dispersion = lockstep
   (single-name alpha scarce); high dispersion = stock-picking alpha
   harvestable.
3. **Mean absolute pairwise correlation** over 20 days across a sampled
   slice of the universe (default 30 tickers, configurable).
   High absolute correlation = macro-driven, low = idiosyncratic.
4. **Sector-leadership churn** via Spearman ρ (`1 - rho**2`) between
   today's 20d-return rankings and 20d-ago rankings across 11 sector
   SPDRs (same set as L.18).

Deliverables:

1. New append-only table `trading_vol_dispersion_snapshots`
   (migration `142_vol_dispersion_snapshot`).
2. New ORM model `VolatilityDispersionSnapshot`
   (`app/models/trading.py`).
3. Pure model `app/services/trading/volatility_dispersion_model.py`:
   log-returns, annualised realised vol (`math.sqrt(252)`), term
   slope, cross-section return std (5d / 20d), Pearson-based mean
   absolute pairwise correlation with explicit `corr_sample_size`,
   Spearman rank correlation via dense ranks, three composite label
   classifiers + deterministic `snapshot_id` keyed on `as_of_date`
   only.
4. DB service `app/services/trading/vol_dispersion_service.py`:
   `compute_and_persist`, `get_latest_snapshot`, `vol_dispersion_summary`.
   Term legs and sector legs fetched via
   `market_data.fetch_ohlcv_df`; universe via
   `scanner.build_snapshot_ticker_universe` capped to
   `brain_vol_dispersion_universe_cap` and with crypto (`*-USD` /
   bare `*USD`) excluded.
5. APScheduler job `vol_dispersion_daily` (07:30 local,
   gated by `BRAIN_VOL_DISPERSION_MODE`).
6. Diagnostics endpoint
   `GET /api/trading/brain/vol-dispersion/diagnostics`
   (frozen shape; keys listed below) with `lookback_days` clamped to
   `[1, 180]`.
7. Structured ops log `[vol_dispersion_ops] event=...` with events
   `vol_dispersion_computed`, `vol_dispersion_persisted`,
   `vol_dispersion_skipped`, `vol_dispersion_refused_authoritative`.
8. Release-blocker script
   `scripts/check_vol_dispersion_release_blocker.ps1`.
9. Docker soak `scripts/phase_l21_soak.py` (run inside the `chili`
   container; asserts 51 invariants including L.17 / L.18 / L.19 /
   L.20 additive-only guards, deterministic `snapshot_id` across
   duplicate sweeps, and low-coverage rows still persisted but
   `compute_and_persist` returning `None`).

### Frozen `vol_dispersion_summary` shape

```
{
  "mode": "off" | "shadow" | "compare" | "authoritative",
  "lookback_days": int,
  "snapshots_total": int,
  "by_vol_regime_label": {
    "vol_compressed": int,
    "vol_normal": int,
    "vol_expanded": int,
    "vol_spike": int
  },
  "by_dispersion_label": {
    "dispersion_low": int,
    "dispersion_normal": int,
    "dispersion_high": int
  },
  "by_correlation_label": {
    "correlation_low": int,
    "correlation_normal": int,
    "correlation_spike": int
  },
  "mean_vixy_close": float | null,
  "mean_vix_slope_4m_1m": float | null,
  "mean_cross_section_return_std_20d": float | null,
  "mean_abs_corr_20d": float | null,
  "mean_sector_leadership_churn_20d": float | null,
  "mean_coverage_score": float | null,
  "latest_snapshot": { ...full latest row as a dict... } | null
}
```

The diagnostics endpoint wraps this in
`{"ok": true, "vol_dispersion": {...}}`.

### Composite label logic

**`vol_regime_label`** (rules evaluated in order):

1. VIXY + realised vol both missing → `vol_normal` (numeric 0).
2. VIXY ≥ `vixy_spike` **and** slope `vix_slope_4m_1m < 0`
   (backwardation) → `vol_spike` (numeric +2).
3. VIXY ≥ `vixy_high` **or** realised-vol 20d ≥ `realized_vol_high`
   → `vol_expanded` (numeric +1).
4. VIXY < `vixy_low` **and** slope positive (contango) **and**
   realised-vol 20d < `realized_vol_low` → `vol_compressed` (numeric -1).
5. Otherwise → `vol_normal`.

**`dispersion_label`**:
* `cs_std_20d < cs_std_low` → `dispersion_low` (numeric -1)
* `cs_std_20d > cs_std_high` → `dispersion_high` (numeric +1)
* otherwise → `dispersion_normal` (numeric 0)

**`correlation_label`**:
* `mean_abs_corr_20d < corr_low` → `correlation_low` (numeric -1)
* `mean_abs_corr_20d > corr_high` → `correlation_spike` (numeric +1)
* otherwise → `correlation_normal` (numeric 0)

Rows whose `coverage_score` is below
`BRAIN_VOL_DISPERSION_MIN_COVERAGE_SCORE` (default 0.5) are **still
persisted** (for ops visibility / post-mortem) but
`compute_and_persist` returns `None` to signal no confident snapshot
is available.

## Release-blocker pattern (mandatory)

A line is a blocker if it contains `[vol_dispersion_ops]` **and**
either:

- `event=vol_dispersion_persisted` **and** `mode=authoritative`
- `event=vol_dispersion_refused_authoritative`

Phase L.21.1 is shadow-only; an authoritative persist in deploy logs
means config drift has bypassed governance. The gate also fails on
`mean_coverage_score < MinCoverageScore` or
`snapshots_total < MinSnapshots` when a diagnostics dump is provided
via `-DiagnosticsJson`.

### Commands

```powershell
docker compose logs chili scheduler-worker brain-worker --since 30m 2>&1 |
  Out-File -Encoding utf8 vd_logs.txt
.\scripts\check_vol_dispersion_release_blocker.ps1 -Path vd_logs.txt

curl.exe -sk "https://localhost:8000/api/trading/brain/vol-dispersion/diagnostics?lookback_days=14" -o vd.json
.\scripts\check_vol_dispersion_release_blocker.ps1 `
  -DiagnosticsJson .\vd.json -MinCoverageScore 0.5 -MinSnapshots 1
```

Exit 0 = pass; Exit 1 = blocker.

## Rollout order (explicit; do not improvise)

Matches the canonical shadow-rollout pattern used by L.17 – L.20:

1. **off → shadow** (L.21.1 — this phase):
   - Set `BRAIN_VOL_DISPERSION_MODE=shadow` in `.env`.
   - `docker compose up -d --force-recreate chili brain-worker scheduler-worker`.
   - Verify `Vol dispersion daily (07:30; mode=shadow)` in
     `scheduler-worker` logs.
   - Verify `/api/trading/brain/vol-dispersion/diagnostics` returns
     `mode: "shadow"`.
   - Run release blocker on live logs — expect exit 0.
2. **shadow → compare** (L.21.2 plan): add a parity writer
   that compares daily vol / dispersion / correlation composites
   against a known-good external source (e.g. CBOE VIX / VIX9D /
   VIX3M term structure, TRI/R² cross-sectional benchmarks) over a
   minimum window of N trading days before opening authoritative.
3. **compare → authoritative** (L.21.2 hard step): only after
   governance sign-off. The service's `RuntimeError` guard only
   starts meaning something once the flag is authoritative.

## Rollback

Reverse the flip:

1. Set `BRAIN_VOL_DISPERSION_MODE=off` in `.env`.
2. `docker compose up -d --force-recreate chili brain-worker scheduler-worker`.
3. Verify `/api/trading/brain/vol-dispersion/diagnostics` now reports
   `mode: "off"` and the scheduler log no longer registers the
   `vol_dispersion_daily` job.
4. Re-run the release blocker against a fresh 30m log slice (expect
   exit 0 still; rollback is not a blocker).

The table `trading_vol_dispersion_snapshots` is **append-only and
safe to retain** on rollback. Hard reset is
`TRUNCATE trading_vol_dispersion_snapshots` — no other phase depends
on these rows.

## Additive-only guarantees

- `market_data.get_market_regime()` is **not** modified.
- L.17's `trading_macro_regime_snapshots`, L.18's
  `trading_breadth_relstr_snapshots`, L.19's
  `trading_cross_asset_snapshots`, and L.20's
  `trading_ticker_regime_snapshots` are **not written, updated, or
  deleted** by L.21. The soak script asserts pre / post counts are
  identical around an L.21 write for all four tables.
- Existing scheduler jobs retain their cron slots (prescreen 02:00,
  scan 02:30, divergence 06:15, macro 06:30, breadth+RS 06:45,
  cross-asset 07:00, ticker regime 07:15); L.21's new slot is 07:30
  to avoid collisions.
- `build_snapshot_ticker_universe` is read-only from L.21's
  perspective; the sweep calls it with `limit=max(cap*3, cap+10)` but
  never mutates scan / watchlist state.

## Verification bundle (Phase L.21.1 sign-off)

- Migration 142 applied: `schema_version` shows
  `142_vol_dispersion_snapshot`. ✓
- Pure unit tests: `tests/test_volatility_dispersion_model.py` —
  27/27 green. ✓
- API smoke tests: `tests/test_phase_l21_diagnostics.py` —
  diagnostics frozen shape + `lookback_days` clamp 422. ✓
- Release-blocker smoke tests (5/5): clean, auth-persist, refused,
  diag-ok, diag-below-coverage. ✓
- Docker soak: `scripts/phase_l21_soak.py` inside the `chili`
  container — 51/51 checks green (includes L.17 + L.18 + L.19 +
  L.20 additive-only guards, deterministic `snapshot_id` across
  duplicate sweeps, low-coverage persisted-but-None). ✓
- Live scheduler registration confirmed:
  `Vol dispersion daily (07:30; mode=shadow)`. ✓
- Live diagnostics confirms `mode=shadow` after `.env` flip. ✓
- Release blocker on live logs after flip: zero
  `[vol_dispersion_ops]` blocker lines. ✓
- scan_status frozen contract: unchanged (live probe green). ✓
- L.17 / L.18 / L.19 / L.20 / L.21 pure tests: 17 + 20 + 22 + 22 +
  27 = 108/108 — no regression. ✓

## L.21.2 pre-flight checklist (not yet opened)

Do **not** open L.21.2 without all of the following:

1. User supplies the explicit authoritative consumer path
   (who reads `vol_regime_label` / `dispersion_label` /
   `correlation_label`? under which pattern authority? what does
   `vol_spike` / `dispersion_high` / `correlation_spike` cause —
   tighten stops, shrink size, disable correlation-seeking
   mean-reversion, boost dispersion-hungry stat-arb?).
2. A parity comparison window with an external or historical-backtest
   source (minimum 20 trading days) in `compare` mode, with drift
   bounds agreed (e.g. vol-regime label agreement ≥ 85% vs vendor
   VIX / VIX3M classifier; cross-sectional std within ±10% of the
   CBOE / Bloomberg reference).
3. A governance gate is wired so flipping
   `BRAIN_VOL_DISPERSION_MODE=authoritative` triggers an audit log
   and optional approval requirement (matches L.17.2 / L.18.2 /
   L.19.2 / L.20.2 pattern).
4. A backfill job (or explicit decision not to backfill) for the
   history window L.21.2's consumers need; includes a universe
   migration plan if `BRAIN_VOL_DISPERSION_UNIVERSE_CAP` needs to
   grow past 60.
5. Re-run the full release-blocker + soak bundle after the
   authoritative flip.

Until those are in place, the service hard-refuses `authoritative`
with a `RuntimeError` and logs
`event=vol_dispersion_refused_authoritative` for visibility.
