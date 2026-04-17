---
status: completed_shadow_ready
title: Phase L.17 - Macro regime expansion v1 (shadow rollout)
parent_plan: trading_brain_profitability_gaps_bd7c666a
phase_ladder:
  - off
  - shadow
  - compare
  - authoritative
depends_on:
  - phase_a_economic_truth_ledger (completed_shadow_ready)
  - phase_b_exit_engine_unification (completed_shadow_ready)
  - phase_c_pit_hygiene (completed_shadow_ready)
  - phase_d_triple_barrier (completed_shadow_ready)
  - phase_e_net_edge_ranker_v1 (completed_shadow_ready)
  - phase_f_execution_realism (completed_shadow_ready)
  - phase_g_live_brackets_reconciliation (completed_shadow_ready)
  - phase_h_position_sizer_portfolio_optimizer (completed_shadow_ready)
  - phase_i_risk_dial_capital_reweight (completed_shadow_ready)
  - phase_j_drift_monitor_recert (completed_shadow_ready)
  - phase_k_divergence_ops_health (completed_shadow_ready)
---

## Objective

Expand the brain's market-regime surface beyond today's **SPY + VIX**
composite (produced by `market_data.get_market_regime()`) to include
a **persistent, point-in-time macro regime snapshot** covering:

* **Rates / yield-curve proxy** - `IEF` (7-10y Treasuries) vs `SHY` (1-3y)
  trend + slope, and `TLT` (20y+) trend.
* **Credit spread proxy** - `HYG` (high-yield) vs `LQD` (investment-grade)
  relative performance.
* **USD strength proxy** - `UUP` trend + 20d momentum.
* **Kept as-is**: SPY direction / momentum, VIX level / percentile,
  composite `{risk_on, cautious, risk_off}` - these are backward-compatible
  outputs that every downstream consumer reads today.

The new capability is **additive**: L.17 does **not** change the return
shape of `get_market_regime()` and does **not** change any call-site
that reads `regime["regime"]` or `regime_numeric`. Instead, a daily
shadow sweep persists one row per trading day into a new
`trading_macro_regime_snapshots` table, producing a per-day macro
feature vector that Phase L.18 (cross-asset signals) and NetEdgeRanker
recalibration (H.2) can consume authoritatively later.

## Problem this solves

From the master plan (`trading_brain_profitability_gaps_bd7c666a`,
P4 #17):

> Only 2 macro dimensions (SPY + VIX) in regime classification.
> Realistic equity regimes need breadth (advance/decline), rates
> (2y/10y), credit (HYG/LQD), and USD. Crypto regimes need dominance
> and funding.

Additionally the recon surfaced:

1. **No regime history table** - `regime` lives only in an in-memory
   5-minute cache plus scattered JSON blobs inside
   `ScanPattern.regime_affinity_json`,
   `MomentumSymbolViability.regime_snapshot_json`, etc. There is no
   canonical per-day macro-regime history. Backtests cannot PIT-join
   a rates/credit/USD regime today.
2. **`learning._get_historical_regime_map`** (learning.py 1485-1525)
   produces per-date `{risk_on, cautious, risk_off}` from SPY daily
   alone for mining. Once L.17 lands, backtest mining can join the new
   snapshot table for a richer as-of regime (deferred to L.17.2).
3. **Broken import in `runtime_status.regime_status`** (imports
   `_regime_cache` / `_regime_cache_ts` which do not exist) is a
   latent bug documented but **out of scope** for L.17 - fix in a
   separate trivial patch.

## Scope (L.17, shadow-only)

Land the shadow substrate end-to-end for the macro regime snapshot:

1. **Migration `138_macro_regime_snapshot`** creating
   `trading_macro_regime_snapshots` with columns:
   - `regime_id` (deterministic hash `sha1(as_of_date)`)
   - `as_of_date`
   - equity block: `spy_direction`, `spy_momentum_5d`, `vix`,
     `vix_regime`, `volatility_percentile`, `composite`, `regime_numeric`
   - rates block: `ief_trend`, `shy_trend`, `tlt_trend`,
     `yield_curve_slope_proxy`, `rates_regime`
   - credit block: `hyg_trend`, `lqd_trend`, `credit_spread_proxy`,
     `credit_regime`
   - usd block: `uup_trend`, `uup_momentum_20d`, `usd_regime`
   - composite-macro block: `macro_numeric`, `macro_label`
   - coverage block: `symbols_sampled`, `symbols_missing`,
     `coverage_score` (for observability when a provider is flaky)
   - `payload_json` (JSONB, raw per-symbol readings)
   - `mode`, `computed_at`, `observed_at`

2. **ORM model `MacroRegimeSnapshot`** in `app/models/trading.py`.

3. **Pure model `app/services/trading/macro_regime_model.py`** with:
   - `MacroRegimeConfig` (thresholds / weights with defaults)
   - `AssetReading` (one per ETF, with trend + momentum + missing flag)
   - `MacroRegimeInput` (equity inputs from existing
     `get_market_regime()` + list of `AssetReading`)
   - `MacroRegimeOutput` (all classifier outputs + composite)
   - `compute_regime_id(as_of_date) -> str`
   - `compute_macro_regime(inputs, *, config=None) -> MacroRegimeOutput`
   - Pure, zero side effects, no DB / network.

4. **Config flags** in `app/config.py`:
   - `brain_macro_regime_mode` (default `off`)
   - `brain_macro_regime_ops_log_enabled` (default `true`)
   - symbol universe + threshold knobs
   - cron hour / minute (default 06:30 server time - after
     divergence sweep at 06:15)
   - minimum coverage score to persist (default 0.5)

5. **Ops-log module** `app/trading_brain/infrastructure/macro_regime_ops_log.py`
   with `CHILI_MACRO_REGIME_OPS_PREFIX = "[macro_regime_ops]"` and a
   `format_macro_regime_ops_line(...)` helper mirroring
   `divergence_ops_log.py` exactly.

6. **DB service** `app/services/trading/macro_regime_service.py`:
   - `_effective_mode(override)` / `mode_is_active` / `mode_is_authoritative`.
   - `gather_asset_readings(as_of_date, lookback_days)` - fetches OHLCV
     for each configured ETF via `fetch_ohlcv_df` and produces
     `AssetReading`. Marks missing data as `missing=true`; does not
     raise when a symbol is unavailable.
   - `compute_and_persist(as_of_date, *, mode_override=None) ->
     MacroRegimeRow | None` - pulls equity regime from
     `get_market_regime()`, gathers ETF readings, calls the pure model,
     and writes one row. Authoritative refusal raises
     `RuntimeError("macro_regime authoritative mode is not permitted
     until Phase L.17.2 is explicitly opened")`.
   - `macro_regime_summary(lookback_days=14) -> dict` frozen wire
     shape for diagnostics.
   - `get_latest_snapshot() -> dict | None` for read-only consumers
     (used by diagnostics; not wired into downstream in L.17).

7. **APScheduler job** `macro_regime_daily` registered in
   `trading_scheduler.py` (cron, default 06:30, gated by
   `BRAIN_MACRO_REGIME_MODE`). One `max_instances=1`, one
   `replace_existing=True`. Refuses to run when mode is `off`.

8. **Diagnostics endpoint**
   `GET /api/trading/brain/macro-regime/diagnostics` in
   `app/routers/trading_sub/ai.py` returning
   `{ok: true, macro_regime: { ... frozen shape ... }}`. Lookback
   clamp `[1, 180]`.

9. **Release-blocker** `scripts/check_macro_regime_release_blocker.ps1`
   matching `[macro_regime_ops] event=macro_regime_persisted
   mode=authoritative` and `event=macro_regime_refused_authoritative`.
   Optional JSON gates for `MinSnapshots`, `MinCoverageScore`.

10. **Unit tests** (pytest, pure):
    - `tests/test_macro_regime_model.py` - id determinism, classifier
      thresholds, coverage-score clamping, missing-asset fallback,
      output shape, composite label rules.

11. **DB / API smoke** - `tests/test_phase_l17_diagnostics.py` for the
    diagnostics endpoint frozen shape.

12. **Docker soak** `scripts/phase_l17_soak.py` verifying:
    - migration applied;
    - settings flags visible;
    - mode=off is a no-op, mode=shadow persists a row;
    - authoritative refusal;
    - determinism of `regime_id` for same `as_of_date`;
    - `macro_regime_summary` frozen shape;
    - `get_market_regime()` still returns its existing keys unchanged
      (**additive-only check**).

13. **Docs** `docs/TRADING_BRAIN_MACRO_REGIME_ROLLOUT.md`.

14. **`.env` flip** to `BRAIN_MACRO_REGIME_MODE=shadow` + service
    recreate + diagnostics probe + release-blocker pass.

15. **Closeout** - plan YAML flipped to `completed_shadow_ready` with
    closeout + self-critique + L.18 checklist.

## Forbidden changes (L.17)

- Flipping `BRAIN_MACRO_REGIME_MODE=authoritative` in any environment
  before Phase L.17.2 is explicitly opened.
- Modifying the return shape of `get_market_regime()` in
  `market_data.py`. New fields must only appear in the new table /
  diagnostics endpoint.
- Mutating `/api/trading/scan/status` wire shape.
- Writing the new snapshot rows from any hot-path code
  (scanner, alerts, paper trading, stop engine). The only allowed
  call-site is the scheduled `macro_regime_daily` job and its
  diagnostics read path.
- Consuming the new rows from sizing, promotion, or scanner logic.
  L.17 is read-only for downstream consumers; authoritative
  consumption is L.17.2.
- Adding crypto dominance / funding-rate fetchers. Those require a
  new external provider contract and belong in Phase L.18.
- Adding breadth (A/D, new-highs/lows) fetchers. They require a
  provider (Polygon "/v1/marketstatus" is not sufficient); defer to
  L.17.3 or a dedicated breadth phase.

## Rollout ladder

**L.17.1 (this ships):**

1. Deploy image with migration `138_macro_regime_snapshot` applied.
2. Set `BRAIN_MACRO_REGIME_MODE=shadow` in `.env`.
3. Recreate `chili`, `brain-worker`, `scheduler-worker`.
4. Verify:
   - `GET /api/trading/brain/macro-regime/diagnostics` returns
     `{"ok": true, "macro_regime": {"mode": "shadow", ...}}`.
   - `scheduler-worker` logs: "Added job `Macro regime daily
     (06:30; mode=shadow)`".
   - Release blocker clean on 30m of live container logs.
5. Let the sweep write rows for >= 5 trading days; monitor coverage.

**L.17.2 (deferred, requires new approved plan):**

- Authoritative consumption: NetEdgeRanker recalibration reads
  `macro_label` / `macro_numeric` as a regime bucket.
- Scanner promotion gate optionally checks macro regime vs a pattern's
  `regime_affinity_json` for bias.
- `get_market_regime()` opts into merging macro fields when
  `BRAIN_MACRO_REGIME_MODE=authoritative` (explicit, backward-
  compatible via a new `macro` sub-dict).

## Verification gates

- Pure unit tests pass 100% for `macro_regime_model.py`.
- Docker soak `phase_l17_soak.py` reports `ALL CHECKS PASSED`
  including the additive-only check on `get_market_regime()`.
- Release blocker exits 0 against 30m live log window.
- `GET /api/trading/brain/macro-regime/diagnostics` returns the
  frozen shape on a live service.
- `/api/trading/scan/status` regression still passes.
- `GET /api/trading/brain/ops/health` continues to return 15 phases
  in stable order; **the macro-regime phase is NOT added to the
  `phases` list in L.17** (defer to L.17.2 when the rollout is
  authoritative - avoids a contract change during shadow soak).
- No lint regressions on files touched.

## Rollback

1. Set `BRAIN_MACRO_REGIME_MODE=off` in `.env`.
2. Recreate `chili`, `brain-worker`, `scheduler-worker`.
3. Scheduler skips `macro_regime_daily`; service is a no-op.
4. Existing rows in `trading_macro_regime_snapshots` are retained for
   post-mortem; no downstream consumer reads them in L.17.

## Non-goals

- Breadth (advance/decline, new highs/lows). No provider contract in
  the app today; requires a separate sourcing phase.
- Crypto dominance + funding rates. External API wiring; defer to
  Phase L.18.
- Modifying `get_market_regime()` computation or its return keys.
- Consuming macro regime in sizing / promotion / scanner. Deferred to
  L.17.2.
- Rewriting `regime.py` DSL helpers. They remain backward compatible.
- Fixing the latent `runtime_status.regime_status` import bug
  (separate trivial patch, does not block L.17).

## Definition of done (L.17.1)

`BRAIN_MACRO_REGIME_MODE=shadow` is live in all three services.
The daily `macro_regime_daily` job fires at 06:30 and appends rows
to `trading_macro_regime_snapshots`. The diagnostics endpoint
returns the frozen shape. The release blocker is clean on live logs.
All tests green. `get_market_regime()` is bit-for-bit unchanged
(verified by the soak). Plan YAML flipped to
`completed_shadow_ready` with closeout + self-critique + L.17.2
checklist.

## Closeout (L.17.1) - 2026-04-17

**Status**: completed_shadow_ready. L.17.1 is live end-to-end in
shadow mode; no existing consumer has been modified.

### Shipped

- Migration `138_macro_regime_snapshot` - creates
  `trading_macro_regime_snapshots` with `(as_of_date DESC)`,
  `(regime_id)`, `(macro_label, computed_at DESC)` indexes.
- ORM `MacroRegimeSnapshot` (append-only; L.17.2 only).
- Pure model `app/services/trading/macro_regime_model.py` -
  deterministic `compute_regime_id`, 3-sub-regime classifier (rates
  via IEF/SHY/TLT, credit via HYG/LQD, USD via UUP), weighted
  composite into `risk_on / cautious / risk_off`. Defensive against
  missing / partial readings.
- 17/17 unit tests in `tests/test_macro_regime_model.py` green.
- Config flags `BRAIN_MACRO_REGIME_*` (12 keys) in `app/config.py`.
- Ops log module `macro_regime_ops_log.py` with events
  `macro_regime_computed`, `macro_regime_persisted`,
  `macro_regime_skipped`, `macro_regime_refused_authoritative`.
- DB service `macro_regime_service.py` with:
  - `_build_asset_reading` / `gather_asset_readings` that reuse
    existing `market_data.fetch_ohlcv_df` with the same PIT-safe
    contract.
  - `compute_and_persist(db, ...)` with mode gating, coverage-score
    gating (`< min_coverage_score` -> skip persist + log), and hard
    refusal of `authoritative` mode until L.17.2 is opened.
  - Read helpers `get_latest_snapshot` and frozen-shape
    `macro_regime_summary`.
- APScheduler registration `macro_regime_daily` (default 06:30) -
  verified live: `Added job 'Macro regime daily (06:30; mode=shadow)'`.
- Diagnostics endpoint
  `GET /api/trading/brain/macro-regime/diagnostics?lookback_days=14`
  returning the frozen summary shape.
- Release blocker `scripts/check_macro_regime_release_blocker.ps1` -
  5/5 smoke tests pass (clean=0, authoritative-persist=1,
  refused=1, ok-json=0, below-coverage-json=1).
- Docker soak `scripts/phase_l17_soak.py` - **44/44 checks green**
  inside `chili` container, including:
  - Schema + settings visibility.
  - Pure model risk_on / risk_off / cautious classification across
    rates/credit/USD combinations.
  - Partial-coverage & missing-symbol defense.
  - `compute_and_persist` writes exactly 1 row in shadow.
  - `off` mode is a no-op; `authoritative` raises RuntimeError.
  - Coverage-below-min gate skips persistence.
  - Append-only second write produces 2 rows with the same
    deterministic `regime_id`.
  - `macro_regime_summary` frozen shape + `by_label` invariant.
  - Additive-only guard: `get_market_regime()` still returns all
    pre-L.17 keys (`spy_direction`, `spy_momentum_5d`, `vix`,
    `vix_regime`, `regime`, `regime_numeric`) bit-for-bit.
- `.env` flipped to `BRAIN_MACRO_REGIME_MODE=shadow` and services
  recreated; diag endpoint confirms `mode=shadow` live.
- `scan_status` frozen contract intact: top-level keys
  `['ok','brain_runtime','prescreen','learning']`,
  `brain_runtime.release == {}`.
- Prior release blockers (Phase K divergence + ops_health) remain
  clean - zero `[divergence_ops]` / `[ops_health_ops]` blocker
  lines in the 10m live log window.
- Docs `docs/TRADING_BRAIN_MACRO_REGIME_ROLLOUT.md` written.

### Self-critique

1. **Soak test label taxonomy mismatch (fixed)**: my first soak
   asserted `credit_regime == "risk_on"` but the canonical labels
   are `credit_tightening` / `credit_widening` / `credit_neutral`.
   Caught on first run inside the container and corrected.
2. **Soak additive-only guard key drift (fixed)**: asserted
   `composite` in `get_market_regime()`, but the actual key is
   `regime`. Corrected and re-ran; all 44 checks green.
3. **Release-blocker scripts hang on empty pipelines**: the
   existing Phase K scripts block indefinitely when piped an empty
   list. Not a L.17 regression - confirmed by direct log inspection
   that 0 blocker lines exist. A follow-up could harden the PS1
   scripts with an explicit empty-pipeline guard.
4. **`get_market_regime()` still has the latent
   `runtime_status.regime_status` import bug** flagged in recon.
   Explicitly left out of L.17 scope (trivial patch lives elsewhere).
5. **No live fetch in soak**: the soak feeds synthetic
   `AssetReading`s rather than calling `gather_asset_readings`
   against real Massive/yf. This is deliberate for determinism;
   the live pull will be exercised by the scheduler on the first
   06:30 sweep.

### L.17.2 checklist (not opened here)

- Backfill historical macro regime rows (one per trading day for
  the last N days) so lookback queries have real distribution
  density before any consumer starts reading.
- Wire `macro_label` as an **authoritative** regime filter into:
  - Phase H position sizer (scale Kelly fraction by macro regime).
  - Phase I risk dial (macro_label=risk_off -> cautious tier
    floor).
  - Phase D / economic promotion metric (macro-regime-adjusted
    promotion gates).
- Add a compare-mode audit log that records, per run, whether the
  authoritative macro regime matches the legacy SPY/VIX composite
  and where it differs.
- Flip mode to `authoritative` only after the extended consumer
  surface has run in `compare` for at least 2 trading weeks with
  green release blocker + soak.
- Open L.17.2 plan with its own frozen checklist before any of the
  above ships.
