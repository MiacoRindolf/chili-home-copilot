# Pattern trade feature schema v1

`feature_schema_version` = **`1`**

All keys are optional unless noted; missing keys mean “not computed”.

| Key | Type | Description |
|-----|------|-------------|
| `schema` | string | Always `"1"` |
| `backtest_return_pct` | number | Aggregate backtest return % for the run |
| `backtest_win_rate` | number | Aggregate win rate (0–100) |
| `backtest_trade_count` | int | Total trades in run |
| `trade_return_pct` | number | Simulated trade return % (this row) |
| `entry_price` | number | Entry price |
| `exit_price` | number | Exit price |
| `hold_bars_est` | int | Estimated bars held (from bar timestamps / timeframe) |
| `rel_volume` | number | Relative volume if available from indicators |
| `atr_pct` | number | ATR as % of price if computable from OHLC window |
| `close_vs_sma20` | number | Close / SMA20 - 1 if SMA20 in indicators |
| `pattern_match_strength` | number | 0–1 if pattern engine exposes it |

Future versions may add regime, sector, SPY context, earnings flags, etc.
