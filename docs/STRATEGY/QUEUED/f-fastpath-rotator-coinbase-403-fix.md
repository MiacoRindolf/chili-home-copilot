# f-fastpath-rotator-coinbase-403-fix

STATUS: QUEUED
SLUG: fastpath-rotator-coinbase-403-fix
PROPOSED: 2026-05-07 evening
SEVERITY: HIGH (blocks all rotator activity; supersedes the top-of-book fix)

## TL;DR

Coinbase Exchange `https://api.exchange.coinbase.com/products` returns
**HTTP 403 Forbidden** to every container on the operator's Docker
network (chili, scheduler-worker, fast-data-worker — all three
verified 2026-05-07 ~22:11 UTC). Sandbox-egress (different IP) gets
394 products fine. WS to `wss://advanced-trade-ws.coinbase.com` works
from the same containers (fast-path alerts firing live). REST-only
block, almost certainly Cloudflare bot-detection on the rotator's
default User-Agent `chili-fast-path-rotator/1`.

The rotator's `_http_get_json` is the only blocked path. Once unblocked,
the rotator can do its scan and the prior `top_of_book` workaround
(env var disabling that gate) becomes irrelevant in scope but still
correct for ship-it pragmatism.

## Symptom

```python
import urllib.request
urllib.request.urlopen('https://api.exchange.coinbase.com/products', timeout=8)
# HTTPError: HTTP Error 403: Forbidden
```

Reproduces from all three Docker containers. The `chili-fast-path-rotator/1`
User-Agent is sent by `_http_get_json` in `app/services/trading/fast_path/universe_rotator.py:99`.

Prior memory entries note similar 451/403 issues for other crypto APIs:
- `project_binance_geoblock.md`: fapi.binance.com returns 451 from US.
- `project_universal_egress_block.md`: ALL crypto/finance APIs TCP-unreachable from every container 2026-04-29 (later resolved).
- `project_massive_blocked.md`: Massive/Polygon TCP-refuse from host.

This is not a fresh egress block — Coinbase WS is fine. It's specifically
the REST endpoint with the rotator's UA hitting Cloudflare's bot rules.

## Goal

Replace `urllib.request` in `_http_get_json` with the same HTTP client
that the existing `coinbase_ohlcv` module uses — which **is** working
(scheduler-worker logs show `coinbase_ohlcv` successfully making
hundreds of `/products/{id}/candles?granularity=86400` calls every
hour, with proper 200 responses for valid products and 404s for
delisted ones).

`app/services/trading/coinbase_ohlcv.py` is the reference. It uses
either `requests` with proper headers or `curl_cffi` (used elsewhere
in the codebase per memory `project_F-leak-3 yf Thread leak fix`).
Reuse whatever it does.

## Acceptance criteria

1. `app/services/trading/fast_path/universe_rotator.py:_http_get_json`
   uses the same HTTP client/headers as `coinbase_ohlcv`.
2. From inside scheduler-worker:
   ```
   docker exec ... python -c "from app.services.trading.fast_path.universe_rotator import _http_get_json; r = _http_get_json('https://api.exchange.coinbase.com/products'); print('count:', len(r) if r else 'NONE')"
   ```
   prints `count: 8XX` (not None).
3. After force-recreate + manual rotator trigger:
   - Rotator pass takes ~95 s (real Coinbase scan).
   - `fast_path_universe` populated with ≥ 25 rows in `status='shadow'`.
   - The earlier env workaround `CHILI_FAST_PATH_UNIVERSE_MIN_TOP_OF_BOOK_USD=0`
     is REMOVED. The proper top-of-book gate (or its proper
     `/products/{id}/book?level=1` replacement from
     `f-fastpath-rotator-top-of-book-fix`) takes effect.
4. Tests: `tests/test_fastpath_universe_rotator.py` updated to inject a
   test HTTP client that mirrors the new shape.
5. CC report.

## Brain integration (reuse, don't rewrite)

- `app/services/trading/coinbase_ohlcv.py` — copy its HTTP-client
  pattern (requests session + headers + retry).
- Memory `reference_fleak3_yf_thread_leak_fix.md` — the curl_cffi shared
  Session pattern from yfinance fix may apply here too.

## Constraints / do not touch

- Hard Rule 1: live-placement safety belts unchanged.
- No change to gate thresholds (still settings-tunable).
- No new magic numbers.
- Edit-tool truncation discipline: any edit to universe_rotator.py
  uses the splice pattern from the start (per memory
  `reference_2026_05_07_widespread_truncation`).

## Out of scope

- Coinbase Advanced Trade `/api/v3/brokerage/...` (different host,
  different auth model — separate brief if Exchange continues to
  block).
- IP rotation / proxy.
- VPN integration.

## Bundling decision

This brief is **strictly higher priority than `f-fastpath-rotator-top-of-book-fix`**.
Recommend bundling them as a single CC run because both are in the
same file, both are HTTP-shape changes, and shipping them together
avoids a thrash where the operator has to recreate twice.

Proposed combined NEXT_TASK title:
**`f-fastpath-rotator-coinbase-fixes-bundle`**.

Includes:
1. Replace `_http_get_json` with the working coinbase_ohlcv pattern.
2. Add `_fetch_book` helper for `/products/{id}/book?level=1`.
3. Update `_fetch_pair_snapshot` to call `_fetch_book` for top-of-book
   sizes.
4. Remove the env workaround line for `CHILI_FAST_PATH_UNIVERSE_MIN_TOP_OF_BOOK_USD=0`.
5. Update tests for both changes.
