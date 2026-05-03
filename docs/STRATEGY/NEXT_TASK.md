# NEXT_TASK: f8a-evaluation-rerun-2

STATUS: DONE

## Goal

Re-run the F8a fade hypothesis evaluation against accumulated `fast_signal_decay` + `fast_exits` data. This is the **third** F8a evaluation cycle:

1. `f8a-evaluation` (2026-05-02 17:48 UTC) — found 0 verdict-grade cells, suggestive evidence at horizon=1, recommended 24h more soak.
2. `f8a-evaluation-rerun` (2026-05-03 04:35 UTC) — still 0 verdict-grade cells (decay miner basis), but **surfaced 142 closed pullback round trips** as a verdict-grade realized-P/L signal (avg −6.7 bps, 30% win rate). Recommended pivot to F9.
3. **THIS TASK** — re-run with three structural corrections folded in:
   - **n=37, not n=142** (per f-leak-1.5 integrity probe; same root-cause family as F-hygiene-2.1's MultipleResultsFound).
   - **Validation-count UPSERT now active** (per F-hygiene-3.1; residuals no longer silently dropped at ~70% rate).
   - **Horizon=3600 concentration acknowledged** (pullback's 49-min avg hold maps to horizon=3600; validations don't spread across other horizons).

This is **a pure analysis task**, identical in structure to the prior two evaluations. Deliverable is `docs/STRATEGY/CC_REPORTS/<date>_f8a-evaluation-rerun-2.md`. Zero code commits.

After this task:

1. **F8a verdict is settled** with corrected measurement and more soak data: supported, refuted, or "still suggestive — keep soaking."
2. **F9 has a clear go/no-go.** If verdict is "fade refuted at horizon=3600," F9 brief is next. If "supported," F8b. If "still suggestive," more soak and another rerun.
3. **No code changes.**

## Why now

- F-hygiene-3 closed cleanly: validation UPSERT structurally in place, microsecond-dup audit complete (branch C), runbook published. Measurement is now cleaner.
- F8a soak has been accumulating for ~36h since the last rerun. Sample counts should have grown materially.
- The two interpretation caveats from F-hygiene-3's review need explicit handling in this brief: closest-horizon-mapping concentration and cold-start variance artifact.

## Architectural commitments

- **Read-only against `fast_signal_decay` + `fast_alerts` + `fast_exits` + `fast_executions` + `fast_path_status`.** No mutations.
- **No code changes.** Zero commits beyond the doc/NEXT_TASK marker.
- **Use the existing MIN_SAMPLES floor and negative-edge exclusion criterion** the brain already encodes. Don't re-derive them.
- **Honest reporting.** Same tier system: verdict-grade ≥ 30, suggestive 10–29, sparse < 10. Don't fabricate verdicts from suggestive cells.
- **Three lenses, not one:**
  - **Decay-miner per-cell** (mean ± 2σ, observation-side).
  - **Realized P/L per-ticker** (n=37+ as of last check; primary lens for the strategic question).
  - **Realized-validation residuals at horizon=3600** (newly available after F-hygiene-3.1 UPSERT — should be a cleaner accuracy lens than mean alone).

## Scope — analysis, not code

### 1. Pull current state of `fast_signal_decay` for `volume_breakout_pullback_long`

Same SQL as prior briefs:

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

Bucket cells: verdict-grade (≥ 30), suggestive (10–29), sparse (< 10).

### 2. Realized P/L on closed pullback round trips — DISTINCT exits, not JOIN cardinality

**Critical correction from prior brief:** the prior rerun used a top-level JOIN that inflated n by ~3.8x. Use the integrity-probe-style query:

```sql
SELECT COUNT(*) FILTER (WHERE entry_execution_id IN (
  SELECT e.id FROM fast_executions e
  JOIN fast_alerts a ON a.ticker=e.ticker
                    AND a.alert_type=e.alert_type
                    AND a.fired_at=e.alert_fired_at
  WHERE a.alert_type='volume_breakout_pullback_long'
)) AS distinct_pullback_exits FROM fast_exits;
-- Expected: ~37 last time; should have grown over the soak.

-- Per-ticker, DISTINCT exits:
WITH pullback_exits AS (
  SELECT x.* FROM fast_exits x
  JOIN fast_executions e ON e.id = x.entry_execution_id
  WHERE e.id IN (
    SELECT e2.id FROM fast_executions e2
    JOIN fast_alerts a ON a.ticker=e2.ticker
                      AND a.alert_type=e2.alert_type
                      AND a.fired_at=e2.alert_fired_at
    WHERE a.alert_type='volume_breakout_pullback_long'
  )
)
SELECT
  e.ticker,
  COUNT(*) AS exits,
  SUM(x.realized_pnl_usd) AS total_pnl_usd,
  COUNT(*) FILTER (WHERE x.realized_pnl_usd > 0) AS wins,
  AVG(x.realized_return_pct) AS avg_return_pct,
  AVG(x.holding_period_s) AS avg_hold_s
FROM pullback_exits x
JOIN fast_executions e ON e.id = x.entry_execution_id
GROUP BY e.ticker
ORDER BY exits DESC;
```

Compute aggregate avg return and win rate from the per-ticker distinct counts.

### 3. Validation residual signal at horizon=3600 (post-UPSERT)

Per F-hygiene-3's review: pullback's 49-min average hold maps `min(HORIZONS_S, key=...)` → 3600s. **Validations land at horizon=3600 specifically.** Other horizons get zero realized-residual signal.

```sql
-- Validation-only cells (UPSERT INSERT branch fired)
SELECT COUNT(*) AS validation_only_cells
FROM fast_signal_decay
WHERE alert_type='volume_breakout_pullback_long'
  AND sample_count = 0
  AND realized_validation_count > 0;

-- Per-ticker validation residuals at horizon=3600:
SELECT ticker, score_bucket,
       sample_count,
       realized_validation_count AS val_n,
       realized_validation_residual,
       CASE WHEN realized_validation_count > 0
            THEN realized_validation_residual / realized_validation_count
            ELSE NULL END AS avg_val_residual_bps
FROM fast_signal_decay
WHERE alert_type='volume_breakout_pullback_long'
  AND horizon_s = 3600
ORDER BY ticker, score_bucket;
```

The residual is `mean - realized_return` (or similar — verify by reading `decay_miner._handle_exit_inserted`). A residual near zero means the miner mean was a good predictor; a large positive residual means the miner over-predicted; large negative means under-predicted. **For F8a's "is the fade real?" question, residual signal at horizon=3600 is the cleanest measurement.**

### 4. Comparison table to prior evaluations

| Metric | f8a-evaluation (2026-05-02 17:48) | f8a-evaluation-rerun (2026-05-03 04:35) | This run | Δ |
|---|---|---|---|---|
| Verdict-grade cells (n ≥ 30) | 0 | 0 | ? | ? |
| Suggestive cells (10–29) | 2 | 9 | ? | ? |
| Sparse cells | 81 | 92 | ? | ? |
| Total observations | 208 | 468 | ? | ? |
| Total post-fix alerts (`id > 2300`) | 114 | 191 | ? | ? |
| Distinct pullback exits | (not measured) | **37** (corrected from 142) | ? | ? |
| Validation-only cells | 0 | 0 | ? (should be > 0 by now) | ? |
| Per-bucket validations max | 1 | 2 | ? | ? |
| `db_errors` | 13 (frozen) | 0 (durable) | 0 | unchanged |

### 5. Decay-miner health snapshot

Same checks as prior:
- `obs_scheduled / obs_finalized` ratio.
- `pending_heap` oscillation per `dispatch-decay-heap-trend.ps1 12`.
- `db_errors` should still be 0.
- Watchdog OK heartbeat firing.

### 6. Verdict logic — refined for the new lenses

```
PRIMARY LENS: realized P/L on distinct pullback exits.
SECONDARY LENS: validation-residual at horizon=3600 (per-bucket signal accuracy).
TERTIARY LENS: decay-miner mean ± 2σ at horizons ≥ 5s (observation-side, may still be sparse).

IF distinct-exit n ≥ 30 AND aggregate avg P/L is clearly negative
  AND no per-ticker subset shows positive edge above noise:
    -> "Fade hypothesis REFUTED. Recommend F9: signal redesign."

ELIF distinct-exit n ≥ 30 AND aggregate is near zero or positive:
    -> "Fade hypothesis SUPPORTED. Recommend F8b: calibrate DELAY_S from data."

ELIF distinct-exit n ≥ 30 AND results are mixed (e.g., BTC positive, others negative):
    -> "Fade hypothesis SUBSET-SUPPORTED. Recommend F8b restricted to the
        positive subset, OR F9 if the subset is too narrow to be production-viable."

ELIF distinct-exit n < 30:
    -> "Insufficient distinct exits. Recommend continuing soak. Project ETA
        from observed exit rate."
```

**Don't conflate decay-miner mean with realized P/L.** They measure different things: miner mean is forward-return at horizon-N from alert-fire moment; realized P/L is entry-to-exit P/L on actual paper trades. The miner is the predictor; the realized data is the truth.

### 7. Interpretation caveats (mandatory in the report)

- **Cold-start backfill variance artifact:** suggestive cells with stderr=0 or near-zero may reflect duplicate observations from the catchup batch, not genuinely tight measurements. Note explicitly when reporting cells in this state.
- **Horizon=3600 concentration:** validations only land at horizon=3600 for pullback. Reporting realized-residual at any other horizon (1, 5, 30, 60, 300, 1800, 3600, 14400) requires checking that horizon's `realized_validation_count > 0`; otherwise it has no data and should be reported as such.
- **F8a-fix capture rate sanity:** should still be 100% post-fix. Flag any drift.

### 8. ETA projection (if more soak is needed)

Same shape as prior:
- Distinct-exit rate over last 24h.
- Hours-to-30-distinct-exits at observed rate.
- Specific clock time for re-run-3 if applicable.

### 9. Write the CC report

`docs/STRATEGY/CC_REPORTS/<date>_f8a-evaluation-rerun-2.md` follows PROTOCOL.md format. Include:
- Three-evaluation comparison table.
- Per-lens verdicts (decay-miner, realized P/L, validation-residual at h=3600).
- Per-ticker breakdown of distinct realized P/L.
- Validation-count growth proof (validation_only_cells > 0 if UPSERT INSERT branch fired).
- Caveats applied per Section 7.
- Recommendation for next NEXT_TASK (F8b / F9 / soak more).
- Open questions for Cowork.

## Brain integration (reuse, don't rewrite)

- `fast_signal_decay` table — truth source for miner-side and validation-residual-side.
- `fast_exits` + `fast_executions` + `fast_alerts` — truth source for realized P/L.
- F-hygiene-3.1 UPSERT — validation residuals now landing structurally.
- F-hygiene-3.3 runbook (`docs/RUNBOOKS/fast_alerts-microsecond-dup.md`) — canonical query patterns.
- `scripts/dispatch-decay-heap-trend.ps1` — heap + errs trend.
- `scripts/dispatch-stats-logger.ps1` — chili memory if running.
- F8a's CC reports + Cowork reviews — context.

## Constraints / do not touch

- **No code commits.** One markdown file is the entire deliverable.
- **No threshold tuning.**
- **No live placement enable.**
- **No migrations.**
- **No fast-data-worker restart** (would interrupt soak).
- **Don't conflate alert types** — `volume_breakout_long` ≠ `volume_breakout_pullback_long`.
- **Don't average across tickers** without flagging — per-ticker first.
- **Don't extrapolate from spikes** — same convention as prior briefs.
- **Realized P/L is the primary lens for verdict** — but report all three lenses.

## Out of scope

- F8b / F9 — next briefs depending on this verdict.
- f-leak-3 — conditional on next OOM event.
- Refactor of decay_miner / horizon-mapping logic.
- The "write validations to nearby horizons with weights" idea (future signal-design improvement).
- Cross-pair correlation analysis.
- Dedupe at observation-write time (cold-start variance artifact fix).
- The structural fast_alerts microsecond-resolution change (separate decision).

## Success criteria

1. `git log --oneline -3` shows ONE new commit, pushed: `docs(strategy): F8a evaluation rerun-2 report + mark NEXT_TASK done`. No code commits.
2. `docs/STRATEGY/CC_REPORTS/<date>_f8a-evaluation-rerun-2.md` follows PROTOCOL.md format and includes:
   - Three-evaluation comparison table.
   - Per-lens verdicts.
   - Distinct-exit count via the `IN (SELECT id ...)` query (NOT a top-level JOIN).
   - Validation-residual analysis at horizon=3600.
   - Recommendation for next NEXT_TASK with one-line description.
3. The verbatim verification SQL section reproduces the verdict from raw table state.
4. If the recommendation is "soak more," includes a specific clock time for re-run-3.
5. F8a soak continues uninterrupted on fast-data-worker.

## Open questions for Cowork (surface in your report only if relevant)

1. **If validation-residual signal is meaningfully different from decay-miner mean** at horizon=3600, that's strategically important — it means the miner's mean is NOT a good predictor of realized return. Surface explicitly with the magnitude of the disagreement.

2. **If validation_only_cells is still 0** despite the UPSERT being in place, that means every exit's chosen horizon happens to be a populated cell. Either (a) the UPSERT INSERT branch hasn't been triggered yet because no exit has hit a cold cell, or (b) something is preventing it from firing. Distinguish by checking the implementation in `decay_miner._handle_exit_inserted`.

3. **If per-ticker realized P/L shows BTC positive AND DOGE negative** at higher distinct-exit counts (n ≥ 30 each), that's the "subset-supported" branch. F8b restricted to BTC is a defensible recommendation; F9 is the alternative. Cowork's call.

4. **If distinct exits are still < 30**, the strategic discussion is whether `VOL_BREAKOUT_MULT = 2.0` is too aggressive for fade evaluation in any reasonable timeframe. Fourth iteration would just be more of the same. F9 (different signal) becomes more attractive than continued soak. Surface explicitly.

## Rollback plan

- N/A. No code changes. The CC report is informational; no production impact.
