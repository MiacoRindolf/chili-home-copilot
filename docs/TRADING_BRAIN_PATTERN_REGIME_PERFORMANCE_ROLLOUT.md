# Phase M.1 — Pattern × Regime Performance Ledger Rollout (Shadow)

## Scope

Phase M.1 ships the **first consumer** of the L.17 – L.22 regime
snapshot stack: a **shadow-only** daily ledger that stratifies closed
paper-trade performance by `pattern_id` and by regime label along **8
dimensions**. No downstream consumer (scanner, promotion, sizing,
alerts, NetEdgeRanker, playbook) reads this table in M.1 — it is
observability + offline analysis only.

The 8 regime dimensions (all 1-D slicing — no 2-D interactions yet):

| Dimension              | Source table                                       | Source column                        |
|------------------------|----------------------------------------------------|--------------------------------------|
| `macro_regime`         | `trading_macro_regime_snapshots` (L.17)            | `regime_label`                       |
| `breadth_label`        | `trading_breadth_relstr_snapshots` (L.18)          | `breadth_composite_label`            |
| `cross_asset_label`    | `trading_cross_asset_snapshots` (L.19)             | `composite_label`                    |
| `ticker_regime`        | `trading_ticker_regime_snapshots` (L.20)           | `regime_label` (ticker-keyed)        |
| `vol_regime`           | `trading_vol_dispersion_snapshots` (L.21)          | `vol_regime_label`                   |
| `dispersion_label`     | `trading_vol_dispersion_snapshots` (L.21)          | `dispersion_label`                   |
| `correlation_label`    | `trading_vol_dispersion_snapshots` (L.21)          | `correlation_label`                  |
| `session_label`        | `trading_intraday_session_snapshots` (L.22)        | `session_label`                      |

Each closed trade (from `trading_paper_trades` where `status='closed'`)
is resolved to the most-recent-at-or-before-`entry_date` snapshot
label per dimension; if no snapshot exists, the label is explicit:
`regime_unavailable`.

## Authority contract

- Phase M.1 is **shadow-only**. `brain_pattern_regime_perf_mode` may
  be `off`, `shadow`, or `compare`. Setting it to `authoritative`
  causes the service to emit
  `[pattern_regime_perf_ops] event=pattern_regime_perf_refused_authoritative`
  and raise `RuntimeError`. Phase M.2 opens the authoritative path
  with explicit governance + parity window.
- No downstream consumer reads
  `trading_pattern_regime_performance_daily` in M.1. It is a pure
  observability table.

## Ship list

1. **Migration 144** (`144_pattern_regime_performance_ledger`) —
   creates `trading_pattern_regime_performance_daily` with 4 indexes:
   `ix_pattern_regime_perf_as_of`, `ix_pattern_regime_perf_lookup`
   (`pattern_id, regime_dimension, regime_label, as_of_date DESC`),
   `ix_pattern_regime_perf_run`, and a partial index
   `ix_pattern_regime_perf_confident` (`WHERE has_confidence`).
2. **ORM model** `PatternRegimePerformanceDaily` in
   `app/models/trading.py`.
3. **Pure model**
   `app/services/trading/pattern_regime_performance_model.py`
   (dataclasses `ClosedTradeRecord`, `RegimeLookup`,
   `PatternRegimeCell`, `PatternRegimePerfConfig`,
   `PatternRegimePerfInput`, `PatternRegimePerfOutput`; functions
   `compute_ledger_run_id`, `build_pattern_regime_cells`). 25/25 unit
   tests pass.
4. **Ops log** `app/trading_brain/infrastructure/pattern_regime_perf_ops_log.py`
   with prefix `[pattern_regime_perf_ops]` and events
   `pattern_regime_perf_computed` / `_persisted` /
   `_refused_authoritative` / `_skipped`.
5. **DB service**
   `app/services/trading/pattern_regime_performance_service.py`:
   `compute_and_persist`, `pattern_regime_perf_summary`. Loads trades
   from `trading_paper_trades` (window `exit_date` in
   `[as_of - window_days, as_of - 1 day]`), loads L.17 – L.22
   snapshot rows with a 45-day buffer so boundary entries can still
   resolve a most-recent label, runs the pure model, and appends one
   row per `(pattern_id, regime_dimension, regime_label)` tuple.
6. **APScheduler job** `pattern_regime_perf_daily` at **23:00 local**,
   gated by `BRAIN_PATTERN_REGIME_PERF_MODE`. Registration is
   conditional on `include_web_light` and mode != `off`/`authoritative`.
7. **Diagnostics endpoint**
   `GET /api/trading/brain/pattern-regime-performance/diagnostics`
   (frozen shape, `lookback_days` clamped to `[1, 180]`).
8. **Release blocker**
   `scripts/check_pattern_regime_perf_release_blocker.ps1` (5 gates —
   all smoke-tested locally: clean → 0, authoritative-persisted → 1,
   refused → 1, diagnostics OK → 0, diagnostics below min-rows → 1).
9. **Docker soak** `scripts/phase_m_soak.py` — 15/15 checks pass in
   the live `chili` container.

## Frozen diagnostics shape

```
{
  "ok": true,
  "pattern_regime_performance": {
    "mode": "shadow",
    "lookback_days": 14,
    "window_days": 90,
    "min_trades_per_cell": 3,
    "latest_as_of_date": "YYYY-MM-DD" | null,
    "latest_ledger_run_id": "<16-hex>" | null,
    "ledger_rows_total": N,
    "confident_cells_total": N,
    "by_dimension": {
      "macro_regime":      { "total_cells": N, "confident_cells": N, "by_label": {...} },
      "breadth_label":     { "total_cells": N, "confident_cells": N, "by_label": {...} },
      "cross_asset_label": { "total_cells": N, "confident_cells": N, "by_label": {...} },
      "ticker_regime":     { "total_cells": N, "confident_cells": N, "by_label": {...} },
      "vol_regime":        { "total_cells": N, "confident_cells": N, "by_label": {...} },
      "dispersion_label":  { "total_cells": N, "confident_cells": N, "by_label": {...} },
      "correlation_label": { "total_cells": N, "confident_cells": N, "by_label": {...} },
      "session_label":     { "total_cells": N, "confident_cells": N, "by_label": {...} }
    },
    "top_pattern_label_expectancy":    [ /* top 25 confident cells, expectancy DESC */ ],
    "bottom_pattern_label_expectancy": [ /* bottom 25 confident cells, expectancy ASC */ ]
  }
}
```

## Release blocker

A line is a blocker if it contains **both**
`[pattern_regime_perf_ops]` **and** any of:
- `event=pattern_regime_perf_persisted mode=authoritative`
- `event=pattern_regime_perf_refused_authoritative`

Optionally the script also fails when the diagnostics JSON reports
`ledger_rows_total < MinLedgerRows` or
`confident_cells_total < MinConfidentCells` (when these gates are
explicitly requested).

```powershell
docker compose logs chili --since 30m 2>&1 |
  .\scripts\check_pattern_regime_perf_release_blocker.ps1

curl.exe -sk "https://localhost:8000/api/trading/brain/pattern-regime-performance/diagnostics?lookback_days=14" -o prp.json
.\scripts\check_pattern_regime_perf_release_blocker.ps1 `
  -DiagnosticsJson .\prp.json -MinLedgerRows 1
```

Exit 0 = pass. Exit 1 = blocker line or failed gate.

## Rollout (shadow, this phase)

1. Set in `.env`:
   ```
   BRAIN_PATTERN_REGIME_PERF_MODE=shadow
   BRAIN_PATTERN_REGIME_PERF_OPS_LOG_ENABLED=true
   BRAIN_PATTERN_REGIME_PERF_CRON_HOUR=23
   BRAIN_PATTERN_REGIME_PERF_CRON_MINUTE=0
   BRAIN_PATTERN_REGIME_PERF_WINDOW_DAYS=90
   BRAIN_PATTERN_REGIME_PERF_MIN_TRADES_PER_CELL=3
   BRAIN_PATTERN_REGIME_PERF_MAX_PATTERNS=500
   BRAIN_PATTERN_REGIME_PERF_LOOKBACK_DAYS=14
   ```
2. `docker compose up -d --force-recreate chili brain-worker scheduler-worker`.
3. Verify:
   - `Pattern x regime perf daily (23:00; mode=shadow)` in
     `scheduler-worker` logs.
   - `/api/trading/brain/pattern-regime-performance/diagnostics`
     returns `mode: "shadow"`.
   - Release blocker on live logs — expect exit 0.

## Rollback

1. Set `BRAIN_PATTERN_REGIME_PERF_MODE=off` in `.env`.
2. `docker compose up -d --force-recreate chili brain-worker scheduler-worker`.
3. Verify
   `/api/trading/brain/pattern-regime-performance/diagnostics` now
   reports `mode: "off"` and the scheduler log no longer registers
   the `pattern_regime_perf_daily` job. Existing persisted rows
   remain for forensic review — no data is deleted.

## Phase M.2 pre-flight (not in this phase)

Before opening authoritative consumption:

1. **Decide the consumer** — e.g. NetEdgeRanker tilts per-pattern
   sizing by `expectancy` within the current regime cell.
2. **Confirm parity window**: the authoritative consumer must first
   run in `compare` mode (or equivalent) for a full business cycle
   before any sizing / scanner / promotion behaviour reads the
   ledger.
3. **Add guardrails** on the read path:
   - minimum `n_trades` per cell (`has_confidence == true`)
   - fallback to the trade-global stats when
     `regime_label == "regime_unavailable"`
   - coverage floor on snapshot availability
4. **Governance**: require an explicit kill-switch toggle + approval
   log entry for flipping `mode=authoritative`, modelled on the
   existing `chili_governance` pattern used in Phase J / K.
5. **Blocker update**: extend the release-blocker to allow an
   authoritative event only when governance approval is present; keep
   refusal blocking unconditionally.

## Additive-only guarantee

Phase M.1 touches **only**:
- new migration `144`
- new table `trading_pattern_regime_performance_daily`
- new pure model + service + scheduler job + endpoint + ops-log +
  release-blocker script + soak script + docs

No existing L.17 – L.22 tables, services, schedulers, endpoints, or
ops logs are modified. The soak verifies row-count of peer
L.17 – L.22 snapshot tables is unchanged around a full M.1 write
cycle (check 15).
