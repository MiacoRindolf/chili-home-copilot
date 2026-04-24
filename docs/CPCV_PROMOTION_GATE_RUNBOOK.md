# CPCV promotion gate runbook (Q1.T1)

## Evaluator routing (Q1.T1.6)

`scan_patterns.pattern_evidence_kind` selects which CPCV implementation runs inside `finalize_promotion_with_cpcv` and in `scripts/backfill_cpcv_metrics.py`:

| Value | Use case | Mechanism |
|--------|-----------|-----------|
| **`realized_pnl`** (default) | Rule-based patterns with a `trading_pattern_trades` history | **Trade-sequence CPCV:** combinatorial purged CV on the time-ordered realized `outcome_return_pct` series. Each OOS fold’s score is the annualized Sharpe of realized returns in that fold. **DSR** / **PBO** use that same return stream (no triple-barrier relabeling, no LightGBM). |
| **`ml_signal`** | Future ML-driven signal patterns | **Legacy path:** triple-barrier labels + `LGBMClassifier` on feature rows (Q1.T1 contract), unchanged. |

- New and backfilled **promoted/live** rows default to **`realized_pnl`** (migration **169**).
- Set **`ml_signal`** only when a pattern is explicitly an ML-signal strategy that should use the classifier evaluator.
- **Promotion thresholds** (`DSR ≥ 0.95`, `PBO ≤ 0.2`, median path Sharpe ≥ 0.5, `n_trades ≥ 15`, and **graded** `cpcv_n_paths` — see below) apply to **both** evaluators; only the input series and path Sharpe construction differ.

After deploying T1.6, run a **manual** `backfill_cpcv_metrics.py` once against canonical chili (see below) so promoted/live patterns pick up metrics from the correct evaluator. Keep **`CHILI_CPCV_PROMOTION_GATE_ENABLED=false`** until operators review that rerun.

## Flag

- **Env:** `CHILI_CPCV_PROMOTION_GATE_ENABLED` (Settings: `chili_cpcv_promotion_gate_enabled`).
- **Default:** `false`. When off, CPCV runs **only at promotion-attempt time** (after ensemble / v2 DSR+holdout pass); metrics are logged (`[cpcv_promotion_gate]`) and persisted on `scan_patterns` when a row exists; promotion is **not** blocked by CPCV.
- When **on**, promotion fails with `detail["blocked"] == "cpcv_promotion_gate_failed"` if thresholds are not met.

## Flag-flip readiness criteria

Turning on **enforcement** (`CHILI_CPCV_PROMOTION_GATE_ENABLED=true`) is **not** a config-only change: operators should treat the following as **minimum** preconditions.

1. **Evidence breadth:** at least **5** patterns have been **evaluated** under **realized-PnL** CPCV with a non-NULL `cpcv_n_paths` (i.e. they produced combinatorial path evidence, not only “skipped” rows). Track with:

   ```sql
   SELECT COUNT(*) FROM scan_patterns WHERE cpcv_n_paths IS NOT NULL;
   ```

2. **Calibration stability:** **zero** patterns demoted on **any single procedural-count threshold** in a given backfill or promotion batch (catches future regressions where a tier boundary misfires while headline metrics still look fine).

3. **Operator review:** the per-scanner demote distribution (if any) has been reviewed against the **three-reading playbook** in [Production-shape dry-run (cheat sheet)](#production-shape-dry-run-cheat-sheet) below (&lt;10% proportional, 10–20% skewed, exit code 2 stop).

As of the T1.7 closeout backfill, only **one** promoted/live row had enough `trading_pattern_trades` history to evaluate (`n=1` above the PTR floor), so these criteria are **not** yet met. Weekly scheduled backfill (see **`CHILI_CPCV_WEEKLY_BACKFILL_ENABLED`**) is intended to grow the evaluated set as trade history accumulates; re-check the `COUNT(*)` query after each material run.

## Thresholds (all required when enforcing)

| Metric | Gate |
|--------|------|
| `deflated_sharpe` | ≥ 0.95 |
| `pbo` | ≤ 0.2 |
| `cpcv_n_paths` | **Graded (Q1.T1.7)** — see **`cpcv_n_paths` tiers** below (defaults: full **≥ 50**, provisional **20–49**, fail **&lt; 20**). |
| `cpcv_median_sharpe` | ≥ 0.5 (annualized, path median) |
| Effective sample (`n_trades`) | ≥ **`CHILI_CPCV_MIN_TRADES`** (default **15**): **ML** path = rows after triple-barrier labeling; **realized_pnl** path = realized PTR trades with `outcome_return_pct`. See sample-size tiers below. |

### Sample-size tiers — `n_trades` (Q1.T1.5)

DSR and CPCV are well-defined for modest **n**; confidence intervals widen as **n** shrinks. The gate uses three bands (defaults; tune via settings):

| Tier | `n_trades` (labeled, post-barrier) | Meaning |
|------|--------------------------------------|---------|
| **Insufficient** | **&lt; 15** (`chili_cpcv_min_trades`) | No CPCV / gate outcome — skip or insufficient evidence. |
| **Provisional** | **15 ≤ n &lt; 30** | Gate may **pass** if all metric thresholds hold; `promotion_gate_reasons` includes **`provisional_sample_size`** (wider CIs; not full-confidence promotion). |
| **Full confidence** | **≥ 30** (`chili_cpcv_full_confidence_min_trades`) | Pass/fail on metrics only; no trade-count provisional tag. |

Rationale: CHILI pattern-scale datasets are smaller than institutional backtests; **15** keeps CPCV and DSR usable while **30** remains the bar for treating evidence as “full” promotion strength.

### Sample-size tiers — `cpcv_n_paths` (Q1.T1.7)

Parallel to **`n_trades`**: combinatorial path count is capped by sample size; a single institutional-style floor (**50** paths) can reject strong realized-PnL CPCV on ~100–200 trades.

| Tier | `cpcv_n_paths` | Meaning |
|------|----------------|--------|
| **Infeasible / insufficient paths** | **&lt; 20** (`chili_cpcv_n_paths_provisional_min`) | **Fail** with **`cpcv_n_paths_below_provisional_min`** (floor is not lowered below 20). |
| **Provisional** | **20 ≤ paths &lt; 50** (upper bound `chili_cpcv_n_paths_full_min`) | Gate may **pass** if DSR, PBO, median Sharpe, and `n_trades` rules all pass; reasons include **`provisional_small_paths`** (alongside **`provisional_sample_size`** when `n_trades` is also in the 15–29 band). |
| **Full confidence (paths)** | **≥ 50** (`chili_cpcv_n_paths_full_min`) | No path-count provisional tag when all other checks pass. |

When both trade count and path count sit in provisional bands, **`provisional_sample_size`** and **`provisional_small_paths`** may appear together on a passing row.

### Small-sample CPCV (purge / embargo / paths)

`purge_size` and `embargo_size` **auto-scale** with labeled row count **n** (defaults: 5% and 2% of **n**, floors 2 and 1). Before `CombinatorialPurgedCV` runs, the code checks that **min train fold &gt; purge + embargo**; if not, it shrinks purge/embargo or skips with **`cv_infeasible_for_sample_size`**. Target combinatorial path budget: **`min(CHILI_CPCV_TARGET_PATHS_MAX, max(10, n // 5))`** (default cap **100**).

| Env (optional) | Setting | Default |
|----------------|---------|---------|
| `CHILI_CPCV_PURGE_FRAC` | `chili_cpcv_purge_frac` | `0.05` |
| `CHILI_CPCV_EMBARGO_FRAC` | `chili_cpcv_embargo_frac` | `0.02` |
| `CHILI_CPCV_MIN_TRADES` | `chili_cpcv_min_trades` | `15` |
| `CHILI_CPCV_N_PATHS_PROVISIONAL_MIN` | `chili_cpcv_n_paths_provisional_min` | `20` |
| `CHILI_CPCV_N_PATHS_FULL_MIN` | `chili_cpcv_n_paths_full_min` | `50` |
| `CHILI_CPCV_TARGET_PATHS` | `chili_cpcv_target_paths_max` | `100` |

Full-confidence boundary for **trade count** is **`chili_cpcv_full_confidence_min_trades`** (default **30**); for **path count**, **`chili_cpcv_n_paths_full_min`** (default **50**). Adjust in code or env as needed.

## Enable / disable

1. Set `CHILI_CPCV_PROMOTION_GATE_ENABLED=true` in the environment (or `.env`).
2. Restart the FastAPI process and workers so `settings` reloads.
3. **Disable:** set to `false` and restart — prior CPCV columns remain; the legacy ensemble + DSR/holdout behavior is unchanged; CPCV stops blocking immediately.

## Interpret reject rates

- `cpcv_n_paths_below_provisional_min`: path count is below the **20** floor — not enough combinatorial paths for a meaningful CPCV read; often too few trades or infeasible CV after triple-barrier filtering. Check data depth and autoscaled purge/embargo (or env overrides).
- `cv_infeasible_for_sample_size`: even after shrinking purge/embargo, no fold satisfies **train &gt; purge + embargo**; need more labeled rows or looser fractions (operator env).
- `provisional_sample_size` in reasons (with **pass**): acceptable under small-sample policy; treat as provisional promotion until **`n_trades` ≥ 30** (default full-confidence band for trades).
- `provisional_small_paths` in reasons (with **pass**): path count is in the **20–49** band; treat as provisional until **`cpcv_n_paths` ≥ 50** (default full-confidence band for paths) or policy says otherwise.
- `dsr_below_0_95`: selection-bias-adjusted Sharpe is weak on **barrier** returns — edge may be luck or multiple testing.
- `pbo_above_0_2`: CSCV-style PBO on strategy vs buy-and-hold barrier returns suggests instability.
- `median_sharpe_below_0_5`: CPCV path OOS Sharpe median is weak.

## Rollback

1. Turn **off** the flag and restart.
2. Optional: clear stored evidence (per-row):

```sql
UPDATE scan_patterns
SET
  promotion_gate_passed = NULL,
  promotion_gate_reasons = NULL,
  cpcv_n_paths = NULL,
  cpcv_median_sharpe = NULL,
  cpcv_median_sharpe_by_regime = NULL,
  deflated_sharpe = NULL,
  pbo = NULL,
  n_effective_trials = NULL
WHERE cpcv_n_paths IS NOT NULL;
```

## Backfill / demotion

```bash
conda run -n chili-env python scripts/backfill_cpcv_metrics.py --dry-run
conda run -n chili-env python scripts/backfill_cpcv_metrics.py --commit
```

- **Host:** use **`conda run -n chili-env`** (or `conda activate chili-env` first). Plain `python` on PATH often lacks **`skfolio`** → `ModuleNotFoundError: No module named 'skfolio'`.

- **Large patterns:** the script defaults to **`--max-labeled-rows 20000`** so CPCV subsamples before combinatorial CV + LightGBM (avoids OOM or silent process exit on patterns with very large `trading_pattern_trades`). Use **`0`** for no cap (optionally set **`CHILI_CPCV_MAX_LABELED_ROWS`** in `.env` when the kwarg is omitted). Single-pattern promotion via `evaluate_pattern_cpcv` still uses no cap unless that setting is set.

- **`--commit`:** persists **each pattern in its own transaction** (commit right after that pattern’s metrics are applied) so crashes mid-run keep earlier rows. Processing order is **newest first** (`scan_patterns.updated_at` descending, then `id` descending).

- **Selection (default):** only promoted/live rows with **`cpcv_n_paths IS NULL`** so you can **rerun** after a crash without redoing finished patterns. **`--all`** recomputes CPCV for every promoted/live row.

- **Default** is dry-run (no writes) unless `--commit` is passed.
- Dry-run prints counts: evaluated, would-pass CPCV, would-demote, and would-demote **by scanner** (`swing` / `day` / `breakout` / `momentum` / `patterns`).
- Exit code **2** if would-demote **> 20%** of evaluated patterns — **do not** run `--commit` until operators review.

Failing patterns (when not `skipped`) are set to `lifecycle_stage = 'challenged'` only with `--commit`.

- **Where to point `DATABASE_URL`:** **`--dry-run`** on **`chili_staging`** is recommended (production-shaped, no writes). **`--commit`** for lasting effect must target the **canonical** DB (usually production **`chili`** after operator review); committing only on **`chili_staging`** is ephemeral — see [STAGING_DATABASE.md](STAGING_DATABASE.md) (“Why staging…”).

Requires migration **163** (`scan_patterns` CPCV columns) applied before ORM-backed backfill against production-shaped DBs.

## Shadow funnel (7d)

- **Table / view:** `cpcv_shadow_eval_log`, `cpcv_shadow_funnel_v` (migration **164**). Rollback: [ROADMAP_DEVIATION_002.md](ROADMAP_DEVIATION_002.md).
- **API:** `GET /api/brain/cpcv_shadow_funnel`
- **UI:** Trading Brain → **Ops** tab → **CPCV shadow funnel (7d)** panel under pattern lifecycle counters.

## Investigation checklist

1. Read `[cpcv_promotion_gate]` lines for `enforced`, `pass`, `dsr`, `pbo`, `paths`, `med_sh`, `reasons`.
2. Inspect `scan_patterns.promotion_gate_reasons` and `oos_validation_json.ensemble_promotion_gate` for OOS flows.
3. Confirm `TEST_DATABASE_URL` uses a `*_test` DB for any pytest that truncates.

## Rollout calendar (concrete)

Use **US equity (NYSE) trading days** only. Record **T0** in the operator change calendar (first shadow session).

**Example anchor:** T0 = **2026-04-22** (Wednesday). Replace with the real go-live date if different.

| Phase | Trading days (inclusive) | `CHILI_CPCV_PROMOTION_GATE_ENABLED` | Scope |
|-------|--------------------------|--------------------------------------|--------|
| **Shadow** | **Day 1–14** (T0 = day 1) | `false` | All families: CPCV runs, rows append to `cpcv_shadow_eval_log`, `/brain` funnel updates; **no** CPCV block. |
| **Momentum-only enforce** | **Day 15–28** | `true` | Enforce CPCV **only** on the Momentum scanner lane (operator defines which patterns qualify — config or allowlist). |
| **All-families enforce** | **Day 29+** | `true` **only if** the **cumulative Sharpe delta** for the momentum-only window (days 15–28) is **≥ 0** vs the documented baseline (e.g. equal-weight or flag-off counterfactual). Otherwise extend momentum-only or stay in shadow. |

**During days 15–28:** Measure Sharpe (or pre-approved paper KPI) **daily** for the momentum-only cohort vs baseline; keep a short operator log for the day-29 decision.

### Rollback trigger (single-day)

If **any one trading day** has **> 50%** of promotion attempts that **reached** the CPCV gate rejected (`detail["blocked"] == "cpcv_promotion_gate_failed"` among those attempts):

1. Set `CHILI_CPCV_PROMOTION_GATE_ENABLED=false` and restart app + workers.
2. Notify operators (incident channel / on-call per org policy).
3. Investigate `promotion_gate_reasons` and shadow funnel asymmetry by scanner.

*Automation (cron/monitor flipping the flag) is optional; until wired, operators perform the above manually when the metric fires.*

## Production-shape dry-run (cheat sheet)

Point **`DATABASE_URL`** at a Postgres database that mirrors production **shape** (same schema as app migrations through **163** and **164** — `scan_patterns` CPCV columns + `cpcv_shadow_eval_log` / `cpcv_shadow_funnel_v`). If those migrations are not applied yet, run the app once against that database (or apply migrations via your normal deploy path) so ORM queries and the shadow view exist. **Preferred:** use **`chili_staging`** (full copy of `chili`, refreshed daily — see [`docs/STAGING_DATABASE.md`](STAGING_DATABASE.md)). **Do not** use **`chili_test`** for this dry-run. Use a **dedicated** `*_test` database only for pytest (it truncates). For a read-only rehearsal on prod-like data, use **`chili_staging`** or another **snapshot/clone** URL, never the live trading writer. Optional: set **`STAGING_DATABASE_URL`** in `.env` to the staging URL for reference; override **`DATABASE_URL`** for the script session as needed. From the repo root, with conda env **`chili-env`**: `conda run -n chili-env python scripts/backfill_cpcv_metrics.py --dry-run` (default is dry-run; omit `--commit`). Exit code **0** means the run finished and would-demote share is ≤20% of evaluated patterns; exit **2** means would-demote **>**20% — **do not** run with `--commit`, copy the full console summary (including scanner bucket breakdown) back to the operator channel, and wait for review before any commit or demotion.

### Interpreting dry-run results

Signals to capture from the dry-run output

Exit code. 0 = demote share ≤ 20% (safe to consider --commit). 2 = > 20% (do not --commit without operator review).
Summary block. promoted_or_live_total, evaluated, would_pass_cpcv_gate, would_demote_total. Compute demote rate = would_demote_total / evaluated when evaluated > 0.
Per-scanner demote lines. would_demote_scanner[...] — the asymmetry diagnostic. Compare to the current promoted-pattern mix per scanner; disproportionate demotes on a single scanner indicate that scanner has been promoting on weak OOS evidence.

Three readings

< 10% demotes, breakdown roughly proportional to promoted mix. Gate is conservative-but-fair. Action: --commit in a maintenance window, then begin the 14-day shadow → momentum-only enforce calendar. Q1.T2 (regime classifier) can start the same session.
10–20% demotes but skewed (e.g. 80% of demotes from day-trade or momentum scanners). This is the operator-perception-gap diagnostic firing — those scanners have been promoting on weak OOS evidence; the new gate correctly catches them. Action: still --commit, but extend shadow window to 21 days. Expect those scanners to need Q1.T4 (StrategyParameter adaptive thresholds) and Q1.T2 (regime tagging) before they can repromote at scale. This is not a failure; it is the gate doing its job.
Exit code 2 (> 20% demotes). Stop. Do not --commit. Paste the per-scanner breakdown for operator review. Most likely interpretation: the existing gate has been substantially over-promoting and the new gate correctly tightens — but at > 20%, understand why before letting lifecycle state flow. Possible short-term mitigation: temporarily relax DSR threshold from 0.95 to 0.90 while building regime/feature infrastructure in Q1.T2 and Q1.T4, then ratchet back to 0.95 once those upgrades land. Any threshold change must be a separate PR with its own runbook entry; do not edit thresholds inline.
