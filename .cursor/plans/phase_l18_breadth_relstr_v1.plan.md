---
status: completed_shadow_ready
title: Phase L.18 - Breadth + cross-sectional relative-strength snapshot v1 (shadow rollout)
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
  - phase_l17_macro_regime_v1 (completed_shadow_ready)
---

## Objective

Fill the **breadth + sector rotation + cross-sectional relative-strength**
gap called out in the master plan (P4 #18) as the companion to L.17's
macro regime expansion. Land a **shadow-only** daily snapshot that:

1. Records how many members of a fixed reference universe (the 11 US
   sector SPDRs plus SPY/QQQ/IWM benchmarks) are **advancing vs
   declining** each day - a Phase A/D-lite proxy that does not need
   live exchange A/D feeds.
2. Classifies each sector SPDR's trend (up / flat / down) using the
   same `classify_trend` thresholds as L.17.
3. Computes per-sector **relative strength vs SPY** (20d momentum of
   sector minus 20d momentum of SPY), plus small-cap-vs-large
   (`IWM - SPY`) and tech-vs-broad (`QQQ - SPY`) style tilts.
4. Emits a composite `breadth_label` in `{broad_risk_on, mixed,
   broad_risk_off}` and `leader_sector` (name of the strongest
   sector by 20d RS vs SPY).

The new capability is **additive**: L.18 does **not** mutate
`market_data.get_market_regime()` nor L.17's
`trading_macro_regime_snapshots`, nor any other Phase A-L.17 wire
shape. It only appends rows to a new `trading_breadth_relstr_snapshots`
table for later authoritative consumption in L.18.2.

## Problem this solves

From the master plan (P4 #18, paraphrased):

> Regime classification has no breadth (advance/decline), no sector
> rotation signal, no small-cap-vs-large tilt, and no cross-sectional
> relative-strength feature. Many profitable equity regimes are
> identifiable primarily through sector leadership (e.g. XLK/XLE
> rotating) or size-factor tilt (IWM strength), which today are
> invisible to the brain.

Reconnaissance (2026-04-17) surfaced all 12 items **green**:

- No existing Python breadth / A-D / McClellan logic.
- Only partial sector ETF usage (XLK/XLF/XLE/XLV in static lists).
- No `relative_strength` / `sector_rotation` / `beta_to_spy` helpers.
- `market_data.fetch_ohlcv_df` is reusable identically to L.17.
- Latest migration is `138_macro_regime_snapshot`; next is `139_*`.
- `macro_regime_daily` occupies 06:30; free scheduler slot at
  **06:45** in the same cron family.
- `ops_health_model.PHASE_KEYS` has 15 entries (A-K); L.17 is
  intentionally **not** a phase in the health panel, so L.18 also
  will not be added to `/ops/health` in L.18.1.

## Scope (L.18.1, shadow-only)

Land the shadow substrate end-to-end for the breadth + RS snapshot:

1. **Migration `139_breadth_relstr_snapshot`** creating
   `trading_breadth_relstr_snapshots` with columns:
   - `snapshot_id` (deterministic hash `sha1(as_of_date)`)
   - `as_of_date`
   - breadth block: `members_sampled`, `members_advancing`,
     `members_declining`, `members_flat`, `advance_ratio`,
     `new_highs_count`, `new_lows_count`
   - sector block (per SPDR: XLK/XLF/XLE/XLV/XLY/XLP/XLI/XLB/XLU/
     XLRE/XLC): `{sector}_trend`, `{sector}_rs_vs_spy_20d`,
     `{sector}_momentum_20d`. Stored as JSONB in `sector_json` for
     space/flexibility, not 33 columns.
   - benchmark block: `spy_trend`, `spy_momentum_20d`,
     `qqq_trend`, `qqq_momentum_20d`, `iwm_trend`,
     `iwm_momentum_20d`.
   - tilt block: `size_tilt` (IWM-vs-SPY 20d RS), `style_tilt`
     (QQQ-vs-SPY 20d RS).
   - composite block: `breadth_numeric` (1 / 0 / -1),
     `breadth_label` (`broad_risk_on` / `mixed` /
     `broad_risk_off`), `leader_sector` (varchar), `laggard_sector`
     (varchar).
   - coverage block: `symbols_sampled`, `symbols_missing`,
     `coverage_score`.
   - `payload_json` (JSONB, raw per-symbol readings + config).
   - `mode`, `computed_at`, `observed_at`.
   Indexes: `(as_of_date DESC)`, `(snapshot_id)`,
   `(breadth_label, computed_at DESC)`.

2. **ORM model `BreadthRelstrSnapshot`** in
   `app/models/trading.py` (append-only, mirrors migration exactly).

3. **Pure model `app/services/trading/breadth_relstr_model.py`**
   with:
   - `BreadthRelstrConfig` (trend thresholds, tilt threshold,
     weights, min_coverage_score).
   - `UniverseMember` (symbol, last_close, prev_close,
     momentum_20d, trend, missing).
   - `BreadthRelstrInput` (as_of_date, members list, benchmark
     readings dict).
   - `BreadthRelstrOutput` mirroring the ORM fields above.
   - `compute_snapshot_id(as_of_date)` - deterministic `sha1`.
   - `classify_trend(momentum_20d, cfg)` - reused directly from
     L.17 via module-level import (no duplicate definition;
     L.17 `classify_trend` is already side-effect-free and the
     thresholds are a strict subset of what L.18 needs).
   - `_count_advance_decline(members)` - pure counting.
   - `_compute_rs_vs_spy(sector_mom20, spy_mom20)` - pure diff.
   - `_composite_breadth_label(advance_ratio, size_tilt,
     leader_rs, cfg)` - rules-based mapping to
     `broad_risk_on` / `mixed` / `broad_risk_off`.
   - `compute_breadth_relstr(input, config=None)` - the sole
     public entry point; handles missing data defensively and
     always returns the frozen `BreadthRelstrOutput` shape.

4. **Unit tests `tests/test_breadth_relstr_model.py`** covering:
   - `compute_snapshot_id` determinism + type.
   - `UniverseMember` invariants.
   - `_count_advance_decline` with all-up, all-down, mixed, and
     missing entries.
   - `compute_breadth_relstr` for risk_on / risk_off / mixed
     canonical cases.
   - Partial coverage and all-missing returns the frozen "empty"
     output with `coverage_score=0`.
   - Leader/laggard identification is stable for ties.
   - `rs_vs_spy` sign is correct when sector momentum beats SPY.
   - Payload has stable top-level keys.
   - Duplicate / extra symbol entries are ignored.

5. **Config flags** in `app/config.py` (mirror `brain_macro_regime_*`):
   - `brain_breadth_relstr_mode: str = "off"` (`off` / `shadow` /
     `compare` / `authoritative`; `authoritative` refused until
     L.18.2)
   - `brain_breadth_relstr_ops_log_enabled: bool = True`
   - `brain_breadth_relstr_cron_hour: int = 6`
   - `brain_breadth_relstr_cron_minute: int = 45`
   - `brain_breadth_relstr_min_coverage_score: float = 0.5`
   - `brain_breadth_relstr_trend_up_threshold: float = 0.01`
   - `brain_breadth_relstr_strong_trend_threshold: float = 0.03`
   - `brain_breadth_relstr_tilt_threshold: float = 0.02`
   - `brain_breadth_relstr_risk_on_ratio: float = 0.65`
   - `brain_breadth_relstr_risk_off_ratio: float = 0.35`
   - `brain_breadth_relstr_lookback_days: int = 14`

6. **Ops log module
   `app/trading_brain/infrastructure/breadth_relstr_ops_log.py`** with
   `CHILI_BREADTH_RELSTR_OPS_PREFIX = "[breadth_relstr_ops]"` and
   `format_breadth_relstr_ops_line(...)` for events
   `breadth_relstr_computed`, `breadth_relstr_persisted`,
   `breadth_relstr_skipped`,
   `breadth_relstr_refused_authoritative`.

7. **DB service
   `app/services/trading/breadth_relstr_service.py`** with:
   - `_build_member_reading(symbol)` - reuses
     `market_data.fetch_ohlcv_df(symbol, interval="1d",
     period="3mo")` (same contract as L.17).
   - `gather_member_readings()` - orchestrates the fixed
     sector + benchmark basket.
   - `compute_and_persist(db, as_of_date=None, mode_override=None,
     members_override=None)` - main entry point with:
     - mode gating (off -> no-op, authoritative -> hard refusal
       with `RuntimeError` and ops log).
     - coverage-score gating (below min -> skip persist + log).
     - pure compute via `compute_breadth_relstr`.
     - append-only `INSERT` with `RETURNING id`.
     - ops log events before persist and after persist.
   - `get_latest_snapshot(db)` - for diagnostics.
   - `breadth_relstr_summary(db, lookback_days)` - returns the
     frozen diagnostics shape:
     - `mode`, `lookback_days`, `snapshots_total`
     - `by_breadth_label` (dict with `broad_risk_on`,
       `mixed`, `broad_risk_off`)
     - `by_leader_sector`, `by_laggard_sector` (dicts)
     - `mean_advance_ratio`, `mean_coverage_score`
     - `latest_snapshot` (compact subset of last row or None).

8. **Scheduler registration** in `trading_scheduler.py`:
   - New worker `_run_breadth_relstr_daily_job()` following the
     exact L.17 pattern (mode gating, authoritative skip log,
     guarded with `run_scheduler_job_guarded`).
   - Registered alongside `macro_regime_daily` with id
     `breadth_relstr_daily`, default cron 06:45, only registered
     when `include_web_light` and mode not in
     `("off", "authoritative")`.

9. **Diagnostics endpoint**
   `GET /api/trading/brain/breadth-relstr/diagnostics?lookback_days=14`
   in `app/routers/trading_sub/ai.py`. Returns
   `{"ok": true, "breadth_relstr": <breadth_relstr_summary shape>}`.

10. **API smoke test
    `tests/test_phase_l18_diagnostics.py`** - 2 tests: frozen shape
    + lookback clamp 422 on `lookback_days=0` and `lookback_days=181`.

11. **Release-blocker script
    `scripts/check_breadth_relstr_release_blocker.ps1`** mirroring
    the L.17 script. Fails on:
    - `[breadth_relstr_ops] event=breadth_relstr_persisted mode=authoritative`
    - `[breadth_relstr_ops] event=breadth_relstr_refused_authoritative`
    Optional diagnostics JSON gate: fails when
    `snapshots_total < MinSnapshots` or
    `mean_coverage_score < MinCoverageScore`.

12. **Docker soak `scripts/phase_l18_soak.py`** with at minimum:
    - Schema + settings visibility.
    - Pure model for risk_on / risk_off / mixed synthetic members.
    - Compute-and-persist writes exactly 1 row in shadow.
    - `off` mode no-op; `authoritative` raises RuntimeError.
    - Coverage-score gate skips persistence.
    - Determinism: same `as_of_date` -> same `snapshot_id`.
    - Append-only: second write produces 2 rows.
    - `breadth_relstr_summary` frozen shape.
    - **Additive-only guard**: `get_market_regime()` still
      returns pre-L.18 keys unchanged AND
      `trading_macro_regime_snapshots` is unaffected.

13. **Docs
    `docs/TRADING_BRAIN_BREADTH_RELSTR_ROLLOUT.md`** - rollout,
    rollback, config table, ops log table, L.18.2 checklist.

14. **`.env` flip** - append `BRAIN_BREADTH_RELSTR_MODE=shadow` and
    related knobs; recreate `chili`, `brain-worker`,
    `scheduler-worker`; verify diag `mode=shadow` and scheduler
    registered `breadth_relstr_daily`.

## Forbidden changes (L.18.1)

- Flipping `BRAIN_BREADTH_RELSTR_MODE=authoritative` in any
  environment before Phase L.18.2 is explicitly opened.
- Mutating the return shape of `market_data.get_market_regime()`,
  `macro_regime_summary`, or any Phase A-L.17 diagnostics surface.
- Using breadth/RS rows to gate sizing, route selection, or scan
  admission in L.18.1.
- Writing to `scan_patterns`, `trading_paper_trades`,
  `trading_risk_state`, or `trading_macro_regime_snapshots` from
  the breadth service.
- Adding a 16th phase to `ops_health_model.PHASE_KEYS`. L.17 is
  intentionally absent from the health panel and L.18 follows.
- Calling `compute_and_persist` from any hot-path module
  (scanner, alerts, paper trading, stop engine). The only allowed
  call-sites are the scheduled sweep job and the diagnostics
  endpoint.
- Duplicating `classify_trend` - re-use L.17's definition via
  import.

## Verification gates

- Pure unit tests pass 100% for `breadth_relstr_model.py`.
- Docker soak `phase_l18_soak.py` reports `ALL GREEN`
  including the additive-only check.
- Release blocker exits 0 against 30m live log window.
- `GET /api/trading/brain/breadth-relstr/diagnostics` returns the
  frozen shape on a live service.
- `/api/trading/scan/status` regression still passes
  (`['ok','brain_runtime','prescreen','learning']`).
- `GET /api/trading/brain/ops/health` still returns 15 phases
  (unchanged by L.18).
- `GET /api/trading/brain/macro-regime/diagnostics` still returns
  the L.17 frozen shape.

## Rollback

1. Set `BRAIN_BREADTH_RELSTR_MODE=off` in `.env`.
2. `docker compose up -d --force-recreate chili brain-worker
   scheduler-worker` to drop the APScheduler job.
3. Data table stays in place (append-only, cheap). No reverse
   migration.

## Non-goals (L.18.1)

- Authoritative consumption (sizing / promotion / scan admission
  conditioning on `breadth_label` or `leader_sector`) - deferred
  to L.18.2.
- Real exchange advance/decline feeds (NYSE $ADVN, $DECN, etc.) -
  the ETF basket is a useful proxy; full A/D is a separate
  provider contract in L.18.2+.
- New-highs / new-lows across the entire US equity universe -
  covered by prescreener lists today; a structural count is a
  separate milestone.
- Crypto breadth / dominance / funding - that is L.20.
- Rewriting or replacing `correlation_budget.py` sector
  taxonomy - L.18 uses its own ETF basket.

## Definition of done (L.18.1)

`BRAIN_BREADTH_RELSTR_MODE=shadow` is live in all three services.
The daily `breadth_relstr_daily` job fires at 06:45 and appends
rows to `trading_breadth_relstr_snapshots`. The diagnostics
endpoint returns the frozen shape. The release blocker is clean
on live logs. All tests green. `get_market_regime()` and
`trading_macro_regime_snapshots` remain bit-for-bit unchanged
(verified by the soak). Plan YAML flipped to
`completed_shadow_ready` with closeout + self-critique + L.18.2
checklist.

## Closeout (2026-04-17)

**Status: shadow rollout shipped end-to-end. L.18.2 deferred for
authoritative consumer wiring + parity window.**

### What shipped

1. Migration `139_breadth_relstr_snapshot` + ORM
   `BreadthRelstrSnapshot` (schema drift: none; additive
   append-only table).
2. Pure model `app/services/trading/breadth_relstr_model.py` -
   14-symbol basket (11 sector SPDRs + SPY/QQQ/IWM), per-member
   trend/direction classification, A/D proxy, per-sector RS vs
   SPY 20d, size/style tilts, leader/laggard pick, composite
   `breadth_label`.
3. Unit tests `tests/test_breadth_relstr_model.py` - 20/20 green
   (determinism, frozen dataclass, tie-break, partial coverage,
   all-missing, size/style tilt sign).
4. Config flags (10 `BRAIN_BREADTH_RELSTR_*` settings on
   `app/config.py`).
5. Ops log `app/trading_brain/infrastructure/
   breadth_relstr_ops_log.py` - `[breadth_relstr_ops]` prefix,
   one-line structured format.
6. DB service `app/services/trading/breadth_relstr_service.py`:
   ETF fetch via `market_data.fetch_ohlcv_df`, mode gating,
   authoritative hard-refusal (RuntimeError), coverage-score
   gate, append-only write, read helpers.
7. APScheduler job `breadth_relstr_daily` (06:45, gated by
   `BRAIN_BREADTH_RELSTR_MODE`). Registered alongside L.17's
   06:30 macro regime job without collision.
8. Diagnostics endpoint
   `/api/trading/brain/breadth-relstr/diagnostics` +
   `tests/test_phase_l18_diagnostics.py` (frozen shape + lookback
   clamp 422).
9. Release blocker
   `scripts/check_breadth_relstr_release_blocker.ps1` - 5/5
   smoke tests pass (clean/auth/refused/diag-ok/diag-low-cov).
10. Docker soak `scripts/phase_l18_soak.py` - 41/41 checks green
    inside the running `chili` container, including the
    additive-only guard vs L.17's `trading_macro_regime_snapshots`.
11. `.env` flipped `BRAIN_BREADTH_RELSTR_MODE=shadow`; all three
    services recreated; scheduler registered
    `Breadth + RS daily (06:45; mode=shadow)`; diagnostics
    confirms `mode: "shadow"`; live-log release blocker PASS.
12. Docs `docs/TRADING_BRAIN_BREADTH_RELSTR_ROLLOUT.md` (rollout
    order, release blocker patterns, rollback, additive
    guarantees, L.18.2 pre-flight checklist).

### Regression bundle

- `scan_status` frozen contract: live probe returns
  `['brain_runtime','learning','ok','prescreen']` with
  `brain_runtime.release == {}` - unchanged.
- L.17 `macro-regime/diagnostics` still returns `mode: "shadow"`
  + frozen keys.
- `get_market_regime()` pre-L.17 keys (`spy_direction`,
  `spy_momentum_5d`, `vix`, `vix_regime`, `regime`,
  `regime_numeric`) untouched (soak asserts this on the L.17
  additive check, which L.18 inherits and extends).
- `ops_health` still reports 15 phases; L.18 intentionally
  absent (matches the forbidden-changes clause).
- L.18 pure tests 20/20 green locally with `chili-env`.

### Known operational gotchas

- PowerShell release-blocker scripts can hang when fed a
  completely empty pipeline (not L.18-specific; previously
  observed on divergence + ops_health). The L.18 script uses
  the same structure; the operator workaround (use an inline
  `Where-Object` pre-filter, then pipe only matching lines) is
  documented in the rollout doc.
- The scheduler job only registers on
  `CHILI_SCHEDULER_ROLE=web_light` / `scheduler-worker`, not on
  the Uvicorn web process (confirmed in live logs:
  `chili-1 | CHILI_SCHEDULER_ROLE=none`). No action needed;
  this matches L.17.
- Local pytest against the live Postgres container remains
  contention-prone (documented since Phase I). Pure-model tests
  always run locally; DB-touching validation is handled by the
  Docker soak.

### Self-critique

1. **Coverage gate semantics are asymmetric vs L.17.** L.17
   classifies a "cautious" label when coverage is partial-but-
   above-threshold; L.18 only uses the coverage gate to decide
   **persist vs skip**. That is fine for L.18.1 (we do not want
   bad readings polluting the history), but L.18.2 should
   consider a **partial-coverage label** so downstream consumers
   can distinguish "truly mixed" from "data missing, held to
   mixed by fallback". Ticket for L.18.2.
2. **Composite label ignores size tilt on neutral A/D.** The
   `_composite_breadth_label` docstring reserves `size_tilt`
   for future refinement but does not use it when the A/D
   ratio is neutral. Acceptable for L.18.1 - we want the
   simplest possible label while shadow data is collected - but
   this is the first thing L.18.2 should tune once there is
   enough history to measure the size-tilt signal quality.
3. **`breadth_relstr_daily` uses `web_light`-style cron.** The
   06:45 slot is picked to avoid colliding with L.17's 06:30
   macro regime job and the 06:15 divergence panel. If L.18.2
   adds a compare writer with a different cron, the choice of
   06:45 may need revisiting.
4. **Did not backfill prior days.** L.18.1 is live-going-forward
   only. The diagnostics endpoint starts at `snapshots_total=0`.
   That is fine for shadow, but L.18.2 must decide the backfill
   window before flipping consumers.
5. **Sector taxonomy is hard-coded.** The 11 GICS SPDRs are
   correct today (2026) but will drift when/if an index provider
   splits/merges sectors (see the XLRE split in 2016, the XLC
   split in 2018). A future migration should move the symbol
   list to config. Not a L.18.1 blocker.
6. **No parity writer yet.** L.18's `compare` mode is reserved
   in the mode enum but has no comparison target. L.18.2 must
   land that comparison (e.g. FINVIZ breadth, WSJ sector heat,
   or internal backtest-derived breadth) before opening
   authoritative.

### L.18.2 checklist (do not open without all of these)

1. **Authoritative consumer contract signed off.** Who reads
   `breadth_label`, `leader_sector`, `size_tilt`, and under
   which risk authority? What do they do with a
   `broad_risk_off` label (size down? block? alert only?).
2. **Parity comparison window.** Minimum 20 trading days of
   `compare` mode with an agreed external source; composite
   label disagreement <= 15%; `advance_ratio` correlation >=
   0.7 with the external A/D series.
3. **Backfill plan.** Either a one-shot backfill script for
   the history window consumers need, or an explicit decision
   to start authoritative with only go-forward history.
4. **Governance gate.** Flipping
   `BRAIN_BREADTH_RELSTR_MODE=authoritative` triggers an audit
   log entry and (optionally) a manual-approval requirement,
   mirroring L.17.2 and Phase I.
5. **Re-run full release-blocker + soak bundle after the
   authoritative flip** - both new blocker paths
   (`event=breadth_relstr_persisted mode=authoritative`) must
   stay at zero hits.
6. **Ops-health inclusion.** Add L.18 to
   `ops_health_model.PHASE_KEYS` only when authoritative - keep
   shadow out of the health panel to avoid false greens.
7. **Drift monitor.** Decide whether breadth label drift is
   something Phase J's drift monitor should watch. If yes,
   wire it; if no, document the decision.

The shadow rollout is ready to stay on indefinitely while the
L.18.2 contract is drafted. No further action on L.18.1 is
required.
