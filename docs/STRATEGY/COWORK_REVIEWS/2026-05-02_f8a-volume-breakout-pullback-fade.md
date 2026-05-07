# Cowork Review: f8a-volume-breakout-pullback-fade

**Reviewing:** `docs/STRATEGY/CC_REPORTS/2026-05-02_f8a-volume-breakout-pullback-fade.md`
**Reviewer:** Cowork.
**Date:** 2026-05-02.

## Verdict

**Honest run with a real bug surfaced.** The pipeline is wired correctly at the alert-emission layer, but a cross-ticker drain bug in the heap drops most observations at the decay miner. Claude Code caught it itself, documented three fix options, and pushed back on the "use original_close as fallback" tempting-but-wrong option. Approve the architectural skeleton, queue F8a-fix as the next task. **The experiment hasn't actually run yet** — soak window had zero organic firings; all 19 deferred emits were snapshot-replay catchup. We need the bug fixed AND organic firings before we can interpret anything.

## What Claude Code did right

1. **Caught its own bug honestly.** `on_book_emit(ticker, book)` was popping the heap for ALL tickers whose deadline passed, not just the triggering ticker. For non-matching pops, the post-pullback `best_bid/best_ask/close` fields couldn't be set. Decay miner needs `features.close` to compute forward returns; missing it = observation dropped as malformed. The bug is subtle (the alert emits correctly, the lineage is correct, only the price-capture is missing) and could easily have shipped silently. Claude Code wrote a verification query, found the empty `best_bid/best_ask`, traced it to the drain logic, and surfaced it.

2. **Three fix options proposed, with the right one chosen.** Per-ticker heaps (cleanest), DB lookup at drain (works but adds I/O), use `original_close` (contaminates the experiment by mixing two different prices into the same decay distribution). Claude Code voted #2; I'm voting #1 for reasons below. Either of these is honest engineering.

3. **Refused to use original_close as fallback.** The temptation is real — it's the smallest change. But the original close is the price AT THE TOP OF THE SPIKE, not the price at the deferred-fire moment. Using it as the entry price would conflate "spike-time price → forward return" with "post-spike price → forward return," exactly the two distributions the fade experiment is trying to distinguish. Claude Code saw this and rejected it. That's the right epistemic move.

4. **The unsolicited observability commit (`c5f9746`) is genuinely useful.** Brief said subtask 3 = no commit. Claude Code added 5 lines that surface `vol_pullback`, `pullback_heap`, `pullback_dropped` in the per-minute supervisor metrics line. Now I can `docker compose logs --since 5m | grep pullback_heap` to verify the cap is bounded without writing a probe. The deviation is minor and additive; flagged honestly. Approve.

5. **Refused to lower `VOL_BREAKOUT_MULT` to "seed faster" data.** Open Question 2 specifically asks if we should temporarily lower the threshold. Claude Code's vote: no, ship as-is, "lowering thresholds to seed data is the kind of move F6 told us not to make." That's exactly the F6.5 lesson applied. Same discipline.

## Findings (the real news)

### Finding 1: The experiment hasn't actually run

Zero organic `volume_breakout_long` firings during the 30-minute soak window. All 19 `volume_breakout_pullback_long` rows came from snapshot-replay catchup at container restart — bars from 20+ minutes earlier whose deferred deadlines had already passed by the time the heap drained on the first live book emit. Mechanically correct, scientifically null.

Three implications:
- **Don't read anything into the n=1 +21 bps observation.** It's a single catchup point. Could be noise, could be a genuine recovery move; n=1 makes both indistinguishable.
- The 30-minute window caught a quiet patch. Volume breakouts at `MULT=2.0` are uncommon (~120/24h historically), so a quiet half-hour is normal. Future soak windows will accumulate organic data naturally.
- F8a's verdict requires waiting AND fixing the drain bug. Right now we have neither the data nor the working pipeline to interpret it.

### Finding 2: The cross-ticker drain bug

`features.close` (and `best_bid/best_ask`) is only set when the heap-popped entry's ticker matches the book emit that triggered the drain. Non-matching pops carry an alert with valid lineage but no current price — decay miner drops them as malformed. **Most observations are getting silently dropped.**

Of the 19 catchup pullback alerts that landed in `fast_alerts`, only 2 produced rows in `fast_signal_decay`. The other 17 were dropped at the miner. That's a 90% data loss rate.

This isn't a safety bug — no fills happened, no money lost. But the experiment is structurally broken until fixed.

## Decision: per-ticker heaps (option 1) over DB lookup (option 2)

Both work. Both are correct. Per-ticker heaps wins on three counts:

1. **Cleaner semantics.** "When a book emits for ticker T, drain T's pending entries." That's a one-line invariant. The DB-lookup option requires "when a book emits for any ticker, walk the global heap, look up books for non-matching tickers, hope the most-recent row is current enough." Three more failure modes (stale book, no recent book, DB latency).
2. **Zero I/O addition.** `on_book_emit` is hot. Adding even one DB roundtrip per cross-ticker pop costs latency we don't need to pay. Per-ticker heaps moves all the work to the in-memory layer where it's microseconds.
3. **Same total memory bound.** The 1000-entry cap can be enforced as either a global cap or a per-ticker cap (200 per ticker × 5 tickers). Either way, total state is bounded.

The only argument for DB lookup is that it handles the case where book emits for a ticker stop entirely (the heap entry would never drain). But:
- We have a 60s recency gate that would block any drained-after-stalled alert anyway.
- We already have the candle_freshness healthcheck that detects stalled tickers.
- If we worry about this, the cleaner answer is a periodic "drain expired entries with current best-effort" that runs on a low-frequency timer — not a DB lookup per cross-ticker pop.

Going with per-ticker heaps in F8a-fix.

## Answers to the Open Questions

### 1. Cross-ticker drain fix — per-ticker heaps. See above.

### 2. Should we lower `VOL_BREAKOUT_MULT` to seed faster?

**No.** Claude Code's instinct is right — the F6 lesson applies. Lowering thresholds to manufacture data invalidates the data. Hold at 2.0; let organic firings accumulate. If we want more decay-miner samples faster, the right move is more pairs (F8b candidate) or more signal types, not more frequent firings on a single signal.

### 3. The +21 bps n=1 datapoint

Don't interpret. Document as a single observation. Wait for n ≥ 30 organic firings before drawing inferences.

### 4. Decay miner needs `features.close`

Resolved by F8a-fix. Per-ticker heaps means every drained entry has a current book by construction.

## Engineering concerns (smaller)

1. **The `c5f9746` observability commit was a brief deviation.** Claude Code flagged it. Approve; the metric is genuinely useful for future debugging. Document inline rationale next time so future readers see why it was added beyond the brief.

2. **Unit-test harness for the deferred-emit pipeline** (Claude Code's deferred item). Reasonable for a future hardening pass. Mock the book-emit event clock, assert deadline behavior, assert per-ticker drain semantics. Not now.

3. **The MAX_PENDING_DEFERRED cap drops new arrivals (not the head).** That's the right policy — the head is about to fire. Document the policy in the code comment so future maintainers don't "fix" it.

## State of the world after F8a

- 6 protocol runs landed clean (F5 cleanup, cleanup-2, trades-history, F6, F6.5, F8a).
- F8a shipped a working scaffold + a real bug. The bug needs F8a-fix to land before the experiment can produce meaningful data.
- 0 organic deferred firings yet. Once F8a-fix is in, we soak for ~24h and see what accumulates.
- All 8 fast-path safety belts intact. Live placement still gated. Calibration gates still blocking everything (correctly).

## Workflow assessment

This run hit the protocol's most useful behavior so far: **Claude Code caught and surfaced its own bug rather than shipping it silently.** The verification query that found the empty `best_bid/best_ask` was Claude Code probing its own work, not me catching it after the fact. That's the protocol's most valuable property — Cowork-driven verification criteria force Claude Code to actually look at what shipped instead of trusting its own implementation.

Six clean runs total now. Pattern is reliable.

## Next move: F8a-fix (per-ticker heaps)

Will write the brief in the next message. Single commit, surgical change to `scanner.py`. Convert `_pullback_heap: list[_DeferredEmit]` to `_pullback_heaps: dict[str, list[_DeferredEmit]]`. Modify `on_book_emit` to drain only the triggering ticker's heap. Cap stays — split across tickers it's even more comfortable. Then soak ~24h to gather organic data on the actual experiment.

After F8a-fix soaks, two paths depending on the data:
- **If `volume_breakout_pullback_long` shows positive forward returns at short-to-medium horizons** (consistent with mean-reversion thesis): the fade works, calibrate `DELAY_S` from data, allow fills to land, watch realized P/L.
- **If it's also negative** (signal is symmetrically bad in both directions): fade thesis refuted, drop the experiment, move to F9 (order-book momentum or trade-tape aggression).

Either outcome is informative. Both close out F8a cleanly.
