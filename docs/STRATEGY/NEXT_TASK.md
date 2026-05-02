# NEXT_TASK: f8a-evaluation

STATUS: DONE

## Goal

Evaluate the F8a fade hypothesis — does `volume_breakout_pullback_long` show statistically meaningful forward returns at short-to-medium horizons? — using whatever organic data exists in `fast_signal_decay` at task-run time.

This is **a pure analysis task, not a code-shipping task**. The deliverable is `docs/STRATEGY/CC_REPORTS/<date>_f8a-evaluation.md`. Zero code commits unless something is fundamentally broken in the data pipeline.

After this task:

1. **The fade hypothesis has a verdict, or an honest "still waiting" with a clear restart criterion.** If the data is statistically actionable, write the verdict. If not, write a partial readout with what's accumulated and recommend continuing the soak.
2. **The next strategic move is named.** Either F8b (calibrate `VOL_BREAKOUT_PULLBACK_DELAY_S` from data, move toward live), F9 (new signal types — fade refuted, move on), or "soak another N hours and re-run this same task."
3. **No code changes.** No threshold tuning. No new gates. No retroactive scope creep into F8b. The brain reads what's there and reports.

## Why now

- F-hygiene-1 verified end-to-end at 2026-05-02 16:00 UTC: all 5 pairs streaming, 0 reconnects, `last_error=None` everywhere, decay miner watchdog silent.
- Post-fix capture rate at 100% in production. F8a-fix is doing its job in the wild.
- 76 `fast_signal_decay` rows / 193 total observations for `volume_breakout_pullback_long` accumulated as of last probe — but ~8 obs/bucket on average, below MIN_SAMPLES (= 30) at the per-bucket level.
- Whether the data is sufficient to call yes/no on the fade hypothesis depends on **how concentrated the firings are** in any individual bucket × horizon cell. Some buckets may have 30+ already; others may have <5. Only Claude Code looking at the live table can tell.
- This task is no-op-cheap if data is sparse (Claude Code returns early with a "still waiting" report); zero-latency if it's ready (verdict in hand).

## Architectural commitments

- **Read-only against `fast_signal_decay` + `fast_alerts` + `fast_path_status`.** No mutations.
- **No new code.** No edits to scanner, miner, executor, gates, calibration helpers, or any module.
- **Use the existing MIN_SAMPLES floor and negative-edge exclusion criterion** that the brain already encodes — don't re-derive them inline. They're in `app/services/trading/fast_path/calibration.py`.
- **Honest reporting.** If a bucket has n=12, it has n=12 — don't report it as a verdict-grade signal. The decay miner already has a "negative edge auto-exclusion" rule (`mean + 2*stderr < 0 AND n ≥ 30`) that we can mirror in reporting.
- **One CC report, no commits.** The only file written is `docs/STRATEGY/CC_REPORTS/<date>_f8a-evaluation.md`.

## Scope — analysis, not code

### 1. Pull current state of `fast_signal_decay` for `volume_breakout_pullback_long`

```sql
SELECT ticker, score_bucket, horizon_s,
       sample_count,
       mean_return,
       m2_return,
       -- stderr derives from Welford: sqrt(m2 / (n-1)) / sqrt(n)
       CASE WHEN sample_count > 1
            THEN SQRT(m2_return / (sample_count - 1)) / SQRT(sample_count)
            ELSE NULL
       END AS stderr_return,
       realized_validation_count,
       realized_validation_residual,
       last_updated
FROM fast_signal_decay
WHERE alert_type = 'volume_breakout_pullback_long'
ORDER BY ticker, score_bucket, horizon_s;
```

Bucket the cells into three states:
- **Verdict-grade** (`sample_count >= 30`) — report mean ± stderr, flag positive/negative significance using the same `mean ± 2*stderr` rule the brain uses.
- **Suggestive** (`10 <= sample_count < 30`) — report the numbers but explicitly mark "below MIN_SAMPLES; not statistically actionable yet."
- **Sparse** (`sample_count < 10`) — count cells in this state but don't report individual numbers.

### 2. Cross-reference with `fast_alerts`

- Total `volume_breakout_pullback_long` alerts written since F8a-fix landed (id > 2300, by F8a-fix's CC convention).
- Capture rate (with `best_bid` AND `close`) over the same window — should be ~100%; flag any drift.
- Hourly distribution to spot bursty hours (the 60-alert spike at 11:00 UTC 2026-05-02 was an example) vs sustained accumulation.

### 3. Decay-miner health snapshot

From the supervisor metrics line in `docker compose logs fast-data-worker --since 5m`:
- `obs_scheduled` vs `obs_finalized` ratio — if scheduled is growing but finalized is stuck, the miner is wedged on something (book channel silent for some ticker, etc.).
- `pending_heap` size — should oscillate around steady-state, not grow monotonically.
- `db_errors` — should be 0 or stable; growing means something is failing flushes.
- Watchdog: any `[fast_path] decay_miner watchdog` log lines indicating the task died and was restarted? (No restart policy yet, but the watchdog reports.)

### 4. Verdict logic

Apply this decision tree:

```
IF >= 3 verdict-grade cells AND >= 1 of them is positive (mean - 2*stderr > 0)
  AND no verdict-grade cell is negative (mean + 2*stderr < 0):
    -> "Fade hypothesis SUPPORTED at <list horizons>. Recommend F8b: calibrate DELAY_S."

ELIF >= 3 verdict-grade cells AND all are negative or zero:
    -> "Fade hypothesis REFUTED. Recommend F9: explore new signal types."

ELIF some verdict-grade cells positive, some negative, no consistent pattern:
    -> "Fade signal is noisy. Recommend either: (a) longer soak for more samples,
        OR (b) F8b only on the consistently-positive horizons."

ELIF < 3 verdict-grade cells:
    -> "Insufficient data. Recommend continuing soak. Project ETA based on
        current per-hour rate."
```

**Don't fabricate a verdict from suggestive or sparse cells.** If only 1 cell crossed MIN_SAMPLES and it's borderline, that's "insufficient data," not "weak signal."

### 5. ETA projection (if more soak is needed)

If the recommendation is "soak more," compute:
- Average alerts/hour over last 24h.
- Projected hours-to-MIN_SAMPLES at the median bucket.
- A specific clock time at which it's reasonable to re-run this task.

This makes the "wait" actionable.

### 6. Write the CC report

`docs/STRATEGY/CC_REPORTS/<date>_f8a-evaluation.md` follows PROTOCOL.md format:
- **What was analysed**: timestamp window, total alerts, total observations, per-bucket sample-count distribution.
- **The verdict** (or "insufficient data" with ETA).
- **Per-bucket table** for verdict-grade and suggestive cells (mean, stderr, sample_count, last_updated).
- **Health snapshot** (decay_miner, supervisor metrics).
- **Recommendation** for the next NEXT_TASK.md (F8b, F9, or repeat).
- **Open questions for Cowork** if anything surfaced that wasn't anticipated.

### 7. Verbatim verification SQL — for next review

Same shape as F8a-fix's report — paste the exact SELECTs run, with their counts.

## Brain integration (reuse, don't rewrite)

- `fast_signal_decay` table — the truth source. Don't approximate from `fast_alerts` raw data; the miner has already done the Welford reduction.
- `app/services/trading/fast_path/calibration.py` — has the existing MIN_SAMPLES + negative-edge logic. Mirror its conventions in the report.
- F8a's CC report (`docs/STRATEGY/CC_REPORTS/2026-05-02_f8a-volume-breakout-pullback-fade.md`) — provides baseline conventions: score buckets, horizon set, capture-rate verification SQL.
- F8a-fix's CC report (`docs/STRATEGY/CC_REPORTS/2026-05-02_f8a-fix-per-ticker-heaps.md`) — id-since-fix convention (`id > 2300`).

## Constraints / do not touch

- **No code commits.** Zero. The deliverable is one markdown file.
- **No threshold tuning.** Don't change `VOL_BREAKOUT_MULT`, `VOL_BREAKOUT_PULLBACK_DELAY_S`, MIN_SAMPLES, the negative-edge exclusion criterion, score bucket cutoffs, or anything else.
- **No live placement enable.** Default mode stays paper. The 8-belt safety contract is unchanged.
- **No migrations.**
- **Don't conflate `volume_breakout_long` with `volume_breakout_pullback_long`** — they're different alert types. F8a is about the pullback-long, the fade.
- **Don't average across tickers** without thinking — bucket the analysis by `(ticker, score_bucket, horizon_s)` because that's how the decay miner stores it. Cross-ticker pooling is its own decision.
- **Don't extrapolate from the 11:00 UTC spike** — it's a single hour. Use it as one data point in the hourly distribution, not as a representative rate.

## Out of scope

- F8b: calibrating `VOL_BREAKOUT_PULLBACK_DELAY_S`. That's the *next* task if the verdict is "fade supported."
- F9: new signal types. That's the next task if the verdict is "fade refuted."
- F7: Kelly sizing. Still deferred until F8 produces a tradeable signal.
- Live-mode enablement. Even if the verdict is positive, going live is a separate operator decision.
- Refactoring the decay miner.
- Adding more horizons.
- Cross-pair correlation analysis.

## Success criteria

1. `git log --oneline -3` shows ONE new commit, pushed to origin: `docs(strategy): F8a evaluation report + mark NEXT_TASK done`. No code commits.
2. `docs/STRATEGY/CC_REPORTS/<date>_f8a-evaluation.md` exists, follows PROTOCOL.md format, contains the verdict (or honest "insufficient data") and the recommended next task.
3. The report's verbatim SQL section reproduces the verdict from raw table state — anyone can re-run the SELECTs and get the same numbers.
4. If the recommendation is "soak more," it includes a specific projected re-run clock time based on observed per-hour rate.
5. If the recommendation is "F8b" or "F9," it includes a one-line description of what that brief should focus on (so the next NEXT_TASK.md is straightforward to write).

## Open questions for Cowork (surface in your report only if relevant)

1. **Cross-ticker pooling.** Decay miner stores per-ticker-per-bucket-per-horizon. If individual tickers have <30 samples but pooled across tickers there's >30, is that pooled aggregate trustworthy? My instinct: NO — different tickers have different microstructure (BTC vs DOGE spread, depth, fill cadence). Report per-ticker. Flag if it changes the verdict.

2. **The pre-fix 03:00 UTC catchup batch (37 alerts, 2 with_bid).** Those rows are in `fast_alerts` but most don't have `features.close`, so the miner couldn't generate decay observations for them. They're effectively excluded. Verify that's the case — they shouldn't pollute current `fast_signal_decay` rows.

3. **Realized-validation residuals** (`realized_validation_count`, `realized_validation_residual`). If any are populated, that's the miner having actually validated a prediction against a future-realized return. Useful sanity check — but the field may be 0 for everything if no fills landed during the soak (executor `live_placed=0`, paper-only). Note in the report whether this signal is even available.

4. **What if no verdict-grade cells exist yet, AND the per-hour rate is so low that ETA-to-MIN_SAMPLES is >7 days?** That's a different problem than "wait another 12h." Means the signal is too rare at current thresholds for meaningful evaluation in any reasonable timeframe. Surface this if it's the situation; the right response is probably "lower the bar to something more frequent before evaluating fade" — but that's an F8b-or-similar decision, not this task's call.

## Rollback plan

- N/A. No code changes. The CC report is informational; if it's wrong, write a follow-up correction in the next review cycle. No production impact.
