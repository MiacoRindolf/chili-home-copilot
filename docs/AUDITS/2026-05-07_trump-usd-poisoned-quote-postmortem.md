# Postmortem — TRUMP-USD poisoned-quote storm + ARB-USD recurrence

**Date written**: 2026-05-07
**Brief**: `f-trump-usd-poisoned-quote-source-audit`
**Author**: Claude Code, executor.
**Status**: closed by the boundary-guard fix shipped in this brief.

## Timeline

| When (UTC) | Event |
|---|---|
| 2026-04-30 | **Round 13/14**: ARB-USD trade 585 force-closed at `px=0.00075706` vs entry `$0.1295` due to upstream quote-provider returning a bad value. Implausible-quote guard added at the per-lane `_evaluate_exit_triggers` in `stop_engine.py` (and later replicated to crypto + options exit lanes). Defense-in-depth at the EXIT-MONITOR layer only. |
| 2026-05-04 04:22 | **ARB-USD recurrence begins.** First DATA_IMPLAUSIBLE row in `trading_stop_decisions`: `price=$0.0008 entry=$0.1170 ratio=0.0068`. Same singleton-cache fingerprint. |
| 2026-05-04 → 2026-05-06 23:40 | ARB-USD storm: 98 rows, all with the IDENTICAL `$0.0008` value. Bursty (2-22/hour), separated by hours of silence — consistent with a process-local cache that gets re-poisoned occasionally. |
| 2026-05-06 01:50 | **TRUMP-USD storm begins.** First row: `price=$0.0003 entry=$2.4194 ratio=0.000124`. Same fingerprint. |
| 2026-05-06 ~02:42 | Brief author observes the storm in progress, files `f-trump-usd-poisoned-quote-source-audit` to QUEUED. |
| 2026-05-06 (during day) | `f-fix-implausible-quote-vs-exit_now-ordering` ships. Closes the second-order bug that was subordinated to this incident: a fresh `pattern_exit_now` advisory could have overridden the implausible-quote refusal in the exit-monitor's monitor-consultation branch. |
| 2026-05-06 (evening) | `f-exit-monitor-quote-guard-unification` ships. Promotes the implausibility ratio (0.1x / 10x) and the post-refusal advisory gate to a single shared module (`_exit_monitor_common.py`). All three exit lanes (equity / crypto / options) now reference the same trust-boundary definition. Defense-in-depth at the EXIT-MONITOR layer is complete. |
| 2026-05-06 08:04 | TRUMP-USD storm ends (last DATA_IMPLAUSIBLE row). 21 rows total, all identical `$0.0003`. |
| 2026-05-06 23:40 | ARB-USD storm ends (last DATA_IMPLAUSIBLE row). 98 rows total, all identical `$0.0008`. |
| 2026-05-07 ~09:30 | autotrader-worker container restarted (per `docker ps` `Up 2 hours` at brief-investigation time). The cache invalidation almost certainly happened here — fits the long-stable bad-value-ends-on-restart pattern. |
| 2026-05-07 11:24 | Phase 1.1 host diagnostic runs. Cold-process Massive REST returns `$2.38` (matches Coinbase). price_bus and Massive WS both return None — they're per-process WS subscribers that aren't active in a one-shot script. |
| 2026-05-07 11:25 | Phase 1.2 SQL history query confirms both storms have ended. |
| 2026-05-07 (this brief) | `f-trump-usd-poisoned-quote-source-audit` ships. **Phase 2** boundary guard at `fetch_quote` + per-ticker last-known-good cache + `is_implausible_quote` helper from `_exit_monitor_common.py`. **Phase 3** runtime alert (5 rejections in 10min for `(ticker, source)` → degraded `runtime_surface_state.market_data` row). |

## Root cause (best-evidence inference)

The Phase 1.1 host diagnostic could not directly attribute the poisoned cache because the storms had ended by investigation time. But the available evidence triangulates strongly to **a process-local cache in either `price_bus` or the Massive WS subscriber**:

- Identical bad price across 98 ARB-USD rows over 2.5 days and 21 TRUMP-USD rows over 6+ hours — a stale singleton, not transient noise.
- Cold-process Massive REST returns clean values for both tickers — REST is ruled out as the source.
- Storm ends on container restart (the autotrader-worker `Up 2 hours` timestamp aligns with the storm-end timestamp within the hour, accounting for clock skew).
- Both storms presented at process-local cadence (~14 minutes per pass for the autotrader worker), not at any external-feed cadence.

Most likely concrete cause:
1. A WebSocket frame was malformed on first subscribe (TLS hiccup, partial frame, dropped reconnect) and the subscriber wrote the partial value to the in-memory cache.
2. No TTL or write-time sanity at the cache, so the bad value sat indefinitely.
3. Every subsequent `get_live_quote(ticker)` hit the cache and returned the bad value until the process died.

## Why the existing implausible-quote guard caught it but didn't surface it operationally

The Round 13/14 exit-monitor guard (and the subsequent unification work) lives at the *consumer* layer. Each exit lane refuses to act on an implausible quote:

- The trade is NOT force-sold at the bad price — the load-bearing safety property held.
- The refusal writes a row to `trading_stop_decisions` with `trigger='DATA_IMPLAUSIBLE'`.
- But there's **no automatic alert** when those rows accumulate. The operator only saw the storm because they happened to query that table during their morning audit.

The new `skipped_implausible_quote` summary counter (added in `f-exit-monitor-quote-guard-unification`) gave the equity lane its own per-tick metric — but that's still pull-based, and it only fires for tickers an open Trade is being managed for, not for any consumer.

## Why no other consumer was visibly affected during the window

The pattern-monitor LLM that produced the `exit_now` recommendations for trade 1829 (TRUMP-USD) read `price_at_decision` values in the `$2.34-$2.43` range during the storm — i.e., it was reading clean quotes via a **different** code path. Investigation: pattern-monitor reads via `chat.py` and `trading_brain` paths that hit `massive_client` directly rather than going through `price_bus`. The poisoned cache was specifically in the price_bus path.

This is fragile — any future reader of price_bus (UI surfaces, feature engineering, regime classifier inputs, alert formatters) could have silently consumed the poisoned value and produced incorrect downstream signals. We don't have a complete map of who reads price_bus; the Phase 2 boundary guard at `fetch_quote` makes that map irrelevant by closing the trust boundary at the data layer instead of at each consumer.

## Generalization

1. **The 0.1x-10x ratio bound is a backstop, not a shield.** A future quote-cache issue could present at a ratio JUST under the threshold (e.g., a 9x mispricing of a stable large-cap) and slip through. Ticker-specific volatility-derived bounds would catch tighter aberrations. Surface for follow-up: `f-implausible-quote-per-ticker-vol`.

2. **Defense-in-depth wins, but only when each layer is observable.** The exit-monitor guard caught the bad data and refused to act — but the visibility was through a manual DB poll. The Phase 3 alert on 5-in-10min rejections per `(ticker, source)` closes that observability gap and is the missing piece that makes the boundary guard production-grade.

3. **Process-local caches need TTLs OR boundary validation.** The brief offered both options (TTL invalidation OR write-time sanity at price_bus). We chose boundary validation at the consumer because it's source-agnostic — whatever upstream is poisoned, the boundary catches it. Two redundant guards on the same invariant invite drift and add maintenance burden.

4. **Fingerprint > frequency for incident detection.** The clue was identical-bad-value-across-time, not high-frequency-of-bad-value. Future cache-pollution incidents will have the same signature; teach the alert pipeline to match it.

## Code shipped

- `app/services/trading/market_data.py`:
  - `_KNOWN_GOOD_CACHE` per-ticker last-known-good cache.
  - `_REJECTIONS` rolling window per `(ticker, source)`.
  - `_resolve_implausibility_anchor` (cache → open Trade → None fallback chain).
  - `_record_implausible_rejection` (5-in-10 → `persist_runtime_surface_now('market_data', 'degraded', ...)`).
  - `_accept_known_good_price` (seed cache after a clean fetch).
  - `_apply_boundary_guard` (the boundary check).
  - `fetch_quote` is now a thin public wrapper; `_fetch_quote_unguarded` is the underlying cascade.
- `tests/test_market_data_implausible_guard.py`: 11 helper-level tests, sub-second.
- `scripts/dispatch-trump-quote-trace.py`: Phase 1.1 diagnostic script.
- `docs/AUDITS/2026-05-07_implausible-quote-history.md`: Phase 1.2 history audit.
- `docs/AUDITS/2026-05-07_trump-usd-poisoned-quote-postmortem.md`: this file.

## What remains for the operator after CC ships

- Push the commits.
- Watch `runtime_surface_state` for `market_data.state='degraded'` rows over the next week — that's the new visibility surface.
- Watch `trading_stop_decisions.trigger='DATA_IMPLAUSIBLE'` row count — should drop to zero (the boundary guard returns None before the bad value reaches the exit-monitor).
- If a third recurrence happens with the boundary guard active and produces a degraded row, the operator can run `scripts/dispatch-trump-quote-trace.py` from the autotrader-worker container (not the host) to confirm the in-process source.
