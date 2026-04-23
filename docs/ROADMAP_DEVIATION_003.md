# Roadmap deviation 003 — Q1.T2 regime classifier (Gaussian HMM)

## Migration ID

- Megaprompt example used **093**; this repository’s `MIGRATIONS` tail after CPCV work ends at **164**. Q1.T2 ships as **`165_regime_snapshot_and_tagging`** in [`app/migrations.py`](../app/migrations.py).
- Run `.\scripts\verify-migration-ids.ps1` before merge.

## hmmlearn pin

- Megaprompt suggested `hmmlearn==0.3.3`. Verified on **Python 3.11** with **NumPy 2.x** and **scikit-learn 1.8.x** in `chili-env` (import + `GaussianHMM.fit` smoke test). Pin remains **`hmmlearn==0.3.3`** unless a future bump fails that matrix.

## Yield-curve feature (no FRED in repo)

- The megaprompt named **DGS10 − DGS2 (FRED)**. This codebase has **no FRED ingestion**.
- Q1.T2 uses **`trading_macro_regime_snapshots.yield_curve_slope_proxy`** (Phase L.17 ETF-based proxy) joined **by `as_of_date`** for point-in-time yield slope. Dates without a macro row omit yield and are **skipped** in the feature matrix (see runbook).
- If full Treasury term-structure features are required later, add a dedicated ingestion PR (e.g. **Q1.T2a**) rather than bolting FRED into the classifier module.

## Snapshot table name

- ORM **`MarketSnapshot`** maps to table **`trading_snapshots`** (not `trading_snapshot`).

## Rollback (manual)

```sql
DROP INDEX IF EXISTS ix_trading_snapshot_regime;
ALTER TABLE trading_snapshots DROP COLUMN IF EXISTS regime_posterior;
ALTER TABLE trading_snapshots DROP COLUMN IF EXISTS regime;
DROP INDEX IF EXISTS ix_regime_snapshot_model_version;
DROP TABLE IF EXISTS regime_snapshot;
```

(Index `ix_trading_snapshot_regime` is on `(regime, bar_start_at)` where `regime` is not null.)
