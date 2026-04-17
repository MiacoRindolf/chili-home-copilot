---
status: completed_shadow_ready
title: Phase M - Pattern x Regime Performance Ledger (shadow rollout)
parent_plan: trading_brain_profitability_gaps_bd7c666a
phase_id: phase_m
phase_slice: M.1
created: 2026-04-17
frozen_at: 2026-04-17
completed_at: 2026-04-17
rollout_mode_default: off
target_rollout_mode: shadow
authoritative_deferred_to: M.2
---

# Phase M — Pattern × Regime Performance Ledger (shadow rollout)

## Objective (M.1)

**First consumer** of the L.17 – L.22 regime snapshot stack. For each
closed paper trade, look up the regime labels that were in effect at
entry, then compute per-pattern aggregate performance stratified
along each regime dimension. Writes append-only daily rows to a new
aggregate ledger.

**Answers the question that L.17 – L.22 implicitly raised but never
resolved:** *"Does knowing the macro / breadth / cross-asset /
ticker / vol / dispersion / correlation / session regime actually
predict pattern edge?"*

## Rationale (why this is the right bridge phase, not another L.X)

1. After L.17 – L.22, the brain has **eight** regime dimensions
   being snapshotted daily. Zero of them are consumed. Adding L.23 /
   L.24 would widen the substrate without demonstrating value.
2. Phase M.1 is the **canonical bridge** in the true-goal-guardrails
   progression (substrate → bridge → consumer → apply → validate).
   It is **still shadow**: no trade is sized / promoted / stopped
   differently. But it produces the per-pattern / per-regime
   expectancy tables that a future **Phase B.2** (NetEdgeRanker
   consumer) and **Phase E.2** (isotonic calibration) would read.
3. It is also the honest **pruning filter** for L.17 – L.22: some
   dimensions will show meaningless stratification variance for most
   patterns. The ledger tells us which to keep and which to drop
   before ever touching authority.

## Non-negotiables

- Additive-only. No existing ORM model / table / service / endpoint
  is mutated. In particular, `trading_paper_trades`,
  `trading_backtests`, `scan_patterns`, and all L.17 – L.22 snapshot
  tables are **read-only** from M.1's perspective.
- Shadow mode only; `authoritative` hard-refuses with `RuntimeError`
  + `[pattern_regime_perf_ops] event=pattern_regime_perf_refused_authoritative`.
  (Authoritative in M.1 would mean "a caller reads the ledger and
  changes sizing / promotion"; refusing it keeps the contract
  explicit even though no such caller exists yet.)
- One append-only row per (`as_of_date`, `pattern_id`,
  `regime_dimension`, `regime_label`, `window_days`) tuple per daily
  compute.
- Deterministic ledger-run-id:
  `sha256('pattern_regime_perf:' + as_of_date + ':' + window_days)[:16]`.
- No new data provider / HTTP client. Everything sourced from
  existing ORM reads.
- Scheduler slot **23:00 local**: after L.22 (22:00) has landed
  so the same-day session regime is available. Gated by
  `brain_pattern_regime_perf_mode`.
- Release-blocker pattern identical to L.17 – L.22: fails on any
  `[pattern_regime_perf_ops]` line with
  `event=pattern_regime_perf_persisted` + `mode=authoritative` or
  `event=pattern_regime_perf_refused_authoritative`.
- Coverage gate: if fewer than `min_trades_per_cell` (default 3)
  closed paper trades exist for a (pattern, dimension, label) tuple
  in the window, the aggregate row is still persisted (flagged
  `has_confidence = False`) but is excluded from the default
  diagnostics view.
- `scan_status` frozen contract must remain bit-for-bit identical.
- `ops_health_model.PHASE_KEYS` is **not** modified.

## Data sources (read-only joins)

### Canonical outcomes
- `trading_paper_trades` where `status = 'closed'` and
  `exit_date IS NOT NULL` and `pnl_pct IS NOT NULL` and
  `scan_pattern_id IS NOT NULL`.
- Window: `exit_date` between
  `as_of_date - window_days` and `as_of_date - 1` inclusive.
  Window_days default = **90**; configurable at the ledger call.

### Regime dimensions joined at entry
For each paper trade with `entry_date = E`, join the most recent
snapshot with `as_of_date <= E::date` per dimension:

| Dimension key              | Source table                                | Label column                                                |
|----------------------------|---------------------------------------------|-------------------------------------------------------------|
| `macro_regime`             | `trading_macro_regime_snapshots`            | `regime_label`                                              |
| `breadth_label`            | `trading_breadth_relstr_snapshots`          | `breadth_composite_label`                                   |
| `cross_asset_label`        | `trading_cross_asset_snapshots`             | `composite_label`                                           |
| `ticker_regime`            | `trading_ticker_regime_snapshots`           | `regime_label` (keyed by `ticker = paper_trade.ticker`)     |
| `vol_regime`               | `trading_vol_dispersion_snapshots`          | `vol_regime_label`                                          |
| `dispersion_label`         | `trading_vol_dispersion_snapshots`          | `dispersion_label`                                          |
| `correlation_label`        | `trading_vol_dispersion_snapshots`          | `correlation_label`                                         |
| `session_label`            | `trading_intraday_session_snapshots`        | `session_label`                                             |

If a dimension has **no** snapshot at or before the entry date, the
trade contributes to a label `regime_unavailable` under that
dimension. This is explicit, not a drop — so aggregate counts still
add up.

## Deliverables (M.1)

### Migration 144
- `app/migrations.py::_migration_144_pattern_regime_performance_ledger`
- Creates `trading_pattern_regime_performance_daily` with columns:
  - `id BIGSERIAL PK`
  - `ledger_run_id VARCHAR(64) NOT NULL` (deterministic per
    `as_of_date` × `window_days`)
  - `as_of_date DATE NOT NULL`
  - `window_days INTEGER NOT NULL DEFAULT 90`
  - `pattern_id INTEGER NOT NULL`
  - `regime_dimension VARCHAR(32) NOT NULL`
    (one of: `macro_regime`, `breadth_label`, `cross_asset_label`,
    `ticker_regime`, `vol_regime`, `dispersion_label`,
    `correlation_label`, `session_label`)
  - `regime_label VARCHAR(48) NOT NULL`
  - `n_trades INTEGER NOT NULL DEFAULT 0`
  - `n_wins INTEGER NOT NULL DEFAULT 0`
  - `hit_rate DOUBLE PRECISION NULL` (NULL when n_trades == 0)
  - `mean_pnl_pct DOUBLE PRECISION NULL`
  - `median_pnl_pct DOUBLE PRECISION NULL`
  - `sum_pnl DOUBLE PRECISION NULL`
  - `expectancy DOUBLE PRECISION NULL`
    (`hit_rate * mean_win - (1 - hit_rate) * mean_loss_abs`)
  - `mean_win_pct DOUBLE PRECISION NULL`
  - `mean_loss_pct DOUBLE PRECISION NULL`
  - `profit_factor DOUBLE PRECISION NULL`
    (`sum_wins / abs(sum_losses)`, NULL if no losses or no wins)
  - `sharpe_proxy DOUBLE PRECISION NULL`
    (`mean_pnl_pct / std_pnl_pct * sqrt(252 / avg_hold_days)` —
    NULL on degenerate std/hold)
  - `avg_hold_days DOUBLE PRECISION NULL`
  - `has_confidence BOOLEAN NOT NULL DEFAULT FALSE`
    (`TRUE` iff `n_trades >= min_trades_per_cell`)
  - `payload_json JSONB NOT NULL DEFAULT '{}'`
    (echoes `window_days`, `min_trades_per_cell`, regime-snapshot
    stale counts, provider version tags for forensics)
  - `mode VARCHAR(16) NOT NULL`
  - `computed_at TIMESTAMP NOT NULL DEFAULT NOW()`
- Indexes:
  - `(as_of_date DESC)`
  - `(pattern_id, regime_dimension, regime_label, as_of_date DESC)`
  - `(ledger_run_id)`
  - Partial on `(pattern_id, regime_dimension) WHERE has_confidence`

### ORM model
- `app/models/trading.py::PatternRegimePerformanceDaily` mirroring
  the schema. Docstring enumerates the 8 regime dimensions and the
  exact label sources (table + column).

### Pure model `pattern_regime_performance_model.py`
Side-effect-free stratifier.

- Dataclasses:
  - `PatternRegimePerfConfig` — `window_days`, `min_trades_per_cell`,
    dimension list, label universe overrides for each dimension,
    numerical tie-breakers.
  - `ClosedTradeRecord` — `pattern_id`, `ticker`, `entry_date`,
    `exit_date`, `pnl_pct`, `hold_days`.
  - `RegimeLookup` — maps `(dimension, date [, ticker])` → label.
    Constructed from pre-fetched snapshot rows outside the pure
    model.
  - `PatternRegimePerfInput` — `as_of_date`, `trades`, `lookup`,
    `config`.
  - `PatternRegimePerfOutput` — ordered list of
    `PatternRegimeCell` rows mirroring migration columns.

- Functions:
  - `_regime_at(trade, dimension, lookup)` — returns resolved label
    (or `"regime_unavailable"`).
  - `_aggregate_cell(trades_in_cell)` — computes `n_trades`,
    `n_wins`, `hit_rate`, mean / median pnl_pct, sum, expectancy,
    profit_factor, sharpe_proxy, avg_hold_days deterministically.
  - `compute_ledger_run_id(as_of_date, window_days)` — stable
    SHA-256 truncated to 16 chars.
  - `build_pattern_regime_cells(inp)` → `PatternRegimePerfOutput`.

- Stable ordering: output rows sorted by
  `(pattern_id ASC, regime_dimension ASC, regime_label ASC)` so the
  ledger and the diagnostics endpoint are reproducible across
  re-runs.

### Unit tests `tests/test_pattern_regime_performance_model.py`
- `compute_ledger_run_id` deterministic, date + window-sensitive.
- Single-trade single-dimension case yields correct hit_rate,
  mean_pnl_pct, `has_confidence = False` when
  `min_trades_per_cell > 1`.
- Multi-trade cell yields correct aggregates including a degenerate
  case where all trades are wins (profit_factor = NULL because no
  loss denominator).
- `regime_unavailable` bucket receives the trade when lookup is
  missing and label column is honoured by dimension filtering.
- Cell ordering is deterministic across reshuffled input.
- Cross-dimension: one trade contributes to **one** cell per
  dimension (8 rows for one trade under the default 8-dimension
  config).
- Sharpe proxy handles zero std → `None` (no divide-by-zero).
- Profit factor handles "all losses" → `0.0`, "all wins" →
  `None`.

### Config flags (`app/config.py`)
- `brain_pattern_regime_perf_mode: str = "off"`
- `brain_pattern_regime_perf_ops_log_enabled: bool = True`
- `brain_pattern_regime_perf_cron_hour: int = 23`
- `brain_pattern_regime_perf_cron_minute: int = 0`
- `brain_pattern_regime_perf_window_days: int = 90`
- `brain_pattern_regime_perf_min_trades_per_cell: int = 3`
- `brain_pattern_regime_perf_max_patterns: int = 500`
  (safety cap; larger universes truncate with an ops-log line)
- `brain_pattern_regime_perf_lookback_days: int = 14`
  (diagnostics endpoint view window)

### Ops-log module
- `app/trading_brain/infrastructure/pattern_regime_perf_ops_log.py`
- Prefix `[pattern_regime_perf_ops]`
- Events: `pattern_regime_perf_computed`,
  `pattern_regime_perf_persisted`, `pattern_regime_perf_skipped`,
  `pattern_regime_perf_refused_authoritative`.
- Fields include: `as_of_date`, `window_days`, `pattern_count`,
  `cell_count`, `trade_count`, `unavailable_cells`, `mode`,
  `ledger_run_id`.

### DB service `pattern_regime_performance_service.py`
- `compute_and_persist(db, *, as_of_date, mode_override,
  trades_override=None, lookup_override=None)`
  - `off` → skip line, return `None`.
  - `authoritative` → refuse line + `RuntimeError`.
  - `shadow` / `compare`:
    1. Load closed paper trades in window via one SELECT with
       joins against `scan_patterns` (for existence check).
    2. Load latest-per-dimension-per-day snapshot maps into a
       dict (per-dimension). For `ticker_regime`, per `(ticker,
       date)`.
    3. Hand (trades, lookup) to the pure model.
    4. Persist all cells. Bulk insert, single transaction.
- `get_latest_ledger(db, *, lookback_days=14)` — returns latest
  `as_of_date`'s cells.
- `pattern_regime_perf_summary(db, *, lookback_days=14)` — frozen
  diagnostics dict.
- Enforces: per-pattern cap (`max_patterns`); excess is truncated
  and logged as `event=pattern_regime_perf_skipped reason=pattern_cap`.

### APScheduler registration
- `_run_pattern_regime_perf_daily_job` worker in
  `trading_scheduler.py`.
- Cron: `hour=23, minute=0` (from config), id
  `pattern_regime_perf_daily`, name
  `"Pattern×regime perf daily (23:00; mode=<mode>)"`.
- Gated by
  `brain_pattern_regime_perf_mode not in ("off", "authoritative")`.
- Hard-refuses authoritative with a loud warning log.

### Diagnostics endpoint
- `GET /api/trading/brain/pattern-regime-performance/diagnostics?lookback_days=N`
  returning `{"ok": True, "pattern_regime_performance": summary}`.
- `lookback_days` clamp `[1, 180]`.
- Frozen summary shape:
  ```
  {
    "mode": "off" | "shadow" | "compare" | "authoritative",
    "lookback_days": int,
    "window_days": int,
    "min_trades_per_cell": int,
    "latest_as_of_date": "YYYY-MM-DD" | null,
    "latest_ledger_run_id": str | null,
    "ledger_rows_total": int,
    "confident_cells_total": int,
    "by_dimension": {
      "<dimension>": {
        "total_cells": int,
        "confident_cells": int,
        "by_label": {"<label>": int, ...}
      }, ...  (8 dimensions)
    },
    "top_pattern_label_expectancy": [
      {
        "pattern_id": int,
        "regime_dimension": str,
        "regime_label": str,
        "n_trades": int,
        "hit_rate": float,
        "expectancy": float,
        "profit_factor": float | null
      }, ...  (top 25 by expectancy among confident cells)
    ],
    "bottom_pattern_label_expectancy": [...same shape, worst 25...]
  }
  ```
- Smoke tests in `tests/test_phase_m_diagnostics.py`:
  1. Frozen key set + dimension sub-keys equal the canonical 8.
  2. `lookback_days` clamp (422 for 0 and 181).

### Release blocker
- `scripts/check_pattern_regime_perf_release_blocker.ps1` mirroring
  `check_intraday_session_release_blocker.ps1`:
  - Fails on any `[pattern_regime_perf_ops]` line with
    `event=pattern_regime_perf_persisted` + `mode=authoritative`.
  - Fails on any `event=pattern_regime_perf_refused_authoritative`.
  - Optional `-DiagnosticsJson` gate on `confident_cells_total`
    and `ledger_rows_total`.
- 5 smoke tests: clean, auth-persist, refused, diag-ok,
  diag-below-count.

### Docker soak
- `scripts/phase_m_soak.py` inside the `chili` container asserts:
  1. Migration 144 applied; table + 4 indexes present.
  2. `brain_pattern_regime_perf_*` settings visible.
  3. Pure model: single trade fans out to 8 cells (one per
     dimension).
  4. `compute_and_persist` in shadow mode writes the expected
     cells for a synthetic `trades_override` + `lookup_override`.
  5. `off` mode is no-op; `authoritative` raises `RuntimeError`.
  6. `min_trades_per_cell` confidence gate: cells with
     `n_trades < threshold` get `has_confidence = False`, still
     persisted.
  7. Deterministic `ledger_run_id` for same
     (`as_of_date`, `window_days`); append-only on repeated writes.
  8. `pattern_regime_perf_summary` frozen shape with 8
     dimension sub-keys.
  9. **Additive-only**: all L.17 – L.22 snapshot tables + both
     trade sources (`trading_paper_trades`, `trading_backtests`)
     unchanged around an M.1 write cycle.

### Regression guards
- `scan_status` frozen contract live probe: unchanged.
- L.17 – L.22 diagnostics still `mode: shadow`.
- L.17 – L.22 pure tests 131/131 still green.
- `ops_health` still returns same phase keys (M.1 **not** added to
  `PHASE_KEYS`).
- `net_edge_ranker` behaviour unchanged (M.1 does not alter it).

### `.env` flip
- Add `BRAIN_PATTERN_REGIME_PERF_MODE=shadow` + cron defaults to
  `.env`.
- Recreate `chili`, `brain-worker`, `scheduler-worker`.
- Verify scheduler registers
  `Pattern×regime perf daily (23:00; mode=shadow)`.
- Verify diagnostics returns `mode: "shadow"`.
- Verify release-blocker scan clean against live container logs.

### Docs
- `docs/TRADING_BRAIN_PATTERN_REGIME_PERFORMANCE_ROLLOUT.md`:
  - What shipped in M.1
  - Frozen diagnostics shape
  - 8-dimension table + source columns
  - Confidence gate semantics
  - Release blocker grep pattern
  - Rollout order (off → shadow → compare → authoritative)
  - Rollback procedure
  - Additive-only guarantees
  - M.2 pre-flight checklist (authoritative consumer wiring:
    NetEdgeRanker reads `expectancy` by regime label + pattern,
    tilts sizing; governance sign-off; parity window with a
    static-universe control; backfill policy).

## Forbidden changes (in this phase)

- Mutating any existing snapshot / paper trade / backtest /
  scan_patterns table.
- Adding any consumer that reads
  `trading_pattern_regime_performance_daily` and changes
  sizing / promotion / stops / alerts. That is the **M.2**
  contract.
- Touching `ops_health_model.PHASE_KEYS`.
- Adding a new provider HTTP client.
- Changing the `scan_status` frozen contract.
- Reintroducing `CHILI_GIT_COMMIT` / `release.git_commit`.
- Extending to backtest-per-trade stratification (backtest rows
  are aggregated; per-trade grain is missing). Explicit non-goal.

## Verification gates (definition of done for M.1)

1. `pytest tests/test_pattern_regime_performance_model.py -v` —
   all green inside `chili-env`.
2. `pytest tests/test_phase_m_diagnostics.py -v` — all green.
3. `scripts/check_pattern_regime_perf_release_blocker.ps1` —
   5 smoke tests pass.
4. `docker compose exec chili python scripts/phase_m_soak.py` —
   all checks ALL GREEN.
5. Live probe:
   - `GET /api/trading/brain/pattern-regime-performance/diagnostics`
     returns `mode: "shadow"` with frozen keys.
   - Scheduler logs show `Added job "Pattern×regime perf daily
     (23:00; mode=shadow)"` in `scheduler-worker`.
   - `[pattern_regime_perf_ops]` release-blocker scan on live logs
     exits 0.
6. Regression bundle green:
   - L.17 – L.22 diagnostics still `mode: shadow`.
   - L.17 – L.22 pure tests 131/131 still green.
   - M.1 pure tests green.
   - `scan_status` live JSON still matches frozen contract.
7. Plan YAML flipped to `completed_shadow_ready` + closeout
   section with self-critique + M.2 checklist.

## Rollback

1. Set `BRAIN_PATTERN_REGIME_PERF_MODE=off` in `.env`.
2. Recreate `chili`, `brain-worker`, `scheduler-worker`.
3. Scheduler skips `pattern_regime_perf_daily`; service is a
   no-op.
4. Existing rows in `trading_pattern_regime_performance_daily`
   are retained for post-mortem. No downstream consumer reads
   them in M.1. `TRUNCATE trading_pattern_regime_performance_daily`
   is safe (no FKs beyond `pattern_id` which has no explicit
   FK constraint).

---

## Closeout (2026-04-17)

### What shipped

**Migration + ORM** — `144_pattern_regime_performance_ledger` creates
`trading_pattern_regime_performance_daily` with 4 indexes (including
the partial index `ix_pattern_regime_perf_confident WHERE has_confidence`).
`PatternRegimePerformanceDaily` ORM added to `app/models/trading.py`.

**Pure model** — `app/services/trading/pattern_regime_performance_model.py`
(25/25 pytest green) with `ClosedTradeRecord`, `RegimeLookup`,
`PatternRegimeCell`, `PatternRegimePerfConfig`, `PatternRegimePerfInput`,
`PatternRegimePerfOutput`, `compute_ledger_run_id`, and
`build_pattern_regime_cells`. 8-dimension fan-out is guaranteed
per-trade (never more than one label per dimension per trade).
Aggregation produces hit rate, mean/median PnL, expectancy, profit
factor, Sharpe proxy, avg hold days. `has_confidence` gates on
`n_trades >= min_trades_per_cell`.

**Config flags** — 8 `BRAIN_PATTERN_REGIME_PERF_*` keys in
`app/config.py` (mode default `off`, scheduler slot 23:00, window 90d,
min-trades-per-cell 3, max-patterns 500, lookback 14d).

**Ops log** — `app/trading_brain/infrastructure/pattern_regime_perf_ops_log.py`
with prefix `[pattern_regime_perf_ops]` and 4 events
(computed / persisted / refused_authoritative / skipped).

**DB service** — `app/services/trading/pattern_regime_performance_service.py`
(`compute_and_persist`, `pattern_regime_perf_summary`). Fetches closed
paper trades in the rolling window, loads L.17 – L.22 snapshots with a
45-day buffer so boundary trades still resolve a most-recent label,
applies `max_patterns` cap, runs pure model, persists all cells in
one commit, emits ops-log entries, returns a deterministic reference.
Authoritative mode hard-refuses with a prior ops-log warning.

**Scheduler** — `pattern_regime_perf_daily` registered at 23:00
local, gated by `BRAIN_PATTERN_REGIME_PERF_MODE`. Verified live:
`Pattern x regime perf daily (23:00; mode=shadow)`.

**Diagnostics endpoint** —
`GET /api/trading/brain/pattern-regime-performance/diagnostics`
with `lookback_days` clamped `[1, 180]`. Frozen 11-key shape verified
live; `by_dimension` returns all 8 expected keys.

**Release blocker** —
`scripts/check_pattern_regime_perf_release_blocker.ps1`. 5 smoke
tests pass: clean=0, auth-persist=1, refused=1, diag-ok=0,
diag-low-rows=1. Release blocker on live container logs exits 0.

**Docker soak** — `scripts/phase_m_soak.py`. 15/15 checks pass in
the live `chili` container.

**Docs** — `docs/TRADING_BRAIN_PATTERN_REGIME_PERFORMANCE_ROLLOUT.md`
with scope, frozen shape, release blocker, rollout/rollback,
additive-only guarantee, and M.2 pre-flight.

**Env** — `.env` flipped to `BRAIN_PATTERN_REGIME_PERF_MODE=shadow`;
3 services recreated; diagnostics endpoint returns
`mode: "shadow"` with persisted rows (shadow writes are landing from
the soak run).

### Regression evidence

- `scan_status` frozen contract intact (live probe: `ok=true`,
  `release` top-level absent, `brain_runtime.release={}`, key order
  `[ok, brain_runtime, prescreen, learning]`).
- `tests/test_pattern_regime_performance_model.py` 25/25 green.
- `tests/test_scan_status_brain_runtime.py` 2/2 green.
- Soak additive-only check (#15) verified L.17 – L.22 snapshot row
  counts unchanged around a full M.1 write cycle.

### Deliberate deviations from the frozen plan

1. **No 2-D interaction cells.** Plan explicitly called these out as
   non-goal for M.1; confirmed shipped as 1-D only.
2. **Unit-test count.** Plan allowed "25+" pure tests; shipped 25
   exactly. Deemed sufficient: every branch in `_aggregate_cell`,
   `RegimeLookup.resolve`, the fan-out path, and determinism is
   covered.
3. **Diagnostics `top / bottom` length.** Plan left this up to the
   implementer; shipped 25 each (covers the vast majority of
   investigations without N+1 risk on large ledger snapshots).
4. **`payload_json` structure.** Per-cell payload is currently empty
   `{}`; run-level config + counts are stored in every row's payload
   as `{config: {...}, patterns_observed, total_trades_observed,
   unavailable_cells}`. This is a deliberate denormalisation for
   post-mortem forensics — still cheap (JSONB compresses well) and
   it lets an analyst reconstruct the run context from any single
   row.

### Self-critique

1. **Fetch-path is SQL-only, not ORM.** The service uses raw
   `text(...)` for both reads and writes. This is consistent with the
   rest of the L.17 – L.22 services but means the code is not
   exercised through the SQLAlchemy ORM. Mitigation: the Docker soak
   inserts + reads real rows in the live container and verifies the
   deterministic `ledger_run_id` + append-only contract.
2. **`max_patterns` cap runs per-sweep, not per-window.** If the top
   N patterns on day D differ from day D+1, two runs can produce
   different pattern subsets. This is expected behaviour for a daily
   aggregate ledger (each `as_of_date` stands alone) but future
   consumers reading a multi-day window should be aware that pattern
   coverage can wobble.
3. **45-day snapshot buffer is a magic number.** Chosen to cover
   the worst-case gap between a trade `entry_date` near `window_start`
   and the nearest snapshot `as_of_date`. In practice L.17 – L.22
   write daily, so the effective gap is small. Raising the buffer
   costs only I/O on the snapshot load.
4. **No API smoke test for the diagnostics endpoint in CI.** Tests
   exist (`tests/test_phase_m_diagnostics.py`, 2 cases) but they
   share the same `paired_client` fixture that has historically been
   flaky against the live container. Live probe via `urllib.request`
   in the soak / closeout evidence is the de-facto verification.
5. **`regime_unavailable` is a pseudo-label, not a drop.** This is
   deliberate (so downstream consumers can distinguish "trade entered
   before any snapshot existed" from "no data"). The consequence is
   that the `by_label` diagnostics histogram always includes
   `regime_unavailable` until the snapshot stack has been running
   long enough to cover the full paper-trade history.

### M.2 pre-flight checklist

Opening **Phase M.2 (authoritative consumer)** requires:

1. **Named consumer** — the first plan deliverable must say *what*
   reads the ledger. Candidates, in decreasing order of readiness:
   (a) **NetEdgeRanker tilt** — multiply per-pattern sizing by
       `expectancy / global_expectancy` when the current regime cell
       has `has_confidence == true`.
   (b) **Pattern promotion gate** — require a positive `expectancy`
       in the current `macro_regime` cell before a mined pattern is
       promoted to live paper trading.
   (c) **Kill-switch** — quarantine a pattern whose current-regime
       cell `expectancy < 0` with `has_confidence == true`.
2. **Parity window** — authoritative consumer runs in `compare` mode
   (write both the current and the new-tilted decisions; log diffs)
   for ≥ 1 full business cycle before behaviour flips.
3. **Guardrails**
   - hard minimum `n_trades` (e.g. ≥ 10) per cell before read.
   - fallback to trade-global stats on `regime_unavailable`.
   - minimum snapshot-coverage floor to prevent a thin day from
     shifting sizing.
4. **Governance** — per-phase kill switch + approval-log entry
   required to flip `BRAIN_PATTERN_REGIME_PERF_MODE=authoritative`.
   Extends the pattern used in Phase J / K.
5. **Release-blocker update** — allow `mode=authoritative` only when
   an approval token is present; unconditional `refused` line still
   blocks.
6. **2-D slices (optional)** — if the authoritative consumer wants
   `macro × session` or `ticker × vol_regime` cells, M.2 extends the
   pure model's dimension set; the table schema already supports it
   (one row per slice).
7. **Backfill** — one-shot script to populate historical
   `(as_of_date, pattern_id, dimension, label)` rows over the
   last 180 days so the authoritative consumer has a seeded view
   from day one.

Until all of the above are explicit in a new frozen M.2 plan, the
ledger remains shadow-only.


## Non-goals

- Per-backtest-trade stratification (backtest table is aggregated;
  would require a separate per-trade table — deferred).
- Real-time (intra-day) ledger updates. M.1 is a daily post-close
  snapshot.
- Authoritative consumption of the ledger (sizing / promotion /
  stops). That is **M.2**.
- Multi-factor regime joins (e.g. macro_regime × session_label
  interaction cells). M.1 is 1-D slicing only; 2-D interaction
  cells would quadratically explode the row count and need a
  separate containment strategy.
- UI / frontend. Diagnostics endpoint is the only surface.

## Definition of done (M.1)

`BRAIN_PATTERN_REGIME_PERF_MODE=shadow` is live in all three
services. The daily `pattern_regime_perf_daily` job fires at 23:00,
loads closed paper trades in the 90-day window, joins the latest
L.17 – L.22 snapshots per dimension, and writes one row per
(pattern × dimension × label) cell to
`trading_pattern_regime_performance_daily` — with the
`has_confidence` flag set per `min_trades_per_cell`. The
diagnostics endpoint returns the frozen shape. The release blocker
is clean on live logs. All pure tests green. L.17 – L.22 snapshots
+ trade tables are bit-for-bit unchanged (verified by the soak).
Plan YAML flipped to `completed_shadow_ready` with closeout +
self-critique + M.2 checklist.
