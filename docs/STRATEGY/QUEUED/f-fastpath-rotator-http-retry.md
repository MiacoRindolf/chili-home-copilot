# f-fastpath-rotator-http-retry

STATUS: QUEUED
SLUG: fastpath-rotator-http-retry
PROPOSED: 2026-05-08
SEVERITY: high (rotator runs but produces 0 actionable rows)

## TL;DR

Rotator's `_http_get_json` has no retry on TCP-layer failures. Live observation 2026-05-08: 371 of 394 per-pair calls fail with `Errno 101 Network is unreachable`, a TCP-layer flakiness in Docker Desktop's NAT. The 23 pairs that succeed all happen to be high-volume (BTC/ETH/etc., already in the universe); mid-tier pairs we want to add are failing every pass. **Add retry-with-backoff.**

## Why now

Stack of all rotator fixes shipped today is in place:
- 403 fix (commit `727456e`): default UA bypasses Cloudflare ✅
- top_of_book endpoint fix (commit `727456e`): `/book?level=1` ✅
- auction_mode filter fix (commit `bb6a4e4`): unblocks 393 of 394 USD products ✅
- `.env` volume threshold $10M → $2M: lets mid-tier through ✅
- All 4 maker-only settings + mig 232 ✅

But none of it matters at runtime: 94% of pair-level HTTP calls fail at TCP. The rotator returns `snapshot_failures=371`, `ranked_n=0`, `promoted_to_shadow=0` despite all 23 successful fetches passing through.

## Symptom (verified live, 2026-05-08 06:57 UTC)

```
{'scanned': 394, 'snapshot_failures': 371,
 'gate_rejections': {'volume_below_threshold': 22, 'top_of_book_below_threshold': 1},
 'ranked_n': 0, 'promoted_to_shadow': 0}
```

Scheduler-worker logs show:
```
[coinbase_ohlcv] X-USD ... request failed: Max retries exceeded ... Failed to establish a new connection: [Errno 101] Network is unreachable
```

repeated for ~370 pairs across the rotator pass. The `_http_get_json` swallows this as `None` and the per-pair snapshot also returns `None`.

## Goal

Add retry-with-backoff to `_http_get_json` in `app/services/trading/fast_path/universe_rotator.py`. Specifically:

1. **3 retry attempts** with exponential backoff (e.g., 0.5s, 1.0s, 2.0s).
2. **Retryable on:** `requests.exceptions.ConnectionError` (which wraps Errno 101), `requests.exceptions.Timeout`, HTTP 503/429.
3. **Non-retryable (give up immediately):** HTTP 4xx (except 429), JSON decode errors.
4. **Per-call total budget cap:** 8s timeout + 3 × 0.5/1.0/2.0s sleeps = ~12s max per call vs ~8s today. Acceptable budget bump.

Optional follow-on (out of scope for this brief):
- Drop pacing from 0.12s → 0.20s if even with retries we still see flakiness.
- Use `requests.Session` with a `urllib3.util.Retry` adapter (more idiomatic than manual loop). Worth a separate brief if cleanup matters; the manual loop is simpler.

## Acceptance criteria

1. After fix, rotator pass logs show:
   - `snapshot_failures` < 50 (down from 371).
   - `promoted_to_shadow` ≥ 10.
2. Per-pass duration goes from ~3.5min (fast-fail-on-connection-error) to ~5-7min (retries succeeding).
3. New helper-level test: `tests/test_fastpath_universe_rotator_retry.py` exercises retry-on-connection-error using a stubbed `requests.get` that fails twice then succeeds.
4. Existing 7 helper tests still pass.

## Brain integration (reuse, don't rewrite)

- `requests.exceptions` already imported via `requests`.
- The existing `_http_get_json` shape stays the same; just internal retry loop added.
- `_PER_REQ_PACING_S` and `_HTTP_TIMEOUT_S` constants stay.

## Constraints / do not touch

- **Edit-tool truncation discipline (HARD).** Splice pattern only for `universe_rotator.py`. Memory `reference_2026_05_07_widespread_truncation.md`.
- No threshold tuning.
- No format change to `_PairCandidate` or other downstream consumers.
- Backoff total ≤ 12s per call so the rotator pass completes in <10min.

## Out of scope

- Universal HTTP retry policy across all Coinbase callers (separate brief if needed; coinbase_ohlcv has its own retry pattern already).
- Dropping pacing.
- Switching to async / aiohttp.
- VPN / proxy / IP rotation.

## Sequencing

1. Truncation scan.
2. Splice-replace `_http_get_json` with retry-loop version.
3. Add helper test.
4. Verify with manual rotator trigger.
5. Commit + push.

## Rollback

`git revert` the commit. The retry path is purely internal to `_http_get_json`; removal restores prior fast-fail behavior.

## Operational state at filing time

- Working copy at commit `bb6a4e4` (auction_mode fix shipped).
- `.env` has volume threshold $2M.
- Rotator runs cleanly but produces 0 promoted rows due to per-pair HTTP flakiness.
- `fast_path_universe` empty.
- Hourly cron will keep retrying; some passes may succeed when Docker NAT happens to be cooperative.

## Notes for the assigned CC session

- The "Network is unreachable" failure is NOT a Cloudflare/Coinbase issue — `/products` (no path) works fine in the same pass. It's intermittent TCP routing flakiness inside Docker Desktop's NAT.
- A `requests.Session` + `urllib3.Retry` adapter would be cleaner than a manual loop. Consider it for the implementation.
- Budget for ~5-7 min total pass time post-fix (was 3.5min with all-fail, will be longer with retries succeeding).
