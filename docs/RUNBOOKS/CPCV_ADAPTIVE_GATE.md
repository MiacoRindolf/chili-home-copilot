# CPCV Adaptive Gate (Phase 2 of f-adaptive-promotion-architecture)

> **Audience:** operator + future Claude Code sessions.
> **Module:** `app/services/trading/cpcv_adaptive_gate.py`
> **Flag:** `chili_cpcv_adaptive_gate_enabled` (default `False`)
> **Shadow log:** `cpcv_adaptive_eval_log` (created by migration 239)
> **Wired at:** `promotion_gate.finalize_promotion_with_cpcv` — single call site
> **Parent brief:** `docs/STRATEGY/QUEUED/f-adaptive-promotion-architecture-2026-05-11.md`
> **Phase brief:** `docs/STRATEGY/QUEUED/f-adaptive-cpcv-gate.md`

## TL;DR

The legacy CPCV gate uses hardcoded thresholds inherited from Lopez de
Prado conventions (`dsr >= 0.95`, `pbo <= 0.2`, `median_sharpe >= 0.5`,
`cpcv_n_paths >= 20`, `min_trades >= 30`). Phase 0's audit showed these
have zero discriminatory power on our population — DSR pegged at 1.000
and PBO at 0.000 across all 39 patterns with CPCV data. The adaptive
gate replaces those numbers with three operator-policy parameters that
have semantic meaning, then derives empirical thresholds from the active
pool's distribution.

**Policy, not magic.** Each remaining number expresses a deliberate
choice — risk appetite, statistical strength, portfolio coupling —
rather than an arbitrary convention.

## Three operator-policy parameters

| Setting | Default | What it means | Effect of raising | Effect of lowering |
|---|---|---|---|---|
| `chili_cpcv_target_promotion_pool_pct` | `0.05` | "I want roughly the top 5% of patterns live by each metric." Drives the empirical percentile threshold (q = 1 − target_pct). | Smaller live pool, tighter standard. | Larger live pool, looser standard. |
| `chili_cpcv_ci_level` | `0.90` | "I want 90% confidence in the lower-bound estimate." Drives Hansen-style DSR CI + Wilson-style PBO upper CI. | Stricter — requires more sample-size evidence to clear. | Looser — small-n patterns clear more easily. |
| `chili_portfolio_marginal_sharpe_min_bps` | `0.0` | "Adding the pattern must improve the active roster's median Sharpe by at least N bps." Default 0 = no-op floor (any positive contribution admits). | Tighter — patterns must add real lift. | Looser — patterns that match the roster average still pass. |

Plus the flag itself:

- `chili_cpcv_adaptive_gate_enabled` (default `False`) — when False the
  wrapper is a byte-identical no-op AND still writes the shadow log so
  operators can opt into observation without flipping authority.

## How the math works

For each candidate pattern with CPCV data:

1. **Bayesian shrinkage** of each metric toward the pool mean with
   weight `w = n / (n + n0)` where `n0` is the pool's median
   trade-count. Pattern 585's profile (11 trades, raw DSR = 1.000)
   ends up around 0.66 after shrinkage rather than the inflated
   ceiling — a low-n pattern can no longer rubber-stamp itself in.
2. **Sample-size-aware confidence interval** on the shrunken metric.
   Hansen-style closed-form lower CI for DSR. Wilson-style binomial
   upper CI for PBO. Median Sharpe uses a Hansen-style scaling via
   `tanh` to keep the math finite. Wide CI for low-n; tight for
   high-n.
3. **Empirical percentile threshold.** The lower-CI (upper-CI for PBO)
   is compared against the pool's `q`-th percentile, where
   `q = 1 - chili_cpcv_target_promotion_pool_pct`. With the default
   5% target, that's the 95th percentile.
4. **Pareto frontier multi-objective.** The candidate is rejected if a
   pool member strictly dominates it across `(shrunk_DSR, -shrunk_PBO,
   shrunk_median_sharpe)`. Stops "checkbox-passes but mediocre"
   patterns from leaking in.
5. **Portfolio marginal Sharpe lift (lightweight proxy).** Candidate's
   shrunken median Sharpe minus the active roster's mean median
   Sharpe, in bps. Admits when `>= chili_portfolio_marginal_sharpe_min_bps`.

> The proxy in step 5 is **directionally informative** rather than the
> full covariance-aware portfolio computation — Phase 2 doesn't have a
> per-roster returns matrix available at gate time. Phase 3+ refinement
> will swap in a true marginal Sharpe once the prediction-mirror has
> a per-pattern returns view wired up. Until then, the marginal-Sharpe
> column in the shadow log is recorded for post-hoc audit but the
> default `min_bps = 0.0` floor makes it a no-op.

## Reading the shadow log

Each evaluation writes 4 rows to `cpcv_adaptive_eval_log`:

- `metric_name = 'dsr'` — DSR raw / shrunken / lower-CI / pool threshold / per-metric eligibility
- `metric_name = 'pbo'` — same for PBO (note: PBO's CI is the *upper* bound; pool threshold is the `(1 − q)` percentile)
- `metric_name = 'median_sharpe'` — same for Sharpe
- `metric_name = 'summary'` — Pareto verdict + portfolio marginal bps + both verdicts (`legacy_verdict_pass` and `adaptive_verdict_pass`)

### Verdict-divergence query (the canonical observation)

```sql
-- All patterns where legacy and adaptive disagree in the last 7 days.
SELECT scan_pattern_id,
       MAX(evaluated_at) AS last_eval,
       BOOL_OR(legacy_verdict_pass) AS legacy_pass,
       BOOL_OR(adaptive_verdict_pass) AS adaptive_pass,
       BOOL_OR(marginal_portfolio_sharpe_bps) AS any_marginal_bps
FROM cpcv_adaptive_eval_log
WHERE evaluated_at >= NOW() - INTERVAL '7 days'
  AND metric_name = 'summary'
GROUP BY scan_pattern_id
HAVING BOOL_OR(legacy_verdict_pass) IS DISTINCT FROM BOOL_OR(adaptive_verdict_pass)
ORDER BY last_eval DESC;
```

### Per-pattern diagnostic

```sql
SELECT metric_name, raw_value, shrunken_value, lower_ci,
       pool_threshold, pool_percentile, eligible,
       pareto_dominant, marginal_portfolio_sharpe_bps,
       legacy_verdict_pass, adaptive_verdict_pass
FROM cpcv_adaptive_eval_log
WHERE scan_pattern_id = $1
ORDER BY evaluated_at DESC, id ASC
LIMIT 8;
```

### Roll-up: how aggressive is the adaptive gate vs legacy?

```sql
SELECT
  COUNT(*) FILTER (WHERE legacy_verdict_pass AND adaptive_verdict_pass)   AS both_pass,
  COUNT(*) FILTER (WHERE legacy_verdict_pass AND NOT adaptive_verdict_pass) AS legacy_only,
  COUNT(*) FILTER (WHERE NOT legacy_verdict_pass AND adaptive_verdict_pass) AS adaptive_only,
  COUNT(*) FILTER (WHERE NOT legacy_verdict_pass AND NOT adaptive_verdict_pass) AS both_fail
FROM cpcv_adaptive_eval_log
WHERE metric_name = 'summary'
  AND evaluated_at >= NOW() - INTERVAL '7 days';
```

## Rollout sequence

### Step 1 — Ship at flag-OFF (this brief)

Module + migration + tests land. Legacy gate continues exclusively. No
shadow log rows are produced *unless* `finalize_promotion_with_cpcv` is
called for a pattern with a persisted id (which it normally is).

### Step 2 — Enable observation only (operator-controlled)

Flip the flag in `trading_settings` to `True`. The wrapper still returns
the **legacy** verdict (because `chili_cpcv_adaptive_gate_enabled` is the
authority switch, and that one stays at False), and additionally the
shadow log captures every adaptive verdict for comparison. Run for 7
days. Inspect the divergence query above.

> Wait — there is only ONE flag and the shadow log already writes
> regardless. Step 2 is therefore *not* a flag flip: it is just
> "operators look at the log." The flag in Step 3 is the same flag.

### Step 3 — Flip authority to adaptive

After the 7-day shadow soak shows the adaptive gate's verdicts are
sound (no obviously-wrong promotions or rejections vs operator
judgment), set:

```bash
# .env (then restart docker compose):
CHILI_CPCV_ADAPTIVE_GATE_ENABLED=1
```

Or via the live settings table:

```sql
UPDATE trading_settings
SET value = 'true'
WHERE key = 'chili_cpcv_adaptive_gate_enabled';
```

(Whichever mechanism the operator uses for other `chili_*` flags.)

From that moment, `maybe_apply_adaptive_gate` returns the adaptive
verdict to `finalize_promotion_with_cpcv`. The legacy `(ok, reasons)`
is still computed (and still recorded in the shadow log) so rollback
is one flag-flip away.

## Tuning the three policy parameters

| Symptom | Tune | Direction |
|---|---|---|
| Too few promotions, roster stagnates | `target_promotion_pool_pct` | Raise (e.g. 0.05 → 0.10) |
| Too many promotions, attention dilutes | `target_promotion_pool_pct` | Lower (e.g. 0.05 → 0.03) |
| Small-n patterns clearing on flukes | `ci_level` | Raise (e.g. 0.90 → 0.95) |
| Real edges with thin samples being blocked | `ci_level` | Lower (e.g. 0.90 → 0.80) |
| Roster becomes correlation-heavy | `portfolio_marginal_sharpe_min_bps` | Raise (e.g. 0 → 25 bps) |
| Marginal-positive contributors being blocked | `portfolio_marginal_sharpe_min_bps` | Lower (already at 0; flag concerns) |

Re-tune *after* 7 days of shadow-log evidence, not as a knee-jerk
reaction to a single odd verdict.

## Rollback procedure

If the adaptive verdict starts producing obviously-wrong rejections
or admissions after the authority flip:

1. **Flip the flag back to False.** This is the single action.
   ```bash
   # .env then docker compose restart
   CHILI_CPCV_ADAPTIVE_GATE_ENABLED=0
   ```
   Or via the settings table:
   ```sql
   UPDATE trading_settings SET value = 'false'
   WHERE key = 'chili_cpcv_adaptive_gate_enabled';
   ```
2. The wrapper resumes returning the legacy verdict on the very next
   call to `finalize_promotion_with_cpcv`. No restart strictly
   required if the settings table is the authority.
3. **Optional cleanup.** If the shadow log has grown large and the
   adaptive verdict is no longer needed for audit:
   ```sql
   TRUNCATE TABLE cpcv_adaptive_eval_log;
   ```
   Migration 239 is additive — there is no schema rollback needed.

## What this does NOT change

- `promotion_gate.promotion_gate_passes` is unchanged. The legacy gate
  runs first and its `(ok, reasons)` is still computed and logged.
- No autotrader / venue / broker / bracket / kill-switch code is
  touched.
- The realized-EV gate (`realized_ev_gate.check_realized_ev_blocking`)
  is unchanged. Both gates remain in series.
- Migration 239 does not add columns to `scan_patterns`. The adaptive
  evaluation reads existing CPCV columns and writes only to its own
  new table.

## Open follow-ups (Phase 3+)

- Replace the lightweight marginal-Sharpe proxy with a true covariance-
  aware portfolio marginal once the prediction-mirror has a per-pattern
  returns matrix wired up.
- Wire `quality_composite_score` (migration 237) as a 4th Pareto axis
  once Phase 3's event-driven re-scoring lands.
- After 7 days of shadow soak, decide whether to roll any of the three
  policy parameters into the existing `trading_settings` UI for live
  tuning, or keep them as deployment-config only.
