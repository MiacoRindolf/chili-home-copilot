# QUEUED TASK: f8b-verification-soak-3 (PROMOTED)

**Promoted to `docs/STRATEGY/NEXT_TASK.md` on 2026-05-07 14:30 UTC as Phase 1 of the combined `f-thread-tail-2026-05-07-2` jumbo brief. `audit-missing-stop-emergency-repair` shipped 2026-05-03; soak data has accumulated for 96+ hours.**

The full Phase 1 body lives in `NEXT_TASK.md`. This file is preserved as a placeholder so the queue history stays linkable; do not edit. If re-queued, restore from `docs/STRATEGY/CC_REPORTS/2026-05-07_f8b-verification-soak-3.md` once it ships, or from git history.

---

The original body below is preserved verbatim for reference.

# QUEUED TASK: f8b-verification-soak-3

**Originally queued as NEXT_TASK on 2026-05-03. Preempted same day by `audit-missing-stop-emergency-repair` (the 2026-05-03 audit found 7 open Robinhood equities with no broker stops and bracket intents parked at `terminal_reject`). Re-promote to NEXT_TASK on or after 2026-05-04 16:30 UTC, after the missing-stop emergency-repair task is DONE and the affected positions are protected.**

The body below is the original task, preserved verbatim so the operator can re-promote without re-deriving the SQL.

---

# NEXT_TASK: f8b-verification-soak-3

STATUS: PENDING

## Goal

Re-execute the F8b verification analysis with ≥ 24h of post-deploy realized data. Two prior soak runs (`f8b-verification-soak` at 10 min post-deploy, `f8b-verification-soak-2` at 28 min post-deploy) were correctly inconclusive: zero post-deploy distinct closed exits both times. **This is the briefed 24h target.** By now, ~15-25 BTC and ~12-18 SOL post-deploy distinct exits should have accumulated — enough for verdict-grade per-ticker decision-tree application.

After this task:

1. **BTC's allowlist membership is decided** with verdict-grade evidence (n ≥ 20 distinct post-deploy exits).
2. **SOL's calibrated 25s delay is validated** against the +3.47 bps counterfactual target.
3. **The next strategic move is named:** F9 (both drift negative), F8b-tightened (BTC drops, SOL stays), F8b stays (both positive), or one-more-soak (still inconclusive).

This is **a pure analysis task**, identical in structure to the prior two soak runs. Deliverable is `docs/STRATEGY/CC_REPORTS/<date>_f8b-verification-soak-3.md`. Zero code commits.

## When to run

**On or after 2026-05-04 16:30 UTC** — ~24h after F8b deploy at 2026-05-03 16:29:20 UTC.

If operator runs before 16:30 UTC, apply the same pre-window provision: bump per-ticker minimum to n=30, report sub-threshold tickers as "inconclusive — recommend f8b-verification-soak-4." This is the third inconclusive-eligible run; if firing again at sub-threshold n, the report's recommendation should explicitly note that **firing more "inconclusive" reports is wasted effort** and the operator should wait the briefed window.

## Why now

Two prior soak runs confirmed:
- F8b allowlist gate is working (zero false rejects).
- DOGE high h=1 + h=5 are verdict-grade-negative; F6.5 negative_edge gate now blocks DOGE high pullback alerts on both horizons. **The brain self-pruned.**
- Pre-deploy data drift continues toward zero: BTC +5.66 → +3.65 (n=8 → 9); SOL +3.34 → +1.58 (n=13 → 14).
- F-hygiene-4.2's C fix delivers ~30 bps DOGE residual reduction; ETH high h=1 verdict-grade positive (curiosity at fire moment, not falsifying).
- 6 first-ever verdict-grade decay cells crossed; none yet at horizons ≥ 5s for BTC or SOL specifically.
- 2 catchup paper_fills (1 BTC, 1 SOL) opened at deploy time; will close ~17:15 UTC May 3.

## Architectural commitments

- **Read-only against `fast_signal_decay` + `fast_alerts` + `fast_exits` + `fast_executions` + `fast_path_status`.** No mutations.
- **No code changes.** One CC report, one doc commit.
- **Use the existing tier system** (verdict-grade ≥ 30 for decay cells; ≥ 20 for distinct realized exits per the F8b decision tree).
- **Three lenses, in priority order** (same as prior soak runs):
  - **Realized P/L per-ticker** on the post-deploy cohort (PRIMARY).
  - **Validation-residual at h=1800** (SECONDARY).
  - **Decay-miner mean ± 2σ at horizons ≥ 5s** (TERTIARY — track BTC/SOL crossings).

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

**Critical: use IN-subquery, not top-level JOIN.** Same anti-inflation pattern as documented in `docs/RUNBOOKS/fast_alerts-microsecond-dup.md`.

Report:

| Ticker | F8a-rerun-2 actual | F8b counterfactual | f8b-verification-soak (10min) | f8b-verification-soak-2 (28min) | This run (~24h) | Verdict |
|---|---|---|---|---|---|---|
| BTC-USD | +5.66 bps n=8 | −0.75 bps n=69 | 0 | 0 | ? | ? |
| SOL-USD | +3.34 bps n=13 | +3.47 bps n=43 | 0 | 0 | ? | ? |

### 2. Cluster-correlation handling

**The 2 catchup paper_fills opened at 2026-05-03 16:29:33 (1 BTC + 1 SOL) are time-correlated.** Treat their aggregate as ONE data point if both close in the same direction. Subsequent organic post-deploy fills are independent.

```sql
-- Identify the catchup-batch fills + closing P/L
SELECT e.ticker, e.id, e.decided_at,
       x.realized_pnl_usd, x.realized_return_pct
FROM fast_executions e
LEFT JOIN fast_exits x ON x.entry_execution_id = e.id
WHERE e.alert_type='volume_breakout_pullback_long'
  AND e.decided_at BETWEEN '2026-05-03 16:29:30' AND '2026-05-03 16:29:40'
ORDER BY e.decided_at;
```

If both catchup fills closed in the same direction, deduct 1 from the effective n in the verdict computation (e.g., 18 distinct exits with 2 catchup → effective n=17).

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

By 24h, DOGE post-fix-only cells should average ~5 bps residuals (vs pre-fix 30+). Confirms the C fix's empirical validation continues.

### 5. SOL calibrated-delay efficacy (the 25s vs 30s comparison)

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

### 6. Verdict-grade decay cells with statistical bounds

```sql
SELECT ticker, score_bucket, horizon_s, sample_count,
       ROUND(mean_return::numeric * 10000, 2) AS mean_bps,
       ROUND((CASE WHEN sample_count > 1
                   THEN SQRT(m2_return/(sample_count-1))/SQRT(sample_count)
                   ELSE NULL END)::numeric * 10000, 2) AS stderr_bps,
       ROUND((mean_return - 2 * SQRT(GREATEST(m2_return / NULLIF(sample_count - 1, 0), 0))
              / SQRT(NULLIF(sample_count, 0)))::numeric * 10000, 2) AS lower_2sigma_bps,
       ROUND((mean_return + 2 * SQRT(GREATEST(m2_return / NULLIF(sample_count - 1, 0), 0))
              / SQRT(NULLIF(sample_count, 0)))::numeric * 10000, 2) AS upper_2sigma_bps
FROM fast_signal_decay
WHERE alert_type='volume_breakout_pullback_long' AND sample_count >= 30
ORDER BY ticker, horizon_s;
```

Track which cells crossed n=30 between f8b-verification-soak-2 (snapshot at 16:57 UTC May 3) and this run. **Specifically watch BTC and SOL at horizons ≥ 5s.** If any cell's mean ± 2σ lands fully one-sided, F6.5's calibrated gates start using that signal automatically.

### 7. Decision tree

```
For each of {BTC-USD, SOL-USD}:

  IF post-deploy effective n ≥ 20 AND mean_bps ≥ +1 (clearly positive):
    -> KEEP in allowlist; pre-deploy suspicions refuted.
  ELIF post-deploy effective n ≥ 20 AND mean_bps ≤ -1 (clearly negative):
    -> DROP from allowlist (next code task).
  ELIF post-deploy effective n ≥ 20 AND mean_bps in [-1, +1] (near-zero):
    -> Inconclusive at this n; trading-cost noise dominates; recommend more soak.
  ELIF post-deploy effective n < 20:
    -> Insufficient. Fourth inconclusive run is overhead — recommend operator
       wait at least 12h before re-running.

Combine outcomes:

  Both KEEP: F8b is validated; consider live-eligibility brief next.
  BTC DROP, SOL KEEP: F8b-tightened (drop BTC); consider F9 in parallel.
  BTC KEEP, SOL DROP: surprising; investigate.
  Both DROP: F9 immediately; full pivot.
  Both inconclusive (after 24h): pivot to F9 anyway — fade hypothesis isn't
    producing decisive realized signal even on the strongest subset.
```

### 8. Write the CC report

`docs/STRATEGY/CC_REPORTS/<date>_f8b-verification-soak-3.md` follows PROTOCOL.md format. Include:
- Five-eval comparison table (F8a-rerun-2 / F8b counterfactual / soak / soak-2 / this run).
- Per-ticker per-lens verdicts.
- Cluster-correlation analysis on the 2 catchup paper_fills.
- SOL calibrated-delay efficacy (25s vs 30s).
- Validation-residual trajectory.
- Verdict-grade decay cells: which horizons crossed since soak-2?
- Recommendation for next NEXT_TASK with one-line description.

## Brain integration (reuse, don't rewrite)

- Same SQL patterns as f8b-verification-soak-2 — refined to use IN-subquery + DISTINCT consistently.
- F-hygiene-4.2's C fix continues to deliver — validate residual reduction trajectory.
- F-hygiene-3.1's UPSERT — validation-count tracks growing.
- F-leak-1.5's integrity-probe pattern — `IN (SELECT id ...)` for distinct counts.
- `docs/RUNBOOKS/fast_alerts-microsecond-dup.md` — canonical query patterns.

## Constraints / do not touch

- **No code commits.**
- **No threshold tuning.**
- **No live placement enable.**
- **No migrations.**
- **No fast-data-worker restart.**
- **Per-ticker analysis is mandatory.** Aggregate is misleading.
- **Realized P/L is the primary verdict lens.**
- **Use IN-subquery for distinct counts.**
- **Cluster-correlation interpretation mandatory** if catchup fills are part of the cohort.

## Out of scope

- Code changes to drop BTC from the allowlist (next task if BTC drifts negative).
- F9 signal redesign brief (next task if both drift negative).
- f-hygiene-5 (structural B fix). Can run in parallel.
- f-leak-3. Still conditional on next OOM event.
- Live-eligibility decision. Separate brief.

## Success criteria

1. `git log --oneline -3` shows ONE new commit, pushed: `docs(strategy): F8b verification soak-3 report + mark NEXT_TASK done`. No code commits.
2. Per-ticker realized P/L on post-deploy cohort reported, with cluster-correlation interpretation applied.
3. Verdict named for each of BTC and SOL using the decision tree.
4. Verbatim verification SQL section reproduces verdict from raw table state.
5. Recommendation for next NEXT_TASK includes a one-line description.
6. F8a soak continues uninterrupted on fast-data-worker.

## Open questions for Cowork (surface in your report only if relevant)

1. **If BTC stays positive on n≥20 post-allowlist** despite counterfactual-uniform-negative, the explanation is likely contemporaneous gate filtering (cooldown / capacity / score correlate with positive BTC outcomes). Surface what the gates filtered for.

2. **If SOL drifts negative or inconclusive at higher n**, the F8a verdict downgrades to "fade refuted on all tickers." F9 becomes the only path forward.

3. **Cluster-correlation effect on n.** With only 2 catchup fills, the cluster effect is small. Note explicitly if/when both close in the same direction.

4. **Verdict-grade cells crossings at horizons ≥ 5s for BTC or SOL.** If any have mean ± 2σ landing fully one-sided, the calibrated-edge gate will start using that signal. Trade rate may step-change as a side-effect.

5. **DOGE residual trajectory post-C-fix.** By 24h the post-fix-only DOGE cells should average ~5 bps residuals. Confirms F-hygiene-4.2's fix delivered as predicted; no action required.

## Rollback plan

- N/A. No code changes. CC report is informational; no production impact.
