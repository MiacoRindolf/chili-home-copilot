# Trading brain — execution realism + venue truth (Phase F)

Per-ticker rolling spread/slippage estimates, capacity cap, a
**fraction-of-notional cost model**, and venue-truth telemetry to
observe expected vs realized fills. Ships in **shadow** only.

## What shipped

### Data
* Migration **132** creates two Phase-F tables:
  * `trading_execution_cost_estimates(id, ticker, side, window_days,
    median_spread_bps, p90_spread_bps, median_slippage_bps,
    p90_slippage_bps, avg_daily_volume_usd, sample_trades,
    last_updated_at)` with `UNIQUE (ticker, side, window_days)`.
  * `trading_venue_truth_log(id, trade_id, ticker, side, notional_usd,
    expected_spread_bps, realized_spread_bps, expected_slippage_bps,
    realized_slippage_bps, expected_cost_fraction,
    realized_cost_fraction, paper_bool, mode, created_at)`.
* ORM rows: `ExecutionCostEstimate`, `VenueTruthLog` in `app/models/trading.py`.

### Code
* Pure `app/services/trading/execution_cost_model.py`:
  * `estimate_cost_fraction(ticker, side, notional, estimate_row)` returns
    `CostFractionBreakdown(spread, slippage, fees, impact, total)` with
    every component expressed as a fraction of notional.
  * `estimate_capacity_usd(estimate_row, max_adv_frac=0.05)` returns the
    max notional that keeps us under `max_adv_frac` of rolling ADV.
* Builder `app/services/trading/execution_cost_builder.py`:
  * `compute_rolling_estimate` derives p50/p90 spread + slippage from
    closed `Trade.tca_*_slippage_bps` rows in the window.
  * `upsert_estimate` / `rebuild_all` - idempotent UPSERT on the UNIQUE
    key. Logs `[execution_cost_ops]`.
  * `estimates_summary` powers the diagnostics endpoint.
* Venue-truth `app/services/trading/venue_truth.py`:
  * `FillObservation` dataclass + `record_fill_observation` write one
    row to `trading_venue_truth_log` and emit `[venue_truth_ops]`.
  * `venue_truth_summary` aggregates mean/p90 cost-gap and worst-N
    tickers.

### Configuration
```
BRAIN_EXECUTION_COST_MODE=shadow        # off | shadow | authoritative
BRAIN_EXECUTION_COST_DEFAULT_FEE_BPS=1.0
BRAIN_EXECUTION_COST_IMPACT_CAP_BPS=50.0
BRAIN_EXECUTION_CAPACITY_MAX_ADV_FRAC=0.05

BRAIN_VENUE_TRUTH_MODE=shadow
BRAIN_VENUE_TRUTH_OPS_LOG_ENABLED=true
```

### API
* `GET /api/trading/brain/execution-cost/diagnostics`
  → `{mode, estimates_total, tickers, by_side, stale_estimates,
      stale_threshold_hours, last_refresh_at}`.
* `GET /api/trading/brain/venue-truth/diagnostics`
  → `{mode, lookback_hours, observations_total,
      mean_expected_cost_fraction, mean_realized_cost_fraction,
      mean_gap_bps, p90_gap_bps, worst_tickers[]}`.

### Ops log grammar
```
[execution_cost_ops] event=<estimate_write|cost_query|summary> mode=<mode> ticker=... ...
[venue_truth_ops]    event=<fill_observation|summary>         mode=<mode> ticker=... ...
```

Blocker signature for both is `mode=authoritative` while rollout stays in
shadow. `[venue_truth_ops]` additionally reports `cost_gap_bps` per fill.

## Related bug fix (Phase F)
`app/services/trading/market_data.py` previously short-circuited
`fetch_ohlcv`, `fetch_ohlcv_df`, and `get_quote` to empty when
`massive_aggregate_variants_all_dead(ticker)` was true. That is correct
for crypto (which legitimately exhausts the `X:BASE{USD,USDT,USDC}`
candidate set) but wrong for equities where Polygon/yfinance are
entirely separate data pipes. The gate is now
`_massive_dead and _massive.is_crypto(ticker)`.

## Rollout ladder
1. `BRAIN_EXECUTION_COST_MODE=off, BRAIN_VENUE_TRUTH_MODE=off` - baseline.
2. Flip both to `shadow` - writers populate the tables, ops logs stream,
   nothing consumes the estimates.
3. Run soak: `python scripts/phase_f_soak.py` → must exit 0.
4. Collect a sample day of real paper fills; check
   `venue_truth_summary.mean_gap_bps` is within ±5bps (Phase F target).
5. Only after sign-off: flip `BRAIN_EXECUTION_COST_MODE=authoritative`
   so NetEdgeRanker (Phase E) consumes the per-ticker estimate. This is
   **NOT** the current phase; it is a future phase gated by production
   soak output.

## Rollback
* Set `BRAIN_EXECUTION_COST_MODE=off` and/or `BRAIN_VENUE_TRUTH_MODE=off`.
  Rows already in the tables are harmless.
* Dead-cache fix is a pure bug fix; to revert, restore
  `if _massive_dead: return []` in `fetch_ohlcv` / `fetch_ohlcv_df` /
  `get_quote`.

## Mandatory release blockers (before cutover)
Run BOTH before shipping:

```powershell
docker compose logs chili --since 30m 2>&1 | .\scripts\check_execution_cost_release_blocker.ps1
docker compose logs chili --since 30m 2>&1 | .\scripts\check_venue_truth_release_blocker.ps1
```

Both must exit 0. Optional extended gates:

```powershell
curl -sk https://localhost:8000/api/trading/brain/execution-cost/diagnostics -o ec.json
.\scripts\check_execution_cost_release_blocker.ps1 -DiagnosticsJson ec.json -MinEstimates 10

curl -sk https://localhost:8000/api/trading/brain/venue-truth/diagnostics -o vt.json
.\scripts\check_venue_truth_release_blocker.ps1 -DiagnosticsJson vt.json -MinObservations 5 -MaxMeanGapBps 20
```

## Known limitations
* ADV fallback: when `adv_lookup_fn` is not provided and no live price
  source is wired, the estimator approximates ADV as
  `sum(trade_notional) / window_days`. That is a conservative under-
  estimate — good for capacity cap, poor for impact modelling. Wire a
  real ADV source before flipping to authoritative.
* No per-asset-class fee schedule. Fees are a single global bps.
* No real-time quote telemetry; observations are fill-based only.
* Only Phase-F paper trades emit venue-truth observations today. Live
  broker reconciliation writes are a future phase (Phase G bracket
  reconciliation).
