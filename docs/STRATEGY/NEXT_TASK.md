# NEXT_TASK: f8a-evaluation-rerun

STATUS: DONE

## Goal

Re-run the F8a fade hypothesis evaluation against accumulated `fast_signal_decay` data after ~24h of additional soak. Same brief shape as `f8a-evaluation` (which produced an "insufficient data" verdict at 2026-05-02 17:48 UTC); the data is now expected to have matured to verdict-grade in ~3+ cells.

This is **a pure analysis task**, identical in structure to the prior evaluation. Deliverable is `docs/STRATEGY/CC_REPORTS/<date>_f8a-evaluation-rerun.md`. Zero code commits unless something is fundamentally broken in the data pipeline.

After this task:

1. **The fade hypothesis has a verdict, or another honest "still waiting" with an updated ETA.** If the data is statistically actionable, write the verdict. If still not, project the next re-run window with current per-hour rates.
2. **The next strategic move is named.** Either F8b (calibrate `VOL_BREAKOUT_PULLBACK_DELAY_S` from data, move toward live), F9 (new signal types — fade refuted), or "soak another N hours and re-run again."
3. **No code changes.** No threshold tuning. No new gates. No retroactive scope creep into F8b.

## When to run

**On or after 2026-05-03 17:00 UTC** — this is the projected re-run time from the prior f8a-evaluation, computed from observed per-cell rates (~1.35/hr DOGE-high-bucket × need 23 more samples ≈ 17h conservatively, rounded up to 24h).

If the operator runs `claude` before 17:00 UTC, Claude Code should:
- Still execute the analysis, but
- Note in the report that the soak window is below the projected ETA, and
- Apply more conservative interpretation thresholds to any verdict-grade cells that exist.

If the operator runs after 17:00 UTC, proceed as briefed.

## Why now (relative to prior evaluation)

- F8a-evaluation at 2026-05-02 17:48 UTC found 0 verdict-grade cells, 2 suggestive (both DOGE-USD horizon=1, expected-negative because horizon=1 IS the fire moment), 81 sparse.
- ETA projection said ~24h to reach 3+ verdict-grade cells under steady-state ~1.35/hr DOGE-high-bucket rate.
- F-hygiene-2 fix (commit `742394f`, `decay_miner exit-validation join MultipleResultsFound`) landed on 2026-05-03 02:30-ish UTC. Pre-fix, exit-validation residuals were silently dropped on duplicate-`fired_at` alerts; post-fix they land correctly. **This may marginally improve `realized_validation_count` density** for any pullback alerts that produced an exit lineage during the soak — though the bigger constraint (gate stack blocks pullback fills, no `fast_exits` rows reference pullback alerts in paper mode) is unchanged. Most cells will still show `realized_validation_count = 0`; only edge cases improve.
- F8a-fix's 100% capture rate invariant has held continuously through the soak window. F-hygiene-1's `last_error` self-clear continues working naturally.

## Architectural commitments

- **Read-only against `fast_signal_decay` + `fast_alerts` + `fast_path_status`.** No mutations.
- **No new code.** No edits to scanner, miner, executor, gates, calibration helpers, or any module.
- **Use the existing MIN_SAMPLES floor and negative-edge exclusion criterion** the brain already encodes — don't re-derive them inline.
- **Honest reporting.** Same tier system as the prior brief (verdict-grade ≥ 30, suggestive 10–29, sparse < 10). Don't fabricate verdicts from suggestive cells.
- **One CC report, no code commits.**

## Scope — analysis, not code

### 1. Pull current state of `fast_signal_decay` for `volume_breakout_pullback_long`

```sql
SELECT ticker, score_bucket, horizon_s,
       sample_count,
       mean_return,
       m2_return,
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

Bucket the cells into the same three tiers:
- **Verdict-grade** (`sample_count >= 30`)
- **Suggestive** (`10 <= sample_count < 30`)
- **Sparse** (`sample_count < 10`)

### 2. Cross-reference with `fast_alerts`

- Total `volume_breakout_pullback_long` alerts written since F8a-fix landed (`id > 2300`).
- Capture rate (with `best_bid` AND `close`) over the same window — should still be ~100%; flag any drift.
- Hourly distribution over the last 24h to spot bursts vs steady accumulation.
- **Compare to prior evaluation's snapshot:** at 2026-05-02 17:48 UTC there were 114 post-fix alerts, 208 observations, 83 cells. How much have these grown?

### 3. Decay-miner health snapshot

Same checks as the prior brief:
- `obs_scheduled` vs `obs_finalized` ratio.
- `pending_heap` size — should oscillate around steady-state per F-hygiene-2.3's diagnostic. If it now grows monotonically, that's a finding.
- `db_errors` — **should be 0 throughout the post-`742394f` window** (durably 0, not just zero on the immediate post-restart probe). Run the dispatch script: `.\scripts\dispatch-decay-heap-trend.ps1 12` and verify.
- Watchdog OK heartbeat firing: `docker compose logs fast-data-worker --since 5m | grep "watchdog: OK"` should show ~5 lines.

### 4. Verdict logic — same decision tree

```
IF >= 3 verdict-grade cells AND >= 1 of them is positive (mean - 2*stderr > 0)
  AND no verdict-grade cell is negative (mean + 2*stderr < 0):
    -> "Fade hypothesis SUPPORTED at <list horizons>. Recommend F8b."

ELIF >= 3 verdict-grade cells AND all are negative or zero:
    -> "Fade hypothesis REFUTED. Recommend F9."

ELIF some verdict-grade cells positive, some negative, no consistent pattern:
    -> "Fade signal is noisy. Recommend either: (a) longer soak, OR (b) F8b
        only on the consistently-positive horizons."

ELIF < 3 verdict-grade cells:
    -> "Insufficient data. Recommend continuing soak."
```

**Important refinement from f8a-evaluation:** horizon=1s is the *fire moment* (no reversion has elapsed); negative-CI there is structurally expected, not evidence against the fade. **Apply the verdict only to horizons ≥ 5s.** A horizon=1s cell that's negative is informational, not falsifying.

If after 48h total soak (i.e., this re-run still finds < 3 verdict-grade cells at horizons ≥ 5s), **strongly consider whether `VOL_BREAKOUT_MULT = 2.0` makes the signal too rare to evaluate in any reasonable timeframe.** That's a strategic discussion (lower MULT = more firings = noisier data, vs. pivot to F9 = different signal shape). Surface as Open Question if relevant.

### 5. ETA projection (if more soak is needed)

Same shape as prior:
- Average alerts/hour over last 24h (excluding spikes).
- Projected hours-to-MIN_SAMPLES at the median bucket of horizons ≥ 5s.
- Specific clock time at which it's reasonable to re-run.

### 6. Write the CC report

`docs/STRATEGY/CC_REPORTS/<date>_f8a-evaluation-rerun.md` follows PROTOCOL.md format. Include explicit comparison to the prior f8a-evaluation snapshot:

| Metric | 2026-05-02 17:48 (prior) | This run | Δ |
|---|---|---|---|
| Verdict-grade cells | 0 | ? | ? |
| Suggestive cells | 2 | ? | ? |
| Sparse cells | 81 | ? | ? |
| Total observations | 208 | ? | ? |
| Total post-fix alerts (`id > 2300`) | 114 | ? | ? |
| `db_errors` | 13 (frozen) | ? (should be 0 throughout window) | ? |

Plus the verdict (or "still insufficient" with updated ETA), per-cell table for verdict-grade and suggestive cells, health snapshot, recommendation, open questions.

### 7. Verbatim verification SQL — for next review

Same SQL set as the prior brief. Append:

```bash
# F-hygiene-2.3 dispatch script — heap + errs trend over last 12h
.\scripts\dispatch-decay-heap-trend.ps1 12
```

## Brain integration (reuse, don't rewrite)

- `fast_signal_decay` table — truth source. Don't approximate from `fast_alerts`.
- `app/services/trading/fast_path/calibration.py` — MIN_SAMPLES + negative-edge logic.
- `scripts/dispatch-decay-heap-trend.ps1` — F-hygiene-2.3 diagnostic for heap + errs trend.
- F8a-evaluation's CC report (`docs/STRATEGY/CC_REPORTS/2026-05-02_f8a-evaluation.md`) — prior snapshot for the comparison table.
- F8a-fix's CC report (`docs/STRATEGY/CC_REPORTS/2026-05-02_f8a-fix-per-ticker-heaps.md`) — `id > 2300` convention.

## Constraints / do not touch

- **No code commits.** Zero. The deliverable is one markdown file.
- **No threshold tuning.** Don't change `VOL_BREAKOUT_MULT`, `VOL_BREAKOUT_PULLBACK_DELAY_S`, MIN_SAMPLES, the negative-edge exclusion criterion, score bucket cutoffs, or anything else.
- **No live placement enable.** Default mode stays paper.
- **No migrations.**
- **Don't conflate `volume_breakout_long` with `volume_breakout_pullback_long`** — different alert types.
- **Don't average across tickers** without flagging — bucket per `(ticker, score_bucket, horizon_s)`.
- **Don't extrapolate from spikes** — treat the 11:00 UTC 2026-05-02 60-alert spike as one data point, same convention as prior.
- **Apply verdict logic only at horizons ≥ 5s** — horizon=1s is the fire moment, not the fade test.

## Out of scope

- F8b: calibrating `VOL_BREAKOUT_PULLBACK_DELAY_S`. Next task if verdict supports.
- F9: new signal types. Next task if verdict refutes.
- F7: Kelly sizing. Still deferred.
- Live-mode enablement.
- Refactoring the decay miner.
- The code-twin shared-helper between exit_manager and decay_miner (deferred from F-hygiene-2).
- Cross-pair correlation analysis.

## Success criteria

1. `git log --oneline -3` shows ONE new commit, pushed to origin: `docs(strategy): F8a evaluation rerun report + mark NEXT_TASK done`. No code commits.
2. `docs/STRATEGY/CC_REPORTS/<date>_f8a-evaluation-rerun.md` exists, follows PROTOCOL.md format, contains the verdict (or honest "insufficient data" with updated ETA), and includes the comparison-to-prior table.
3. The report's verbatim SQL section reproduces the verdict from raw table state — anyone can re-run the SELECTs and get the same numbers.
4. The dispatch-script output (subtask 3 verification) is referenced or excerpted to confirm `db_errors` durably at 0 post-`742394f` and `pending_heap` still oscillating.
5. If the recommendation is "F8b" or "F9," it includes a one-line description of what that brief should focus on.

## Open questions for Cowork (surface in your report only if relevant)

1. **If 3+ verdict-grade cells exist and the verdict is "fade SUPPORTED,"** F8b's brief will need to reckon with the fact that current gate stack blocks pullback fills (so calibration must come from miner means alone, not realized validations). Worth flagging if the verdict comes up positive.

2. **If the per-hour rate has dropped vs the prior evaluation** (i.e., even fewer firings than the steady-state ~2.3/hr), that's a tell that the signal got rarer over the soak window. Could reflect market conditions calming, or a structural issue. Worth noting.

3. **If the F-hygiene-2.1 fix has produced any non-zero `realized_validation_count`** that's a small but genuine signal — would mean exit-validation is now firing on at least some lineage. Even small numbers are interesting because pre-fix it was structurally near-zero.

4. **48-hour total soak threshold.** If this re-run still finds < 3 verdict-grade cells at horizons ≥ 5s, the strategic discussion is whether `VOL_BREAKOUT_MULT = 2.0` is too aggressive for fade evaluation in *any* reasonable timeframe. Surface explicitly — that's a Cowork decision, not Claude Code's call.

## Rollback plan

- N/A. No code changes. The CC report is informational; no production impact.
