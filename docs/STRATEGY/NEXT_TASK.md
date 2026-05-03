# NEXT_TASK: f8b-verification-soak

STATUS: DONE

## Goal

Verify F8b's allowlist + calibrated DELAY_S configuration against ~24h+ of post-deploy realized data. Resolve the counterfactual-vs-realized disagreement on BTC: counterfactual on n=69 says uniformly negative (−0.75 to −5.03 bps across all 10 candidate delays); F8a-evaluation-rerun-2 actual exits on n=8 said +5.66 bps. **Which is right?** Soak data on the new allowlist regime is the tie-breaker.

After this task:

1. **BTC's allowlist membership is decided** based on its actual realized P/L under the new gate stack + calibrated 5s delay. If BTC stays positive on n≥20, counterfactual was missing something (keep BTC, investigate). If BTC drifts negative on n≥20, counterfactual was correct (drop BTC).
2. **SOL's calibrated 25s delay is validated** against the prior 30s default. Expected improvement: +3.47 bps calibrated vs −2.45 bps default on the same cohort. Realized data should confirm.
3. **Strategic next move is named:** F9 (signal redesign), F8b-tightened (drop BTC, soak more on SOL alone), or "F8b stays in production" (both tickers positive).

This is **a pure analysis task**, identical in structure to F8a-evaluation cycles. Deliverable is `docs/STRATEGY/CC_REPORTS/<date>_f8b-verification-soak.md`. Zero code commits.

## When to run

**On or after 2026-05-04 16:30 UTC** — ~24h after F8b deploy at 2026-05-03 16:28 UTC.

If the operator runs `claude` before 16:30 UTC, Claude Code should still execute the analysis but apply more conservative interpretation thresholds — small n means noise dominates, decisions become less reliable. Specifically: bump the per-ticker minimum from 20 to 30 and report any sub-threshold tickers as "inconclusive — more soak."

## Why now

- F8b deployed 2026-05-03 16:28 UTC: ticker allowlist `{BTC-USD, SOL-USD}` + calibrated delays `{BTC: 5s, SOL: 25s}`.
- The counterfactual on n=69 BTC samples refuted the n=8 realized P/L finding. **One of those two is wrong; we need realized data on the new cohort to find out which.**
- f8a-evaluation-rerun cycles established the convention: realized P/L is the primary lens for verdict-grade strategic questions. The decay-miner per-cell lens remains useful but secondary (and known-noisy per f-hygiene-4's findings).
- Continued soak on the full 5-ticker set produces no new strategic information; the allowlist scope is correct as long as we verify the {BTC, SOL} subset is real.

## Architectural commitments

- **Read-only against `fast_signal_decay` + `fast_alerts` + `fast_exits` + `fast_executions` + `fast_path_status`.** No mutations.
- **No code changes.** One CC report, one doc commit.
- **Use the existing tier system** (verdict-grade ≥ 30, suggestive 10–29, sparse < 10). Don't fabricate verdicts from suggestive cells.
- **Three lenses, in priority order:**
  - **Realized P/L per-ticker** on the post-deploy cohort (PRIMARY). The truth.
  - **Validation-residual at h=1800** post-F-hygiene-4.2 fix, post-F-hygiene-3.1 UPSERT (SECONDARY — measure cleaner now).
  - **Decay-miner mean ± 2σ** at horizons ≥ 5s (TERTIARY — known-noisy per F-hygiene-4 with 30 bps systematic disagreement still pending f-hygiene-5).

## Scope — analysis, not code

### 1. Distinct realized P/L per-ticker, post-deploy cohort

**Critical: filter on `decided_at > '2026-05-03 16:28 UTC'` (or whatever the actual F8b deploy timestamp was — verify from `git log --format=%aI 15e142e`).** This isolates the new cohort.

```sql
WITH pullback_eids AS (
  SELECT e.id FROM fast_executions e
  JOIN fast_alerts a ON a.ticker=e.ticker
                    AND a.alert_type=e.alert_type
                    AND a.fired_at=e.alert_fired_at
  WHERE a.alert_type='volume_breakout_pullback_long'
    AND e.decided_at > '<F8b deploy timestamp>'
)
SELECT e.ticker, COUNT(*) AS exits,
       ROUND(SUM(x.realized_pnl_usd)::numeric, 4) AS pnl,
       COUNT(*) FILTER (WHERE x.realized_pnl_usd > 0) AS wins,
       ROUND((100.0 * COUNT(*) FILTER (WHERE x.realized_pnl_usd > 0)
              / COUNT(*))::numeric, 1) AS win_rate_pct,
       ROUND(AVG(x.realized_return_pct * 100)::numeric, 2) AS avg_ret_bps,
       ROUND(AVG(x.holding_period_s)::numeric, 0) AS avg_hold_s
FROM fast_exits x
JOIN fast_executions e ON e.id = x.entry_execution_id
WHERE x.entry_execution_id IN (SELECT id FROM pullback_eids)
GROUP BY e.ticker ORDER BY exits DESC;
```

Report per-ticker n, mean bps, win rate, avg hold. Compare to F8a-evaluation-rerun-2 baseline:

| Ticker | F8a-rerun-2 actual | F8b counterfactual | This run (post-allowlist) | Verdict |
|---|---|---|---|---|
| BTC-USD | +5.66 bps n=8 | −0.75 bps n=69 | ? | ? |
| SOL-USD | +3.34 bps n=13 | +3.47 bps n=43 | ? | ? |
| ETH-USD | −6.44 bps n=10 | (blocked) | should be 0 (gate-blocked) | ? |
| DOGE-USD | −14.39 bps n=12 | (blocked) | should be 0 (gate-blocked) | ? |

### 2. Allowlist gate efficacy

```sql
-- Per-ticker reject distribution since deploy
SELECT e.ticker, e.reject_reason, COUNT(*) AS n
FROM fast_executions e
WHERE e.alert_type='volume_breakout_pullback_long'
  AND e.decided_at > '<F8b deploy timestamp>'
  AND e.decision='rejected'
GROUP BY 1, 2 ORDER BY 1, n DESC;
```

Expected: ETH/DOGE/AVAX have `pullback_ticker_not_allowed:<ticker>` as the dominant reject reason. BTC/SOL may have other reject reasons (cooldown, capacity, etc.) — those are fine, not the allowlist's job.

If BTC/SOL ever show `pullback_ticker_not_allowed` rejects, that's a bug — the allowlist is rejecting a ticker that should be allowed. Surface as a finding.

### 3. Validation-residual at h=1800, post-F-hygiene-4.2 fix

The C fix landed 2026-05-03 ~15:30 UTC (commit `bc42fb1`). New observations from `_finalize_one_obs` use exit-side price (best_bid for long); pre-fix observations stick. Per-ticker disagreement table:

```sql
SELECT ticker, score_bucket, horizon_s, sample_count,
       realized_validation_count AS val_n,
       ROUND(realized_validation_residual::numeric * 10000, 2) AS resid_bps_abs,
       ROUND(mean_return::numeric * 10000, 3) AS miner_mean_bps
FROM fast_signal_decay
WHERE alert_type='volume_breakout_pullback_long'
  AND realized_validation_count > 0
ORDER BY horizon_s, ticker, score_bucket;
```

Expected: DOGE residuals start dropping toward ~0 bps as new observations accumulate (the C fix corrects the half-spread offset). BTC/ETH/SOL residuals stay elevated until f-hygiene-5 (Hypothesis B / horizon mismatch) lands.

### 4. Decay-miner per-cell snapshot (verdict-grade growth)

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

By this run's window, expect: 1+ verdict-grade cells (DOGE high h=1 was at n=29 in rerun-2). Note that h=1 verdict-grade is structurally not falsifying (fire moment); other horizons matter more.

### 5. SOL calibrated-delay efficacy

Compare SOL realized P/L on the post-deploy cohort (delay=25s) vs prior cohort (delay=30s). The counterfactual showed delay=25s should produce +3.47 bps and delay=30s would produce −2.45 bps on the same data — a ~6 bps swing. Realized P/L should reflect this if the counterfactual model is accurate.

```sql
-- SOL realized P/L pre vs post F8b deploy
SELECT
  CASE WHEN e.decided_at < '<F8b deploy timestamp>' THEN 'pre-F8b (30s)' ELSE 'post-F8b (25s)' END AS era,
  COUNT(*) AS exits,
  ROUND(AVG(x.realized_return_pct * 100)::numeric, 2) AS avg_ret_bps,
  ROUND((100.0 * COUNT(*) FILTER (WHERE x.realized_pnl_usd > 0)
         / COUNT(*))::numeric, 1) AS win_rate_pct
FROM fast_exits x
JOIN fast_executions e ON e.id = x.entry_execution_id
JOIN fast_alerts a ON a.ticker=e.ticker
                  AND a.alert_type=e.alert_type
                  AND a.fired_at=e.alert_fired_at
WHERE a.alert_type='volume_breakout_pullback_long' AND e.ticker='SOL-USD'
GROUP BY era;
```

If post-F8b SOL is materially better than pre-F8b, the calibration is validated. If post-F8b is worse, SOL's optimum is unstable — re-investigate.

### 6. Decay-miner health snapshot

Same checks as prior briefs:
- `obs_scheduled / obs_finalized` ratio.
- `pending_heap` oscillation per `dispatch-decay-heap-trend.ps1 24`.
- `db_errors` should still be 0.
- Watchdog OK heartbeat firing.
- All 5 pairs `streaming`, `last_error=NULL`.

### 7. Decision tree

```
Per-ticker realized P/L on post-deploy cohort:

BTC-USD:
  IF n_exits ≥ 20 AND mean_bps > 0 (clearly above zero):
    -> Counterfactual was wrong; KEEP BTC in allowlist.
       Investigation: identify what the counterfactual missed (Open Q3
       hold-period independence is a candidate).
  ELIF n_exits ≥ 20 AND mean_bps near or below zero:
    -> Counterfactual was correct; DROP BTC from allowlist (next code task).
       Fade hypothesis is NOW supported on SOL only.
  ELIF n_exits < 20:
    -> Inconclusive. Recommend more soak; project ETA from observed rate.

SOL-USD:
  IF n_exits ≥ 30 AND mean_bps clearly positive AND win_rate > 35%:
    -> SOL's edge holds. F8b stays in production.
  ELIF mean_bps drifts negative (mean+2σ < 0):
    -> Even SOL doesn't hold. Pivot to F9 (signal redesign) immediately.
  ELIF still suggestive at n_exits < 30:
    -> More soak.
```

**Combine BTC and SOL outcomes:**
- Both positive: F8b is validated; soak more for live-eligibility decision.
- BTC negative, SOL positive: drop BTC, keep SOL, F9 stays queued for parallel design work.
- BTC positive, SOL negative: surprising; both should be investigated.
- Both negative: F9 immediately, full pivot.

### 8. Write the CC report

`docs/STRATEGY/CC_REPORTS/<date>_f8b-verification-soak.md` follows PROTOCOL.md format. Include:
- Three-eval comparison table (F8a-rerun-2 baseline / F8b counterfactual / this run).
- Per-ticker per-lens verdicts.
- Allowlist gate efficacy (block counts).
- Validation-residual delta on DOGE (the C fix's direct beneficiary; not in F8b's allowlist but tells us the calibration accuracy improvement is real).
- SOL pre-F8b vs post-F8b delta (the 25s vs 30s validation).
- Recommendation for next NEXT_TASK with one-line description.

### 9. Verbatim verification SQL — for next review

Paste all queries used. Pin the deploy timestamp explicitly.

## Brain integration (reuse, don't rewrite)

- F8b's allowlist gate, calibration script, calibration artifact — all in place; don't touch.
- F-hygiene-4.2's miner forward-return fix — directly usable for residual analysis.
- F-hygiene-3.1's UPSERT — validation-count growing structurally.
- F-leak-1.5's integrity-probe pattern — `IN (SELECT id ...)` for distinct counts.
- Prior CC reports' baselines — use for the comparison table.

## Constraints / do not touch

- **No code commits.** One markdown file is the entire deliverable.
- **No threshold tuning.**
- **No live placement enable.**
- **No migrations.**
- **No fast-data-worker restart.**
- **Don't conflate alert types.** Pullback long is its own thing.
- **Per-ticker analysis is mandatory.** Aggregate-only is misleading because the underlying tickers are bimodal.
- **Realized P/L is the primary verdict lens.** Counterfactual was suggestive; realized data is the truth.
- **Don't extrapolate from spikes** — same convention as prior briefs.

## Out of scope

- Code changes to drop BTC from the allowlist (would be the NEXT task if BTC drifts negative).
- F9 signal redesign brief (would be the NEXT task if both drift negative).
- f-hygiene-5 (structural B fix). Can run in parallel; doesn't affect this task.
- f-leak-3. Still conditional on next OOM event.
- Live-eligibility decision. Separate brief with its own approval gate.
- Recalibration of DELAY_S. Manual re-run of `scripts/calibrate-pullback-delay.py` is appropriate after this task verifies the framework.

## Success criteria

1. `git log --oneline -3` shows ONE new commit, pushed: `docs(strategy): F8b verification soak report + mark NEXT_TASK done`. No code commits.
2. CC report includes three-eval comparison table, per-ticker per-lens verdicts, decision-tree outcome.
3. Verbatim verification SQL reproduces the verdict from raw table state.
4. If recommendation is "soak more," includes specific projected re-run time.
5. F8a soak continues uninterrupted on fast-data-worker.

## Open questions for Cowork (surface in your report only if relevant)

1. **If BTC stays positive on n≥20 post-allowlist but counterfactual is uniformly negative**, that's important strategic information: the gate stack's contemporaneous filtering (cooldown / capacity / score thresholds) correlates with positive BTC outcomes. **The signal is real but conditional.** Surface for design review — F9's brief should consider what those gates are filtering for.

2. **If SOL post-F8b realized P/L is materially worse than F8a-rerun-2's prior numbers** (e.g., +1 bps instead of +3 bps), that suggests the 25s calibrated delay is overfit to the counterfactual sample. Worth re-investigating the calibration's hold-period independence assumption (Open Q3 from F8b).

3. **DOGE residual reduction after F-hygiene-4.2's fix** — even though DOGE is gate-blocked, its decay-miner cells continue to update. The new observations should show residuals dropping by ~45 bps (the half-spread DOGE has). If the reduction doesn't materialize, F-hygiene-4.2's fix has a bug worth investigating.

4. **If both BTC and SOL drift negative**, that's the F9 pivot point — full signal redesign, no waiting. Don't soak indefinitely.

5. **If the allowlist gate has any false rejects** (BTC or SOL alerts blocked with `pullback_ticker_not_allowed`), that's a bug. Surface immediately.

## Rollback plan

- N/A. No code changes. The CC report is informational; no production impact.
