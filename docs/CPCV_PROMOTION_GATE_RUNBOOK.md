# CPCV promotion gate runbook (Q1.T1)

## Flag

- **Env:** `CHILI_CPCV_PROMOTION_GATE_ENABLED` (Settings: `chili_cpcv_promotion_gate_enabled`).
- **Default:** `false`. When off, CPCV runs **only at promotion-attempt time** (after ensemble / v2 DSR+holdout pass); metrics are logged (`[cpcv_promotion_gate]`) and persisted on `scan_patterns` when a row exists; promotion is **not** blocked by CPCV.
- When **on**, promotion fails with `detail["blocked"] == "cpcv_promotion_gate_failed"` if thresholds are not met.

## Thresholds (all required when enforcing)

| Metric | Gate |
|--------|------|
| `deflated_sharpe` | ≥ 0.95 |
| `pbo` | ≤ 0.2 |
| `cpcv_n_paths` | ≥ 50 |
| `cpcv_median_sharpe` | ≥ 0.5 (annualized, path median) |
| Labeled samples (`n_trades`) | ≥ 30 |

## Enable / disable

1. Set `CHILI_CPCV_PROMOTION_GATE_ENABLED=true` in the environment (or `.env`).
2. Restart the FastAPI process and workers so `settings` reloads.
3. **Disable:** set to `false` and restart — prior CPCV columns remain; the legacy ensemble + DSR/holdout behavior is unchanged; CPCV stops blocking immediately.

## Interpret reject rates

- High `cpcv_n_paths_lt_50`: not enough combinatorial paths — often too few labeled rows after triple-barrier filtering; check data depth and `purged_size` / `embargo_size`.
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

- **Default** is dry-run (no writes) unless `--commit` is passed.
- Dry-run prints counts: evaluated, would-pass CPCV, would-demote, and would-demote **by scanner** (`swing` / `day` / `breakout` / `momentum` / `patterns`).
- Exit code **2** if would-demote **> 20%** of evaluated patterns — **do not** run `--commit` until operators review.

Failing patterns (when not `skipped`) are set to `lifecycle_stage = 'challenged'` only with `--commit`.

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

Point **`DATABASE_URL`** at a Postgres database that mirrors production **shape** (same schema as app migrations through **163** and **164** — `scan_patterns` CPCV columns + `cpcv_shadow_eval_log` / `cpcv_shadow_funnel_v`). If those migrations are not applied yet, run the app once against that database (or apply migrations via your normal deploy path) so ORM queries and the shadow view exist. Use a **dedicated** database name ending in `_test` for any environment where pytest truncates; for a read-only rehearsal on a copy of prod data, use a **snapshot/clone** URL, never the live trading writer. From the repo root, with conda env **`chili-env`**: `conda run -n chili-env python scripts/backfill_cpcv_metrics.py --dry-run` (default is dry-run; omit `--commit`). Exit code **0** means the run finished and would-demote share is ≤20% of evaluated patterns; exit **2** means would-demote **>**20% — **do not** run with `--commit`, copy the full console summary (including scanner bucket breakdown) back to the operator channel, and wait for review before any commit or demotion.
