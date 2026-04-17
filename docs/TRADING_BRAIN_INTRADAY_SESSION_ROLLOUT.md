# Phase L.22 — Intraday session regime snapshot rollout

Status: **shadow-only (L.22.1)**. Phase L.22.2 opens the authoritative
consumer path explicitly, gated on governance approval + operator
review of the intraday-session composite vs realised P&L by strategy
family (opening-range breakouts vs fade-the-gap vs power-hour
momentum vs midday-compression squeeze).

## Why this phase matters

L.17 – L.21 gave the brain daily regimes (macro, breadth + RS,
cross-asset, per-ticker trend vs mean-revert, vol term structure +
dispersion). They all key off **daily** bars and say nothing about
**how the US equity session itself is trading** — which is exactly
where equity alpha is captured or bled.

L.22 closes that gap with a single post-close daily snapshot of SPY
5-minute bars capturing:

1. **Opening range** (first 30 minutes by default, configurable):
   `or_high`, `or_low`, `or_range_pct`, `or_volume_ratio` — wide
   ranges favour breakout continuations, narrow ranges favour
   consolidation breakouts later in the day.
2. **Gap dynamics**: `gap_open_pct` vs prior close, classified
   against `gap_magnitude_go` (follow-through) vs
   `gap_magnitude_fade` (reversal risk).
3. **Midday compression** (12:00 – 14:00 ET): `midday_range_pct` and
   `midday_compression_ratio` (midday range over full-session range).
   Low ratios flag "chop zones"; high ratios flag continuation of
   morning action.
4. **Power hour** (last 30 minutes): `ph_range_pct`, `ph_volume_ratio`
   (PH volume vs full-session mean), and `close_vs_or_mid_pct`
   (where close lands vs OR midpoint — the session's "verdict").
5. **Intraday realised vol** annualised from 5-minute log returns
   (`math.sqrt(78 * 252)`).
6. **Composite `session_label`** via a deterministic decision tree:
   `session_gap_and_go`, `session_gap_fade`, `session_reversal`,
   `session_trending_up`, `session_trending_down`, `session_range_bound`,
   `session_compressed`, `session_neutral`.

## What L.22.1 ships

Deliverables:

1. New append-only table `trading_intraday_session_snapshots`
   (migration `143_intraday_session_snapshot`).
2. New ORM model `IntradaySessionSnapshot`
   (`app/models/trading.py`).
3. Pure model `app/services/trading/intraday_session_model.py`:
   session-anchor extraction, opening-range / midday / power-hour
   slicers, log-return-based `intraday_rv`, the decision-tree
   classifier, and a deterministic `snapshot_id` keyed on
   `as_of_date` only.
4. DB service `app/services/trading/intraday_session_service.py`:
   `compute_and_persist`, `get_latest_snapshot`,
   `intraday_session_summary`. Bars fetched via
   `market_data.fetch_ohlcv_df(interval="5m", period=…)` for SPY
   (configurable via `BRAIN_INTRADAY_SESSION_SOURCE_SYMBOL`);
   timestamps are UTC from pandas and are converted to US/Eastern
   minute-of-day via `zoneinfo.ZoneInfo("America/New_York")`.
5. APScheduler job `intraday_session_daily` (22:00 local — post
   US-market close, gated by `BRAIN_INTRADAY_SESSION_MODE`).
6. Diagnostics endpoint
   `GET /api/trading/brain/intraday-session/diagnostics`
   (frozen shape; keys listed below) with `lookback_days` clamped to
   `[1, 180]`.
7. Structured ops log `[intraday_session_ops] event=...` with events
   `intraday_session_computed`, `intraday_session_persisted`,
   `intraday_session_skipped`, `intraday_session_refused_authoritative`.
8. Release-blocker script
   `scripts/check_intraday_session_release_blocker.ps1`.
9. Docker soak `scripts/phase_l22_soak.py` (run inside the `chili`
   container; asserts 18 invariants including L.17 + L.18 + L.19 +
   L.20 + L.21 additive-only guards, deterministic `snapshot_id`
   across duplicate sweeps, and low-coverage rows still persisted
   but `compute_and_persist` returning `None`).

### Frozen `intraday_session_summary` shape

```
{
  "mode": "off" | "shadow" | "compare" | "authoritative",
  "lookback_days": int,
  "snapshots_total": int,
  "by_session_label": {
    "session_trending_up": int,
    "session_trending_down": int,
    "session_range_bound": int,
    "session_reversal": int,
    "session_gap_and_go": int,
    "session_gap_fade": int,
    "session_compressed": int,
    "session_neutral": int
  },
  "mean_or_range_pct": float | null,
  "mean_midday_compression_ratio": float | null,
  "mean_ph_range_pct": float | null,
  "mean_intraday_rv": float | null,
  "mean_session_range_pct": float | null,
  "mean_gap_open_pct_abs": float | null,
  "mean_coverage_score": float | null,
  "latest_snapshot": { ...full latest row as a dict... } | null
}
```

The diagnostics endpoint wraps this in
`{"ok": true, "intraday_session": {...}}`.

### Composite label logic (decision tree, evaluated in order)

1. **Bars below `min_bars`** or insufficient session anchors →
   `session_neutral` (numeric 0).
2. **Gap-and-go**: `abs(gap_open_pct) ≥ gap_magnitude_go` and
   `close_vs_or_mid_pct` same sign as `gap_open_pct` with
   `abs(close_vs_or_mid_pct) ≥ trending_close_threshold` →
   `session_gap_and_go` (numeric +3 if up, −3 if down).
3. **Gap-fade**: `abs(gap_open_pct) ≥ gap_magnitude_fade` and
   `close_vs_or_mid_pct` sign **opposite** to `gap_open_pct` with
   `abs(close_vs_or_mid_pct) ≥ reversal_close_threshold` →
   `session_gap_fade` (numeric +3 for gap-down-faded-up, −3 for
   gap-up-faded-down).
4. **Reversal**: `midday_compression_ratio ≤ midday_compression_cut`
   **and** `abs(close_vs_or_mid_pct) ≥ reversal_close_threshold`
   with sign opposite to the open-range drift → `session_reversal`
   (numeric +2 up / −2 down).
5. **Trending**: `abs(close_vs_or_mid_pct) ≥ trending_close_threshold`
   → `session_trending_up` / `session_trending_down` (numeric +1 / −1).
6. **Compressed**: `or_range_pct ≤ or_range_low` and
   `midday_compression_ratio ≤ midday_compression_cut` →
   `session_compressed` (numeric 0).
7. **Range-bound** (fallback when OR is wide but close ≈ OR mid) →
   `session_range_bound` (numeric 0).

Rows whose `coverage_score` is below
`BRAIN_INTRADAY_SESSION_MIN_COVERAGE_SCORE` (default 0.5) are **still
persisted** (for ops visibility / post-mortem) but
`compute_and_persist` returns `None` to signal no confident snapshot
is available.

## Release-blocker pattern (mandatory)

A line is a blocker if it contains `[intraday_session_ops]` **and**
either:

- `event=intraday_session_persisted` **and** `mode=authoritative`
- `event=intraday_session_refused_authoritative`

Phase L.22.1 is shadow-only; an authoritative persist in deploy logs
means config drift has bypassed governance. The gate also fails on
`mean_coverage_score < MinCoverageScore` or
`snapshots_total < MinSnapshots` when a diagnostics dump is provided
via `-DiagnosticsJson`.

### Commands

```powershell
docker compose logs chili scheduler-worker brain-worker --since 30m 2>&1 |
  Out-File -Encoding utf8 is_logs.txt
.\scripts\check_intraday_session_release_blocker.ps1 -Path is_logs.txt

curl.exe -sk "https://localhost:8000/api/trading/brain/intraday-session/diagnostics?lookback_days=14" -o is.json
.\scripts\check_intraday_session_release_blocker.ps1 `
  -DiagnosticsJson .\is.json -MinCoverageScore 0.5 -MinSnapshots 1
```

Exit 0 = pass; Exit 1 = blocker.

## Rollout order (explicit; do not improvise)

Matches the canonical shadow-rollout pattern used by L.17 – L.21:

1. **off → shadow** (L.22.1 — this phase):
   - Set `BRAIN_INTRADAY_SESSION_MODE=shadow` in `.env`.
   - `docker compose up -d --force-recreate chili brain-worker scheduler-worker`.
   - Verify `Intraday session daily (22:00; mode=shadow)` in
     `scheduler-worker` logs.
   - Verify `/api/trading/brain/intraday-session/diagnostics` returns
     `mode: "shadow"`.
   - Run release blocker on live logs — expect exit 0.
2. **shadow → compare** (L.22.2 plan): add a parity writer
   that compares daily session composites vs realised intraday P&L
   by strategy family over a minimum window of N trading days
   before opening authoritative.
3. **compare → authoritative** (L.22.2 hard step): only after
   governance sign-off. The service's `RuntimeError` guard only
   starts meaning something once the flag is authoritative.

## Rollback

Reverse the flip:

1. Set `BRAIN_INTRADAY_SESSION_MODE=off` in `.env`.
2. `docker compose up -d --force-recreate chili brain-worker scheduler-worker`.
3. Verify `/api/trading/brain/intraday-session/diagnostics` now
   reports `mode: "off"` and the scheduler log no longer registers
   the `intraday_session_daily` job.
4. Re-run the release blocker against a fresh 30m log slice (expect
   exit 0 still; rollback is not a blocker).

The table `trading_intraday_session_snapshots` is **append-only and
safe to retain** on rollback. Hard reset is
`TRUNCATE trading_intraday_session_snapshots` — no other phase
depends on these rows.

## Additive-only guarantees

- `market_data.fetch_ohlcv_df` and `get_market_regime()` are **not**
  modified.
- L.17's `trading_macro_regime_snapshots`, L.18's
  `trading_breadth_relstr_snapshots`, L.19's
  `trading_cross_asset_snapshots`, L.20's
  `trading_ticker_regime_snapshots`, and L.21's
  `trading_vol_dispersion_snapshots` are **not written, updated, or
  deleted** by L.22. The soak script asserts pre / post counts are
  identical around an L.22 write for all five tables.
- Existing scheduler jobs retain their cron slots (prescreen 02:00,
  scan 02:30, divergence 06:15, macro 06:30, breadth+RS 06:45,
  cross-asset 07:00, ticker regime 07:15, vol dispersion 07:30);
  L.22's new slot is 22:00 — deliberately post-US-close to avoid
  morning collisions and to ensure the full regular session has
  printed before the snapshot runs.
- No new data provider; 5-minute bars come from the existing
  `fetch_ohlcv_df` path (yfinance-style intraday fetch).

## Verification bundle (Phase L.22.1 sign-off)

- Migration 143 applied: `schema_version` shows
  `143_intraday_session_snapshot`. ✓
- Pure unit tests: `tests/test_intraday_session_model.py` —
  23/23 green. ✓
- API smoke tests: `tests/test_phase_l22_diagnostics.py` —
  diagnostics frozen shape + `lookback_days` clamp 422. ✓
- Release-blocker smoke tests (5/5): clean, auth-persist, refused,
  diag-ok, diag-below-coverage. ✓
- Docker soak: `scripts/phase_l22_soak.py` inside the `chili`
  container — 18/18 checks green (includes L.17 + L.18 + L.19 +
  L.20 + L.21 additive-only guards, deterministic `snapshot_id`
  across duplicate sweeps, low-coverage persisted-but-None, and
  off / authoritative mode-gate behaviour). ✓
- Live scheduler registration confirmed:
  `Intraday session daily (22:00; mode=shadow)`. ✓
- Live diagnostics confirms `mode=shadow` after `.env` flip. ✓
- Release blocker on live logs after flip: zero
  `[intraday_session_ops]` blocker lines. ✓
- scan_status frozen contract: unchanged (live probe green,
  top-level `release` absent, `brain_runtime.release == {}`). ✓
- L.17 / L.18 / L.19 / L.20 / L.21 / L.22 pure tests: 17 + 20 + 22 +
  22 + 27 + 23 = 131/131 — no regression. ✓

## L.22.2 pre-flight checklist (not yet opened)

Do **not** open L.22.2 without all of the following:

1. User supplies the explicit authoritative consumer path
   (who reads `session_label` / numeric? under which pattern
   authority? what does `session_gap_and_go` /
   `session_compressed` / `session_reversal` cause — activate
   ORB strategies, pause mean-reversion, gate power-hour sizing,
   block fade-of-gap patterns when a gap-and-go prints?).
2. A parity comparison window against an external intraday-session
   classifier (minimum 20 trading days) in `compare` mode, with
   drift bounds agreed (e.g. ORB-classification agreement ≥ 80%
   vs a rule-based reference on cached 5-minute bars).
3. A governance gate wired so flipping
   `BRAIN_INTRADAY_SESSION_MODE=authoritative` triggers an audit
   log and optional approval requirement (matches L.17.2 / L.18.2 /
   L.19.2 / L.20.2 / L.21.2 pattern).
4. A backfill decision for the history window L.22.2's consumers
   need (SPY 5-minute cache or vendor download).
5. Re-run the full release-blocker + soak bundle after the
   authoritative flip.

Until those are in place, the service hard-refuses `authoritative`
with a `RuntimeError` and logs
`event=intraday_session_refused_authoritative` for visibility.
