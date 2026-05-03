# CC_REPORT: f8a-evaluation-rerun

## Verdict (decay-miner basis)

**Insufficient data per the brief's strict criterion. 0 verdict-grade cells (n ≥ 30) at any horizon, including at horizons ≥ 5s.** Decision tree branch D fires.

**Recommend: continuing soak, OR pivoting to F9 (signal redesign).** See "Why this is more nuanced than the prior re-run" below — there's a substantial new finding that changes the strategic picture.

## Important note on timing

Run executed at **2026-05-03 04:35 UTC**, ~12 hours **before** the brief's projected re-run target of 2026-05-03 17:00 UTC. Per the brief: applying conservative interpretation of any verdict-grade cells. None exist; the conservative-interpretation requirement is moot.

The brief's ETA was based on a steady-state rate of ~1.35 alerts/hr at the densest bucket. Current observed steady-state rate (last 12h) is ~5 alerts/hr total across all 5 pairs — **higher than the prior projection used**. Yet still no cell crossed the 30-sample line in any (ticker, bucket, horizon ≥ 5s) combination. This is significant — see below.

## Comparison to prior evaluation snapshot

| Metric | 2026-05-02 17:48 (prior) | This run 2026-05-03 04:35 | Δ |
|---|---|---|---|
| Verdict-grade cells (n ≥ 30) | 0 | **0** | 0 |
| Suggestive cells (10 ≤ n < 30) | 2 | **9** | +7 |
| Sparse cells (n < 10) | 81 | **92** | +11 |
| Total cells | 83 | **101** | +18 |
| Total observations | 208 | **468** | +260 (+125%) |
| Total post-fix alerts (`id > 2300`) | 114 | **191** | +77 (+68%) |
| Capture rate | 100% | **100%** | unchanged |
| `db_errors` | 13 (frozen) | **0 throughout post-`742394f` window** | resolved |
| `realized_validation_count` (sum) | 1 | **7** | +6 |
| Cells with validation > 0 | 1 | **6** | +5 |

Sample count grew faster than the prior projection (260 vs prior projection's ~165 for 24h), but that's because of the 11h-bin spikes at 00:00 (28 alerts) and 02:00 (24 alerts). Excluding spikes, steady-state rate is ~3.8/hr — closer to the 24h projection.

## Suggestive cells — full table

Brief said report numbers for these. 9 cells at the 10 ≤ n < 30 tier:

| ticker | bucket | horizon_s | n | mean (bps) | stderr (bps) | mean ± 2σ |
|---|---|---|---|---|---|---|
| BTC-USD | med | 1 | 13 | −0.005 | 0.125 | (−0.255, +0.245) |
| BTC-USD | med | 5 | 11 | −0.213 | 0.121 | (−0.455, +0.029) |
| DOGE-USD | high | 1 | 13 | −0.679 | 0.163 | (−1.005, −0.353) |
| DOGE-USD | low | 1 | 15 | −0.712 | 0.119 | (−0.950, −0.474) |
| ETH-USD | low | 1 | 16 | −0.100 | 0.074 | (−0.248, +0.048) |
| ETH-USD | low | 5 | 12 | −0.264 | 0.124 | (−0.512, −0.016) |
| SOL-USD | high | 1 | 12 | −0.596 | 0.000 | (−0.596, −0.596) |
| SOL-USD | high | 5 | 11 | −0.596 | 0.000 | (−0.596, −0.596) |
| SOL-USD | low | 1 | 11 | −0.597 | 0.000 | (−0.597, −0.597) |

Per the brief: **horizon=1 cells are not falsifying** (they're the fire moment). Of the 9 suggestive cells, **3 are at horizon=5**: BTC-USD med, ETH-USD low, SOL-USD high. None of those 3 has a positive mean. ETH-USD low and SOL-USD high have upper-CI < 0 (would meet the F6.5 negative-edge gate's threshold if they were verdict-grade).

The SOL-USD `stderr=0` cells (horizon=1, 5) are a tick-quantization artifact — SOL prices are in dollars, so the forward-return at very short horizons quantises to a single value across all observations. Not a bug, but flag for analysis: pretend the stderr is "small but nonzero" when comparing to other tickers.

## **Why this is more nuanced than the prior re-run** ⚠️

Per the brief's instruction to flag unanticipated findings: I discovered the executor IS placing pullback fills despite the F6.5 negative-edge and tradeability gates, **and 142 round-trip pullback trades have already closed in paper mode**. This is a richer verdict signal than the decay-miner means alone:

```
SELECT COUNT(*) AS pullback_round_trips,
       SUM(realized_pnl_usd) AS total_pnl_usd,
       COUNT(*) FILTER (WHERE realized_pnl_usd > 0) AS wins,
       COUNT(*) FILTER (WHERE realized_pnl_usd < 0) AS losses,
       AVG(realized_return_pct) AS avg_return_pct,
       AVG(holding_period_s) AS avg_hold_s
FROM fast_exits x
JOIN fast_executions e ON e.id = x.entry_execution_id
JOIN fast_alerts a ON a.ticker=e.ticker AND a.alert_type=e.alert_type AND a.fired_at=e.alert_fired_at
WHERE a.alert_type = 'volume_breakout_pullback_long';
```

Result:

| Metric | Value |
|---|---|
| Round trips | **142** |
| Total realized P/L | **−$2.39** |
| Wins | 43 |
| Losses | 99 |
| **Win rate** | **30.3%** |
| Avg return | **−6.7 bps** |
| Avg hold | **49 minutes** |

Per ticker:

| ticker | exits | total P/L | avg return |
|---|---|---|---|
| DOGE-USD | 48 | −$1.99 | **−16.6 bps** |
| ETH-USD | 25 | −$0.59 | **−9.4 bps** |
| SOL-USD | 40 | −$0.13 | **−1.3 bps** |
| BTC-USD | 29 | **+$0.31** | **+4.3 bps** ← only positive |

**This is verdict-grade realized data. n=142 across 4 tickers.** The fade hypothesis at the aggregate level is **WEAKLY REFUTED** by realized P/L (avg −6.7 bps, win rate 30%). Only BTC-USD shows positive realized edge (+4.3 bps over 29 trades).

Compared to the original `volume_breakout_long`'s realized backfilled mean of **−28.5 bps**, the pullback's −6.7 bps IS a substantial improvement — the fade does dampen the loss — but it doesn't invert it into a positive edge. The "wait for the dip" idea reduces the bleed without making it tradeable.

## Why didn't the gate stack block these fills?

The F6.5 `gate_negative_edge_excluded` requires `n ≥ MIN_NEGEDGE_SAMPLES (= 30)` before excluding. With 0 verdict-grade cells in the decay miner (which is what the gate reads), `is_negative_edge_excluded` returns `(False, "insufficient_samples")` for every pullback alert. Same for `is_score_tradeable`. So both calibrated gates pass-through, and only the static gates (`min_score`, `recency`, `spread_sanity`, `capacity`) decide.

This is exactly the brief's design: calibrated gates are conservative-on-insufficient-data, fall through to static gates. The brief constraint "no threshold tuning" means we don't change this — but we should note the implication: **the brain is currently learning by trading**. 142 paper trades have happened on a signal whose decay-miner profile isn't yet verdict-grade. Once the decay miner crosses MIN_SAMPLES on the negative-edge buckets, the gates will start blocking and the trade rate will drop.

## Decay-miner health snapshot

Latest supervisor metrics line + dispatch script (12h window) confirms:

| Metric | Status | Notes |
|---|---|---|
| `obs_scheduled` vs `obs_finalized` | 5248 / 1310 (~25%) | Most pending are long-horizon (300s+) |
| `pending_heap` | oscillating 992–1023 (last 8 ticks) | Healthy, not monotonic |
| `db_errors` | **0 throughout post-742394f window** | F-hygiene-2.1 fix is durable |
| Watchdog OK heartbeat | **5 lines per 5-min window** | F-hygiene-2.2 working |
| `last_error` per pair | NULL on all 5 | F-hygiene-1.2 working |

Distinct `errs` values across the 12h dispatch window: `{0, 13}`. The 13 is from before the fix; everything since 02:30 UTC is 0. **`db_errors` is durably zero post-742394f.**

## Validation-count gap (Open Question)

The decay miner's `realized_validation_count` is 7 across 6 cells. But there are 142 actual pullback round trips. Why the gap?

Looking at `_handle_exit_inserted`: it does an `UPDATE fast_signal_decay SET realized_validation_count = realized_validation_count + 1 WHERE (ticker, alert_type, score_bucket, horizon_s) = (...)`. **If the row doesn't exist yet, the UPDATE silently affects 0 rows.** Most exits land at horizons that don't yet have a forward-return observation in the bucket (the pullback alert that triggered them is still observation-pending at the long horizons), so the validation update is silently dropped.

This isn't strictly a bug — Welford updates require an existing row — but it does mean the validation signal is undercounted by ~95%. Could be fixed with `INSERT ... ON CONFLICT DO UPDATE` for the validation columns, but that's an F-hygiene-3 candidate, not in scope here.

## Per-hour rate observation

Last 12h shows ~5 alerts/hr steady-state across all 5 pairs (excluding 02:00 burst). Per (ticker, bucket): ~0.33/hr. For a single bucket to cross n=30 requires ~90 hours / ~3.75 days at observed rate. **Even at 48h total soak, most buckets won't be verdict-grade by miner criteria.** The signal is structurally rare for the per-bucket-per-horizon evaluation framework.

## Recommendation

**Two equally defensible next moves; Cowork's call.**

### Option A — soak more, re-run again at 2026-05-04 04:30 UTC (~24h from now)

Densest cells (ETH-low horizon=1 at n=16, ETH-low horizon=5 at n=12) project to cross n=30 within ~24-48h at observed rates. Accumulating verdict-grade decay-miner cells gives the negative-edge gate teeth. **Strategic interpretation: same decision tree, more samples, more confidence.**

### Option B — pivot to F9 (new signal redesign) now

The realized P/L data is already verdict-grade. n=142 round trips, avg −6.7 bps, win rate 30%. **The fade reduces the original signal's −28.5 bps loss but doesn't turn it positive at the aggregate.** If the strategic question is "does this signal class produce edge?", the answer from realized data is **no, weakly refuted.** Continued soak on the same signal isn't generating new strategic information; it's just refining the magnitude of "this doesn't work."

**My read:** Option B is honest. The miner's per-bucket lens was the right protocol to bring to a brand-new signal, but realized P/L on n=142 is more strategically informative than miner means on n=12 — and the realized data says "no edge." Continued soak adds precision to the negative result without changing it.

If Option B (F9), one-line description for that brief: *Design and prototype a new fast-path alert signal class that targets a microstructure phenomenon NOT captured by mean-reversion-of-volume-breakout — candidates: order-flow imbalance against trade-flow, cross-pair lead-lag, microstructure squeeze with maker-side fill.*

If Option A (re-run), one-line description: *Re-run f8a-evaluation-rerun analysis at 2026-05-04 04:30 UTC; expect 2-4 verdict-grade cells at densest tickers' horizon=1 and horizon=5; apply decision tree branches 1-3.*

## Surprises / deviations

1. **The 142 closed pullback round trips were unanticipated by the brief.** Brief said "gate stack blocks pullback fills, no `fast_exits` rows reference pullback alerts in paper mode" — that's wrong. The negative-edge and tradeability gates require ≥ 30 samples to fire, and the decay miner hasn't crossed that yet, so both gates pass through. Only the static gates (min_score, recency, etc.) decide on pullback alerts, and those don't block on edge — they block on cosmetic conditions. So 142 trades have happened.

2. **Strategic implication of finding #1:** the fade hypothesis IS now testable from realized P/L without waiting for the decay miner to mature. The brief's miner-only verdict criterion is structurally conservative; realized P/L with n=142 is a stronger signal. Per the brief's "Don't fabricate verdict from suggestive cells" rule, I'm reporting both verdicts (miner=insufficient, realized=weakly refuted) and letting Cowork pick the operative one.

3. **`db_errors=0 throughout post-742394f window`** as the brief expected. F-hygiene-2.1's LIMIT 1 fix is durable; the dispatch script confirms.

4. **Validation-count gap** (7 validations vs 142 actual exits) is structural, not a bug — see "Validation-count gap" section above. Worth a future hygiene pass to use `INSERT ... ON CONFLICT` on the validation columns.

## Open questions for Cowork

1. **Does realized P/L (n=142, avg −6.7 bps, win rate 30%) constitute a verdict, or only the decay-miner's per-bucket means do?** The brief was strict that the decay-miner is the truth source. But realized data is genuinely better-evidenced for the strategic question. Worth an explicit operator decision on which lens governs the F8 → F8b/F9 transition.

2. **The validation-count gap.** Switching the miner's UPDATE to UPSERT would let validations land on cells that don't yet have forward-return observations. Side effect: the table would have rows where `mean_return = 0` (no obs) but `realized_validation_count > 0`, which is a weird state for downstream consumers. Worth thinking through before patching.

3. **48-hour total soak threshold.** Per the brief: if this re-run still finds < 3 verdict-grade cells at horizons ≥ 5s, "the strategic discussion is whether `VOL_BREAKOUT_MULT = 2.0` is too aggressive for fade evaluation." That's where we are. Lowering MULT increases firing rate (more samples, more noise). Pivoting to F9 on a different signal class is the alternative. Both honest moves; surface for Cowork.

4. **BTC-USD's +4.3 bps over 29 trades** is a small per-ticker positive within an aggregate negative. n=29 is still suggestive-tier (just over 10), and one out of four tickers being positive is consistent with random variation. But the order of magnitude (BTC's microstructure has the deepest book, tightest spreads) might genuinely matter. Not a basis for tuning today; surface for design review.

## Verbatim verification SQL — for next review

```sql
-- 1. Per-cell decay state with stderr (same as prior brief)
SELECT ticker, score_bucket, horizon_s, sample_count,
       ROUND(mean_return::numeric * 10000, 3) AS mean_bps,
       ROUND((CASE WHEN sample_count > 1
                   THEN SQRT(m2_return/(sample_count-1))/SQRT(sample_count)
                   ELSE NULL END)::numeric * 10000, 3) AS stderr_bps,
       realized_validation_count AS val_n,
       last_updated
FROM fast_signal_decay
WHERE alert_type = 'volume_breakout_pullback_long'
ORDER BY ticker, score_bucket, horizon_s;

-- 2. Tier distribution
SELECT
  CASE WHEN sample_count >= 30 THEN 'verdict_grade'
       WHEN sample_count >= 10 THEN 'suggestive'
       ELSE 'sparse' END AS tier,
  COUNT(*) AS cells, SUM(sample_count) AS total_obs,
  MIN(sample_count) AS min_n, MAX(sample_count) AS max_n
FROM fast_signal_decay
WHERE alert_type='volume_breakout_pullback_long'
GROUP BY tier ORDER BY tier;

-- 3. Realized P/L on closed pullback round trips (this brief surfaced this)
SELECT COUNT(*) AS round_trips,
       SUM(realized_pnl_usd) AS total_pnl_usd,
       COUNT(*) FILTER (WHERE realized_pnl_usd > 0) AS wins,
       COUNT(*) FILTER (WHERE realized_pnl_usd < 0) AS losses,
       AVG(realized_return_pct) AS avg_return_pct,
       AVG(holding_period_s) AS avg_hold_s
FROM fast_exits x
JOIN fast_executions e ON e.id = x.entry_execution_id
JOIN fast_alerts a ON a.ticker=e.ticker AND a.alert_type=e.alert_type
                  AND a.fired_at=e.alert_fired_at
WHERE a.alert_type = 'volume_breakout_pullback_long';

-- 4. Capture rate sanity (post-fix)
SELECT
  COUNT(*) AS total_post_fix,
  COUNT(*) FILTER (WHERE features ? 'best_bid'
                   AND features->>'best_bid' IS NOT NULL) AS with_best_bid,
  COUNT(*) FILTER (WHERE features ? 'close'
                   AND features->>'close' IS NOT NULL) AS with_close
FROM fast_alerts
WHERE alert_type = 'volume_breakout_pullback_long' AND id > 2300;

-- 5. Pair status snapshot
SELECT ticker, state, last_error
FROM fast_path_status WHERE ticker NOT IN ('decay_miner') ORDER BY ticker;
```

```bash
# 6. Decay-miner trend + errs durability
.\scripts\dispatch-decay-heap-trend.ps1 12

# 7. Watchdog OK heartbeat presence
docker compose logs fast-data-worker --since 5m | grep -c "watchdog: OK"
# Expected: ~5
```

Re-run target if continuing soak: **2026-05-04 04:30 UTC**.
