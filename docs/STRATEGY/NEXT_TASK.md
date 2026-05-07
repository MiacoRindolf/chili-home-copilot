# NEXT_TASK: f-exit-parity-metric-v2

STATUS: PENDING

**Promoted from `docs/STRATEGY/QUEUED/f-exit-parity-metric-v2.md` on 2026-05-07 12:30 UTC after the prior thread shipped `f-trump-usd-poisoned-quote-source-audit` (closing the six-cycle implausible-quote chain). See `docs/STRATEGY/CC_REPORTS/2026-05-07_f-trump-usd-poisoned-quote-source-audit.md` and `docs/STRATEGY/COWORK_REVIEWS/2026-05-07_f-trump-usd-poisoned-quote-source-audit.md` for the closeout.**

**Why this is next**: Both gating dependencies named in the original placeholder are met:
- `f-exit-parity-persist` shipped 2026-05-05 (mig 225, `agree_strict_bool` column).
- `f-time-decay-unit-fix` shipped 2026-05-05 (mig 227, fixes 81% of patterns that were silently mis-computing time-decay).
- Parity data has been accumulating for ~48h — sufficient for the verdict-query soak.

This is the highest-value remaining algo-trader-architect work in the QUEUED backlog. The current binary `agree_bool` / `agree_strict_bool` answers "did they agree?" but doesn't tell us **direction of disagreement**, **magnitude of P/L impact**, **asymmetric-close imbalance**, or **which rule drives the disagreements**. The new schema replaces a yes/no gate with a rolling 24h confidence-interval check on tracking error and bias — the metrics an algo trader actually uses to gate a strangler-fig cutover.

## Goal

Replace the binary `agree_bool` / `agree_strict_bool` parity decision in `trading_exit_parity_log` with a **multi-dimensional, signed, statistically tractable** decomposition. The current design answers a yes/no question ("did they agree?"). The new design answers the questions an algo trader actually needs answered:

- **Which engine is more aggressive at closing?** (asymmetric-close imbalance)
- **When both close, do they pick the same exit price?** (tracking error in bps)
- **Is there a systematic P/L bias toward one engine?** (bias t-statistic)
- **Which RULE differences drive the disagreements?** (priority_winner cohort breakdown)
- **Is parity drifting over time, or stable?** (rolling-window stats)

The cutover gate becomes a **rolling 24h confidence-interval check** on tracking error and bias, not a one-shot threshold on a boolean.

## Why now

`f-exit-parity-persist` gives us persistence and `agree_strict_bool`. That's enough to answer "do labels agree at 99%+?" — but it doesn't tell us:

- **Direction of disagreements.** A row with `agree_strict_bool=false` could mean canonical exits earlier (cuts losses faster — good) or canonical exits later (gives back profit — bad). The boolean doesn't distinguish.
- **Magnitude of P/L impact.** Two engines disagreeing on label but exiting at the same `bar.close` produces zero P/L delta. The boolean treats this the same as engines that exit at different prices.
- **Asymmetry of behavior.** "Canonical-only-close" rows have very different implications than "legacy-only-close" rows; the current schema collapses them.
- **Rule-level diagnostic.** When labels disagree, knowing whether it was trail-vs-bos (priority order issue) vs. bos-vs-time_decay (different rule firing thresholds) is the key input to deciding WHAT to fix in the cutover prep.

`f-time-decay-unit-fix` shipped (mig 227), so the `bars_held` input to canonical's time_decay rule is consistent across timeframes. The priority_winner column won't show false-positive "time_decay differences" that are actually unit bugs.

## Brain integration / source material

- `app/services/trading/live_exit_engine.py:329-345` — live row construction; current `agree_bool` and `agree_strict_bool` are computed here.
- `app/services/backtest_service.py:1438-1462` — backtest sink append; loose-`agree_bool` definition at lines 1439-1442.
- `app/models/trading.py:1715-1755` — `ExitParityLog` ORM. Migration 225 added `agree_strict_bool`; this task adds 4 more columns.
- `app/services/trading/exit_evaluator.py::ExitDecision` — already exposes `reason_code` and `r_multiple` fields. The `priority_winner` column derives from `reason_code`.
- `scripts/dispatch-exit-parity-verdict.ps1` — replaced by the new verdict query in Step 5 below.

## Path

### Step 1 — Migration `_migration_NNN_exit_parity_metric_v2`

(Use the next sequential ID at the time of execution. Verify with `scripts/verify-migration-ids.ps1`.)

```sql
ALTER TABLE trading_exit_parity_log
    ADD COLUMN IF NOT EXISTS action_class VARCHAR(32) NULL,
    ADD COLUMN IF NOT EXISTS label_match BOOLEAN NULL,
    ADD COLUMN IF NOT EXISTS exit_price_drift_bps DOUBLE PRECISION NULL,
    ADD COLUMN IF NOT EXISTS priority_winner VARCHAR(32) NULL;

ALTER TABLE trading_exit_parity_log
    ADD CONSTRAINT trading_exit_parity_log_action_class_check
    CHECK (action_class IS NULL OR action_class IN (
        'both_hold', 'both_close', 'canonical_only_close', 'legacy_only_close'
    ));

CREATE INDEX IF NOT EXISTS ix_exit_parity_action_class_created
    ON trading_exit_parity_log (action_class, created_at);
CREATE INDEX IF NOT EXISTS ix_exit_parity_priority_winner_created
    ON trading_exit_parity_log (priority_winner, created_at);
```

NULL on existing rows is fine — they pre-date the metric. Verdict queries filter on `action_class IS NOT NULL` to restrict to v2-era rows.

### Step 2 — Compute the new fields at row-write time

In **both** `live_exit_engine.py` and `backtest_service.py`, before inserting/queueing the parity row, compute:

```python
# Derive action_class from the four-state decomposition.
legacy_closes = (legacy_action != "hold")
canonical_closes = (canonical_action != "hold")
if not legacy_closes and not canonical_closes:
    action_class = "both_hold"
elif legacy_closes and canonical_closes:
    action_class = "both_close"
elif canonical_closes and not legacy_closes:
    action_class = "canonical_only_close"
else:
    action_class = "legacy_only_close"

# label_match only meaningful when both closed.
label_match = (legacy_action == canonical_action) if action_class == "both_close" else None

# exit_price_drift_bps in basis points, signed.
# Direction-aware: positive = canonical produced BETTER realized P/L.
sign = 1.0 if state.direction == "long" else -1.0
exit_price_drift_bps = None
if (action_class == "both_close"
    and legacy_exit_price is not None
    and canonical_exit_price is not None
    and legacy_exit_price > 0):
    exit_price_drift_bps = float(
        sign * (canonical_exit_price - legacy_exit_price) / legacy_exit_price * 10000.0
    )

# priority_winner = the canonical reason_code when labels disagree.
priority_winner = None
if action_class == "both_close" and not label_match:
    priority_winner = canonical_reason_code  # from ExitDecision.reason_code
elif action_class == "canonical_only_close":
    priority_winner = canonical_reason_code
elif action_class == "legacy_only_close":
    priority_winner = legacy_action
```

Add the four fields to the row construction in both modules. Reuse the existing `_phase_b_shadow_parity` and `_phase_b_bt_shadow_parity` hook points — minimal surface change.

### Step 3 — Direction-aware sign convention for shorts

Already encoded in Step 2's `sign = 1.0 if state.direction == "long" else -1.0`. Document the sign convention in `ExitParityLog`'s docstring: **positive `exit_price_drift_bps` always means canonical did better than legacy**.

### Step 4 — Refactor `agree_bool` semantics

Once `action_class` and `label_match` are populated, the legacy `agree_bool` becomes redundant. Two options:

**Option A — leave `agree_bool` and `agree_strict_bool` populated** for backward compat with the old verdict query. Doesn't break old analyses, just adds the new dimensions.

**Option B — deprecate `agree_bool` going forward.** Set it to NULL on new rows, document that v2-era rows use `action_class + label_match` instead. Cleaner schema; breaks old verdict query.

**Recommend Option A** for this task. Cleanup migration deferred to a separate brief if the boolean columns are still in use after a few weeks.

### Step 5 — New verdict query (replaces dispatch-exit-parity-verdict.ps1)

`scripts/dispatch-exit-parity-verdict-v2.ps1`:

```sql
\echo '## 1. Action-class population (last 24h, by source)'
SELECT
    source,
    action_class,
    COUNT(*) AS n,
    ROUND(100.0 * COUNT(*)::numeric / SUM(COUNT(*)) OVER (PARTITION BY source), 2) AS pct_of_source
FROM trading_exit_parity_log
WHERE created_at >= NOW() - INTERVAL '24 hours' AND action_class IS NOT NULL
GROUP BY source, action_class
ORDER BY source, n DESC;

\echo ''
\echo '## 2. Tracking error and bias on both_close rows (last 24h, by source)'
\echo '   The single quantitative answer: are the engines P/L equivalent?'
SELECT
    source,
    COUNT(*) AS both_close_n,
    ROUND(AVG(exit_price_drift_bps)::numeric, 4)              AS bias_bps,
    ROUND(STDDEV(exit_price_drift_bps)::numeric, 4)           AS tracking_error_bps,
    ROUND(
        (AVG(exit_price_drift_bps) /
         NULLIF(STDDEV(exit_price_drift_bps) / SQRT(COUNT(*)::float), 0)
        )::numeric, 4
    ) AS t_statistic,
    ROUND(MIN(exit_price_drift_bps)::numeric, 4)              AS worst_drift_bps,
    ROUND(MAX(exit_price_drift_bps)::numeric, 4)              AS best_drift_bps
FROM trading_exit_parity_log
WHERE created_at >= NOW() - INTERVAL '24 hours'
  AND action_class = 'both_close'
  AND exit_price_drift_bps IS NOT NULL
GROUP BY source;

\echo ''
\echo '## 3. Label-match rate on both_close rows (last 24h)'
SELECT
    source,
    COUNT(*) AS both_close_n,
    COUNT(*) FILTER (WHERE label_match = TRUE) AS labels_match,
    ROUND(100.0 * COUNT(*) FILTER (WHERE label_match = TRUE)::numeric / NULLIF(COUNT(*),0), 2) AS labels_match_pct
FROM trading_exit_parity_log
WHERE created_at >= NOW() - INTERVAL '24 hours' AND action_class = 'both_close'
GROUP BY source;

\echo ''
\echo '## 4. Asymmetric-close imbalance (last 24h)'
SELECT
    source,
    COUNT(*) FILTER (WHERE action_class = 'canonical_only_close') AS canonical_only_n,
    COUNT(*) FILTER (WHERE action_class = 'legacy_only_close') AS legacy_only_n,
    ROUND(
        COUNT(*) FILTER (WHERE action_class = 'canonical_only_close')::numeric
        / NULLIF(COUNT(*) FILTER (WHERE action_class IN ('canonical_only_close', 'legacy_only_close')), 0)
    , 4) AS canonical_aggressive_share
FROM trading_exit_parity_log
WHERE created_at >= NOW() - INTERVAL '24 hours' AND action_class IS NOT NULL
GROUP BY source;
\echo '   Ideal canonical_aggressive_share is 0.5 (balanced).'
\echo '   Skew >= 0.6 or <= 0.4 indicates one engine is consistently more aggressive.'

\echo ''
\echo '## 5. Priority-winner cohort breakdown (last 24h)'
SELECT
    source,
    priority_winner,
    COUNT(*) AS n,
    ROUND(AVG(exit_price_drift_bps)::numeric, 4) AS avg_drift_bps_for_this_winner,
    ROUND(STDDEV(exit_price_drift_bps)::numeric, 4) AS stddev_drift_bps_for_this_winner
FROM trading_exit_parity_log
WHERE created_at >= NOW() - INTERVAL '24 hours'
  AND action_class IN ('both_close', 'canonical_only_close', 'legacy_only_close')
  AND priority_winner IS NOT NULL
GROUP BY source, priority_winner
ORDER BY source, n DESC;

\echo ''
\echo '## 6. Rolling tracking error: last 1h vs last 24h vs last 7d'
WITH windows AS (
    SELECT '1h' AS w, NOW() - INTERVAL '1 hour' AS cutoff
    UNION ALL SELECT '24h', NOW() - INTERVAL '24 hours'
    UNION ALL SELECT '7d', NOW() - INTERVAL '7 days'
)
SELECT
    w.w AS window,
    p.source,
    COUNT(*) AS n,
    ROUND(AVG(p.exit_price_drift_bps)::numeric, 4) AS bias_bps,
    ROUND(STDDEV(p.exit_price_drift_bps)::numeric, 4) AS tracking_error_bps
FROM windows w
LEFT JOIN trading_exit_parity_log p
    ON p.created_at >= w.cutoff
    AND p.action_class = 'both_close'
    AND p.exit_price_drift_bps IS NOT NULL
GROUP BY w.w, p.source
ORDER BY p.source, w.w;
```

### Step 6 — Cutover gate (the dynamic part)

The new gate for "is canonical safe to flip to authoritative?" is a COMPOSITE check, not a single threshold. Codify it in `scripts/dispatch-exit-parity-cutover-gate.ps1`:

```sql
-- Threshold constants (NOT magic numbers - well-known quant defaults):
--   T_STAT_CRITICAL = 1.96  -- 95% CI z-score, two-sided
--   TE_MAX_BPS      = 10.0  -- ~1bp/% — looser than typical execution TE
--   ASYM_LOW        = 0.4   -- balanced asymmetric-close window lower bound
--   ASYM_HIGH       = 0.6   -- balanced asymmetric-close window upper bound
--   MIN_SAMPLE_N    = 1000  -- per-source minimum for verdict
WITH stats AS (
    SELECT
        source,
        COUNT(*) AS both_close_n,
        AVG(exit_price_drift_bps) AS bias_bps,
        STDDEV(exit_price_drift_bps) AS te_bps,
        AVG(exit_price_drift_bps) / NULLIF(STDDEV(exit_price_drift_bps) / SQRT(COUNT(*)::float), 0) AS t_stat,
        COUNT(*) FILTER (WHERE label_match = FALSE) AS label_mismatches
    FROM trading_exit_parity_log
    WHERE created_at >= NOW() - INTERVAL '24 hours'
      AND action_class = 'both_close'
      AND exit_price_drift_bps IS NOT NULL
    GROUP BY source
),
asym AS (
    SELECT
        source,
        COUNT(*) FILTER (WHERE action_class = 'canonical_only_close')::numeric
        / NULLIF(COUNT(*) FILTER (WHERE action_class IN ('canonical_only_close', 'legacy_only_close')), 0)
            AS canonical_aggressive_share,
        COUNT(*) FILTER (WHERE action_class IN ('canonical_only_close', 'legacy_only_close')) AS asym_n
    FROM trading_exit_parity_log
    WHERE created_at >= NOW() - INTERVAL '24 hours' AND action_class IS NOT NULL
    GROUP BY source
)
SELECT
    s.source,
    s.both_close_n,
    s.bias_bps,
    s.te_bps,
    s.t_stat,
    s.label_mismatches,
    a.asym_n,
    a.canonical_aggressive_share,
    CASE
        WHEN s.both_close_n < 1000 THEN 'INSUFFICIENT_DATA'
        WHEN ABS(s.t_stat) > 1.96 THEN 'FAIL_BIAS_SIGNIFICANT'
        WHEN s.te_bps > 10 THEN 'FAIL_TRACKING_ERROR_HIGH'
        WHEN a.canonical_aggressive_share < 0.4 OR a.canonical_aggressive_share > 0.6
             THEN 'FAIL_ASYMMETRIC_AGGRESSIVE'
        ELSE 'PASS'
    END AS verdict
FROM stats s
LEFT JOIN asym a USING (source);
```

The thresholds (1.96, 10 bps, 0.4-0.6) are well-known quant defaults — not magic numbers:
- **1.96** = the 95% CI z-score from a normal distribution. Standard significance threshold for two-sided tests.
- **10 bps** = ~1bp/% — looser than typical execution tracking error, tight enough to detect material engine drift.
- **0.4-0.6** = an asymmetric-close share between 40-60% means neither engine is dominantly more aggressive.

These belong in a constant block at the top of the gate query with inline comments documenting each threshold's reference standard.

### Step 7 — Tests

`tests/test_exit_parity_metric_v2.py`:

1. `action_class='both_hold'` when both legacy and canonical hold
2. `action_class='both_close'` + `label_match=true` when both fire same action
3. `action_class='both_close'` + `label_match=false` when both fire different actions
4. `action_class='canonical_only_close'` when canonical fires, legacy holds
5. `action_class='legacy_only_close'` when legacy fires, canonical holds
6. `exit_price_drift_bps` sign convention for longs (canonical higher = positive)
7. `exit_price_drift_bps` sign convention for shorts (canonical lower = positive)
8. `exit_price_drift_bps` is NULL when one or both prices are NULL
9. `priority_winner` populated correctly across all action_class branches
10. Verdict gate query: synthetic dataset of 1500 rows with t_stat=0.5, te=8 bps, share=0.5 returns 'PASS'
11. Verdict gate: synthetic with t_stat=2.5 returns 'FAIL_BIAS_SIGNIFICANT'
12. Verdict gate: synthetic with te=15 bps returns 'FAIL_TRACKING_ERROR_HIGH'
13. Verdict gate: synthetic with share=0.7 returns 'FAIL_ASYMMETRIC_AGGRESSIVE'
14. Verdict gate: only 500 rows returns 'INSUFFICIENT_DATA'

### Step 8 — Smoke verification

After deploy:

1. Trigger one brain-worker FractionalBacktest cycle.
2. SQL probe:
   ```sql
   SELECT action_class, COUNT(*) FROM trading_exit_parity_log
   WHERE action_class IS NOT NULL GROUP BY action_class;
   ```
   Expect: `both_hold` dominates (most bars are non-events), `both_close` and `canonical_only_close` / `legacy_only_close` present in smaller numbers.
3. Run `scripts/dispatch-exit-parity-verdict-v2.ps1`. All 6 sections produce non-empty output.
4. Run `scripts/dispatch-exit-parity-cutover-gate.ps1`. Expected verdict is `INSUFFICIENT_DATA` (only minutes of post-deploy data). In 24h+, the verdict should converge.

## Constraints / do not touch

- **No flip to `brain_exit_engine_mode=authoritative`.** Stays `shadow`. Cutover decision is gated on the new verdict, separate task.
- **Do not modify `agree_bool` or `agree_strict_bool` semantics.** Old columns stay populated (Option A from Step 4). New columns layer on top.
- **Do not touch the canonical evaluator semantics.**
- **No new magic numbers in code.** Threshold constants are at the top of the gate query with documented references. If the data suggests they should move, that's a separate tuning brief.
- **Tests use `_test`-suffixed DB.** PROTOCOL Hard Rule 5.
- **Migration ID** = next sequential at execution time (verify with `verify-migration-ids.ps1`).

## Out of scope

- Flipping authoritative mode. Strictly out — this task only adds metrics, doesn't act on them.
- Bootstrap CIs on tracking error. The t-statistic + z-score gate is rigorous enough for this phase. Bootstrap is a follow-up if the parametric assumption (normality of price drift) turns out to be violated in the data.
- Per-pattern parity scoring. Aggregate-level for cutover; per-pattern is a Phase D thing.
- Per-ticker parity scoring. Same logic.
- Removing `agree_bool` / `agree_strict_bool`. Cleanup deferred.

## Success criteria

1. **Migration lands cleanly.** `verify-migration-ids.ps1` passes. CHECK constraint on `action_class` enforced.
2. **All four new columns populated** on every new row from both live and backtest paths.
3. **Sign convention for shorts** correctly captured in `exit_price_drift_bps` (positive = canonical did better).
4. **All 14 new tests pass + existing parity tests still pass** against `chili_test`.
5. **Verdict query produces meaningful output** in all 6 sections.
6. **Cutover-gate query** produces a verdict (initially likely `INSUFFICIENT_DATA`, then converges with soak time).
7. **CC report** at `docs/STRATEGY/CC_REPORTS/<date>_f-exit-parity-metric-v2.md` per PROTOCOL format. Include verdict snapshot at +1h post-deploy.

## Rollback plan

- **Code rollback**: `git revert` the row-construction changes; new columns stay NULL on new rows. Verdict query gracefully handles NULLs (filter `WHERE action_class IS NOT NULL`).
- **Migration rollback**:
  ```sql
  ALTER TABLE trading_exit_parity_log
      DROP COLUMN action_class,
      DROP COLUMN label_match,
      DROP COLUMN exit_price_drift_bps,
      DROP COLUMN priority_winner;
  ```
  Per PHASE_ROLLBACK_RUNBOOK.

## Open questions for Cowork (surface in CC report only if relevant)

1. **Threshold tuning** — 1.96 / 10 bps / 0.4-0.6 are quant defaults. If observed tracking error in the post-deploy data is much smaller (e.g., median TE = 1 bps), tighten the threshold to e.g. 5 bps to surface drift earlier.
2. **`agree_bool` deprecation** — once v2 metrics are populated, should the old boolean columns be NULL'd on new rows (Option B from Step 4)? Defer that decision to a separate cleanup brief.
3. **Per-pattern verdict** — once aggregate parity is clean, add a `GROUP BY scan_pattern_id` to see if any specific patterns produce more drift than others. Out of scope for this task.
4. **The trail_monotonicity cutover question** (deferred from f-exit-parity-persist) — the v2 metrics, especially `priority_winner='trail'` rows, will quantify exactly how often the trail rule difference matters. After 24h of v2 data, the decision "flip trail_monotonic at the same time as authoritative, or in a separate phase" can be made empirically.
