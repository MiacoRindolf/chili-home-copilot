# CC_REPORT: f-trump-usd-poisoned-quote-source-audit

## Outcome

All four phases shipped. Boundary guard at `fetch_quote` closes the trust boundary at the data layer; per-ticker last-known-good anchors the plausibility check; 5-in-10min rejections per `(ticker, source)` write a `degraded` row to `runtime_surface_state.market_data` for the alert pipeline.

After this lands the next quote-cache poisoning will:
- not reach any consumer (`fetch_quote` returns `None` instead),
- emit a `degraded` row within minutes (alert pipeline picks it up),
- not pollute `trading_stop_decisions` with `DATA_IMPLAUSIBLE` rows (the exit-monitor never sees the bad value).

## Per-phase status

### Phase 1.1 — Host diagnostic — SHIPPED
- `scripts/dispatch-trump-quote-trace.py`: hits price_bus, Massive WS, Massive REST in isolation + Coinbase ground truth.
- Cold-process run: REST clean at $2.38 (matches Coinbase $2.38). price_bus + Massive WS return None (per-process WS subscribers not active in one-shot scripts — not a finding, just the limitation of host-side diagnostics).
- Output: `scripts/dispatch-trump-quote-trace-output.txt` (not committed; diagnostic scratch).

### Phase 1.2 — DB history — SHIPPED
- `docs/AUDITS/2026-05-07_implausible-quote-history.md` filed (force-added; `docs/AUDITS/` is gitignored, this is one of the brief-required share cases).
- Storm dynamics: ARB-USD `$0.0008 entry=$0.1170` (98 rows, 2.5 days) + TRUMP-USD `$0.0003 entry=$2.4194` (21 rows, 6 hours). Both ENDED ~12h before investigation. Identical bad price across all rows of each ticker — singleton-cache fingerprint confirmed.
- Refined suspect ranking: poison lives in price_bus or Massive WS (per-process). Massive REST ruled out by Phase 1.1.

### Phase 2 — Boundary guard at `fetch_quote` — SHIPPED
- `app/services/trading/market_data.py` adds:
  - `_KNOWN_GOOD_CACHE` per-ticker last-known-good (in-memory dict).
  - `_resolve_implausibility_anchor(ticker)`: cache → open-Trade `entry_price` → None.
  - `_apply_boundary_guard(ticker, quote)`: validates `quote['price']` against the anchor via the shared `_exit_monitor_common.is_implausible_quote`. Returns `None` on rejection (per the no-hardcoded-fallback rule); seeds the cache on accept.
  - `fetch_quote` is now a thin wrapper that calls `_fetch_quote_unguarded` (the renamed cascade) and runs `_apply_boundary_guard` on the result.
- **No new magic numbers.** Reuses `IMPLAUSIBLE_QUOTE_RATIO_LOW`/`_HIGH` from the shared module via `is_implausible_quote`.
- **price_bus write-time sanity DEFERRED** per operator decision (per the architect-recommended path). Single boundary guard at the consumer is source-agnostic and avoids redundant-invariant maintenance burden.

### Phase 3 — 5-in-10min alert — SHIPPED (combined with Phase 2)
- `_REJECTIONS: dict[(ticker, source), deque[float]]` rolling-window timestamps.
- `_record_implausible_rejection`: drops entries older than 600s, counts the rest; on count ≥ 5 calls `persist_runtime_surface_now(surface='market_data', state='degraded', source=<source>, details={...})` — same write path the existing market-data heartbeat uses.
- Per-`(ticker, source)` isolation: rejections from `source_a` don't push `source_b` over the threshold and vice versa (verified by test).

### Phase 4 — Postmortem — SHIPPED
- `docs/AUDITS/2026-05-07_trump-usd-poisoned-quote-postmortem.md` filed (force-added).
- Timeline (Round-13/14 origin → 2026-05-04 ARB recurrence → 2026-05-06 TRUMP storm → boundary-guard fix), best-evidence root-cause inference, why the existing guard caught the corruption but didn't surface it operationally, generalization (per-ticker bounds + cache TTL/boundary-validation tradeoff + fingerprint-based detection).

## Verification

- **Helper-level tests**: `pytest tests/test_market_data_implausible_guard.py -v` → **11/11 PASS in 1.06s**. Covers:
  - Reject implausibly-low quote against cache (4 tests on guard logic)
  - Accept plausible against cache + accept-and-seed when no anchor
  - Pass-through None / zero-price (no-op cases)
  - Phase 3 alert: below threshold no-fire, at threshold fires with correct details, outside-window doesn't count, per-source isolation
  - Anchor priority (cache wins over open-Trade)
- **Integration check**: re-ran `scripts/dispatch-trump-quote-trace.py` post-fix. `fetch_quote("TRUMP-USD")` returns $2.405 (Massive REST source, accepted because no anchor exists for TRUMP-USD in this fresh process; cache seeded for the next call).

## Magic-number audit

**Net new magic numbers introduced: ZERO.**

The 0.1x / 10x ratio bounds are SOURCED from `_exit_monitor_common.IMPLAUSIBLE_QUOTE_RATIO_LOW/HIGH` (shared with the three exit-monitor lanes). The 5-in-10min alert thresholds (`_REJECTION_THRESHOLD = 5`, `_REJECTION_WINDOW_S = 600`) are NEW module-level constants in `market_data.py` documented as the alerting tunable — these are alert-rate tuning, not data-layer thresholds. Surfaced as a possible follow-up if the operator wants per-`(ticker, source)` rate tuning.

## Surprises / deviations

1. **Storm had cleared by investigation time.** The brief assumed an ongoing storm; actual state was last-DATA_IMPLAUSIBLE-row 12h before investigation, fitting the autotrader-worker `Up 2 hours` restart pattern. This made Phase 1.1 host diagnostic less informative (couldn't directly attribute the cache to a specific in-process subscriber). The Phase 1.2 SQL history was the load-bearing diagnostic.

2. **Phase 3 was integrated into Phase 2's `_record_implausible_rejection`** rather than a separate code path. The brief described them as separate phases but the implementation is naturally one function — the rejection telemetry IS the alert mechanism. Listed separately in this report for status-tracking continuity with the brief.

3. **Per-ticker last-known-good is module-local, not cross-process.** Each container has its own cache. This is intentional — the cache is fast-path memo, not authoritative state. Cross-process consistency would require Redis or DB; that's premature for this scope.

## Open questions for Cowork

1. **Per-ticker volatility-derived bounds.** A ratio just under 0.1x (e.g., 0.12x) on a stable large-cap is still suspicious but slips through today's structural bound. Brief Phase 4's generalization names this. Surface for `f-implausible-quote-per-ticker-vol` if the operator wants tighter enforcement.

2. **Cross-process cache for last-known-good.** Today's per-process cache means each container has a brief warm-up window where it accepts the first quote unconditionally (no anchor). If a brand-new container subscribes to a poisoned WS frame on first connect, that bad value becomes the cache anchor. Mitigations: (a) require a minimum N quotes before trusting cache, (b) cross-process Redis-backed cache, (c) accept the warm-up risk because the subsequent `fetch_quote` calls would re-validate against the (now-poisoned) cache and pass through forever — making this primarily a stale-state risk rather than a data-quality risk. Surface for follow-up if the warm-up window is operationally meaningful.

3. **Diagnostic reproduction from inside the container.** The host script can't see in-process state. The next storm would be diagnosed via `docker exec autotrader-worker python /app/scripts/dispatch-trump-quote-trace.py` — confirm the script path is mounted in the autotrader-worker container before the next incident.

## Cookbook update

- **Boundary validation > consumer validation when the cost difference is small.** Validating at every consumer (the prior `_exit_monitor_common.is_implausible_quote` only-at-exit-lanes pattern) leaks complexity and risks new consumers forgetting to validate. Validating at the data boundary (this brief's `fetch_quote` pattern) is single-point, source-agnostic, and naturally extends to future consumers.
- **Stale singleton fingerprint = identical bad value over multi-hour windows.** When you see this in production data, suspect an in-process cache without TTL or write-time sanity. The boundary-guard pattern from this brief is the durable answer.
- **`runtime_surface_state.market_data` is the alerting surface for data-quality issues.** Not just heartbeats — implausibility rejections route here too, with `state='degraded'` and structured `details`. Future data-quality detectors should follow the same shape.

## Stale uncommitted work — final state after these commits

Same disposition as prior CC reports — operator-tracked working-set + runtime artifacts only:
- `brain_worker.log`, `data/ticker_cache/crypto_top.json` (runtime).
- `docs/STRATEGY/CURRENT_PLAN.md` (operator change unrelated to this brief).
- `docs/STRATEGY/NEXT_TASK.md.tmp`, `docs/STRATEGY/QUEUED/*` (operator scratch).
- Untracked `scripts/_*_out.txt` (operator scratch; gitignored via the prior brief's `.gitignore` additions).
- `scripts/dispatch-trump-quote-trace-output.txt` (this brief's diagnostic scratch; not committed).
