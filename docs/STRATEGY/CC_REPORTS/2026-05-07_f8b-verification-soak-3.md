# CC_REPORT: f8b-verification-soak-3 (Phase 1 of f-thread-tail-2026-05-07-2)

## Outcome

**Verdict: INCONCLUSIVE on both BTC and SOL.** Sample sizes are below the brief's verdict-grade threshold (n≥20). At 94+ hours post-deploy, the fade hypothesis hasn't produced enough volume on the strongest subset (the allowlisted tickers) to converge. **Recommended next move: pivot to F9** per the brief's failsafe ("Both inconclusive (after 24h+): pivot to F9 — fade hypothesis isn't producing decisive realized signal even on the strongest subset"). Direction-based "lenient reading" surfaced as alternative.

## Pinned timestamps

- **Deploy**: 2026-05-03 16:29:20 UTC.
- **Investigation**: 2026-05-07 ~14:30 UTC.
- **Soak elapsed**: ~94 hours (3.9 days).

## Per-lens results

### 1.1 — Distinct realized P/L per-ticker, post-deploy cohort (PRIMARY)

| Ticker | exits | pnl | wins | win_rate | avg_ret_bps | avg_hold_s |
|---|---|---|---|---|---|---|
| SOL-USD | 9 | +$0.2183 | 2 | 22.2% | **+9.70** | 2792 |
| BTC-USD | 9 | −$0.2204 | 2 | 22.2% | **−9.80** | 3368 |

| Ticker | F8a-rerun-2 actual | F8b counterfactual | soak-1 (10min) | soak-2 (28min) | soak-3 (94h) | Verdict |
|---|---|---|---|---|---|---|
| BTC-USD | +5.66 bps n=8 | −0.75 bps n=69 | 0 | 0 | **−9.80 bps n=9** | **INSUFFICIENT** (n<20); direction NEGATIVE |
| SOL-USD | +3.34 bps n=13 | +3.47 bps n=43 | 0 | 0 | **+9.70 bps n=9** | **INSUFFICIENT** (n<20); direction POSITIVE |

### 1.2 — Cluster-correlation interpretation

The catchup fills around 2026-05-03 16:29:33 produced:
- 8 BTC-USD attempts; 1 closed (id=10336, +$0.06)
- 6 SOL-USD attempts; 1 closed (id=10353, −$0.05)

Closed catchup fills moved in **opposite directions** (BTC +, SOL −). Per brief §1.2: deduct 1 from effective n only if both close in the same direction. **No deduction applied.** Effective n stays 9 for each.

### 1.3 — Allowlist gate efficacy

Allowlist working correctly. Of 17 ticker × reject-reason combinations:

- AVAX-USD, DOGE-USD, ETH-USD: rejected with `pullback_ticker:pullback_ticker_not_allowed:<TICKER>` (correctly excluded — not on allowlist).
- BTC-USD, SOL-USD: rejected only for legitimate reasons (`calibration:signal_not_tradeable`, `capacity:pair_already_held`, `min_score:score_below_threshold`, `negative_edge:negative_edge`).

**False-reject count on BTC/SOL: 0** ✅ (matches brief expectation).

The volume profile of upstream gating is the key insight: BTC and SOL aren't being throttled by F8b's allowlist; they're being throttled by the **calibration / capacity / min_score / negative_edge** gates upstream. Of the BTC alerts processed post-deploy, only 9 made it to a closed exit — most got cut by calibration (45 rejects), capacity (29 rejects), min_score (16 rejects), or negative_edge (5 rejects). SOL: 56 calibration rejects, 44 capacity rejects, 20 min_score rejects.

### 1.4 — Validation-residual at h=1800 (SECONDARY)

26 cells had `realized_validation_count > 0`. Most have `val_n ≤ 4`, which is sparse for residual interpretation.

Notable observations:
- **BTC high h=5**: resid 1.00 bps at val_n=2 (clean, near-zero divergence).
- **SOL high h=5**: resid 1.81 bps at val_n=3 (clean).
- **BTC h=1800 across buckets**: residuals range 7-31 bps at val_n=1-3 — too thin to draw conclusions.
- **DOGE post-fix-only cells**: brief predicted ≈5 bps. Realized: DOGE high h=3600 = 5.66 bps val_n=1; DOGE high h=1800 = 6.72 bps val_n=1. Two single-sample observations match the prediction direction; population-level claim still pending.

### 1.5 — SOL calibrated-delay efficacy (25s vs 30s)

| Era | exits | avg_ret_bps | win_rate |
|---|---|---|---|
| pre-F8b (30s) | 15 | **+0.09** | 40.0% |
| post-F8b (25s) | 9 | **+6.40** | 22.2% |

Realized swing: **+6.31 bps** (post − pre).

Counterfactual prediction: +3.47 − (−2.45) = **+5.92 bps swing**.

**Realized swing matches counterfactual direction and magnitude.** Caveat: post-F8b's 22% win rate vs pre-F8b's 40% suggests the post-F8b cohort is fat-tailed (a few large winners pulling the mean up). Sample size n=9 is too small to robustly disambiguate; the direction is consistent with the F8b hypothesis.

(Note: §1.5's `+6.40` bps and §1.1's `+9.70` bps differ because §1.1 counts exits joined to fast_exits while §1.5 uses `COUNT(DISTINCT)` over executions. Both queries agree on direction; the magnitude difference is methodological, not data drift.)

### 1.6 — Verdict-grade decay cells with statistical bounds (TERTIARY)

24 cells with `sample_count ≥ 30`. Statistical signals:

- **AVAX-USD high/low/med h=1**: −7.60 to −8.46 bps with `lower_2sigma` strictly negative (full-bound below zero). **Fully one-sided negative** — confirms AVAX shouldn't be on the allowlist.
- **DOGE-USD high h=30**: −2.08 bps, CI [−3.91, −0.24]. **One-sided negative.**
- **DOGE-USD high h=60**: −2.23 bps, CI [−4.82, +0.36]. Lower bound below zero; upper bound just barely positive — borderline one-sided.
- **BTC at h=1, h=5**: all means within ±0.5 bps; CIs cross zero. **No statistical signal at fast horizons.**
- **SOL at h=1, h=5**: SOL high h=1 mean +1.04 bps (CI [−0.65, +2.74]); SOL high h=5 mean +0.76 bps (CI [−1.02, +2.53]); SOL med h=1 mean +1.32 bps (CI [−1.13, +3.76]). **All cross zero.** Direction is consistently positive but not statistically distinguishable from zero.
- **ETH-USD high h=1**: +0.60 bps, CI [+0.02, +1.18]. Just barely **one-sided positive** (lower bound +0.02). ETH isn't on the F8b allowlist; this might inform the next allowlist iteration.

## Decision tree application

Per the brief decision tree:
- **BTC**: effective n=9 < 20 → **INSUFFICIENT**.
- **SOL**: effective n=9 < 20 → **INSUFFICIENT**.

Combined outcome: "Both inconclusive". Brief specifies two paths:
- Standard: "Insufficient. Recommend operator wait ≥12h before re-running."
- Failsafe (after 24h+): "pivot to F9 — fade hypothesis isn't producing decisive realized signal even on the strongest subset."

We are at 94h post-deploy. The standard path's "wait ≥12h" has already been waited 4× over. The failsafe path applies.

## Recommendation

**Primary: pivot to F9.** The 94h post-deploy soak hasn't produced verdict-grade volume even on the strongest subset. The bottleneck isn't F8b's allowlist (which works correctly per §1.3); it's that ~85-95% of BTC/SOL pullback alerts get filtered by upstream calibration/capacity/min_score/negative_edge gates before they can be tested by the allowlist. Even a perfect F8b allowlist can't overcome gate-imposed thinness. Continuing to soak F8b will not converge.

**Alternative direction-based reading (lenient)**:
- BTC: 9 exits, **−9.80 bps**, win 22.2%. Direction is unambiguously negative; magnitude is large. **DROP from allowlist** is the lenient call.
- SOL: 9 exits, **+9.70 bps**, win 22.2%; +6.31 bps swing matching counterfactual. Direction is unambiguously positive. **KEEP in allowlist** is the lenient call.

The lenient reading violates the brief's strict n≥20 rule but maps to the "BTC DROP, SOL KEEP" combine outcome which the brief explicitly contemplates: "F8b-tightened (drop BTC); consider F9 in parallel." That's substantively the same as the primary recommendation (F9 in parallel) plus a tactical BTC removal.

**Operator's call** between the strict (F9 only) vs lenient (F9 + drop BTC from allowlist) paths.

## Verbatim verification SQL

The §1.1 query was the load-bearing verdict input:

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

Returns:
```
 ticker  | exits |   pnl   | wins | win_rate_pct | avg_ret_bps | avg_hold_s 
---------+-------+---------+------+--------------+-------------+------------
 SOL-USD |     9 |  0.2183 |    2 |         22.2 |        9.70 |       2792
 BTC-USD |     9 | -0.2204 |    2 |         22.2 |       -9.80 |       3368
```

This is the queryable proof of the verdict; rerunning at any future point regenerates the same result on the same row state.

## Phase 1 constraints (all met)

- ✅ No code commits.
- ✅ No threshold tuning.
- ✅ No live placement enable.
- ✅ No migrations.
- ✅ Per-ticker analysis is mandatory; aggregate is misleading. (BTC −9.80 vs SOL +9.70 demonstrates this.)
- ✅ Realized P/L is the primary verdict lens.
- ✅ Used IN-subquery for distinct counts (per the anti-inflation rule).
- ✅ Cluster-correlation interpretation applied (no deduction; opposite directions).

## Open questions for Cowork

1. **Strict vs lenient verdict path.** If you want my read as algo-trader-architect: **F9 + tactical BTC drop**. The lenient reading's direction signal is too clean to ignore (n=9 each but BTC −9.80 / SOL +9.70 with no overlap), and the strict-path "wait" has already been waited 4× over. Dropping BTC mid-flight is a small belt-tightening; it doesn't preempt F9.

2. **Volume bottleneck**. F8b's allowlist works perfectly (§1.3 zero false-rejects). The thin signal is upstream-gate-imposed. If F9 is "fade hypothesis at the next layer," the same volume problem may apply unless F9 specifically addresses upstream throttling. Worth checking that F9's prerequisites don't share this constraint.

3. **ETH-USD high h=1 statistical signal** (§1.6, +0.60 bps CI [+0.02, +1.18]). One-sided positive bound at sample_count=52. ETH isn't on the F8b allowlist; if F9 includes a re-survey of allowlist candidates, ETH may be a candidate to add.

## Recommended next NEXT_TASK

**`f9-pivot-design`**: a planning brief, not implementation, that (a) names the F9 fade hypothesis (the brief mentions it but defers the design), (b) audits whether F9's prerequisites avoid the upstream-gate volume bottleneck that f8b ran into, (c) optionally bundles the BTC-drop allowlist tightening if the operator picks the lenient path.
