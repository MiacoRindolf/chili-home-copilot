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

Failing patterns (when not `skipped`) are set to `lifecycle_stage = 'challenged'`.

## Investigation checklist

1. Read `[cpcv_promotion_gate]` lines for `enforced`, `pass`, `dsr`, `pbo`, `paths`, `med_sh`, `reasons`.
2. Inspect `scan_patterns.promotion_gate_reasons` and `oos_validation_json.ensemble_promotion_gate` for OOS flows.
3. Confirm `TEST_DATABASE_URL` uses a `*_test` DB for any pytest that truncates.

## Live rollout (operator)

Recommended: **14 days shadow** (flag off) → enforce on **Momentum** family only (operational filter / config — document operator steps) for 14 days → expand if strike rate and realized OOS Sharpe delta ≥ 0.
