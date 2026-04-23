# Regime classifier runbook (Q1.T2)

## Flag

- **Env:** `CHILI_REGIME_CLASSIFIER_ENABLED` → Settings: `chili_regime_classifier_enabled`.
- **Default:** `false`. When **off**, no weekly retrain job runs, `build_regime_features` / `fit_regime_model` are not invoked from scheduler paths, `trading_snapshots.regime` stays `NULL` on new rows, and `current_regime()` returns `(None, None, None)`. Downstream (future CPCV regime column, StrategyParameter, HRP) must treat `NULL` / `None` as pooled / global behavior.

## Enable / disable

1. Set `CHILI_REGIME_CLASSIFIER_ENABLED=true` in the environment (or `.env`).
2. Restart FastAPI and **scheduler-worker** so settings and cron jobs reload.
3. **Disable:** set to `false` and restart — existing `regime_snapshot` rows remain; new snapshot tags stop. Consumers fall back to pooled parameters when regime is missing.

## Migration

- Apply **`165_regime_snapshot_and_tagging`**: tables/columns/indexes per [`app/migrations.py`](../app/migrations.py). Rollback SQL: [`docs/ROADMAP_DEVIATION_003.md`](ROADMAP_DEVIATION_003.md).

## Feature pipeline (read this before trusting labels)

Five inputs (point-in-time):

1. SPY daily log return — `fetch_ohlcv_df("SPY", …)` (existing market-data stack).
2. 21-day realized vol of SPY — derived from SPY closes.
3. 126-day SPY momentum — `log(close / close.shift(126))`.
4. VIX — `^VIX` close; macro snapshot VIX overrides when present for that `as_of_date`.
5. Yield slope — **`trading_macro_regime_snapshots.yield_curve_slope_proxy`** (not FRED DGS10−DGS2; see deviation doc). If macro has no row for a calendar date, that date is **skipped** in the feature matrix (warning in logs).

**T1 interaction:** `cpcv_median_sharpe_by_regime` is reserved but **not** populated from this HMM in T2. A later small PR may join CPCV path medians to these tags.

## First-time setup

1. Apply migration **165**.
2. Ensure historical **macro regime** rows exist for dates where you need yield slope (Phase L.17 sweep), or expect sparse features until macro backfill catches up.
3. Run **`conda run -n chili-env python scripts/backfill_regime.py --dry-run`** — inspect logs; no DB writes.
4. Run **`--commit`** in a maintenance window after dry-run sanity.
5. **Sanity:** over ~10y, label distribution roughly **~40% / ~40% / ~20%** bull/chop/bear is a loose heuristic; extreme skew (e.g. &gt;95% one label) usually means missing yield/VIX alignment or stale macro — investigate the feature pipeline before trusting the model.

## Weekly retrain (scheduler)

- Job id: **`regime_classifier_weekly`** (default **Sun 04:15** server local time; configurable via `chili_regime_classifier_weekly_cron_*`).
- **Training:** rolling **5 years** ending **21 NYSE business days** before “today”, with **warm start** from the latest artifact under `regime_models/` (gitignored).
- **Decode:** incremental from last `regime_snapshot.as_of` through last completed session.
- **Monitor:** new `model_version` hash each retrain; posterior entropy should not collapse to a single state for months; compare Ops heatmap (`/brain` → Regime × scanner Sharpe) week-over-week.

## Label-flip incident

If a retrain suddenly permutes economic meaning (e.g. “bull” days align with known bear markets):

1. Set flag **off** and restart.
2. Inspect `regime_models/` artifact and training window in logs; verify macro yield proxy and VIX series.
3. Do **not** “fix” by editing thresholds inline — follow change discipline (separate PR + runbook note if thresholds or feature spec change).
4. Optional reset: `UPDATE trading_snapshots SET regime = NULL, regime_posterior = NULL;` and truncate or archive `regime_snapshot` after operator sign-off.

## Backfill script

```powershell
conda run -n chili-env python scripts/backfill_regime.py --dry-run
conda run -n chili-env python scripts/backfill_regime.py --commit
```

Dry-run rolls back the session and does not write `regime_models/` artifacts; `--commit` persists snapshots, tags `trading_snapshots` where `bar_start_at` matches, and saves a new artifact.

## Rollback

- **Flag off** — safe immediate rollback path; downstream must handle `NULL` regime.
- **Schema:** use rollback block in `ROADMAP_DEVIATION_003.md` only if removing T2 entirely.
