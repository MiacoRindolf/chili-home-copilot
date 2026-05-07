# Cowork Review: f6-signal-decay-miner

**Reviewing:** `docs/STRATEGY/CC_REPORTS/2026-05-02_f6-signal-decay-miner.md`
**Reviewer:** Cowork.
**Date:** 2026-05-02.

## Verdict

Excellent execution and — much more importantly — F6 has produced **the most strategically valuable output of this entire project**. The honest answer the data is giving us is hard to look at, and Claude Code surfaced it cleanly without flinching. Approve the implementation. The strategic decisions surfaced in Open Questions are the real work now.

## What Claude Code did right

1. **Architectural commitments held.** Event-driven via Postgres LISTEN/NOTIFY (alert + exit + book channels). Welford-incremental updates, never recompute from scratch. Cold-start backfill is one-shot and self-skips. Pending-observations heap with the natural event flow as the wake clock. Zero `while True: sleep(...)`. This is exactly the architecture the brief asked for — the user's "event-driven, no cycles" principle held without compromise.

2. **Quiet bug catch in own backfill SQL** (load-bearing). First attempt used `bid_levels[1][1]::float` (Postgres array syntax) on a JSONB column. Postgres silently coerced this to **the size field of level 2**, not the price field of level 1. Result: forward returns of ~99% (nonsense). The live event path used Python `bid_levels[0][0]` correctly so production wasn't corrupted, but the backfilled rows were garbage. Fixed in same commit, documented in commit message. This is the kind of bug that survives review under less rigorous engineering.

3. **Subtask 3+4 bundling justified.** Brief said "each independently testable." In practice the cold-start backfill SQL has to know the same direction-aware return formula and bucket-key shape as the live event handler. Splitting would have meant a placeholder `_BACKFILL_UPSERT_SQL = None` that's worse than honest bundling. The protocol's "tight series" allowance covers this. Reverting subtask 4 alone is still meaningful (clear the SQL constant + drop the `_maybe_backfill` call from `start()`).

4. **`calibration.py` as a separate module instead of touching `stop_engine` directly.** Same end-state, less coupling, easier to delete if needed. Better engineering than the brief's hint.

5. **Diagnostic warning loop caught the volume_breakout features-shape gotcha.** Bar-derived alerts don't carry best_bid/best_ask. Both live handler and backfill fall back to `features.close`. Found because `obs_scheduled` came in below `alerts × 8` and the operator wrote a check loop instead of letting it slide.

## The findings (this is the actual work now)

F6 has produced empirical truth on three load-bearing questions. The numbers are the numbers; we have to react to them.

### Finding 1: `volume_breakout_long` is NEGATIVELY predictive

```
volume_breakout_long: 120 alerts, mean forward return = −28.5 bps
```

Not weak — **negative**. The signal correlates with mean reversion, not continuation, on Coinbase 1m crypto. This explains the F5-cleanup mystery (DOGE volume breakouts kept hitting stops): they were entries on a counter-signal. Volume spikes on Coinbase crypto 1m bars appear to mark exhaustion / climax, not breakout.

Quant-lit consensus does not endorse "buy on volume breakouts" without confirmation factors (price action, trend filter, regime). The signal as currently constituted is broken. F8 candidate: drop it, invert it (treat as exit signal on existing longs), or replace with a different volume-based formulation (e.g., volume + price-action confirmation gate).

### Finding 2: Imbalance signals are barely positive

```
imbalance_long:  72 alerts, mean = +2.2 bps
imbalance_short: 61 alerts, mean = +1.9 bps
spread_squeeze:  14 alerts, mean = +4.9 bps  (n too small to trust)
```

Imbalance signals have measurable, statistically real, but **trivially small** forward edge. At 2 bps, after fees and slippage, there's no edge that survives a real spread crossing. Even with the most maker-friendly fee schedule, round-trip cost is 10–20 bps. **2 bps signal − 10 bps cost = negative expected value.**

This is consistent with quant-lit on raw OB imbalance: predictive over 1–5 seconds with effect size in single-digit bps. To make money on it you need either much lower latency, much lower cost, or a richer feature engineering than top-of-book imbalance summed across N levels.

### Finding 3: Calibrated max_hold_s = 1–5 seconds, not 4 hours

The Sharpe-best horizon for high-score imbalance buckets is sub-10s. Matches quant-lit consensus exactly. The hardcoded `MAX_HOLD_S = 14400` was off by **three orders of magnitude** for these signals. F6 has produced empirical proof of the F5-cleanup thesis ("holding period sized for swing, applied to scalp signals"). The bug was real and now we have the receipts.

### Bottom line

At 40 bps tradeability bar, **none of our signals beat cost.** spread_squeeze at +5 bps is the closest, and n=14 means we can't be confident even that's not noise. The calibration gate is correctly blocking almost every signal — and that's not over-tuned thresholds, it's the system telling us the truth.

The F5-cleanup review noted "we're not statistically separable from a coin flip on 3 round trips." F6 has now answered that question across hundreds of pre-trade alert trajectories. **The signals don't have edge.** That's a finding, not a failure.

## Answers to the Open Questions

### 1. Trading cost threshold

Claude Code suggests lowering `TRADING_COST_FRAC` from 1% to 0.2% (40 bps tradeability bar). My read: **don't ship the lower threshold yet**. Reasoning:
- At 40 bps, *still* none of our signals beat cost (best is +5 bps spread_squeeze with n=14)
- Lowering the threshold without finding higher-edge signals is papering over the finding
- The current behavior (gate blocks almost everything) is the correct outcome of the data
- Move to F8 (find better signals) before retuning thresholds

If we ever want to deliberately let weak-but-positive-edge signals through for paper validation purposes, we should do it via a paper-only override flag, not by lowering the production threshold.

### 2. Calibrated max_hold_s of 1–5 seconds

The honest interpretation: **at 1–5s, the signal isn't tradeable at our execution latency floor.** Coinbase live placement is >100ms typical; round-trip (place + verify + exit) is 200–500ms minimum. A 1s hold has zero room to actually fill paper at entry and exit at target.

My answer to Claude Code's three sub-questions:
- **Add an execution-latency floor.** `CALIB_EXEC_FLOOR_S = 10` — calibrated max_hold maxes with this floor. Not a magic number for strategy; it's a hardware/network reality.
- 1s isn't "correct" — it's the honest output of "we have no signal worth holding for, at any horizon shorter than execution can handle."
- The argmax shouldn't exclude 1s/5s horizons in the data (we want to *see* them in `fast_signal_decay`). The exclusion belongs at the consumer (exit_manager), not the miner.

### 3. `volume_breakout_long` is negatively predictive

Treat this as a finding, not a calibration problem. Two responses available:
- **Short-term (F6.5):** add a data-driven exclusion in calibration helpers — any `(alert_type)` where `mean_return + 2*stdev/sqrt(n) < 0` AND `n >= 30` is auto-blocked at the gate level regardless of score. Volume_breakout_long would auto-block based on the current 120-alert sample.
- **Long-term (F8):** redesign or replace the signal. Volume + something else (price action, trend filter, regime). This is a quant-design task, not a calibration task.

### 4. spread_squeeze n=14

Wait for more data. Don't tune. Don't action. Note that the scanner cooldown plus rarity of true squeeze conditions makes this signal infrequent — that's a feature (high specificity) not a bug.

### 5. Should `decay_miner` cap longer horizons (3600s, 14400s) at scheduling time?

Defer. The heap is sized fine; no urgency. If we trim the long horizons we lose the ability to see "imbalance_long low at 14400s has Sharpe 2.2" — even if that's a regime artifact, knowing it exists is informative for F8.

## Engineering concerns (smaller, follow-up)

1. **`ExecContext.engine` for read-only DB lookups.** Claude Code flagged it; I agree it's a minor purity violation but contained. Acceptable. Refactor only if the gate suite grows substantially or if testing surfaces a problem.

2. **No TTL cache on calibration reads.** Currently one SELECT per call. With `ix_fsd_lookup` it's sub-millisecond and the cadence is bootstrap-once-per-position + once-per-alert. Defer until profiling shows it.

3. **Watchdog on the asyncio decay_miner task.** Failure modes (LISTEN connection drops, slow DB) are silent except via metrics. Useful for the hardening pass.

## What this changes in CURRENT_PLAN.md

The "edge-proof bar > 50 round trips" success criterion in `CURRENT_PLAN.md` is no longer the binding question. The binding question is: **do any of our scanner signals carry positive expected value after cost?**

F6's answer is: **no, not at this signal pool.** F8 (signal redesign) is now the load-bearing next move, not F7 (Kelly sizing). Sizing is irrelevant if there's no edge to size.

## Workflow assessment

Four end-to-end protocol runs now (F5 cleanup, cleanup-2, trades-history, F6). Same pattern each time:
- Operator effort: type `claude` once.
- Claude Code stays in scope, surfaces findings honestly, splits commits sensibly, catches its own bugs.
- Cowork reviews, decides direction, queues the next task.

The protocol is paying its keep. F6 was the meatiest task to date (~700 LOC, 4 commits, real DB schema, real architectural commitments) and shipped clean.

## Decisions I'm making (will discuss with operator before writing next NEXT_TASK)

1. **Don't lower trading-cost threshold.** Hold at current default. Use F8 to find signals that actually beat it.
2. **Add execution-latency floor on calibrated max_hold_s.** `CALIB_EXEC_FLOOR_S = 10` (or thereabouts). Small cleanup task or bundled into F6.5.
3. **Add data-driven negative-edge auto-exclusion** at the gate level. Candidate code path in calibration.py. Could ship as F6.5.
4. **F8 (signal redesign) is the next strategic task.** Bigger than F6. Needs operator input on direction (replace/augment/which features).
5. **F7 (Kelly sizing) deferred** until F8 produces a signal with edge worth sizing.

## Next move

Will discuss with operator. Three reasonable paths:

- **Path A:** Ship F6.5 (small) — execution-latency floor + negative-edge auto-exclusion. Then propose F8.
- **Path B:** Skip F6.5; let the system soak for hours at current calibration (mostly blocked) and see what realized P/L the few-passing trades produce.
- **Path C:** Jump straight to F8 — design new signal types with quant-credible scalp edge.

My vote: **Path A then C**. F6.5 is hygiene that takes ~30 min of Claude Code time and locks in the finding without losing data. Then F8 is the meaty strategic task.
