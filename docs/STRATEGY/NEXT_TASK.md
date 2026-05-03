# NEXT_TASK: f8b-verification-soak-2

STATUS: DONE

## Goal

Re-execute the F8b verification analysis with ≥ 24h of post-deploy realized data. The first verification soak (f8b-verification-soak) ran 10 minutes after deploy and was correctly reported as inconclusive (zero post-deploy distinct exits). This is the briefed re-run at the originally projected target.

After this task:

1. **BTC's allowlist membership is decided** with verdict-grade evidence: n ≥ 20 distinct post-deploy exits at the calibrated 5s delay.
2. **SOL's calibrated 25s delay is validated** against expected counterfactual (+3.47 bps target) via realized P/L.
3. **The next strategic move is named** with one of three outcomes: F9 (both drift negative), F8b-tightened (BTC drops, SOL stays), or "F8b stays in production" (both positive).

This is **a pure analysis task**, identical in structure to f8b-verification-soak. Deliverable is `docs/STRATEGY/CC_REPORTS/<date>_f8b-verification-soak-2.md`. Zero code commits.

## When to run

**On or after 2026-05-04 16:30 UTC** — ~24h after F8b deploy at 2026-05-03 16:29:20 UTC.

If operator runs before 16:30 UTC, apply the same pre-window provision as f8b-verification-soak: bump per-ticker minimum to n=30, report sub-threshold tickers as "inconclusive — more soak."

## Why now

f8b-verification-soak (2026-05-03 16:38 UTC) confirmed:
- F8b allowlist gate is working correctly (zero false rejects).
- Zero post-deploy distinct exits (~10 min elapsed). Inconclusive verdict.
- **Drift signal:** BTC went +5.66 → +3.65 bps (n=8 → n=9); SOL went +3.34 → +1.58 bps (n=13 → n=14). Both toward zero. Suggestive of counterfactual correctness, not yet conclusive.
- 6 first verdict-grade decay cells crossed n=30 (mostly at h=1, structurally not falsifying).
- DOGE high pullback now auto-blocks via F6.5 negative-edge gate (architectural validation).
- F-hygiene-4.2's C fix empirically verified (~30 bps DOGE residual reduction).

The post-deploy cohort is the only thing missing. ~24h more soak should produce 15-25 BTC and 12-18 SOL distinct closed exits — enough for verdict-grade per-ticker analysis.

## Architectural commitments

- **Read-only against `fast_signal_decay` + `fast_alerts` + `fast_exits` + `fast_executions` + `fast_path_status`.** No mutations.
- **No code changes.** One CC report, one doc commit.
- **Use the existing tier system** (verdict-grade ≥ 30 for decay cells; ≥ 20 for distinct realized exits per the F8b decision tree).
- **Three lenses, in priority order** (same as f8b-verification-soak):
  - **Realized P/L per-ticker** on the post-deploy cohort (PRIMARY).
  - **Validation-residual at h=1800** (SECONDARY — measure cleaner now post-C-fix).
  - **Decay-miner mean ± 2σ** at horizons ≥ 5s (TERTIARY — verdict-grade cells now exist; track which horizons cross).

## Scope — analysis, not code

### 1. Distinct realized P/L per-ticker, post-deploy cohort

**Pinned deploy timestamp: 2026-05-03 16:29:20 UTC.**

```sql
WITH pullback_eids AS (
  SELECT e.id FROM fast_executions e
  JOIN fast_alerts a ON a.ticker=e.ticker
                    AND a.alert_type=e.alert_type
                    AND a.fired_at=e.alert_fired_at
  WHERE a.alert_type='volume_breakout_pullback_long'
    AND e.decided_at > '2026-05-03 16:29:20'
)
SELECT e.ticker, COUNT(*) AS exits,
       ROUND(SUM(x.realized_pnl_usd)::numeric, 4) AS pnl,
       COUNT(*) FILTER (WHERE x.realized_pnl_usd > 0) AS wins,
       ROUND((100.0 * COUNT(*) FILTER (WHERE x.realized_pnl_usd > 0)
              / NULLIF(COUNT(*), 0))::numeric, 1) AS win_rate_pct,
       ROUND(AVG(x.realized_return_pct * 100)::numeric, 2) AS avg_ret_bps,
       ROUND(AVG(x.holding_period_s)::numeric, 0) AS avg_hold_s
FROM fast_exits x
JOIN fast_executions e ON e.id = x.entry_execution_id
WHERE x.entry_execution_id IN (SELECT id FROM pullback_eids)
GROUP BY e.ticker ORDER BY exits DESC;
```

**Critical: use IN-subquery, not top-level JOIN. The JOIN form inflates by JOIN cardinality (the n=142 → 37 bug from f-leak-1.5 and the n=56 → 14 scratch bug from f8b-verification-soak).** The runbook (`docs/RUNBOOKS/fast_alerts-microsecond-dup.md`) documents the convention.

Report:

| Ticker | F8a-rerun-2 actual | F8b counterfactual | f8b-verification-soak (10min) | This run (~24h) | Verdict |
|---|---|---|---|---|---|
| BTC-USD | +5.66 bps n=8 | −0.75 bps n=69 | 0 post-deploy | ? | ? |
| SOL-USD | +3.34 bps n=13 | +3.47 bps n=43 | 0 post-deploy | ? | ? |

### 2. Cluster-correlation handling

**The 14 catchup paper_fills opened at 2026-05-03 16:29:33 are time-correlated.** They entered at near-identical market state. Treat their aggregate P/L as ONE data point if they all close green or all red.

```sql
-- Identify the catchup-batch fills specifically
SELECT e.ticker, COUNT(*) AS n,
       MIN(e.decided_at) AS earliest,
       MAX(e.decided_at) AS latest,
       MIN(x.realized_pnl_usd) AS min_pnl,
       MAX(x.realized_pnl_usd) AS max_pnl,
       AVG(x.realized_pnl_usd) AS avg_pnl,
       STDDEV(x.realized_pnl_usd) AS stddev_pnl
FROM fast_executions e
JOIN fast_exits x ON x.entry_execution_id = e.id
WHERE e.alert_type='volume_breakout_pullback_long'
  AND e.decided_at BETWEEN '2026-05-03 16:29:30' AND '2026-05-03 16:29:40'
GROUP BY e.ticker;
```

If `stddev_pnl` is small (e.g., < 30% of |avg_pnl|), the cluster is highly correlated — note this in the report and apply the "one data point" interpretation.

### 3. Allowlist gate efficacy

```sql
-- Per-ticker reject distribution since deploy
SELECT e.ticker, e.reject_reason, COUNT(*) AS n
FROM fast_executions e
WHERE e.alert_type='volume_breakout_pullback_long'
  AND e.decided_at > '2026-05-03 16:29:20'
  AND e.decision='rejected'
GROUP BY 1, 2 ORDER BY 1, n DESC;

-- Allowlist false-reject canary
SELECT COUNT(*) AS false_rejects
FROM fast_executions e
WHERE e.alert_type='volume_breakout_pullback_long'
  AND e.decided_at > '2026-05-03 16:29:20'
  AND e.ticker IN ('BTC-USD', 'SOL-USD')
  AND e.reject_reason LIKE 'pullback_ticker%';
-- Expected: 0.
```

### 4. Validation-residual at h=1800, post-F-hygiene-4.2 fix

```sql
SELECT ticker, score_bucket, horizon_s, sample_count,
       realized_validation_count AS val_n,
       ROUND(realized_validation_residual::numeric * 10000, 2) AS resid_bps,
       ROUND(mean_return::numeric * 10000, 3) AS miner_mean_bps
FROM fast_signal_decay
WHERE alert_type='volume_breakout_pullback_long'
  AND realized_validation_count > 0
ORDER BY ticker, horizon_s;
```

Track residual reduction trajectory across cells. By 24h the C fix's effect should be visible across all DOGE cells (residuals dropping from 30+ bps toward ~5 bps).

### 5. SOL calibrated-delay efficacy

```sql
WITH pullback_eids AS (
  SELECT e.id, e.decided_at FROM fast_executions e
  JOIN fast_alerts a ON a.ticker=e.ticker AND a.alert_type=e.alert_type
                    AND a.fired_at=e.alert_fired_at
  WHERE a.alert_type='volume_breakout_pullback_long' AND e.ticker='SOL-USD'
)
SELECT
  CASE WHEN p.decided_at < '2026-05-03 16:29:20' THEN 'pre-F8b (30s)' ELSE 'post-F8b (25s)' END AS era,
  COUNT(DISTINCT p.id) AS exits,
  ROUND(AVG(x.realized_return_pct * 100)::numeric, 2) AS avg_ret_bps,
  ROUND((100.0 * COUNT(DISTINCT p.id) FILTER (WHERE x.realized_pnl_usd > 0)
         / NULLIF(COUNT(DISTINCT p.id), 0))::numeric, 1) AS win_rate_pct
FROM fast_exits x
JOIN pullback_eids p ON p.id = x.entry_execution_id
GROUP BY era ORDER BY era;
```

Counterfactual predicted SOL post-F8b should be +3.47 bps vs pre-F8b's −2.45 bps on the same data — a ~6 bps swing. Realized data on the new cohort tests this.

### 6. Decay-miner per-cell snapshot (verdict-grade growth)

```sql
SELECT
  CASE WHEN sample_count >= 30 THEN 'verdict_grade'
       WHEN sample_count >= 10 THEN 'suggestive'
       ELSE 'sparse' END AS tier,
  COUNT(*) AS cells, SUM(sample_count) AS total_obs
FROM fast_signal_decay
WHERE alert_type='volume_breakout_pullback_long'
GROUP BY tier ORDER BY tier;
```

Detail the verdict-grade cells specifically. Which horizons crossed? At horizons ≥ 5s, has any cell's mean ± 2σ landed entirely positive or entirely negative?

### 7. Decay-miner health

Standard checks:
- `obs_scheduled / obs_finalized` ratio.
- `pending_heap` oscillation per `dispatch-decay-heap-trend.ps1 24`.
- `db_errors` should still be 0.
- Watchdog OK heartbeat.

### 8. Decision tree

```
For each of {BTC-USD, SOL-USD}:

  IF post-deploy distinct exits ≥ 20 AND mean_bps ≥ +1 (clearly positive):
    -> KEEP in allowlist; pre-F8b suspicions refuted.
  ELIF post-deploy distinct exits ≥ 20 AND mean_bps ≤ -1 (clearly negative):
    -> DROP from allowlist (next code task).
  ELIF post-deploy distinct exits ≥ 20 AND mean_bps in [-1, +1] (near-zero):
    -> Inconclusive at this n; recommend more soak; trading-cost noise dominates.
  ELIF post-deploy distinct exits < 20:
    -> Insufficient. Recommend f8b-verification-soak-3 with projected ETA.

Combine outcomes:

  Both KEEP: F8b stays; consider live-eligibility brief next.
  BTC DROP, SOL KEEP: F8b-tightened (drop BTC, recalibrate); consider F9 in parallel.
  BTC KEEP, SOL DROP: surprising (counterfactual was opposite); investigate.
  Both DROP: F9 immediately; the fade hypothesis fails on the strongest subset.
  Both inconclusive: more soak.
```

### 9. Apply cluster-correlation interpretation

If the catchup-batch fills (entered at 16:29:33) all close in the same direction, treat their aggregate as one data point. This affects the post-deploy n: e.g., 18 distinct exits with 14 from the cluster = effective n ≈ 4 + 1 = 5, not 18. **Surface this explicitly in the verdict.**

### 10. Write the CC report

`docs/STRATEGY/CC_REPORTS/<date>_f8b-verification-soak-2.md` follows PROTOCOL.md format. Include:
- Four-eval comparison table (F8a-rerun-2 / F8b counterfactual / f8b-verification-soak / this run).
- Per-ticker per-lens verdicts.
- Cluster-correlation analysis on the catchup-batch fills.
- SOL calibrated-delay efficacy (post-F8b 25s vs pre-F8b 30s).
- Validation-residual trajectory (DOGE specifically).
- Verdict-grade decay cells: which horizons crossed?
- Recommendation for next NEXT_TASK with one-line description.

## Brain integration (reuse, don't rewrite)

- Same SQL patterns as f8b-verification-soak — refined to use IN-subquery + DISTINCT consistently.
- F-hygiene-4.2 fix in place — residual analysis is now meaningful.
- F-hygiene-3.1 UPSERT in place — validation-count tracks growing.
- F-leak-1.5 integrity probe pattern — `IN (SELECT id ...)` for distinct counts.
- `docs/RUNBOOKS/fast_alerts-microsecond-dup.md` — canonical query patterns.

## Constraints / do not touch

- **No code commits.**
- **No threshold tuning.**
- **No live placement enable.**
- **No migrations.**
- **No fast-data-worker restart.**
- **Per-ticker analysis is mandatory.** Aggregate is misleading because the underlying tickers were bimodal.
- **Realized P/L is the primary verdict lens.**
- **Use IN-subquery for distinct counts.** Top-level JOINs inflate.
- **Cluster-correlation interpretation is mandatory** when the catchup-batch is part of the cohort.

## Out of scope

- Code changes to drop BTC from the allowlist (next task if BTC drifts negative).
- F9 signal redesign brief (next task if both drift negative).
- f-hygiene-5 (structural B fix). Can run in parallel.
- f-leak-3. Still conditional on next OOM event.
- Live-eligibility decision. Separate brief.
- DELAY_S recalibration. Manual re-run of `scripts/calibrate-pullback-delay.py` after this verifies framework.

## Success criteria

1. `git log --oneline -3` shows ONE new commit, pushed: `docs(strategy): F8b verification soak-2 report + mark NEXT_TASK done`. No code commits.
2. Per-ticker realized P/L on post-deploy cohort reported, with cluster-correlation interpretation applied.
3. Verdict named for each of BTC and SOL using the decision tree.
4. Verbatim verification SQL section reproduces verdict from raw table state.
5. Recommendation for next NEXT_TASK includes a one-line description.
6. F8a soak continues uninterrupted on fast-data-worker.

## Open questions for Cowork (surface in your report only if relevant)

1. **If BTC stays positive on n≥20 post-allowlist** despite counterfactual-uniform-negative, the explanation is likely contemporaneous gate filtering (cooldown / capacity / score thresholds correlate with positive BTC outcomes). Surface what the gates filtered for; that's input to F9's design.

2. **If SOL drifts negative or inconclusive at higher n**, the F8a verdict is "fade refuted on all tickers." F9 becomes the only path forward.

3. **Cluster-correlation effect on n.** If 14 of 25 post-deploy exits are from the catchup batch and all closed green, effective n is ~12 (10 distinct organic + 1 cluster + ~1 noise). Be explicit in the verdict about which n drove the call.

4. **Verdict-grade cells at horizons ≥ 5s.** If any have mean ± 2σ landing fully on one side of zero, the calibrated-edge gate will start using that signal automatically. Surface explicitly — it's the system tightening, the trade rate may drop as a side-effect.

5. **DOGE residual trajectory post-C-fix.** By 24h the post-fix-only DOGE cells should average ~5 bps residuals (vs 30+ bps pre-fix). Confirms the C fix delivered as predicted; no action required.

## Rollback plan

- N/A. No code changes. CC report is informational; no production impact.
