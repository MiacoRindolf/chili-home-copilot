# Brain pattern attribution (live vs research)

CHILI links **closed trades** to **ScanPattern** rows via `Trade.scan_pattern_id`. The endpoint `live_vs_research_by_pattern` (see `app/services/trading/attribution_service.py`) compares:

- Research fields on `ScanPattern`: `win_rate`, `oos_win_rate`, `oos_avg_return_pct`
- Live aggregates: closed trades in the window with that `scan_pattern_id`

## How trades get `scan_pattern_id`

1. **Broker-connected proposal execution** — `_execute_proposal` in `app/services/trading/alerts.py` sets `scan_pattern_id` from `StrategyProposal.signals_json` when entries include `scan_pattern_id`, `pattern_id`, or `scanPatternId`.
2. **Manual / no-broker path** — the same helper is used so manual recorded trades also pick up the ID when present on the proposal.
3. **Alerts / picks** — other code paths that create `Trade` rows should pass through the same signal shape so proposals carry the pattern id from the scanner or Brain UI.

## Dashboard coverage

`/api/trading/brain/stats` includes `attribution_coverage`: percentage of **closed** trades for the logged-in user that have `scan_pattern_id` set. Low coverage means live vs research panels are uninformative even if patterns promote cleanly.

## Optional auto downgrade

With `brain_live_depromotion_enabled=true` and `brain_default_user_id` set, each learning cycle runs `run_live_pattern_depromotion`: patterns that are **promoted** but whose **live** win rate trails **research OOS** by more than `brain_live_depromotion_max_gap_pct` (with at least `brain_live_depromotion_min_closed_trades` closes) are set `active=false` and `promotion_status=degraded_live`.
