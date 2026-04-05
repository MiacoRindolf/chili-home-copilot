# Broker Execution Audit — Robinhood Integration

**Date:** 2026-04-04
**File:** `app/services/broker_service.py` (1347 lines)
**Scope:** Order placement reliability, position sync accuracy, session management, rate limiting, error handling.

---

## Executive Summary

The Robinhood integration via `robin_stocks` is **functional but has several reliability gaps** that could cause silent failures, position drift, or missed fills in production. The issues range from **High** (could lose money) to **Low** (cosmetic/logging).

---

## Findings (severity-ranked)

### HIGH — Could cause incorrect positions or missed trades

| # | Issue | Location | Detail |
|---|-------|----------|--------|
| H1 | **No timeout on order placement** | `place_buy_order`, `place_sell_order` | `robin_stocks` HTTP calls use default urllib timeout (no limit). A hung API call blocks the caller indefinitely. | 
| H2 | **Partial fill not handled** | `sync_orders_to_db` L1240-1255 | When RH state is `"filled"`, it sets `trade.status = "open"`. But `partially_filled` maps to `"working"` and is never rechecked for the filled portion. If an order partially fills then stays `partially_filled` for hours, the filled shares are invisible to the local DB. |
| H3 | **Position sync drift: stale-close race** | `sync_positions_to_db` L821-860 | Trades not found in `rh_tickers` are auto-closed with a market quote. If Robinhood API returns an incomplete list (pagination, network glitch), legitimate open positions get closed prematurely. No confirmation step or staleness threshold. |
| H4 | **No order confirmation polling** | `place_buy_order`, `place_sell_order` | After placing, the function returns immediately. If the order is `queued` or `unconfirmed`, there is no automatic follow-up. The `sync_orders_to_db` job must be separately scheduled — if it's delayed, fill events are missed. |

### MEDIUM — Degrades reliability or observability

| # | Issue | Location | Detail |
|---|-------|----------|--------|
| M1 | **Session TTL is too aggressive** | `_LOGIN_TTL = 3600` (1h) | Robinhood sessions last ~24h with `store_session=True`. Re-authenticating every hour burns TOTP codes unnecessarily and could trigger rate limits or lockouts. Suggest 14400 (4h) with refresh-on-401 logic. |
| M2 | **Cache TTL masks stale data** | `_CACHE_TTL = 300` (5 min) | Portfolio and position data is cached 5 minutes. During fast-moving markets, cached data could lead to wrong position sizing or missed stop-loss levels. Suggest 60s for positions, 120s for portfolio. |
| M3 | **No retry on transient HTTP errors** | All `robin_stocks` calls | Network blips (502, 503, connection reset) cause immediate failure. One retry with backoff would prevent false failures. |
| M4 | **`is_connected()` silently re-authenticates** | L556-564 | If the session expired and TOTP is configured, `is_connected()` calls `login()` — which can block and fail. Callers don't expect that. Should separate "is session alive" from "try to reconnect". |
| M5 | **No rate-limit detection** | All order/data calls | Robinhood API rate-limits at ~1 req/s. Burst requests (e.g. during position sync + order placement) could get 429s. No exponential backoff implemented. |

### LOW — Cosmetic, logging, or minor robustness

| # | Issue | Location | Detail |
|---|-------|----------|--------|
| L1 | **Pickle-based session persistence** | `_complete_login` L439-449 | Persists auth tokens as pickles in `~/.tokens/`. Pickle files are a security risk if the filesystem is shared. Consider encrypted storage or environment-variable-based token caching. |
| L2 | **`_safe_float` swallows parse errors** | L1132-1138 | Returns 0.0 for any non-numeric value. A `"N/A"` string from RH becomes 0.0 silently, which could affect P&L calculations. Should log at DEBUG level. |
| L3 | **Instrument URL resolution is unbounded** | `_resolve_instrument_ticker` | Makes an HTTP call per unknown instrument URL. No rate limit or batch resolution. Could be slow during initial sync of many orders. |
| L4 | **`cleanup_manual_trades` lacks dry-run mode** | L863-917 | Auto-closes manual trades that don't match RH positions. This is destructive and irreversible. A `dry_run` parameter would help debugging. |
| L5 | **Pattern Day Trader (PDT) warning not checked** | Order placement | No pre-flight check for PDT restrictions (< $25k account, 3+ day trades in 5 days). Robinhood will reject the order, but the error message may be cryptic. |

---

## Recommended Fixes (priority order)

1. **Add request timeout wrapper** around all `robin_stocks` HTTP calls (10s for data, 15s for orders).
2. **Handle `partially_filled` state** — track cumulative quantity, update Trade model for the filled portion while keeping the remainder as `working`.
3. **Add a staleness threshold** to `sync_positions_to_db` — don't auto-close a trade unless it's been missing from RH for at least 2 consecutive sync cycles.
4. **Implement order confirmation polling** — after `place_buy_order`/`place_sell_order`, start a background task that polls the order every 5s for up to 60s until it reaches a terminal state.
5. **Lower cache TTL for positions** to 60s and add a `force_refresh` parameter.
6. **Add 1-retry with exponential backoff** for HTTP 429/502/503 responses.
7. **Lengthen `_LOGIN_TTL`** to 14400s and add refresh-on-401 fallback.
8. **Add PDT pre-flight check** before order placement using account info.

---

## Test Gap Analysis

- **No unit tests exist** for `broker_service.py`. All functions depend on a live Robinhood API.
- **Recommended test files:**
  - `tests/test_broker_service.py` — mock `robin_stocks.robinhood` module, test order placement, sync logic, error handling, cache behavior.
  - `tests/test_broker_sync.py` — test position drift scenarios (missing tickers, partial fills, concurrent syncs).
- **Integration test:** Run `sync_positions_to_db` with a paper trading account to validate round-trip accuracy.
