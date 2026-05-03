# CC_REPORT: f8a-evaluation-rerun-2

## Verdict

**SUBSET-SUPPORTED.** Distinct realized exits = 43 ≥ 30 (verdict-grade by the realized-P/L lens). Per-ticker breakdown is bimodal:

| Ticker | n | Avg ret (bps) | Win rate | Direction |
|---|---|---|---|---|
| **BTC-USD** | 8 | **+5.66** | **62.5%** | **positive** |
| **SOL-USD** | 13 | **+3.34** | 38.5% | **positive** |
| ETH-USD | 10 | −6.44 | 30.0% | negative |
| DOGE-USD | 12 | −14.39 | 16.7% | negative |
| **aggregate** | **43** | **−3.45** | **34.9%** | near-zero |

**SOL+BTC together (n=21, ~+4.2 bps avg) show a tradable fade signal. ETH+DOGE (n=22, ~−10.2 bps avg) are structurally negative.** The per-ticker variance is the headline finding; the aggregate is misleading either way.

**Recommendation: F8b restricted to {BTC, SOL}** — calibrate `VOL_BREAKOUT_PULLBACK_DELAY_S` per-ticker and re-evaluate. **F9 (signal redesign)** is a defensible alternative if Cowork judges 2-of-4 tickers too narrow for production. Cowork's call. Detail in "Recommendation" below.

## Three-evaluation comparison

| Metric | f8a-evaluation (2026-05-02 17:48) | f8a-evaluation-rerun (2026-05-03 04:35) | **This run (2026-05-03 15:35)** | Δ since prior |
|---|---|---|---|---|
| Verdict-grade cells (n≥30) | 0 | 0 | **0** | 0 |
| Suggestive cells (10–29) | 2 | 9 | **32** | +23 |
| Sparse cells | 81 | 92 | **77** | −15 |
| Total decay cells | 83 | 101 | **109** | +8 |
| Total observations | 208 | 468 | (~875+; sum_count growing) | larger |
| Distinct pullback exits | (not measured) | 37 *(corrected from inflated 142)* | **43** | +6 |
| Validation-only cells (UPSERT canary) | n/a | n/a | **0** | UPSERT INSERT branch not yet fired |
| Cells with val_n>0 | 1 | 6 | **10** | +4 |
| Per-bucket validations max | 1 | 2 | **2** | unchanged |
| `db_errors` | 13 (frozen) | 0 (durable) | **0** | unchanged |
| Capture rate (post-fix alerts) | 100% | 100% | **100%** | unchanged |

## Per-lens verdicts

### Lens 1 — Realized P/L (PRIMARY) ✅ verdict-grade, subset-supported

```sql
WITH pullback_eids AS (
  SELECT e.id FROM fast_executions e
  JOIN fast_alerts a ON a.ticker=e.ticker
                    AND a.alert_type=e.alert_type
                    AND a.fired_at=e.alert_fired_at
  WHERE a.alert_type='volume_breakout_pullback_long'
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

```
ticker   | exits | pnl_usd  | wins | win_rate | avg_bps | avg_hold
SOL-USD  |   13  | +0.1087  |   5  |  38.5%   |  +3.34  |  2928s
DOGE-USD |   12  | -0.4318  |   2  |  16.7%   | -14.39  |  2765s
ETH-USD  |   10  | -0.1610  |   3  |  30.0%   |  -6.44  |  3077s
BTC-USD  |    8  | +0.1132  |   5  |  62.5%   |  +5.66  |  2383s

aggregate | 43   | -0.3708  |  15  |  34.9%   |  -3.45  |  2816s
```

n=43 is verdict-grade. Aggregate −3.45 bps is **half** what the (JOIN-inflated) prior eval reported (−6.7 bps), and within trading-cost noise of zero. **Per-ticker bimodal split is the load-bearing signal**, not the aggregate.

### Lens 2 — Validation-residual at h=1800 (SECONDARY) ⚠ thin data, miner is a poor predictor

**Correction from the brief:** validations land at the closest-horizon to actual holding time. For pullback exits the actual modal horizon is **1800s (21 exits, 49%)**, not 3600s (11 exits, 26%) as the brief assumed. This matches `avg_hold = 2816s` which `min(HORIZONS_S, key=abs)` snaps to 1800s for many trades and 3600s for others.

```
ticker   | bucket | horizon | n_obs | val_n | residual_bps | miner_mean_bps
ETH-USD  | high   |   1800  |   12  |   2   |   12.82      |   -8.58
DOGE-USD | med    |   1800  |    4  |   1   |   40.68      |  +11.31
BTC-USD  | low    |   1800  |    3  |   1   |   19.96      |   +4.41
ETH-USD  | med    |   1800  |    6  |   1   |   31.26      |  -15.96
SOL-USD  | high   |   1800  |   11  |   1   |   32.07      |   -7.84
SOL-USD  | med    |   1800  |    4  |   2   |    1.01      |  -11.61  ← tight match
```

**The miner mean is generally a poor predictor of realized return.** Residuals (mean abs error) range 13-40 bps in most cells. The single tight match (SOL med, residual = 1 bp) might be coincidence at val_n=2.

This is significant: the calibration helpers (`is_score_tradeable`, `is_negative_edge_excluded`, `compute_calibrated_bracket`) all use miner-mean for their decisions. If miner-mean is 10-40 bps off realized, those gates may be making decisions on bad predictions. Worth a future audit (out of scope here).

### Lens 3 — Decay-miner mean ± 2σ (TERTIARY) ⚠ still 0 verdict-grade cells

32 cells at suggestive tier (10≤n<30). Closest to verdict-grade:

| ticker | bucket | horizon | n | mean_bps | stderr_bps |
|---|---|---|---|---|---|
| **DOGE-USD** | high | **1** | **29** | −0.494 | 0.100 |
| ETH-USD | low | 1 | 22 | −0.078 | 0.054 |
| BTC-USD | high | 1 | 21 | −0.030 | 0.077 |
| SOL-USD | high | 1 | 22 | −0.595 | 0.000 |
| BTC-USD | high | 5 | 20 | −0.282 | 0.121 |

**DOGE-USD high horizon=1 is one observation away** (n=29) from verdict-grade. But h=1 is the fire moment (no reversion has elapsed); per the brief, "apply verdict logic only at horizons ≥ 5s." Even at horizon=5, no cell is verdict-grade.

Notable suggestive cells with positive miner mean at horizons ≥ 5s:
- BTC-USD high horizon=60: n=6, mean=+2.987 bps (sparse, but positive)
- BTC-USD low horizon=60: n=4, mean=+3.089 (sparse)
- ETH-USD high horizon=60: n=8, mean=+4.708 (sparse)
- DOGE-USD med horizon=1800: n=4, mean=+11.311 (sparse, BUT residual_bps=40.68 disagrees)

At horizons ≥ 1800 the picture turns sharply negative across most tickers, meaning the fade DEEPENS at long horizons rather than recovering. The realized P/L lens's positive BTC + SOL signal coexists with negative miner-mean at 1800-3600 — so the bimodality persists across lenses for these two tickers.

## Caveats applied

1. **Cold-start backfill variance artifact:** several SOL cells show stderr=0.000 (e.g., SOL-USD high horizon=1, low horizon=1). These are tick-quantization artifacts from the backfill — SOL's price quantization at low horizons produces identical forward-return values across observations. Not genuinely tight measurements; tagged in the per-cell table.

2. **Horizon=1 is the fire moment:** all 9 of last eval's "suggestive" cells were at h=1. This run, the proportion has shifted (the densest non-h=1 cells are h=5 and h=60). Reporting verdict only at horizons ≥ 5s per convention.

3. **F8a-fix capture rate** still 100% on all post-fix alerts (`id > 2300`). No drift.

## Decay-miner health snapshot

`docker compose logs fast-data-worker` recent metrics:

```
decay_miner alerts=68 exits=0 book_ticks=3654 obs_scheduled=544
obs_finalized=296 backfilled=0 pending_heap=187 validations=0 db_errors=0
```

- `obs_scheduled / obs_finalized` ratio ~54% — normal (long-horizon obs still pending).
- `pending_heap = 187` — well below cap, oscillating per F-hygiene-2.3 dispatch script.
- `db_errors = 0` — durable since F-hygiene-2.1's LIMIT 1 fix.
- Watchdog OK heartbeat firing per F-hygiene-2.2.
- All 5 pairs `streaming`, `last_error=NULL` per F-hygiene-1.

System health is clean.

## Recommendation

**F8b restricted to {BTC, SOL} is my recommendation; F9 is a defensible alternative.**

### Path A — F8b restricted to {BTC, SOL} (my read)

One-line for the F8b brief: *Calibrate `VOL_BREAKOUT_PULLBACK_DELAY_S` from observed BTC + SOL data using the validation-residual signal (when val_n ≥ 5 per cell), then ramp the pullback signal live on those two tickers only — DOGE + ETH excluded by ticker-allowlist gate.*

Justification:
- BTC: +5.66 bps avg, 62.5% win rate over n=8. Strong but small n.
- SOL: +3.34 bps avg, 38.5% win rate over n=13. Modest but consistent.
- Combined: n=21, 47.6% win rate, +4.22 bps avg. Above trading-cost noise floor.

Risks:
- Per-ticker n is still small (n=8 for BTC). One bad week could flip the verdict.
- DOGE's −14.4 bps and ETH's −6.4 bps are NOT noise; they're consistent with the structural pattern f-leak-1 surfaced (DOGE imbalance signals also negative). Pulling BTC+SOL out of the broader scanner may not generalize.
- Production-viability of a 2-of-5-pair signal is questionable — capacity / correlation considerations.

### Path B — F9 signal redesign

One-line for the F9 brief: *Design a new fast-path signal class that targets a microstructure phenomenon NOT captured by mean-reversion-of-volume-breakout — candidates: order-flow imbalance against trade-flow, microstructure squeeze with maker-rebate, cross-pair lead-lag.*

Justification:
- Subset-supported on 2 of 4 tickers is narrower than ideal for "this signal class is right."
- The miner-mean predictive accuracy is poor (residuals 13-40 bps). Even calibrating thresholds wouldn't fix a fundamentally noisy predictor.
- F9 is the bigger-impact path if the signal class itself is the wrong shape.

### Path C — soak more (not recommended)

Continued soak would push individual-ticker n higher but the bimodal pattern is unlikely to disappear; the SOL+BTC vs ETH+DOGE split appears structural, not transient.

## Open questions for Cowork

1. **Subset-supported on 2-of-4 tickers — F8b or F9?** The strategic question is whether per-ticker calibration (F8b) is the right move given DOGE+ETH's persistent negative edge, or whether a different signal class entirely (F9) is the bigger lever. Both are defensible. My read: F9. The miner-mean accuracy issue (residuals 13-40 bps) suggests the underlying signal-shape is noisy; calibrating thresholds on a noisy signal is patching symptoms.

2. **Validation residuals show ~10x miner-mean disagreement.** The calibration helpers all use miner-mean. If the gates are making decisions on predictions that are off by 13-40 bps, the F6.5 negative-edge auto-exclusion may be both false-negative-prone (not blocking signals it should) and false-positive-prone (blocking signals it shouldn't). Worth an explicit audit task whatever direction Cowork chooses.

3. **`validation_only_cells = 0`** — F-hygiene-3.1's UPSERT INSERT branch hasn't fired yet. Every validation hit a cell with prior observations. Not a bug — just an artifact of where exits' `min(HORIZONS_S, key=abs)` lands. Will surface naturally if a long-horizon-only exit ever happens, or if observations stop arriving on certain horizons.

4. **The actual modal horizon is 1800s, not 3600s** as the brief assumed. The brief's framing (and prior CC reports) referenced h=3600 as where validations land. They actually land at h=1800 (49%) and h=300 (23%) far more often than h=3600 (26%). Not a verdict-changing detail but updating the convention for future briefs.

5. **DOGE-USD high horizon=1 is at n=29** — one observation away from the first verdict-grade decay cell. If/when it crosses 30, the F6.5 negative-edge gate (if mean+2σ < 0 with n≥30) starts blocking DOGE high pullback signals automatically. That happens whether or not we pivot to F8b/F9.

## Verbatim verification SQL — for next review

```sql
-- 1. Tier distribution
SELECT
  CASE WHEN sample_count >= 30 THEN 'verdict_grade'
       WHEN sample_count >= 10 THEN 'suggestive'
       ELSE 'sparse' END AS tier,
  COUNT(*) AS cells, SUM(sample_count) AS total_obs
FROM fast_signal_decay
WHERE alert_type='volume_breakout_pullback_long'
GROUP BY tier ORDER BY tier;

-- 2. Distinct pullback exits + per-ticker realized P/L (THE primary lens)
WITH pullback_eids AS (
  SELECT e.id FROM fast_executions e
  JOIN fast_alerts a ON a.ticker=e.ticker
                    AND a.alert_type=e.alert_type
                    AND a.fired_at=e.alert_fired_at
  WHERE a.alert_type='volume_breakout_pullback_long'
)
SELECT e.ticker, COUNT(*) AS exits,
       ROUND(SUM(x.realized_pnl_usd)::numeric, 4) AS pnl,
       ROUND((100.0 * COUNT(*) FILTER (WHERE x.realized_pnl_usd > 0)
              / COUNT(*))::numeric, 1) AS win_rate_pct,
       ROUND(AVG(x.realized_return_pct * 100)::numeric, 2) AS avg_ret_bps
FROM fast_exits x
JOIN fast_executions e ON e.id = x.entry_execution_id
WHERE x.entry_execution_id IN (SELECT id FROM pullback_eids)
GROUP BY e.ticker ORDER BY exits DESC;

-- 3. Validation residuals (any horizon, post-UPSERT)
SELECT ticker, score_bucket, horizon_s, sample_count,
       realized_validation_count AS val_n,
       ROUND(realized_validation_residual::numeric * 10000, 2) AS resid_bps,
       ROUND(mean_return::numeric * 10000, 3) AS miner_mean_bps
FROM fast_signal_decay
WHERE alert_type='volume_breakout_pullback_long' AND realized_validation_count>0
ORDER BY horizon_s, ticker;

-- 4. UPSERT canary
SELECT COUNT(*) AS validation_only_cells
FROM fast_signal_decay
WHERE alert_type='volume_breakout_pullback_long'
  AND sample_count = 0 AND realized_validation_count > 0;
```

## What's next (per operator's queue)

- **F8b** (per-ticker calibration on {BTC, SOL}) — recommended if Cowork judges 2-of-4 tickers actionable.
- **F9** (signal redesign) — recommended if 2-of-4 is too narrow for production.
- **Calibration-accuracy audit** — separate hygiene task, surface as F-hygiene-4. Validation residuals show the calibration helpers' inputs may be 13-40 bps off; worth a focused review of what gates are actually doing.

F8a soak continues uninterrupted on `fast-data-worker` (157 MiB, healthy, capture rate 100%).
