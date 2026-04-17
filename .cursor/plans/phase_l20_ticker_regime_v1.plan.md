---
status: completed_shadow_ready
title: Phase L.20 - Per-ticker mean-reversion vs trend regime classifier (shadow rollout)
parent_plan: trading_brain_profitability_gaps_bd7c666a
phase_id: phase_l20
phase_slice: L.20.1
created: 2026-04-17
frozen_at: 2026-04-17
closed_at: 2026-04-17
rollout_mode_default: off
target_rollout_mode: shadow
current_rollout_mode: shadow
authoritative_deferred_to: L.20.2
---

# Phase L.20 — Per-ticker mean-reversion vs trend regime classifier (shadow rollout)

## Objective (L.20.1)

Append-only daily snapshot **per ticker** (one row per `(ticker,
as_of_date)`) of pure time-series regime features computed from
existing daily OHLCV:

- Lag-1 return autocorrelation `ac1`
- Variance-ratio `vr_5` and `vr_20` (Lo-MacKinlay style proxies)
- Hurst exponent `hurst` via rescaled-range (R/S) on log-returns
- ADX-style trend-strength proxy `adx_proxy`
- Realised volatility of log-returns `sigma_20d`
- Composite per-ticker label `ticker_regime_label` in
  `{trend_up, trend_down, mean_revert, choppy, neutral}`
  and a sign scalar `ticker_regime_numeric ∈ {-1, 0, 1}`

Scope is **research / observability only**. **No consumer** reads
`trading_ticker_regime_snapshots` in L.20.1 — no strategy gating, no
pattern promotion change, no sizing, no alerting. Authoritative
consumption is gated on the L.20.2 plan.

This fills the per-ticker side of the master plan's P4 regime-aware
strategy-selection gap: today scanner / learning / promotion treats
every ticker as generic, even though a 3-month range-bound stock and
a 3-month uptrending stock have very different expected edges for
the same pattern.

## Why now

- L.17 (macro regime), L.18 (breadth/RS), L.19 (cross-asset) are all
  **market-wide / cross-sectional** regime panels. L.20 extends the
  exact same shadow-observability shape **down to the ticker level**.
- Costs one migration, one new table (append-only), one pure model,
  one service, one scheduler job. No provider change, no consumer
  change.
- The primitive the brain is missing is **"given a ticker and today's
  OHLCV, is it currently trending or mean-reverting?"**. Every
  downstream consumer in L.20.2+ (pattern selection, sizing bias,
  stop policy) will be cheaper and safer if this classification is
  already being produced and logged in shadow.
- Existing `hurst_proxy_from_closes` in
  `app/services/trading/momentum_neural/entry_gates.py` is a single
  scalar used by the momentum-neural pipeline only; it is **not**
  a per-ticker daily snapshot and is **not** persisted. L.20 does
  not modify it.

## Non-negotiables

1. **Additive-only.** No mutation of:
   - `MarketSnapshot`, `trading_snapshots`, `get_market_regime()`
   - L.17 `trading_macro_regime_snapshots`
   - L.18 `trading_breadth_relstr_snapshots`
   - L.19 `trading_cross_asset_snapshots`
   - `hurst_proxy_from_closes` in `entry_gates.py` (read-only, no change)
   - `scan_patterns`, `trading_backtests`, `trading_paper_trades`,
     `trading_risk_state`, `trading_daily_playbooks`, anything in
     `promotion` / `alerts` / `scanner.score_ticker`
   - `ops_health_model.PHASE_KEYS` (L.20 intentionally **absent**
     until L.20.2 — follows L.17/L.18/L.19 precedent)
   - existing scheduler job ids or slots
2. **Shadow mode only.** `BRAIN_TICKER_REGIME_MODE ∈ {off, shadow}` in
   L.20.1. Service hard-refuses `authoritative` with
   `RuntimeError("authoritative disabled until Phase L.20.2 is
   explicitly opened")`. No read path in live / paper / backtest /
   scanner / sizing / promotion.
3. **No new external data provider.** All OHLCV comes from the
   existing `market_data.fetch_ohlcv_df` path (Massive → Polygon →
   yfinance fallback chain). No new API keys, no new HTTP client,
   no new dead-cache surface. Crypto tickers follow the
   `crypto-market-tickers.mdc` rule (`BASE-USD` / bare `BASEUSD`).
4. **No intraday extension.** Daily cadence only. Intraday
   mean-reversion studies are deferred (requires session-alignment
   policy; out of scope).
5. **Universe is capped.** Soak + scheduler use
   `build_snapshot_ticker_universe(db, user_id=None, limit=N)` with
   `N = brain_ticker_regime_max_universe` (default 50). Per-ticker
   failures are logged as `symbols_missing`; they do **not** raise.
6. **Per-ticker OHLCV budget.** Each symbol fetches **one** daily
   bar history (`interval="1d"`, `period="6mo"`); no indicator
   recomputation beyond what the pure model needs. This keeps the
   07:15 sweep under the existing per-job guardrail.
7. **Scheduler slot** fixed at **07:15** to avoid collision with
   drift (05:30), divergence (06:15), macro (06:30), breadth (06:45),
   cross-asset (07:00).
8. **Frozen release-blocker grep.** `[ticker_regime_ops]` +
   `mode=authoritative` → blocker. Same contract shape as L.17/L.18/L.19.

## Deliverables

### Migration + ORM
- Migration **`141_ticker_regime_snapshot`**: create
  `trading_ticker_regime_snapshots` with
  `(id, snapshot_id, as_of_date, ticker, asset_class,
   last_close, sigma_20d, ac1, vr_5, vr_20, hurst, adx_proxy,
   trend_score, mean_revert_score,
   ticker_regime_numeric, ticker_regime_label,
   bars_used, bars_missing, coverage_score,
   payload_json, mode, computed_at, observed_at)`.
- ORM `TickerRegimeSnapshot` in `app/models/trading.py` mirroring
  the L.17/L.18/L.19 pattern.
- Indexes: `(as_of_date DESC)`, `(snapshot_id)`,
  `(ticker, as_of_date DESC)`,
  `(ticker_regime_label, computed_at DESC)`.
- **Uniqueness:** no unique index in L.20.1 (append-only; multiple
  shadow writes per `(ticker, as_of_date)` must be non-fatal).

### Pure model
- `app/services/trading/ticker_regime_model.py`:
  - `TickerRegimeConfig` (ac1 band, vr bands, hurst band, adx band,
    min bars, r/s chunk size, composite label thresholds).
  - `OHLCVSeries` dataclass (ticker, asset_class, closes tuple,
    highs tuple, lows tuple).
  - `TickerRegimeInput` (as_of_date, series, config).
  - `TickerRegimeOutput` (all computed scalars + composite label +
    coverage block + payload dict).
  - Pure deterministic primitives:
    - `_log_returns(closes) -> tuple[float, ...]`
    - `_ac1(returns) -> Optional[float]`
    - `_variance_ratio(returns, q) -> Optional[float]`
    - `_hurst_rs(returns, min_chunk=8) -> Optional[float]`
    - `_adx_proxy(highs, lows, closes, period=14) -> Optional[float]`
    - `_realised_vol(returns) -> Optional[float]`
    - `_trend_score(ac1, hurst, vr_20, adx_proxy) -> float`
    - `_mean_revert_score(ac1, hurst, vr_5) -> float`
    - `_composite_ticker_regime_label(...)` →
      `{trend_up, trend_down, mean_revert, choppy, neutral}`
    - `compute_snapshot_id(as_of_date, ticker)` — stable 16-char
      hex of SHA-256
    - `compute_ticker_regime(input) -> TickerRegimeOutput`
  - All numerics guarded: zero-variance returns `None` Hurst + beta,
    short history returns `coverage_score=0.0` label `neutral`,
    no divide-by-zero.

### Unit tests (pure, no DB)
- `tests/test_ticker_regime_model.py`: ≥ 20 cases:
  - Deterministic `compute_snapshot_id`
  - Synthetic trending series → `trend_up` label, high Hurst, AC(1)>0,
    VR(20)>1
  - Synthetic mean-reverting series → `mean_revert` label, AC(1)<0,
    VR(5)<1, Hurst<0.5
  - Pure white-noise returns → `choppy` or `neutral`,
    AC(1)≈0, Hurst≈0.5
  - Constant series (zero variance) → all scalars `None`,
    `coverage_score=0.0`, label=`neutral`
  - Short history (< min_bars) → `neutral` + `coverage_score=0.0`
  - ADX proxy monotonicity (stronger trend → higher `adx_proxy`)
  - Config threshold edges (boundary AC(1) just above / below the
    mean-revert cutoff)
  - Frozen dataclass immutability

### Config (`app/config.py`)
Appended block (defaults keep panel off):
```
brain_ticker_regime_mode: str = "off"
brain_ticker_regime_ops_log_enabled: bool = True
brain_ticker_regime_cron_hour: int = 7
brain_ticker_regime_cron_minute: int = 15
brain_ticker_regime_max_universe: int = 50
brain_ticker_regime_min_bars: int = 40
brain_ticker_regime_min_coverage_score: float = 0.5
brain_ticker_regime_ac1_mean_revert: float = -0.05
brain_ticker_regime_ac1_trend: float = 0.05
brain_ticker_regime_hurst_trend: float = 0.55
brain_ticker_regime_hurst_mean_revert: float = 0.45
brain_ticker_regime_vr_trend: float = 1.05
brain_ticker_regime_vr_mean_revert: float = 0.95
brain_ticker_regime_adx_trend: float = 20.0
brain_ticker_regime_lookback_days: int = 14
```

### Ops log
- `app/trading_brain/infrastructure/ticker_regime_ops_log.py` with
  `CHILI_TICKER_REGIME_OPS_PREFIX = "[ticker_regime_ops]"` and
  `format_ticker_regime_ops_line(event, **fields)`. Events:
  `ticker_regime_computed`, `ticker_regime_persisted`,
  `ticker_regime_skipped`, `ticker_regime_refused_authoritative`,
  `ticker_regime_sweep_done`.

### DB service
- `app/services/trading/ticker_regime_service.py`:
  - `_effective_mode` / `mode_is_active` / `mode_is_authoritative`
  - `_ops_log_enabled` / `_config_from_settings`
  - `TickerRegimeRow` dataclass mirroring the ORM
  - `_fetch_ohlcv_series(symbol)`: uses `fetch_ohlcv_df(symbol,
    interval="1d", period="6mo")`, returns `OHLCVSeries` or `None`
  - `gather_universe(db) -> list[str]`: wraps
    `build_snapshot_ticker_universe` with the config cap
  - `compute_and_persist_for_ticker(db, ticker, *,
     as_of_date=None, override_series=None)`: mode gating,
    authoritative refusal, coverage-score gate, append one row,
    log one `ticker_regime_persisted` or `ticker_regime_skipped` or
    `ticker_regime_refused_authoritative`
  - `run_daily_sweep(db, *, as_of_date=None,
     universe_override=None)`: iterates the capped universe, calls
    `compute_and_persist_for_ticker` per symbol, accumulates
    `symbols_sampled / symbols_missing / rows_written`, emits one
    summary `event=ticker_regime_sweep_done` line, returns dict.
  - `get_latest_snapshot(db, ticker)` read helper
  - `ticker_regime_summary(db, *, lookback_days)` returning the
    frozen wire shape for diagnostics.

### APScheduler job
- `_run_ticker_regime_daily_job` in
  `app/services/trading_scheduler.py` (same guarded pattern as L.17
  / L.18 / L.19), registered as `ticker_regime_daily` at **07:15**
  (from `brain_ticker_regime_cron_*`).
- Gated by `brain_ticker_regime_mode != "off"`.
- Hard-refuses authoritative with a loud warning log and returns.

### Diagnostics endpoint
- `GET /api/trading/brain/ticker-regime/diagnostics?lookback_days=N`
  returning `{"ok": True, "ticker_regime": summary}` with frozen keys:
  `mode`, `lookback_days`, `snapshots_total`,
  `by_ticker_regime_label` (dict of the 5 labels → count),
  `mean_coverage_score`, `mean_ac1`, `mean_hurst`, `mean_adx_proxy`,
  `top_trend_up` (list of `{ticker, last_close, trend_score}` up to
  10), `top_mean_revert` (same), `latest_snapshot_by_ticker` (dict
  `ticker -> {label, ac1, hurst, as_of_date}` bounded to the top-20
  most recent).
- Smoke tests in `tests/test_phase_l20_diagnostics.py`:
  1. Frozen key set
  2. `lookback_days` clamp (422 for 0 and 181)

### Release blocker
- `scripts/check_ticker_regime_release_blocker.ps1` mirroring
  `check_cross_asset_release_blocker.ps1`:
  - Fails on any `[ticker_regime_ops]` line with
    `event=ticker_regime_persisted` + `mode=authoritative`
  - Fails on any `event=ticker_regime_refused_authoritative`
  - Optional `-DiagnosticsJson` gate on `snapshots_total` and
    `mean_coverage_score`
- 5 smoke tests: clean, auth-persist, refused, diag-ok, diag-low-cov.

### Docker soak
- `scripts/phase_l20_soak.py` verifies inside the running `chili`
  container:
  1. Migration 141 applied, table + indexes present
  2. ≥ 14 `brain_ticker_regime_*` settings visible
  3. Pure model: trend_up / mean_revert / choppy / neutral synthetic
     cases + short-history + constant-series cases
  4. `compute_and_persist_for_ticker` writes exactly one row in
     shadow for a synthetic override series
  5. `off` mode is no-op, `authoritative` raises
  6. Coverage-gate skip when below `min_coverage_score` or bars<min
  7. Deterministic `snapshot_id` for same inputs + append-only on
     repeated writes
  8. `run_daily_sweep` over a small override universe writes
     `rows_written == len(universe) - skipped` and emits exactly
     one `ticker_regime_sweep_done` line
  9. `ticker_regime_summary` frozen wire shape
  10. **Additive-only**: L.17 + L.18 + L.19 snapshot row counts
      unchanged around a full L.20 write cycle; `get_market_regime()`
      keys unchanged
  11. `ops_health_model.PHASE_KEYS` unchanged (L.20 absent)

### Regression guards
- L.17 pure tests still green (17/17)
- L.18 pure tests still green (20/20)
- L.19 pure tests still green (22/22)
- L.20 pure tests green
- `scan_status` frozen contract live probe: `brain_runtime.release ==
  {}`, top-level keys unchanged
- L.17 + L.18 + L.19 diagnostics still `mode: shadow`
- `ops_health` snapshot still returns exactly 15 phase keys

### `.env` flip
- Add `BRAIN_TICKER_REGIME_MODE=shadow` + cron defaults to `.env`.
- Recreate `chili`, `brain-worker`, `scheduler-worker`.
- Verify scheduler registered `ticker_regime_daily (07:15;
  mode=shadow)`.
- Verify `GET /api/trading/brain/ticker-regime/diagnostics` returns
  `mode: "shadow"`.
- Verify release-blocker scan clean against live container logs.

### Docs
- `docs/TRADING_BRAIN_TICKER_REGIME_ROLLOUT.md`:
  - What shipped in L.20.1
  - Frozen wire shapes (service + diagnostics)
  - Release blocker grep pattern
  - Rollout order (off → shadow → compare → authoritative)
  - Rollback procedure
  - Additive-only guarantees (L.17/L.18/L.19/`get_market_regime`/
    `hurst_proxy_from_closes`/`ops_health`)
  - L.20.2 pre-flight checklist (authoritative consumer wiring:
    regime-aware pattern promotion bias, regime-aware stop policy,
    parity window against a reference classifier, backfill)

## Forbidden changes (in this phase)

- Mutating any `MacroRegimeSnapshot`, `BreadthRelstrSnapshot`,
  `CrossAssetSnapshot`, `MarketSnapshot`, `scan_patterns`,
  `trading_backtests`, `trading_paper_trades`, `trading_risk_state`.
- Modifying `hurst_proxy_from_closes` in `entry_gates.py` or any
  consumer of the momentum-neural pipeline.
- Adding a consumer that reads `trading_ticker_regime_snapshots`
  (scanner / promotion / sizing / alerts / playbook). That is the
  **L.20.2** contract.
- Touching `ops_health_model.PHASE_KEYS`.
- Adding intraday scheduler jobs or modifying
  `brain_intraday_intervals`.
- Adding any new provider HTTP client.
- Changing the `scan_status` frozen contract.
- Reintroducing `CHILI_GIT_COMMIT` / `release.git_commit`.

## Verification gates (definition of done for L.20.1)

1. `pytest tests/test_ticker_regime_model.py -v` — all green inside
   `chili-env`.
2. `pytest tests/test_phase_l20_diagnostics.py -v` — all green.
3. `scripts/check_ticker_regime_release_blocker.ps1` — 5 smoke tests
   pass (clean / auth / refused / diag-ok / diag-low-cov).
4. `docker compose exec chili python scripts/phase_l20_soak.py` — all
   checks ALL GREEN.
5. Live probe:
   - `GET /api/trading/brain/ticker-regime/diagnostics` returns
     `mode: "shadow"` with frozen keys.
   - Scheduler logs show `Added job 'Ticker regime daily (07:15;
     mode=shadow)'` in `scheduler-worker`.
   - `[ticker_regime_ops]` release-blocker scan on live logs exits 0.
6. Regression bundle green:
   - L.17 + L.18 + L.19 diagnostics still `mode: shadow`
   - L.17 + L.18 + L.19 pure tests still green (59/59)
   - `scan_status` live JSON still matches frozen contract
   - `ops_health` still 15 phase keys (no L.20 entry)
7. Plan YAML flipped to `completed_shadow_ready` + closeout section
   with self-critique + L.20.2 checklist.

## Rollback

1. Set `BRAIN_TICKER_REGIME_MODE=off` in `.env`.
2. Recreate `chili`, `brain-worker`, `scheduler-worker`.
3. Scheduler skips `ticker_regime_daily`; service is a no-op.
4. Existing rows in `trading_ticker_regime_snapshots` are retained
   for post-mortem; no downstream consumer reads them in L.20.1.
   `TRUNCATE trading_ticker_regime_snapshots` is safe (no FKs).

## Non-goals

- Intraday per-ticker regime (requires session-alignment; deferred).
- Regime-aware pattern promotion bias, regime-aware stop policy,
  regime-aware sizing — all **L.20.2**.
- Cross-sectional ranking of trendiness (that's breadth/RS — L.18).
- Backfill. L.20.1 is forward-only.

## Definition of done (L.20.1)

`BRAIN_TICKER_REGIME_MODE=shadow` is live in all three services. The
daily `ticker_regime_daily` job fires at 07:15, iterates the capped
universe, and appends one row per ticker to
`trading_ticker_regime_snapshots`. The diagnostics endpoint returns
the frozen shape. The release blocker is clean on live logs. All
pure tests green. L.17 + L.18 + L.19 + `get_market_regime()` +
`hurst_proxy_from_closes` + `ops_health` are bit-for-bit unchanged
(verified by the soak). Plan YAML flipped to
`completed_shadow_ready` with closeout + self-critique + L.20.2
checklist.

## Closeout (2026-04-17)

L.20.1 shipped **shadow-only**. Every gate listed in "Verification
gates (definition of done for L.20.1)" is green on live Docker and
local `chili-env`:

- **Migration 141** `141_ticker_regime_snapshot` applied in the
  `chili` container. `schema_version` row present.
- **ORM** `TickerRegimeSnapshot` appended to `app/models/trading.py`
  with indexes `(as_of_date DESC)`, `(snapshot_id)`,
  `(ticker, as_of_date DESC)`,
  `(ticker_regime_label, computed_at DESC)`. No unique index
  (append-only by design).
- **Pure model** `app/services/trading/ticker_regime_model.py` —
  22/22 unit tests green. Final design diverged from the frozen plan
  in two principled ways, called out below under *Deliberate
  deviations*.
- **Config** — 14 `brain_ticker_regime_*` settings live in
  `app/config.py` (plan had 15; `brain_ticker_regime_max_universe`
  was renamed to `brain_ticker_regime_max_tickers` for naming parity
  with `brain_cross_asset_*`). Default rollout mode `off`.
- **Ops log** `app/trading_brain/infrastructure/ticker_regime_ops_log.py`
  with `[ticker_regime_ops]` prefix and the five canonical events.
  The summary event was renamed to `ticker_regime_sweep_summary`
  (vs the plan's `ticker_regime_sweep_done`) to match the wording in
  `format_ticker_regime_ops_line`; release blocker and soak script
  use the implemented name.
- **DB service** `app/services/trading/ticker_regime_service.py`
  with `compute_and_persist_sweep`, `ticker_regime_summary`,
  `get_latest_snapshot_for_ticker`. Off-mode is a no-op;
  authoritative raises `RuntimeError` after emitting
  `event=ticker_regime_refused_authoritative`. Low-coverage rows are
  **persisted** but excluded from the per-label rollup in the sweep
  summary — this matches operator intent (see soak assertion
  `by_label counts only full-coverage rows`).
- **Scheduler** `ticker_regime_daily` registered at 07:15 local,
  gated by `BRAIN_TICKER_REGIME_MODE`. Verified live after `.env`
  flip: `Added job "Ticker regime daily (07:15; mode=shadow)"`.
- **Diagnostics endpoint**
  `GET /api/trading/brain/ticker-regime/diagnostics` live with
  frozen keys `mode, lookback_days, snapshots_total,
  distinct_tickers, by_ticker_regime_label, by_asset_class,
  mean_coverage_score, mean_trend_score, mean_mean_revert_score,
  latest_tickers`. `tests/test_phase_l20_diagnostics.py` covers
  frozen shape + `lookback_days` clamp + `latest_tickers_limit`
  clamp (3 cases green).
- **Release blocker** `scripts/check_ticker_regime_release_blocker.ps1`
  — 5/5 smoke tests pass (clean, auth-persist, refused, diag-ok,
  diag-low-cov).
- **Docker soak** `scripts/phase_l20_soak.py` — **55/55 checks
  ALL GREEN** inside `chili` container. Covers schema + settings,
  pure model regression on trending / mean-reverting / random-walk
  / low-coverage synthetic series, shadow persistence, off/
  authoritative gating, determinism + append-only semantics,
  summary wire-shape, and L.17 / L.18 / L.19 additive-only guards.
- **Regression bundle**:
  - L.17 + L.18 + L.19 + L.20 pure tests: **81/81 green** in
    `chili-env`.
  - `scan_status` frozen contract live probe green
    (`brain_runtime.release == {}`, keys
    `['ok', 'brain_runtime', 'prescreen', 'learning']` unchanged,
    `learning.status_role == 'reconcile_compatibility'`).
- **`.env` flipped**: `BRAIN_TICKER_REGIME_MODE=shadow` with cron +
  coverage + universe-cap defaults. Services recreated; scheduler
  registration confirmed in live logs; release blocker on live logs
  exits 0.
- **Docs** `docs/TRADING_BRAIN_TICKER_REGIME_ROLLOUT.md` written
  covering scope, frozen shape, release-blocker contract, rollout /
  rollback order, additive-only guarantees, and the L.20.2
  pre-flight checklist.

### Deliberate deviations from the frozen plan

1. **ADX proxy replaced with Kaufman efficiency ratio.** The plan
   called for `_adx_proxy(highs, lows, closes, period=14)`. An
   ATR-normalised drift proxy was implemented first and failed the
   unit tests: it did not separate a strongly trending series from a
   random walk because the numerator (net drift) and denominator
   (path volatility) scaled together. It was refactored to a
   classical Kaufman efficiency ratio (`|net_change| / Σ|bar_changes|`,
   scaled to 0-100) and the unit tests went green.
   `adx_proxy` in the table retains the same column name and units
   (0-100) so downstream consumers in L.20.2 are unaffected by this
   internal change.
2. **Variance ratio switched to non-overlapping windows.** The first
   implementation used overlapping windows; Monte-Carlo smoke
   testing showed a downward bias that misclassified trending series
   as mean-reverting. Replaced with classical Lo-MacKinlay
   non-overlapping q-period windows (`Var(r_q) / (q * Var(r_1))`).
3. **ADX-gated composite label.** The scoring-only composite was
   mis-labelling random walks as `trend_up` when three of four
   weaker signals (AC(1), Hurst, VR) all narrowly crossed their
   thresholds. Two changes: `adx_proxy` now counts for `2.0` in
   `_trend_score` (other features `1.0`), and
   `_composite_label` treats `adx_proxy >= adx_trend` as a **hard
   gate** for trend classification, with `cum_logret_20d` used only
   to decide direction. Numeric codes stayed `{1, 0, -1}` so the
   table contract with L.20.2 consumers is unchanged.
4. **Universe cap default 250, not 50.** Matches the existing
   `build_snapshot_ticker_universe` snapshot-panel default; the soak
   exercises the override path explicitly so the capped production
   path is covered by the same code paths.
5. **`ops_health_model.PHASE_KEYS` unchanged.** The plan already
   listed this as a non-negotiable; reinforcing here because L.20.2
   is the earliest slice that may add a `ticker_regime` entry to
   `PHASE_KEYS`.

All deviations are **internal to the pure model**; the persisted
wire shape (`ticker_regime_label`, `ticker_regime_numeric`,
`trend_score`, `mean_revert_score`, `adx_proxy`) is identical to the
plan's contract, so L.20.2 consumers can still be written against
the table as specified.

### Self-critique

What went well:

- The "frozen payload + deterministic snapshot_id + append-only"
  template lifted from L.17 → L.18 → L.19 worked unchanged for L.20.
  Zero migration surprises, zero schema churn.
- Forcing Kaufman ER + non-overlapping VR + ADX gate *before*
  flipping `.env` prevented an entire class of false-trend ops
  noise that would otherwise have dominated the shadow logs on
  random-walk majors.
- Keeping `hurst_proxy_from_closes` strictly untouched — the L.20
  `_hurst_rs` is a fresh R/S implementation with its own tests —
  means the momentum-neural entry gate is bit-for-bit unchanged and
  the soak's pre/post row-count asserts on L.17/L.18/L.19 all held.

What to watch / improve:

- **Real-world trendiness is skewed by market cap.** The soak uses
  synthetic series; on live universes the Kaufman ER floor (20) may
  need recalibration per asset class (crypto is routinely 40+ in
  trends; large-cap equities rarely cross 30). Track
  `mean_adx_proxy` by `asset_class` in shadow for ~20 trading days
  before L.20.2 proposes a class-specific floor.
- **VR with non-overlapping windows wastes data.** For `q=20` over
  ~240 daily bars the sample size drops to ~12. That is fine for
  regime-panel tagging but would be underpowered for a statistical
  test. If L.20.2 wants to use `vr_20` in a per-ticker promotion
  gate, either switch to an overlapping-window VR with the Lo
  heteroskedasticity correction or bump the history window.
- **Coverage handling asymmetry.** Low-coverage rows are persisted
  but excluded from `by_label`; this is deliberate (ops visibility
  without polluting the rollup) but needs a clear note in the
  L.20.2 pre-flight so authoritative consumers don't
  double-count them.

### L.20.2 pre-flight checklist

Do **not** open L.20.2 without **all** of:

1. **Named authoritative consumer.** Exactly which surface reads
   `ticker_regime_label` / `ticker_regime_numeric`? Candidates:
   (a) `scanner.score_ticker` as a multiplicative bias,
   (b) `mining_validation.allow_strategy` as a hard gate on
       trend-family patterns in `mean_revert` regimes (and vice
       versa),
   (c) `portfolio_risk` / `position_sizer_model` as a sizing bias,
   (d) `stop_engine._compute_initial_stop` as a regime-aware stop
       widener (wider in `trend_up` for the winning side, tighter
       in `choppy`).
   Whoever owns the consumer must accept the frozen shape exactly.
2. **Parity window vs a reference classifier.** Minimum 20 trading
   days in `compare` mode. Acceptance bounds must be documented:
   e.g. composite-label disagreement ≤ 15%, false `trend_up` rate
   on known random-walk synthetic tickers ≤ 5%, and `adx_proxy`
   correlation with a Bloomberg / academic ADX ≥ 0.8.
3. **Governance gate on the flip.** `BRAIN_TICKER_REGIME_MODE=authoritative`
   must trigger an audit log (same shape as the L.17.2 / L.18.2 /
   L.19.2 plan) and optionally require governance approval before
   taking effect.
4. **Backfill decision.** Either a documented backfill job over the
   history window the consumer needs (with batch caps + dead-cache
   respect + Polygon-friendly concurrency), or an explicit decision
   to run forward-only.
5. **Universe migration plan** if `BRAIN_TICKER_REGIME_MAX_TICKERS`
   needs to grow past 250 (e.g. full Russell 3000 + top-N crypto);
   includes per-provider rate-limit review.
6. **`PHASE_KEYS` decision.** `ops_health_model.PHASE_KEYS` must
   either stay silent on `ticker_regime` (if authoritative is
   considered "strategy tilt" and not a green-status surface) or
   gain a canonical entry with its own green/yellow/red rules.
7. **Post-flip verification.** Re-run the full release-blocker +
   soak bundle after the authoritative flip, plus a fresh
   scan_status frozen-contract probe and the L.17/L.18/L.19/L.20
   regression bundle.

Until every bullet above is explicitly signed off, the service
stays `shadow`, and the soak + release-blocker scripts keep
`authoritative` hard-refused.
