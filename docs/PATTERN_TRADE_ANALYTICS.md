# Pattern trade analytics

## Unit of observation

Each row in `trading_pattern_trades` (`PatternTradeRow`) is **one simulated trade** (or future: one pattern match) at an **`as_of_ts`** aligned to the **entry** of that trade.

- **Outcomes** for v1: `outcome_return_pct` and `label_win` come from the backtesting engine’s trade record. Forward returns (`fwd_ret_*b`) may be filled when OHLC path analysis is added.
- **Features** live in `features_json` with `feature_schema_version` (see [pattern_trade_features_v1.md](pattern_trade_features_v1.md)).

## Entry assumption

Simulated trades use the engine’s **entry bar timestamp** as `as_of_ts`. Entry-on-close vs next-open is determined by the strategy implementation, not this table.

## Evidence hypotheses

`trading_pattern_evidence_hypotheses` stores **analytics-derived** rules (bucket splits, filters) distinct from `trading_hypotheses` (A/B dynamic pool). Status: `proposed` → `validated` → `applied` → `retired`.

## APIs

- `GET /api/trading/brain/pattern/{pattern_id}/trade-analytics` — aggregates, buckets, stability flags.

## Related code

- Ingestion: `app/services/trading/pattern_trade_storage.py`
- Features: `app/services/trading/pattern_trade_features.py`
- Analysis: `app/services/trading/pattern_trade_analysis.py`
- Apply (dry-run): `app/services/trading/pattern_evolution_apply.py`
