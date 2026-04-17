---
status: completed_shadow_ready
title: Phase L.19 - Cross-asset signals v1 (shadow rollout)
parent_plan: trading_brain_profitability_gaps_bd7c666a
phase_id: phase_l19
phase_slice: L.19.1
created: 2026-04-17
frozen_at: 2026-04-17
rollout_mode_default: off
target_rollout_mode: shadow
authoritative_deferred_to: L.19.2
---

# Phase L.19 — Cross-asset signals v1 (shadow rollout)

## Objective (L.19.1)

Append-only daily snapshot of **cross-asset lead/lag signals** that
consumes L.17 (macro regime: rates/credit/USD) and L.18 (breadth + RS)
feature vectors plus on-demand daily OHLCV for bonds / USD / crypto,
and emits a composite `cross_asset_label` + `cross_asset_numeric` +
payload of raw features. Scope is **research / observability only**;
no consumer reads the snapshot in L.19.1.

This fills the P4 #18 gap from the master plan:

> Cross-asset signals: crypto reactions to equity open, bond sell-off,
> USD strength vs crypto, VIX shocks vs breadth — a documented edge
> source not mined here.

## Why now

- L.17 (macro regime) and L.18 (breadth + RS) are shadow-ready and
  produce the **exact inputs** L.19 needs (rates/credit/USD regime +
  advance_ratio + size/style tilt + leader/laggard sector).
- Adding L.19 as an **additive** panel costs one migration, one table,
  one pure model, one service, one scheduler job — same shape as the
  two prior panels.
- L.19 is the first cross-asset surface in the trading brain; today
  nothing joins BTC / bonds / USD / breadth into a single
  disagreement signal. Shadow-only logging is the safest first step.

## Non-negotiables

1. **Additive-only.** No mutation of:
   - `trading_macro_regime_snapshots` (L.17)
   - `trading_breadth_relstr_snapshots` (L.18)
   - `get_market_regime()` return keys (read-only)
   - `_take_intraday_crypto_snapshots` job (no SPY intraday writes)
   - `ops_health_model.PHASE_KEYS` (L.19 intentionally excluded until
     L.19.2 — follows the L.17/L.18 precedent)
   - existing scheduler job ids or slots
2. **Shadow mode only.** `BRAIN_CROSS_ASSET_MODE ∈ {off, shadow}` in
   L.19.1. Service hard-refuses `authoritative` with
   `RuntimeError("authoritative disabled until Phase L.19.2 is
   explicitly opened")`. No read path in live / paper / backtest.
3. **No new external data provider.** All legs come from the existing
   `market_data.fetch_ohlcv_df` path (Massive → Polygon → yfinance
   fallback chain). No new API keys, no new HTTP client.
4. **No intraday extension.** Daily cadence only. Event-window
   "equity open" studies deferred to L.19.2 (requires explicit
   session-alignment policy).
5. **On-demand reads only.** L.19 does not rely on rows pre-existing
   in `trading_snapshots`. It fetches each leg on the sweep.
6. **Scheduler slot** fixed at **07:00** to avoid collision with
   divergence (06:15), drift (05:30), macro (06:30), breadth (06:45).
7. **Frozen release-blocker grep.** `[cross_asset_ops]` +
   `mode=authoritative` → blocker. Same contract as L.17/L.18.

## Deliverables

### Migration + ORM
- Migration **`140_cross_asset_snapshot`**: create
  `trading_cross_asset_snapshots` with `(id, snapshot_id, as_of_date,
  bond_equity_lead_*, credit_equity_lead_*, usd_crypto_lead_*,
  vix_breadth_divergence_*, crypto_equity_beta_*, cross_asset_numeric,
  cross_asset_label, symbols_sampled, symbols_missing, coverage_score,
  payload_json, mode, computed_at, observed_at)`.
- ORM `CrossAssetSnapshot` in `app/models/trading.py` mirroring the
  L.17/L.18 pattern.
- Indexes: `(as_of_date DESC)`, `(snapshot_id)`,
  `(cross_asset_label, computed_at DESC)`.

### Pure model
- `app/services/trading/cross_asset_model.py`:
  - `CrossAssetConfig` (thresholds for "fast" 5d lead/lag, "slow"
    20d, tilt thresholds, min coverage, beta window default 60d).
  - `AssetLeg` (symbol, last_close, ret_5d, ret_20d, ret_1d, missing).
  - `CrossAssetInput` (as_of_date, equity_leg=SPY, rates_leg=TLT,
    credit_hy_leg=HYG, credit_ig_leg=LQD, usd_leg=UUP, crypto_btc_leg,
    crypto_eth_leg, vix_percentile, vix_level, breadth_advance_ratio,
    breadth_label, macro_label, config).
  - `CrossAssetOutput` (all computed scalars + composite label +
    coverage block + payload dict).
  - Pure deterministic functions: `_bond_equity_lead`,
    `_credit_equity_lead`, `_usd_crypto_lead`,
    `_vix_breadth_divergence`, `_crypto_equity_beta`,
    `_composite_cross_asset_label`, `compute_snapshot_id`,
    `compute_cross_asset`.

### Unit tests (pure, no DB)
- `tests/test_cross_asset_model.py`: ≥ 20 cases:
  - `compute_snapshot_id` determinism + error on empty inputs
  - Each lead/lag scalar sign + magnitude
  - Divergence detection: VIX shock + breadth risk-on = `divergence`
  - All-agree cases: risk_on_crosscheck / risk_off_crosscheck
  - Partial coverage (missing crypto / missing credit pair)
  - All-missing → `neutral` + `coverage_score=0.0`
  - Beta window clamping and zero-variance handling
  - Config threshold edge cases
  - Frozen dataclass immutability

### Config
- `app/config.py` additions (10 keys, default off):
  ```
  brain_cross_asset_mode: str = "off"
  brain_cross_asset_ops_log_enabled: bool = True
  brain_cross_asset_cron_hour: int = 7
  brain_cross_asset_cron_minute: int = 0
  brain_cross_asset_min_coverage_score: float = 0.5
  brain_cross_asset_fast_lead_threshold: float = 0.01  # 1% diff
  brain_cross_asset_slow_lead_threshold: float = 0.03  # 3% diff
  brain_cross_asset_beta_window_days: int = 60
  brain_cross_asset_vix_percentile_shock: float = 0.80
  brain_cross_asset_lookback_days: int = 14
  ```

### Ops log
- `app/trading_brain/infrastructure/cross_asset_ops_log.py` with
  `CHILI_CROSS_ASSET_OPS_PREFIX = "[cross_asset_ops]"` and
  `format_cross_asset_ops_line(event, **fields)` matching the one-line
  key=value shape used by L.17/L.18.

### DB service
- `app/services/trading/cross_asset_service.py`:
  - `_effective_mode` / `mode_is_active` / `mode_is_authoritative`
  - `_ops_log_enabled` / `_config_from_settings`
  - `CrossAssetRow` dataclass mirroring the ORM
  - `_build_asset_leg(db, symbol)`: fetch via `fetch_ohlcv_df` with
    `interval="1d"`, `period="3mo"`, derive `ret_1d / ret_5d / ret_20d`,
    guard against short history
  - `gather_asset_legs(db, config) -> CrossAssetInput`: pulls SPY,
    TLT, HYG, LQD, UUP, BTC-USD, ETH-USD; reads L.17 + L.18 latest
    snapshots for context (macro_label, breadth_advance_ratio,
    breadth_label, vix_percentile, vix_level) via existing helpers
    (`macro_regime_service.get_latest_snapshot`,
    `breadth_relstr_service.get_latest_snapshot`,
    `market_data.get_market_regime` read-only)
  - `compute_and_persist(db, *, as_of_date=None, override_input=None)`:
    mode gating, authoritative refusal, coverage-score gate, append
    one row, log one `event=cross_asset_persisted` or
    `event=cross_asset_skipped` or
    `event=cross_asset_refused_authoritative`
  - `get_latest_snapshot(db)` read helper
  - `cross_asset_summary(db, *, lookback_days)` returning the frozen
    wire shape for diagnostics

### APScheduler job
- `_run_cross_asset_daily_job` in `app/services/trading_scheduler.py`
  (same guarded pattern as L.17/L.18), registered as
  `cross_asset_daily` at **07:00** (or `brain_cross_asset_cron_*`).
- Gated by `brain_cross_asset_mode != "off"`.
- Hard-refuses authoritative with a loud warning log and returns.

### Diagnostics endpoint
- `GET /api/trading/brain/cross-asset/diagnostics?lookback_days=N`
  returning `{"ok": True, "cross_asset": summary}` with frozen keys:
  `mode, lookback_days, snapshots_total, by_cross_asset_label,
  by_leader_sector, mean_coverage_score, mean_bond_equity_lead_5d,
  mean_credit_equity_lead_5d, mean_usd_crypto_lead_5d, latest_snapshot`.
- Smoke tests in `tests/test_phase_l19_diagnostics.py`:
  1. Frozen key set
  2. `lookback_days` clamp (422 for 0 and 181)

### Release blocker
- `scripts/check_cross_asset_release_blocker.ps1` mirroring
  `check_breadth_relstr_release_blocker.ps1`:
  - Fails on any `[cross_asset_ops]` line with
    `event=cross_asset_persisted` + `mode=authoritative`
  - Fails on any `event=cross_asset_refused_authoritative`
  - Optional `-DiagnosticsJson` gate on `snapshots_total` and
    `mean_coverage_score`
- 5 smoke tests: clean, auth-persist, refused, diag-ok, diag-low-cov.

### Docker soak
- `scripts/phase_l19_soak.py` verifies inside the running `chili`
  container:
  1. Migration 140 applied, table present
  2. 10 `brain_cross_asset_*` settings visible
  3. Pure model: risk_on_crosscheck / risk_off_crosscheck /
     divergence / neutral synthetic cases + partial coverage
  4. `compute_and_persist` writes exactly one row in shadow
  5. `off` mode is no-op, `authoritative` raises
  6. Coverage-gate skip when below `min_coverage_score`
  7. Deterministic `snapshot_id` for same inputs + append-only on
     repeated writes
  8. `cross_asset_summary` frozen wire shape
  9. **Additive-only**: L.17 + L.18 snapshot row counts unchanged
     around a full L.19 write cycle; `get_market_regime()` keys
     unchanged
  10. `ops_health` PHASE_KEYS unchanged (L.19 absent)

### Regression guards
- L.17 pure tests still green (17/17)
- L.18 pure tests still green (20/20)
- `scan_status` frozen contract live probe: `brain_runtime.release ==
  {}`, top-level keys unchanged
- L.17 diagnostics mode still shadow
- L.18 diagnostics mode still shadow
- `ops_health` snapshot still returns exactly 15 phase keys

### `.env` flip
- Add `BRAIN_CROSS_ASSET_MODE=shadow` + cron defaults to `.env`.
- Recreate `chili`, `brain-worker`, `scheduler-worker`.
- Verify scheduler registered `cross_asset_daily (07:00;
  mode=shadow)`.
- Verify `GET /api/trading/brain/cross-asset/diagnostics` returns
  `mode: "shadow"`.
- Verify release-blocker scan clean against live container logs.

### Docs
- `docs/TRADING_BRAIN_CROSS_ASSET_ROLLOUT.md`:
  - What shipped in L.19.1
  - Frozen wire shapes (service + diagnostics)
  - Release blocker grep pattern
  - Rollout order (off → shadow → compare → authoritative)
  - Rollback procedure
  - Additive-only guarantees (L.17/L.18/get_market_regime/ops_health)
  - L.19.2 pre-flight checklist (authoritative consumer wiring +
    parity window + intraday event window expansion)

## Forbidden changes (in this phase)

- Mutating any `MacroRegimeSnapshot`, `BreadthRelstrSnapshot`,
  `MarketSnapshot`, or `get_market_regime()` field.
- Adding a consumer (sizing / promotion / scanner / NetEdgeRanker)
  that reads `trading_cross_asset_snapshots`. That is the **L.19.2**
  contract.
- Touching `ops_health_model.PHASE_KEYS`.
- Adding intraday scheduler jobs or modifying `brain_intraday_intervals`.
- Adding any new provider HTTP client.
- Changing the `scan_status` frozen contract.
- Reintroducing `CHILI_GIT_COMMIT` / `release.git_commit`.

## Verification gates (definition of done for L.19.1)

1. `pytest tests/test_cross_asset_model.py -v` — all green inside
   `chili-env`.
2. `pytest tests/test_phase_l19_diagnostics.py -v` — all green.
3. `scripts/check_cross_asset_release_blocker.ps1` — 5 smoke tests
   pass (clean/auth/refused/diag-ok/diag-low-cov).
4. `docker compose exec chili python scripts/phase_l19_soak.py` — all
   checks ALL GREEN.
5. Live probe:
   - `GET /api/trading/brain/cross-asset/diagnostics` returns
     `mode: "shadow"` with frozen keys.
   - Scheduler logs show `Added job 'Cross-asset daily (07:00;
     mode=shadow)'` in `scheduler-worker`.
   - `[cross_asset_ops]` release-blocker scan on live logs exits 0.
6. Regression bundle green:
   - L.17 + L.18 diagnostics still `mode: shadow`
   - L.17 + L.18 pure tests still green
   - `scan_status` live JSON still matches frozen contract
   - `ops_health` still 15 phase keys (no L.19 entry)
7. Plan YAML flipped to `completed_shadow_ready` + closeout section
   with self-critique + L.19.2 checklist.

## Rollback

1. Set `BRAIN_CROSS_ASSET_MODE=off` in `.env`.
2. Recreate `chili`, `brain-worker`, `scheduler-worker`.
3. Scheduler skips `cross_asset_daily`; service is a no-op.
4. Existing rows in `trading_cross_asset_snapshots` are retained for
   post-mortem; no downstream consumer reads them in L.19.1.

## Non-goals

- Intraday event-window studies ("BTC reaction to equity open"
  ±1h, ±15m, opening-auction behavior). Deferred to **L.19.2** or
  later — requires explicit session-alignment policy and is provider
  sensitive.
- Rolling cross-asset correlation for every pair in the portfolio.
  L.19 publishes BTC-SPY beta only; wider pair matrices are a sizer
  input (H.2) not a panel.
- News / event overlays. Those are **L.20** (P4 #19).
- Changing `get_market_regime()` keys or signatures.
- Changing any consumer (sizing / scanner / promotion).
- Authoritative consumption of the new table (L.19.2).

## Definition of done (L.19.1)

`BRAIN_CROSS_ASSET_MODE=shadow` is live in all three services. The
daily `cross_asset_daily` job fires at 07:00 and appends rows to
`trading_cross_asset_snapshots`. The diagnostics endpoint returns the
frozen shape. The release blocker is clean on live logs. All pure
tests green. L.17 + L.18 + `get_market_regime()` + `ops_health` are
bit-for-bit unchanged (verified by the soak). Plan YAML flipped to
`completed_shadow_ready` with closeout + self-critique + L.19.2
checklist.

## Closeout (L.19.1)

### What shipped (verified)

- Migration `140_cross_asset_snapshot` applied — new
  `trading_cross_asset_snapshots` table (append-only, 3 indexes).
- ORM `CrossAssetSnapshot` in `app/models/trading.py`.
- Pure classifier `app/services/trading/cross_asset_model.py` with
  `compute_cross_asset`, bond/credit/USD lead classifiers, VIX-breadth
  divergence score, rolling BTC-SPY beta, deterministic
  `compute_snapshot_id`, composite `cross_asset_label` in
  `{risk_on_crosscheck, risk_off_crosscheck, divergence, neutral}`.
- 22/22 unit tests green (`tests/test_cross_asset_model.py`).
- 12 `BRAIN_CROSS_ASSET_*` config keys in `app/config.py`.
- Ops-log module
  `app/trading_brain/infrastructure/cross_asset_ops_log.py` with
  prefix `[cross_asset_ops]` and 4 events
  (`cross_asset_computed`, `cross_asset_persisted`,
  `cross_asset_skipped`, `cross_asset_refused_authoritative`).
- DB service `app/services/trading/cross_asset_service.py` —
  `compute_and_persist`, `gather_asset_legs`, `get_latest_snapshot`,
  `cross_asset_summary`; authoritative mode hard-refused with
  `RuntimeError`; coverage gate at `brain_cross_asset_min_coverage_score`.
- APScheduler job `cross_asset_daily` (07:00 local, gated by
  `BRAIN_CROSS_ASSET_MODE`) registered in `trading_scheduler.py`.
- Diagnostics endpoint
  `GET /api/trading/brain/cross-asset/diagnostics` — frozen shape,
  `lookback_days` clamped `[1, 180]` (422 on out-of-range).
- API smoke tests: `tests/test_phase_l19_diagnostics.py`.
- Release-blocker PS1 script
  `scripts/check_cross_asset_release_blocker.ps1` — 5/5 smoke tests
  pass (clean / auth-persist / refused / diag-ok / diag-low-cov).
- Docker soak `scripts/phase_l19_soak.py` — 47/47 checks ALL GREEN
  inside the `chili` container (includes L.17 and L.18
  additive-only guards).
- `.env` flipped to `BRAIN_CROSS_ASSET_MODE=shadow`; three services
  recreated; scheduler registered
  `Cross-asset daily (07:00; mode=shadow)`; diagnostics returns
  `mode: "shadow"`; release blocker on live logs exits 0.
- Docs: `docs/TRADING_BRAIN_CROSS_ASSET_ROLLOUT.md`.

### Regression evidence

- `scan_status` live probe: `brain_runtime.release={}`, top-level
  `release` absent, top-level keys
  `['ok','brain_runtime','prescreen','learning']` — frozen contract
  intact.
- L.17 pure tests: 17/17 green. L.18 pure tests: 20/20 green. L.19
  pure tests: 22/22 green.
- Soak confirms L.17 `trading_macro_regime_snapshots` row count and
  L.18 `trading_breadth_relstr_snapshots` row count are unchanged
  across an L.19 `compute_and_persist`.

### Self-critique

- **Test-data drift (unit tests):** two unit tests initially failed at
  floating-point threshold boundaries (risk-off classification,
  zero-variance beta). Fixed by widening magnitudes and using exact
  zeros; no model logic was softened to make tests pass. Still, the
  first draft of `_build_input` used values sitting right on a
  threshold edge — a code-smell worth keeping in mind: "float-default
  test fixtures that are numerically neighboring a cutoff will pass
  locally and break on recompile." L.19.2 parity tests should avoid
  this by snapshotting a real daily panel and asserting label stability
  rather than reconstructing inputs.
- **Soak synthetic divergence case:** the first soak run mis-labeled
  the bond-as-risk-off-while-rest-risk-on scenario as
  `risk_on_crosscheck` because TLT's synthetic 5d return only beat SPY
  by 0.01 — exactly at the `fast_lead_threshold`. Moved it to 0.02 of
  clear separation; matches the same class of fix as the unit-test
  drift above.
- **`psql` user mismatch:** early soak iteration used
  `psql -U postgres` which the Compose Postgres container refuses
  (it ships with `chili`/`chili`). Fixed in one shot; no production
  risk, but worth codifying: `docs/DOCKER_FULL_STACK.md` or an inline
  comment in soak scripts should call out the role is `chili`.
- **No external-source parity:** like L.17/L.18, L.19.1 ships as
  observability only. A `divergence` label is not compared against an
  externally measured lead/lag dataset yet. L.19.2 must gate
  authoritative on that parity window.
- **No intraday event-window study:** explicitly out of scope. The
  daily cross-asset table is a lagging summary by construction;
  intraday impulse-response (e.g. BTC at equity-open ±15m) is still
  on the P4 list and will need its own phase.
- **Massive dead-cache + USD/USDT/USDC candidates:** `gather_asset_legs`
  relies on `fetch_ohlcv_df` which already iterates crypto aggregate
  candidates via `market_data`. Verified by reading the Massive
  code-path rule, but no soak assertion covers a partial outage where
  only BTC-USDT has bars. Suggested L.19.2 follow-up: inject a
  Massive-dead simulator into the soak.

### L.19.2 pre-flight checklist (not yet opened)

Do **not** open L.19.2 without all of the following:

1. **Named authoritative consumer path.** Who reads
   `cross_asset_label`, `bond_equity_label`, `crypto_equity_beta`,
   and/or `vix_breadth_divergence_score`? Under which risk authority?
   What does the consumer do with each label
   (size down, block entries, raise alerts, alter correlation budget)?
2. **Parity window in `compare` mode** against an external reference
   (WSJ cross-asset dashboard / Bloomberg IMAP / hand-coded
   "pro-risk" indicator) for a minimum of 20 trading days, with
   drift bounds agreed (composite label disagreement <= 15%, sign
   agreement on `cross_asset_numeric` >= 85%).
3. **Governance gate** wired so flipping
   `BRAIN_CROSS_ASSET_MODE=authoritative` triggers an audit log and
   optional approval requirement (same shape as L.17.2 / L.18.2).
4. **Backfill decision** — explicitly document whether L.19.2
   consumers need a history window, and if so, add a one-shot
   backfill script (respecting PIT-correctness for OHLCV).
5. **Re-run the full bundle** (pure tests + diagnostics tests +
   release-blocker smokes + Docker soak + scan_status contract +
   live release-blocker grep) after the authoritative flip, and
   record evidence in an L.19.2 closeout.

Until those are in place, the service hard-refuses
`authoritative` with a `RuntimeError` and logs
`event=cross_asset_refused_authoritative`.
