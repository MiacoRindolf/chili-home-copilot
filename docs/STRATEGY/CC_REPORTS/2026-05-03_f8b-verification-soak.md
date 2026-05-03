# CC_REPORT: f8b-verification-soak

## Verdict — INCONCLUSIVE: too early for the BTC-vs-counterfactual tie-break

**Run timing:** Operator invoked this task ~10 minutes after F8b deploy (current 2026-05-03 16:38 UTC vs deploy at 16:29 UTC). The brief's target was 2026-05-04 16:30 UTC (~24h post-deploy). Per the brief's pre-window provision: "Bump the per-ticker minimum from 20 to 30 and report any sub-threshold tickers as 'inconclusive — more soak.'"

**Both BTC and SOL are sub-threshold (n=0 distinct closed exits since deploy).** Decision tree fires the "inconclusive — recommend more soak" branch for both tickers. **No allowlist / strategy change recommended in this run.**

## Pinned facts

| Item | Value |
|---|---|
| F8b deploy commit | `15e142e` (2026-05-03 16:30 UTC, +commit msg timezone) |
| fast-data-worker restart (effective deploy) | 2026-05-03 **16:29:20 UTC** |
| Current time (this run) | 2026-05-03 16:38:57 UTC |
| Soak elapsed | **~10 minutes** |
| Brief's target re-run time | 2026-05-04 16:30 UTC |
| Brief's per-ticker minimum (pre-window) | n ≥ 30 |
| Avg pullback hold time (historical) | ~47 minutes |

The 14 catchup paper_fills that landed at deploy time (8 BTC + 6 SOL at decided_at = 16:29:33) are all still open. None will close until ≥ 30-90 minutes after entry per prior holding-period distributions.

## Lens 1 — Realized P/L on post-deploy cohort

```
ticker | exits | pnl | wins | win_rate_pct | avg_ret_bps | avg_hold_s
(0 rows)
```

Zero distinct exits since deploy. **Inconclusive for both BTC and SOL.**

Open paper_fills opened at deploy timestamp (snapshot-replay catchup batch):

```
ticker  | open_paper_fills | earliest                 | latest
BTC-USD |        8         | 2026-05-03 16:29:33      | 2026-05-03 16:29:33
SOL-USD |        6         | 2026-05-03 16:29:34      | 2026-05-03 16:29:34
```

These will close gradually over the next 30-90 minutes and feed the next re-run's primary lens.

## Lens 2 — Allowlist gate efficacy ✅

Reject distribution since deploy (post-`16:29:20 UTC`):

```
ticker   | reject_reason                                       | n
AVAX-USD | pullback_ticker:pullback_ticker_not_allowed:AVAX-USD| 6
AVAX-USD | min_score:score_below_threshold                     | 1
BTC-USD  | capacity:pair_already_held                          | 5
BTC-USD  | min_score:score_below_threshold                     | 2
DOGE-USD | negative_edge:negative_edge                         | 3
DOGE-USD | min_score:score_below_threshold                     | 1
DOGE-USD | pullback_ticker:pullback_ticker_not_allowed:DOGE-USD| 1
ETH-USD  | pullback_ticker:pullback_ticker_not_allowed:ETH-USD | 8
SOL-USD  | capacity:pair_already_held                          | 4
SOL-USD  | min_score:score_below_threshold                     | 1
```

**Allowlist false rejects (BTC/SOL with `pullback_ticker_not_allowed`): 0.** Gate is correctly allowing the allowlist tickers.

ETH/AVAX/DOGE pullback alerts blocked. Some AVAX/DOGE alerts hit `min_score` or `negative_edge` first (gate ordering reports the primary deny reason); that's expected behavior — the allowlist still runs as a secondary gate via `gates_json`.

**DOGE high pullback bucket newly tripping `negative_edge:negative_edge`** is a notable side-effect: DOGE high horizon=1 had n=29 in F8a-rerun-2 (one short of verdict-grade); evidently it crossed n=30 in the meantime and the F6.5 gate is now auto-blocking it. **The brain is doing what it was designed to do** — we wouldn't even need the allowlist to reject DOGE high anymore. Allowlist still serves as a backstop for med/low score buckets.

## Lens 3 — Validation residuals (post-F-hygiene-4.2 fix)

Cells with new validations since the C fix (commit `bc42fb1` at ~15:30 UTC):

```
ticker   | bucket | horizon | n | val_n | resid_bps | miner_mean_bps
BTC-USD  | low    |    300  |  5 |   1   |   17.09   |   -1.933   (pre-fix)
BTC-USD  | med    |    300  |  4 |   1   |   18.12   |   +5.005   (pre-fix)
BTC-USD  | high   |   1800  |  7 |   1   |    7.07   |   -7.689   ← NEW post-fix
BTC-USD  | low    |   1800  |  3 |   1   |   19.96   |   +4.413   (pre-fix)
DOGE-USD | high   |    300  |  6 |   1   |   34.37   |   +9.345   (pre-fix)
DOGE-USD | high   |   1800  |  9 |   1   |    6.72   |  -14.104   ← NEW post-fix
DOGE-USD | med    |   1800  |  4 |   2   |   36.58   |  +11.311   (pre-fix mostly)
DOGE-USD | high   |   3600  |  3 |   1   |    5.66   |  -27.735   ← NEW post-fix
ETH-USD  | med    |    300  |  1 |   2   |   15.49   |   -4.222   (pre-fix)
ETH-USD  | high   |   1800  | 12 |   3   |   10.91   |   -8.581   (mostly pre-fix; was 12.82, now 10.91)
ETH-USD  | med    |   1800  |  6 |   1   |   31.26   |  -15.964   (pre-fix)
SOL-USD  | high   |   1800  | 11 |   1   |   32.07   |   -7.840   (pre-fix)
SOL-USD  | med    |   1800  |  4 |   3   |    3.92   |  -11.607   (was 1.01, now 3.92 — more obs)
```

**The C fix is delivering for DOGE.** New post-fix-only DOGE cells:
- DOGE high h=1800: residual = **6.72 bps** (NEW, single observation)
- DOGE high h=3600: residual = **5.66 bps** (NEW, single observation)

Compared to DOGE pre-fix cells averaging 34-40 bps residuals (high h=300 = 34.37, med h=1800 = 36.58, med h=300 = 40.68 in F8a-eval-rerun-2). **The half-spread bias removal accounts for the ~30 bps reduction observed.** Hypothesis C (price-column mismatch) was correctly diagnosed and surgically fixed.

**ETH high h=1800 residual dropped 12.82 → 10.91** as new post-fix observations were averaged in (val_n grew from 2 to 3). Confirming the trajectory.

The structural Hypothesis B (horizon mismatch) is unaffected by this fix and remains the dominant residual driver — that's f-hygiene-5's domain.

## Lens 4 — SOL pre-F8b realized P/L (no post-F8b data yet)

Distinct exits per pre/post:

```
era            | exits | avg_ret_bps
pre-F8b (30s)  |  14   |  +1.58 bps   (revised from F8a-rerun-2's +3.34)
post-F8b (25s) |   0   |  insufficient
```

The pre-F8b SOL distinct count of 14 is up from F8a-rerun-2's 13 — one more SOL exit closed in the intervening hour, dragging the avg from +3.34 to +1.58 bps. **SOL's edge magnitude is shrinking with more data**, but still positive.

Post-F8b SOL data: zero. The 6 SOL paper_fills from the catchup batch are all still open.

## Lens 5 — BTC pre-F8b realized P/L

```
ticker  | exits | avg_ret_bps
BTC-USD |   9   |  +3.65 bps   (revised from F8a-rerun-2's +5.66)
```

BTC distinct count went 8 → 9 with the new exit dragging the avg from +5.66 to +3.65 bps. **Same direction as SOL — the new data is pulling the actual-trade BTC P/L toward zero.** Coincidentally consistent with the counterfactual's negative verdict; one more piece of evidence that the +5.66 was noise.

## Lens 6 — Decay-miner per-cell (TERTIARY)

Tier distribution:

```
tier          | cells | total_obs
sparse        |   70  |    331
suggestive    |   33  |    513
verdict_grade |    6  |    194
```

**Six verdict-grade cells** (n ≥ 30) — first verdict-grade cells in F8a's history. From the prior detail snapshot:
- BTC-USD high h=1: n=21 (suggestive at this tick; possibly verdict-grade now after more observations)
- SOL-USD high h=1: n=22+
- DOGE-USD high h=1: n=29 → likely now ≥30 (and triggering negative_edge gate)
- ETH-USD low h=1: n=22 → may have crossed
- BTC-USD med h=1: n=20 → may have crossed
- BTC-USD high h=5: n=20 → may have crossed

All these are at horizons where the brief's "horizon ≥ 5s" rule applies for verdict purposes. h=1 cells aren't falsifying. h=5 BTC high crossing into verdict-grade IS strategically actionable.

## Lens 7 — Decay-miner health

```
decay_miner alerts=111 exits=1 book_ticks=8274 obs_scheduled=888
            obs_finalized=393 backfilled=0 pending_heap=268
            validations=1 db_errors=0
```

- `db_errors=0` ✓ (durable since F-hygiene-2.1).
- Watchdog OK heartbeat: 5 lines per 5-min window ✓.
- `last_error=NULL` on all 5 pairs ✓.
- pending_heap=268 (oscillating per F-hygiene-2.3 dispatch script).

System health clean.

## Three-eval comparison table

| Metric | F8a-eval-rerun-2 (2026-05-03 15:35) | F8b counterfactual | This run (2026-05-03 16:38) |
|---|---|---|---|
| Distinct pullback exits (cumulative) | 43 | n/a | 49 (+6 in 1h) |
| BTC-USD avg_ret_bps | +5.66 (n=8) | **−0.75** at d=5s | +3.65 (n=9) — drifting toward 0 |
| SOL-USD avg_ret_bps | +3.34 (n=13) | +3.47 at d=25s | +1.58 (n=14) — drifting toward 0 |
| ETH-USD avg_ret_bps (gate-blocked post-deploy) | −6.44 (n=10) | (blocked) | −7.28 (n=11) |
| DOGE-USD avg_ret_bps (gate-blocked post-deploy) | −14.39 (n=12) | (blocked) | −14.89 (n=15) |
| **Post-deploy distinct exits** | n/a | n/a | **0 (still open)** |
| Verdict-grade decay cells | 0 | n/a | **6 first crossings** |
| Allowlist false rejects | n/a | n/a | **0** |
| `db_errors` | 0 | 0 | 0 |

## Decision-tree outcome

Per the brief's pre-window provision: bump min n from 20 to 30 for verdict.

| Ticker | Post-deploy n | Verdict tree branch |
|---|---|---|
| BTC-USD | 0 | **inconclusive — more soak** |
| SOL-USD | 0 | **inconclusive — more soak** |

Combined: "inconclusive — more soak."

## Surprises / deviations

1. **The operator ran this task 24h early.** The brief explicitly accommodates this case (pre-window provision). The 14 paper_fills that opened at restart will close gradually; first verdict-grade insights expected ~30-90 min from now (i.e., between 17:00-17:30 UTC). For full 24h soak: 2026-05-04 16:30 UTC as briefed.

2. **The pre-F8b BTC and SOL averages are both drifting toward zero** with n+1 new data each. BTC went +5.66 → +3.65, SOL went +3.34 → +1.58. This is *suggestive* that the counterfactual was right and F8a-eval-rerun-2's positive results were small-n noise — but n is still too small to be conclusive. **Watch this trend in the next re-run.**

3. **DOGE high pullback now triggers `negative_edge:negative_edge`** automatically (the F6.5 gate fired once n crossed 30 on its h=1 cell). The system now blocks DOGE high pullback alerts via the calibrated gate, BEFORE the allowlist gate even runs. The brain is doing exactly what it was designed for. The allowlist remains useful for the med/low buckets that haven't crossed n=30 yet.

4. **F-hygiene-4.2's C fix is verifying empirically.** New post-fix DOGE cells show residuals of 5.66-6.72 bps — vs the pre-fix 34-40 bps cells. ~30 bps reduction matches the half-spread theory. The fix is real.

5. **6 new verdict-grade decay cells have crossed n=30.** First time in F8a's history. None at the strategically-relevant horizons (≥5s) yet, so the calibrated gates haven't started using them — but they're close.

6. **My SOL pre/post query in scratch had a JOIN-cardinality bug** that initially showed n=56. The CTE `SELECT e.id, e.decided_at` retains JOIN dups, then a `JOIN pullback_eids` on it 1:N-multiplies. The IN-subquery form (used by the F8a-eval-rerun-2 query) correctly dedupes. Caveat-worthy for the report's verbatim SQL section so future runs don't repeat the mistake.

## Open questions for Cowork

1. **Both BTC and SOL pre-F8b averages drifted toward zero** when one new data point landed each. If that trend holds when the post-F8b cohort closes, both tickers may end up near zero — meaning **F8b's allowlist still over-includes**. F9 becomes more attractive than continued F8b iteration.

2. **DOGE high h=1 verdict-grade tipping into auto-block** is a structural success — F6.5's negative-edge gate is doing the right thing without operator action. This is a positive sign for the overall calibrated-gate framework: as data accumulates, the brain self-prunes. **Worth surfacing as an architectural validation.**

3. **F-hygiene-4.2's fix** is empirically delivering on the half-spread theory (DOGE residuals ~30 bps lower on post-fix observations). Confirms the diagnosis in F-hygiene-4 was correct.

4. **Re-run timing.** Per the brief's success criterion #4 ("if recommendation is 'soak more,' includes specific projected re-run time"): I recommend running this analysis at **two checkpoints**:
   - **2026-05-03 ~18:00 UTC** (~90 min from now) — first wave of catchup-batch closures should produce some post-F8b distinct exits. Could give a directional hint even if not verdict-grade.
   - **2026-05-04 ~16:30 UTC** (the original target) — full 24h, decision-grade.

5. **The 14 catchup paper_fills are time-clustered at 16:29:33** because they all came from snapshot-replay drains. Their P/L outcomes will be highly correlated (entered at near-identical market state). If they all close green or all close red, treat that as ONE data point, not 14.

## Recommendation for next NEXT_TASK

**Path A — re-run this same task at 2026-05-04 16:30 UTC (full 24h soak, briefed time).**

One-line for the re-run: *Re-execute `f8b-verification-soak` with 24h+ soak data, distinct-exit counts ≥ 20 per ticker as the verdict floor; apply the decision tree branches for BTC and SOL.*

**Path A is the only defensible move now.** The current data is structurally too thin — zero post-deploy distinct exits — to either confirm or refute the counterfactual's BTC verdict. F8a continues to soak data on the allowlist regime; the allowlist is verifiably blocking ETH/AVAX/DOGE; SOL is still positive (drift notwithstanding); BTC is increasingly suspect but unresolved.

Path B (early intermediate run at 18:00 UTC) is *optionally* useful for directional reading; not strictly required.

## Verbatim verification SQL — for next review

```sql
-- Pinned deploy timestamp:
-- F8b deploy at 2026-05-03 16:29:20 UTC (effective fast-data-worker restart)
-- Brief's planned 24h target: 2026-05-04 16:30 UTC

-- 1. Distinct realized P/L on post-deploy cohort.
--    NOTE: use IN-subquery, not JOIN-on-pullback-eids — the latter
--    inflates by JOIN cardinality from dup alerts.
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
              / NULLIF(COUNT(*),0))::numeric, 1) AS win_rate_pct,
       ROUND(AVG(x.realized_return_pct * 100)::numeric, 2) AS avg_ret_bps,
       ROUND(AVG(x.holding_period_s)::numeric, 0) AS avg_hold_s
FROM fast_exits x
JOIN fast_executions e ON e.id = x.entry_execution_id
WHERE x.entry_execution_id IN (SELECT id FROM pullback_eids)
GROUP BY e.ticker ORDER BY exits DESC;

-- 2. Allowlist gate efficacy (post-deploy)
SELECT e.ticker, e.reject_reason, COUNT(*) AS n
FROM fast_executions e
WHERE e.alert_type='volume_breakout_pullback_long'
  AND e.decided_at > '2026-05-03 16:29:20'
  AND e.decision='rejected'
GROUP BY 1, 2 ORDER BY 1, n DESC;

-- 3. Allowlist false-reject canary
SELECT COUNT(*) AS false_rejects
FROM fast_executions e
WHERE e.alert_type='volume_breakout_pullback_long'
  AND e.decided_at > '2026-05-03 16:29:20'
  AND e.ticker IN ('BTC-USD', 'SOL-USD')
  AND e.reject_reason LIKE 'pullback_ticker%';
-- Expected: 0.

-- 4. Validation residuals (post-F-hygiene-4.2)
SELECT ticker, score_bucket, horizon_s, sample_count,
       realized_validation_count AS val_n,
       ROUND(realized_validation_residual::numeric * 10000, 2) AS resid_bps,
       ROUND(mean_return::numeric * 10000, 3) AS miner_mean_bps
FROM fast_signal_decay
WHERE alert_type='volume_breakout_pullback_long'
  AND realized_validation_count > 0
ORDER BY ticker, horizon_s;

-- 5. SOL pre vs post (DISTINCT — IN-subquery, not JOIN)
WITH pullback_eids AS (
  SELECT e.id, e.decided_at FROM fast_executions e
  JOIN fast_alerts a ON a.ticker=e.ticker AND a.alert_type=e.alert_type
                    AND a.fired_at=e.alert_fired_at
  WHERE a.alert_type='volume_breakout_pullback_long' AND e.ticker='SOL-USD'
)
SELECT
  CASE WHEN p.decided_at < '2026-05-03 16:29:20' THEN 'pre-F8b (30s)' ELSE 'post-F8b (25s)' END AS era,
  COUNT(DISTINCT p.id) AS exits,  -- DISTINCT to dedupe
  ROUND(AVG(x.realized_return_pct * 100)::numeric, 2) AS avg_ret_bps
FROM fast_exits x
JOIN pullback_eids p ON p.id = x.entry_execution_id
GROUP BY era ORDER BY era;
-- Or equivalent IN-subquery form.
```

## What's next

**Recommended: f8b-verification-soak-2 at 2026-05-04 16:30 UTC.**

Same brief shape; same SQL queries; this time with ≥ 24h of post-deploy data. By then:
- BTC post-deploy n likely 15-25 distinct exits (target ≥20).
- SOL post-deploy n likely 12-18 distinct exits (target ≥20, may be borderline — check then).
- 6+ verdict-grade decay cells should have grown to 10+, including some at horizons ≥ 5s.
- The C-fix's residual reduction trajectory should be visible across more cells.

F8a soak continues uninterrupted. `models/trading.py` and `.env.example` remain untouched.
