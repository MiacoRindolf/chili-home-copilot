# Roadmap deviation 002 — CPCV shadow funnel (Q1.T1 follow-up)

## Migration

- **164_cpcv_shadow_eval_log** — append-only `cpcv_shadow_eval_log` plus rollup view `cpcv_shadow_funnel_v` (7-day window). Next free ID was confirmed after **163_cpcv_promotion_gate_evidence** in [`app/migrations.py`](../app/migrations.py).

## Megaprompt note

The original Q1 megaprompt did not specify this table; it was added as the smallest durable way to power per-scanner shadow metrics before Q1.T6. Verify future tasks against the live `MIGRATIONS` tail before assigning IDs.

## Rollback (manual)

```sql
DROP VIEW IF EXISTS cpcv_shadow_funnel_v;
DROP TABLE IF EXISTS cpcv_shadow_eval_log;
```
