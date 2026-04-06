# Trading debt — invariants (predictions, backtests, cache, alerts)

Short reference for maintainers after the trading-service split and `public_api` surface.

## Prediction authority and mirror

- **Explicit ticker lists** passed to `get_current_predictions` set `explicit_api_tickers=True` for the prediction-mirror read path (non-empty list).
- **`None` or empty list**, SWR background refresh, and inferred universes use `explicit_api_tickers=False` (legacy-authoritative for mirror). See `learning.get_current_predictions` and `learning_predictions._get_current_predictions_impl`.
- Ops logging for prediction reads follows the frozen contract in `docs/TRADING_BRAIN_PREDICTION_MIRROR_ROLLOUT.md` (do not ship the release-blocker pattern described there).

## `compute_prediction` and modules

- **Canonical implementation:** `app.services.trading.learning_predictions.compute_prediction`.
- **`learning.compute_prediction`** is a re-export for backward compatibility and monkeypatch targets.

## Stale-while-revalidate prediction cache

- **Single-process assumption:** the in-memory `_pred_cache` in `learning.py` is not shared across workers or hosts. Multiple uvicorn workers or separate containers each hold their own cache.
- **Explicit ticker requests** bypass the cache.

## Backtest / pattern linkage

- **Source of truth:** `trading_backtests.scan_pattern_id` (and related FKs) must match the pattern card the UI or API is showing. Fixing wrong rows is a **data repair** job (scripts/migrations), not only a router filter.
- See `docs/TRADING_BACKTEST_DB_AUDIT.md` and project rules under “data first”.

## Alert tiers → SMS prefs

- `dispatch_alert` classifies via `classify_alert_tier` then calls `send_sms(message, tier=tier)` when SMS is configured and tier is **A** or **B** (tier **C** is log-only for SMS).
- **Tier A:** e.g. `target_hit`, `stop_hit`, promoted-pattern alerts with high confidence; **Tier B:** standard pattern-backed alerts; **Tier C:** speculative.

## Stable imports

- Prefer **`from app.services.trading import public_api`** for symbols listed in `public_api.__all__` instead of new imports of underscore helpers from `trading_service` or deep modules.
