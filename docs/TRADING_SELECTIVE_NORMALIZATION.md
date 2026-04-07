# Trading / backtest selective normalization

This document describes **targeted** schema normalization for repeated payloads. It is **not** a 3NF warehouse redesign. **Retention and dedupe** remain the primary tools for storage growth; normalization addresses **identical JSON blobs** stored on many `trading_backtests` rows.

## Candidates reviewed

| Area | Repeated? | Stable? | Storage win? | Verdict |
|------|-----------|---------|--------------|---------|
| `BacktestResult.params` (provenance, window, KPI refs) | Yes — same window/config across many tickers/insights | Yes per canonical JSON | **Yes** — large JSON duplicated often | **Normalize now** (`trading_backtest_param_sets`) |
| `BacktestResult.equity_curve` | Unique per run | No | Better via retention / null archive | **Do not normalize** |
| `PatternTradeRow.features_json` | Mostly unique per trade | No | Low | **Do not normalize** (revisit only if metrics show heavy duplication) |
| `brain_activation_events` / `brain_fire_log` | Append-only logs | N/A | Retention, not joins | **Do not normalize** |
| Strategy / timeframe “dimensions” | Partially repeated in text columns | Low value vs FK to `scan_patterns` already | Marginal | **Maybe later** if query patterns need it |

## What was normalized

### Table `trading_backtest_param_sets`

| Column | Purpose |
|--------|---------|
| `id` | Surrogate PK |
| `param_hash` | **Unique** SHA-256 (hex) of canonical JSON |
| `params_json` | Canonical JSONB payload (deduplicated) |
| `created_at` | Insert time |

### `trading_backtests.param_set_id`

- Nullable `INTEGER` FK → `trading_backtest_param_sets(id)` `ON DELETE SET NULL`.
- **`params` remains** on `trading_backtests` as a **compatibility shadow** (readers/APIs continue to work; new code also resolves via `materialize_backtest_params`).

## Canonicalization and hashing

1. **Recursive dict key sort** — keys at every object level sorted lexicographically as strings; lists keep order (semantic).
2. **JSON serialization** — `json.dumps(..., sort_keys=True, separators=(',', ':'), ensure_ascii=True, default=str)`.
3. **Hash** — SHA-256 hex digest of UTF-8 bytes of that string.

**Collision handling:** SHA-256 collisions are treated as impossible for this use case. A unique constraint on `param_hash` ensures one row per digest; concurrent inserts use a savepoint + `IntegrityError` retry path.

## Rollout / backfill

1. Migration **`088_backtest_param_sets`** — creates `trading_backtest_param_sets`, adds `param_set_id` to `trading_backtests`.
2. **New writes** — `save_backtest` and `POST .../backtest/{id}/refresh` call `get_or_create_backtest_param_set` and set `param_set_id` while still writing `params`.
3. **Backfill** — `scripts/backfill_backtest_param_sets.py` (`--dry-run` supported) for existing rows with `params` but null `param_set_id`.

## Read / write paths

- **Write:** [`app/services/backtest_service.py`](../app/services/backtest_service.py) `save_backtest`; [`app/routers/trading_sub/ai.py`](../app/routers/trading_sub/ai.py) `api_refresh_backtest`.
- **Read:** [`materialize_backtest_params`](../app/services/trading/backtest_param_sets.py) — prefers denormalized `params`, falls back to `param_set_id` → `params_json`. Used by stored backtest GET/refresh responses, evidence list, `stored_backtest_rerun`, `ai_context`.

## Future candidates

- If storage reports show many identical `features_json` blobs, consider a small feature-dictionary table **after** measuring duplication.
- If `params` shadow is retired, switch bulk readers (e.g. KPI aggregates) from `BacktestResult.params` to a join or denormalized view.

## Related

- Param analytics unit: [`docs/PATTERN_TRADE_ANALYTICS.md`](PATTERN_TRADE_ANALYTICS.md)
- Retention sweep: [`app/services/trading/data_retention.py`](../app/services/trading/data_retention.py)
