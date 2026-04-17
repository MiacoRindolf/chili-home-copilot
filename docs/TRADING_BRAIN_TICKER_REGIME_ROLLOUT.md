# Phase L.20 — Per-ticker mean-reversion vs trend regime rollout

Status: **shadow-only (L.20.1)**. Phase L.20.2 opens the authoritative
consumer path explicitly, gated on governance approval + operator
review of per-ticker regime stability (AC(1) / VR / Hurst / Kaufman
efficiency ratio) vs realised P&L by pattern family (trend-following
vs mean-reversion strategies).

## What L.20.1 ships

Additive, read-only observability of a per-ticker daily regime label
over the snapshot-universe (scan + watchlist), bounded by
`BRAIN_TICKER_REGIME_MAX_TICKERS` (default 250):

1. New append-only table `trading_ticker_regime_snapshots`
   (migration `141_ticker_regime_snapshot`).
2. New ORM model `TickerRegimeSnapshot` (`app/models/trading.py`).
3. Pure classifier `app/services/trading/ticker_regime_model.py`:
   - Lag-1 autocorrelation of log-returns (`ac1`)
   - Classical Lo-MacKinlay variance ratios with **non-overlapping
     windows** (`vr_5`, `vr_20`)
   - Hurst exponent via rescaled range (R/S)
   - **Kaufman efficiency ratio** scaled to 0-100
     (`adx_proxy` — replaces the earlier ATR-normalised drift proxy;
     the existing `momentum_neural.hurst_proxy_from_closes` helper is
     not touched)
   - 20-day realised volatility (`sigma_20d`) + 20-day cumulative
     log-return as a directional tiebreaker
   - Trend / mean-revert score counters (`trend_score`,
     `mean_revert_score`) — efficiency ratio weighted 2.0
   - ADX-gated composite label in
     `{trend_up, trend_down, mean_revert, choppy, neutral}` with
     numeric `{1, -1, 0, 0, 0}` codes.
4. DB service `app/services/trading/ticker_regime_service.py`:
   `compute_and_persist_sweep`, `ticker_regime_summary`,
   `get_latest_snapshot_for_ticker`. Universe is loaded via
   `build_snapshot_ticker_universe` and OHLCV via `fetch_ohlcv_df`.
5. APScheduler job `ticker_regime_daily` (07:15 local,
   gated by `BRAIN_TICKER_REGIME_MODE`).
6. Diagnostics endpoint
   `GET /api/trading/brain/ticker-regime/diagnostics`
   (frozen shape; keys listed below) with
   `lookback_days` (1–30) and `latest_tickers_limit` (1–200) clamps.
7. Structured ops log `[ticker_regime_ops] event=...` with events
   `ticker_regime_computed`, `ticker_regime_persisted`,
   `ticker_regime_skipped`, `ticker_regime_refused_authoritative`,
   `ticker_regime_sweep_summary`.
8. Release-blocker script
   `scripts/check_ticker_regime_release_blocker.ps1`.
9. Docker soak `scripts/phase_l20_soak.py`
   (run inside the `chili` container; asserts 55 invariants including
   L.17/L.18/L.19 additive-only guards and deterministic `snapshot_id`
   across duplicate sweeps).

### Frozen `ticker_regime_summary` shape

```
{
  "mode": "off" | "shadow" | "compare" | "authoritative",
  "lookback_days": int,
  "snapshots_total": int,
  "distinct_tickers": int,
  "by_ticker_regime_label": {
    "trend_up": int,
    "trend_down": int,
    "mean_revert": int,
    "choppy": int,
    "neutral": int
  },
  "by_asset_class": { "<asset_class>": int, ... },
  "mean_coverage_score": float,
  "mean_trend_score": float,
  "mean_mean_revert_score": float,
  "latest_tickers": [ { ...per-ticker snapshot dict... }, ... ]
}
```

The diagnostics endpoint wraps this in
`{"ok": true, "ticker_regime": {...}}`.

### Composite label logic (deliberately asymmetric)

The Kaufman efficiency ratio is the most principled single-feature
trend detector, so it acts as a **hard gate**:

1. `adx_proxy >= adx_trend` (default 20) **and** `cum_logret_20d != 0`
   → `trend_up` / `trend_down`.
2. `mean_revert_score >= 2` **and**
   `mean_revert_score > trend_score` **and** `adx_proxy` below the
   trend floor → `mean_revert`.
3. Both scores zero **and** `adx_proxy` absent or below trend floor
   → `neutral`.
4. Otherwise → `choppy`.

Rows whose `coverage_score` is below
`BRAIN_TICKER_REGIME_MIN_COVERAGE_SCORE` (default 0.5) are **still
persisted** (for ops visibility and low-liquidity discovery) but are
excluded from the per-label rollup returned to the sweep-summary ops
line.

## Release-blocker pattern (mandatory)

A line is a blocker if it contains `[ticker_regime_ops]` **and**
either:

- `event=ticker_regime_persisted` **and** `mode=authoritative`
- `event=ticker_regime_refused_authoritative`

Phase L.20.1 is shadow-only; an authoritative persist in deploy logs
means config drift has bypassed governance. The gate also fails on
`mean_coverage_score < MinCoverageScore` or
`snapshots_total < MinSnapshots` when a diagnostics dump is provided
via `-DiagnosticsJson`.

### Commands

```powershell
# Against live container logs
docker compose logs chili scheduler-worker brain-worker --since 30m 2>&1 |
  Out-File -Encoding utf8 tr_logs.txt
.\scripts\check_ticker_regime_release_blocker.ps1 -Path tr_logs.txt

# Against a diagnostics dump
curl.exe -sk "https://localhost:8000/api/trading/brain/ticker-regime/diagnostics?lookback_days=14" -o tr.json
.\scripts\check_ticker_regime_release_blocker.ps1 `
  -DiagnosticsJson .\tr.json -MinCoverageScore 0.5 -MinSnapshots 1
```

Exit 0 = pass; Exit 1 = blocker.

## Rollout order (explicit; do not improvise)

Matches the canonical shadow-rollout pattern used by L.17 / L.18 /
L.19:

1. **off -> shadow** (L.20.1 — this phase):
   - Set `BRAIN_TICKER_REGIME_MODE=shadow` in `.env`.
   - `docker compose up -d --force-recreate chili brain-worker scheduler-worker`.
   - Verify `Ticker regime daily (07:15; mode=shadow)` in
     `scheduler-worker` logs.
   - Verify `/api/trading/brain/ticker-regime/diagnostics` returns
     `mode: "shadow"`.
   - Run release blocker on live logs — expect exit 0.
2. **shadow -> compare** (L.20.2 plan): add the parity writer that
   compares daily per-ticker regime label against a known-good
   external source (e.g. academic mean-reversion / trend classifiers
   or vendor regime tags) across the scan universe for a minimum of N
   trading days before opening authoritative.
3. **compare -> authoritative** (L.20.2 hard step): only after
   governance sign-off; the service's `RuntimeError` guard only
   starts meaning something once the flag is authoritative.

## Rollback

Reverse the flip:

1. Set `BRAIN_TICKER_REGIME_MODE=off` in `.env`.
2. `docker compose up -d --force-recreate chili brain-worker scheduler-worker`.
3. Verify `/api/trading/brain/ticker-regime/diagnostics` now reports
   `mode: "off"` and the scheduler log no longer registers the
   `ticker_regime_daily` job.
4. Re-run the release blocker against a fresh 30m log slice (expect
   exit 0 still; rollback is not a blocker).

The table `trading_ticker_regime_snapshots` is **append-only and safe
to retain** on rollback. Hard reset is
`TRUNCATE trading_ticker_regime_snapshots` — no other phase depends
on these rows.

## Additive-only guarantees

- `momentum_neural.hurst_proxy_from_closes` is **not modified**.
  L.20 introduces an independent `_hurst_rs` implementation inside
  `ticker_regime_model.py`; no consumers of the existing helper
  change.
- Phase L.17's `trading_macro_regime_snapshots`, L.18's
  `trading_breadth_relstr_snapshots`, and L.19's
  `trading_cross_asset_snapshots` rows are **not written, updated, or
  deleted** by L.20. The soak script asserts pre/post counts are
  identical around an L.20 write for all three tables.
- Existing scheduler jobs retain their cron slots (prescreen 02:00,
  scan 02:30, divergence 06:15, macro 06:30, breadth+RS 06:45,
  cross-asset 07:00); L.20's new slot is 07:15 to avoid collisions.
- `build_snapshot_ticker_universe` is read-only from L.20's
  perspective; the sweep calls it with `limit=max_tickers` but never
  mutates scan/watchlist state.

## Verification bundle (Phase L.20.1 sign-off)

- Migration 141 applied: `schema_version` shows
  `141_ticker_regime_snapshot`. ✓
- Pure unit tests: `tests/test_ticker_regime_model.py` — 22/22 green
  (Kaufman efficiency ratio + non-overlapping VR + ADX-gated
  composite). ✓
- API smoke tests: `tests/test_phase_l20_diagnostics.py` —
  diagnostics frozen shape + `lookback_days` clamp 422 +
  `latest_tickers_limit` clamp 422. ✓
- Release-blocker smoke tests (5/5): clean, auth-persist, refused,
  diag-ok, diag-below-coverage. ✓
- Docker soak: `scripts/phase_l20_soak.py` inside the `chili`
  container — 55/55 checks green (includes L.17 + L.18 + L.19
  additive-only guards, deterministic `snapshot_id` across duplicate
  sweeps, low-coverage persisted-but-excluded from rollup). ✓
- Live scheduler registration confirmed:
  `Ticker regime daily (07:15; mode=shadow)`. ✓
- Live diagnostics confirms `mode=shadow` after `.env` flip. ✓
- Release blocker on live logs after flip: zero
  `[ticker_regime_ops]` blocker lines. ✓
- scan_status frozen contract: unchanged (live probe green). ✓
- L.17 / L.18 / L.19 / L.20 pure tests: 17 + 20 + 22 + 22 = 81/81 —
  no regression. ✓

## L.20.2 pre-flight checklist (not yet opened)

Do **not** open L.20.2 without all of the following:

1. User supplies the explicit authoritative consumer path
   (who reads `ticker_regime_label` / `ticker_regime_numeric`? under
   which pattern authority? what does `trend_up` / `trend_down` /
   `mean_revert` / `choppy` / `neutral` cause — enable / disable
   which strategy families, size up / down, which sentinel event
   flips it back?).
2. A parity comparison window with an external or historical-backtest
   source (minimum 20 trading days) in `compare` mode, with drift
   bounds agreed (e.g. composite label disagreement <= 15%,
   false-trend rate on random walks <= 5%).
3. A governance gate is wired so flipping
   `BRAIN_TICKER_REGIME_MODE=authoritative` triggers an audit
   log and optional approval requirement (matches L.17.2 / L.18.2 /
   L.19.2 pattern).
4. A backfill job (or explicit decision not to backfill) for the
   history window L.20.2's consumers need; includes a universe
   migration plan if `BRAIN_TICKER_REGIME_MAX_TICKERS` needs to grow
   past 250.
5. Re-run the full release-blocker + soak bundle after the
   authoritative flip.

Until those are in place, the service hard-refuses
`authoritative` with a `RuntimeError` and logs
`event=ticker_regime_refused_authoritative` for visibility.
