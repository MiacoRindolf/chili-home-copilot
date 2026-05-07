# Cowork Review: f8a-evaluation

**Reviewing:** `docs/STRATEGY/CC_REPORTS/2026-05-02_f8a-evaluation.md`
**Reviewer:** Cowork.
**Date:** 2026-05-02.

## Verdict

The right answer to the right question. **"Insufficient data — re-run at 2026-05-03 17:00 UTC"** is exactly the call the data supports. Zero verdict-grade cells, decision tree's fourth branch fires cleanly, ETA is grounded in observed per-cell rate. No fabrication, no premature pivot, no scope creep.

The interesting finding isn't the verdict — it's that **horizon=1s is structurally not where the fade hypothesis lives**, and Claude Code surfaced this correctly. The two suggestive cells (both DOGE-USD horizon=1, both fully-negative CI at ~-0.7 bps) are the *fire moment* — by construction, no reversion has elapsed. The hypothesis lives at 5s / 30s / 60s / 300s / 1800s, where samples are all still in sparse tier. That observation alone is the most useful read of the run.

Approve.

## What Claude Code did right

1. **Held the line on "insufficient data."** The brief asked for honest reporting and explicitly forbade fabricating a verdict from suggestive cells. Claude Code reported the suggestive cells (DOGE-USD horizon=1, n=10 each) honestly, then noted that horizon=1s is structurally pre-reversion and the fade hypothesis lives at later horizons. That's exactly the epistemic discipline F6 was supposed to teach.

2. **Cross-ticker pooling sanity check, with the right conclusion.** Pooled the 5 densest cells, confirmed the pooled view doesn't override the per-ticker call — and noted that horizon=1s being negative pooled is expected, not informative. Avoided the "look, it's significant after pooling!" trap.

3. **ETA grounded in observed rate, not wishful thinking.** ~1.35/hr DOGE-high-bucket × need 23 more samples = ~17h conservatively. Round up to 24h. The 7-day "signal too rare" threshold from the brief is acknowledged as a *future* discussion if the next re-run still finds 0 verdict-grade cells. No premature pivot.

4. **Realized-validation insight.** 82/83 cells have `realized_validation_count = 0`. Claude Code traced this correctly to the gate stack blocking pullback fills (`negative_edge` and `signal_not_tradeable` rejecting most). Validation-loop is closed in theory, unfired in practice. **This is a real architectural observation, not just a counted column** — the validation pathway only activates after a pullback alert produces a real `fast_exits` lineage. With paper mode + the current gate stack, that doesn't happen. Worth keeping in mind for F8b's design (if/when we get there).

5. **Surfaced `db_errors = 13`.** Stable but non-zero. Claude Code correctly noted the brief's instruction was "flag if growing or stable" and surfaced it. Right call — that's the kind of small-but-undiagnosed thing that quietly becomes a bigger issue.

6. **Hourly distribution honesty.** The 11:00 UTC spike (60 alerts) is treated as one anomaly point, not extrapolated. Steady-state computed from the post-spike 6 hours, not from the spike. Discipline.

7. **Capture-rate verification re-run.** 114/114/114 — F8a-fix's invariant is still holding 13 hours later. Doesn't have to be re-checked, but doing so is cheap and the kind of small repeated verification that catches drift early.

## Findings

### The horizon=1s observation is the real story

Both DOGE-USD horizon=1 cells: mean ≈ −0.7 bps, full CI below zero. If that were a longer horizon, it would suggest the fade is *negative* (price keeps moving in the breakout direction, no reversion). At horizon=1s, it's mostly the cost of the fire moment itself — bid-ask spread, slippage to the deferred-emit price. **The fade hypothesis is not yet falsified by these cells.** It's also not yet supported. We need the longer-horizon cells to mature.

This is also a useful tell about *what we'd see if the fade were real.* If the longer horizons stay near zero or skew negative, the fade hypothesis fails. If they skew positive after 5–30s of post-deferred-fire elapsed time, that's the mean reversion the hypothesis predicts. We won't know until those cells reach n ≥ 30.

### `obs_finalized / obs_scheduled = 21%` is structural, not a bug

1051 alerts × 8 horizons = 8408 scheduled. Only 1779 finalized (21%). The remainder are waiting on the longer horizons (300s, 1800s, 3600s, 14400s) — 14400s = 4 hours, so any alert fired in the last 4 hours has its longest horizon still pending. Combined with the sparse alert cadence (~2.3/hr steady state across 5 pairs × 3 buckets), per-cell maturation is what's gating verdict-grade.

24h more soak doesn't just give us more alerts — it lets the existing alerts' long horizons mature into observations. Both effects compound favorably.

### Validation residuals are structurally unavailable in current configuration

`realized_validation_count = 0` across 82/83 cells. The miner only writes a validation residual when a `fast_exits` row references a pullback-alert lineage — which requires a pullback fill, which the gate stack prevents in paper mode. **This is correct behavior, not a bug.** It does mean the fade hypothesis can only be evaluated against the miner's mean/stderr — there's no second signal to triangulate from. Worth designing F8b with this in mind: if F8b's plan involves "use realized validations to refine," it can't, because there are none.

## Answers to the Open Questions

### 1. `db_errors = 13` on decay_miner — investigate now?

**Yes. Fold into a small f-hygiene-2 pass during the 24h soak window.** Easy probe (single grep on `docker compose logs --since 12h`). Stable-but-nonzero is the worst category — not enough to halt, easy to ignore, will quietly grow. Same hygiene-pass logic as F-hygiene-1.

### 2. `VOL_BREAKOUT_MULT = 2.0` too aggressive given observed fire rate?

**Hold at 2.0. Don't tune to seed faster.**

This is the F6 lesson again. Lowering MULT to manufacture more firings invalidates the data. If at the next re-run (2026-05-03 17:00 UTC) we still have 0 verdict-grade cells, *that* is the strategic discussion — not whether to drop the threshold, but whether F8 (fade hypothesis on this signal type) is the right experiment at all, or whether to pivot to F9 (other signal types).

The observed rate is 2.3/hr steady-state. Volume breakouts at MULT=2.0 are *supposed* to be uncommon — that's the whole point of the threshold. Rare-but-sharp is the contract. Patience over re-tuning.

### 3. Watchdog "OK" log line per supervisor metrics tick?

**Yes. Cheap, useful, soak-safe. Fold into f-hygiene-2.** Currently the watchdog only logs on death — silence-as-health is inference. A one-line "watchdog: decay_miner OK" once per supervisor tick (60s) flips this to positive confirmation. Carrying forward from F-hygiene-1's Open Question 4.

### 4. `pending_heap` time-series trend check?

**Add to f-hygiene-2.** A SQL query against the supervisor metrics-line history isn't directly available (those are stdout), but we can either (a) start logging `pending_heap` to a small time-series table, or (b) just do a rolling read across `docker compose logs --since 24h` and grep for the `pending_heap=N` lines to see the trend.

Option (b) is zero-code, perfectly soak-safe, and actually informative. Use it.

## Engineering concerns (smaller)

1. **The validation-loop being structurally closed-but-unfired** is the most interesting architectural observation from this run. F8b's brief, when we write it, should account for the fact that we won't have realized-validation evidence until pullback fills land. Either F8b is "calibrate DELAY_S from miner means alone" (simpler, less robust) or "open the gate stack just enough to land paper fills that produce validation residuals, then calibrate" (more powerful, but moves us closer to live).

2. **The 11:00 UTC spike is worth understanding eventually.** Claude Code noted it was a real Coinbase volume spike on multiple pairs simultaneously. If F8b's calibration treats every alert as i.i.d., a 60-alert burst from one correlated event will skew the running mean. Worth flagging for F8b — but not for evaluation.

3. **`db_errors = 13` is the kind of finding F-hygiene-1 was set up to surface.** Watchdog reports task death but not internal errors. f-hygiene-2 can broaden the visibility surface.

## State of the world after f8a-evaluation

- 9 protocol runs landed clean (F5 cleanup, cleanup-2, trades-history, F6, F6.5, F8a, F8a-fix, f-hygiene-1, f8a-evaluation).
- 0 code commits this run (correct — pure analysis task).
- 1 doc commit pushed: `docs(strategy): F8a evaluation report + mark NEXT_TASK done`.
- F8a soak continues uninterrupted. ETA to next evaluation re-run: ~24h.
- 0 verdict-grade cells. 2 suggestive (both DOGE-USD horizon=1, both fully-negative CI as expected at the fire moment). 81 sparse.
- F8a-fix's 100% capture rate invariant verified at 13h: 114/114/114.
- F-hygiene-1's `last_error` self-clear verified naturally in production: all 5 pairs `last_error=NULL` after 5+ min clean streaming.
- All 8 fast-path safety belts intact. Default mode stays paper. Calibration gates blocking everything (correctly).

## Workflow assessment

This was the test of "queue an analysis task that returns honest 'insufficient data' if the data isn't there." It worked. **Claude Code didn't manufacture a verdict from suggestive cells, didn't pool to fabricate significance, didn't pivot prematurely.** Held the discipline the brief asked for.

The protocol's most useful properties continue to compound: surface real bugs by running the verification SQL (F8a-fix, f-hygiene-1), surface real architectural observations by analyzing the data (this run). Nine runs in, the pattern is fully reliable.

## Decisions confirmed

- "Insufficient data — re-run at 2026-05-03 17:00 UTC" verdict approved.
- Hold `VOL_BREAKOUT_MULT = 2.0`. No threshold tuning to seed faster.
- F8a-evaluation-rerun is the next-after-next task (after the soak).
- Validation residuals being unavailable is structural, not a bug.
- The horizon=1s suggestive cells are the *fire moment*, not the fade test. Hypothesis lives at later horizons.

## Next move

The 24-hour soak window is the bottleneck. Two reasonable paths:

**Path A — f-hygiene-2 during the soak.** Three small soak-safe items, exactly the same shape as F-hygiene-1:
- Investigate `db_errors = 13` on decay_miner. Single grep, then either fix or document.
- Add positive-confirmation `[fast_path] decay_miner watchdog OK` log line per supervisor tick.
- Add a `pending_heap` time-series snapshot (rolling grep of supervisor lines, or a small periodic sample table) to confirm oscillation rather than slow growth.

Three commits, no strategy impact, no calibration. Doesn't compete with the F8a observation window.

**Path B — pure observation window, no work.** Wait 24h, then F8a-evaluation-rerun.

My recommendation: **Path A.** Same logic as the last hygiene cycle — F8a-evaluation-rerun is gated on the soak, hygiene work is soak-safe, both compound favorably. We end up at 2026-05-03 17:00 UTC with a hardened subsystem AND mature data.

If you want minimum moving parts during the data-gathering window, Path B is defensible — but f-hygiene-2's items are all observability, not behavior changes, so they're as soak-safe as not-doing-them.

What's your call?
