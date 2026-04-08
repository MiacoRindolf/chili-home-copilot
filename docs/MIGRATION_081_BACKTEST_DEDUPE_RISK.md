# Migration 081 — global backtest dedupe risk

## What 081 does

In `app/migrations.py`, `_migration_081_graduate_startup_repairs` includes:

```sql
DELETE FROM trading_backtests
WHERE id IN (
  SELECT id FROM (
    SELECT id,
           ROW_NUMBER() OVER (PARTITION BY strategy_name, ticker ORDER BY ran_at DESC) AS rn
    FROM trading_backtests
  ) sub
  WHERE rn > 1
);
```

So for each **`(strategy_name, ticker)`** pair across the **entire** table, only the **latest** row by `ran_at` is kept. There is **no** partition by `scan_pattern_id` or `related_insight_id`.

## Risk

If two different `ScanPattern`s (or two insights) produced backtests that share the same **display strategy name** and **ticker**, **all but one row were deleted** for that pair. The survivor is arbitrary with respect to pattern lineage — whichever row had the newest `ran_at` wins.

This can remove **valid cross-pattern history** and make evidence for one pattern silently disappear.

## Assessment (recommendation)

1. **Do not re-run** this DELETE logic with the same global partition. If a future cleanup is needed, scope it, for example:
   - `PARTITION BY related_insight_id, ticker, strategy_name`, or
   - `PARTITION BY scan_pattern_id, ticker, strategy_name` (where `scan_pattern_id` IS NOT NULL), plus a separate policy for NULL pattern rows.

2. **Audit current state** (staging/prod):

   ```sql
   SELECT strategy_name, ticker, COUNT(*) AS c
   FROM trading_backtests
   GROUP BY strategy_name, ticker
   HAVING COUNT(*) > 1
   ORDER BY c DESC
   LIMIT 100;
   ```

   If counts are high, compare with backups taken before 081 first ran (if available) to estimate data loss.

3. **Prevent recurrence:** consider a **partial unique index** or application upsert key aligned with the grain you want, for example uniqueness on `(scan_pattern_id, ticker, strategy_name)` where `scan_pattern_id IS NOT NULL`, **after** product agreement (may conflict with intentional re-runs at different `ran_at`).

4. **Operational note:** migration 081 runs **once** per environment (schema_version). New duplicates can still accumulate until a **scoped** dedupe or retention policy is implemented.

## Related docs

- [TRADING_BACKTEST_DB_AUDIT.md](./TRADING_BACKTEST_DB_AUDIT.md) — duplicate and linkage queries.
