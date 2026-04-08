# Trading backtest DB audit playbook

Read-only checks for `trading_backtests`, `trading_insights`, and `scan_patterns` before/after scale repair, linkage alignment (migration 084), and aggregate recompute.

Run with a **read-only** PostgreSQL role where possible. Save row counts and sample PKs for your closeout notes.

## Preconditions

- Application code should normalize writes at `save_backtest` (fraction in DB) before you rely on “no percent in DB” gates.
- Order of operations: **audit → code deploy → migrations 083/084 → recompute script → regeneration (explicit pattern ids) → UI verify**.

## 1. Win rate scale (`trading_backtests`)

**Percent left in DB (should be 0 after migration 083):**

```sql
SELECT COUNT(*) AS n_win_rate_gt_1
FROM trading_backtests
WHERE win_rate IS NOT NULL AND win_rate > 1.0;

SELECT COUNT(*) AS n_oos_win_rate_gt_1
FROM trading_backtests
WHERE oos_win_rate IS NOT NULL AND oos_win_rate > 1.0;
```

**Out of range (violates intended [0,1] contract):**

```sql
SELECT COUNT(*) FROM trading_backtests
WHERE win_rate IS NOT NULL AND (win_rate < 0 OR win_rate > 1);

SELECT COUNT(*) FROM trading_backtests
WHERE oos_win_rate IS NOT NULL AND (oos_win_rate < 0 OR oos_win_rate > 1);
```

## 2. Linkage: backtest vs insight (`084` preview)

**Rows migration 084 would align** (`TradingInsight` is authoritative):

```sql
SELECT COUNT(*) AS n_align_084
FROM trading_backtests bt
JOIN trading_insights ti ON ti.id = bt.related_insight_id
WHERE ti.scan_pattern_id IS NOT NULL
  AND (bt.scan_pattern_id IS NULL OR bt.scan_pattern_id != ti.scan_pattern_id);
```

**Mismatch sample (both non-null, different):**

```sql
SELECT bt.id, bt.ticker, bt.scan_pattern_id, ti.id AS insight_id, ti.scan_pattern_id AS insight_pattern_id
FROM trading_backtests bt
JOIN trading_insights ti ON ti.id = bt.related_insight_id
WHERE bt.scan_pattern_id IS NOT NULL
  AND ti.scan_pattern_id IS NOT NULL
  AND bt.scan_pattern_id != ti.scan_pattern_id
LIMIT 50;
```

## 3. Orphans and FK sanity

**Backtests pointing at missing insights** (if FK is SET NULL, may be empty):

```sql
SELECT COUNT(*) FROM trading_backtests bt
WHERE bt.related_insight_id IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM trading_insights ti WHERE ti.id = bt.related_insight_id);
```

**Backtests with `scan_pattern_id` not in `scan_patterns`:**

```sql
SELECT COUNT(*) FROM trading_backtests bt
WHERE bt.scan_pattern_id IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM scan_patterns sp WHERE sp.id = bt.scan_pattern_id);
```

## 4. Strategy name vs pattern name (semantic drift)

Heuristic only — `dynamic_pattern` and composable runs may legitimately differ; review samples manually.

```sql
SELECT COUNT(*) AS n_name_mismatch
FROM trading_backtests bt
JOIN scan_patterns sp ON sp.id = bt.scan_pattern_id
WHERE bt.scan_pattern_id IS NOT NULL
  AND bt.strategy_name IS DISTINCT FROM sp.name;

SELECT bt.id, bt.ticker, bt.strategy_name, sp.name AS pattern_name
FROM trading_backtests bt
JOIN scan_patterns sp ON sp.id = bt.scan_pattern_id
WHERE bt.scan_pattern_id IS NOT NULL
  AND bt.strategy_name IS DISTINCT FROM sp.name
LIMIT 30;
```

## 5. Duplicates (081 global dedupe risk)

Current duplicate groups by `(strategy_name, ticker)` — see [MIGRATION_081_BACKTEST_DEDUPE_RISK.md](./MIGRATION_081_BACKTEST_DEDUPE_RISK.md).

```sql
SELECT strategy_name, ticker, COUNT(*) AS c
FROM trading_backtests
GROUP BY strategy_name, ticker
HAVING COUNT(*) > 1
ORDER BY c DESC
LIMIT 50;
```

## 6. Pattern aggregate drift

**`backtest_count` vs actual rows:**

```sql
SELECT sp.id, sp.name, sp.backtest_count,
       (SELECT COUNT(*) FROM trading_backtests b WHERE b.scan_pattern_id = sp.id) AS actual_bt
FROM scan_patterns sp
WHERE sp.backtest_count IS DISTINCT FROM (
  SELECT COUNT(*) FROM trading_backtests b WHERE b.scan_pattern_id = sp.id
)
LIMIT 50;
```

After checks, run:

```powershell
conda activate chili-env
python scripts/recompute_pattern_stats.py --insights-too
```

## 7. Operational scripts (repo)

| Script | Role |
|--------|------|
| `scripts/diagnose_backtest_pattern_alignment.py` | Quick ORM counts + mismatch samples |
| `scripts/recompute_pattern_stats.py` | Replay migration 072-style aggregates |
| `scripts/regenerate_pattern_backtests.py` | Repopulate evidence for explicit `--pattern-ids` |

## 8. Staging / production checklist

1. Run sections 1–6 on **staging**; record counts in ticket or runbook.
2. Deploy app + run migrations through **084**.
3. Re-run sections 1–2 and 6; expect scale and alignment gates clean.
4. `recompute_pattern_stats.py --insights-too`.
5. For each pattern touched by DELETE/NULL repair: `regenerate_pattern_backtests.py --pattern-ids ...` (batched).
6. Spot-check Brain Pattern Evidence and Trading best-ideas backtest cards.
