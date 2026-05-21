# Trading Storage Repair Runbook

Last updated: 2026-05-21

## What Was Repaired

- Migrations `259` through `261` repair the ORM/Postgres JSONB contract for:
  - `trading_trades.indicator_snapshot`
  - `trading_backtests.params`
  - `trading_proposals.signals_json`
  - `trading_proposals.indicator_json`
  - `trading_scans.indicator_data`
  - `trading_hypotheses.last_result_json`
- Original text payloads are preserved in `trading_jsonb_contract_legacy_text`.
- Double-encoded JSONB strings are unwrapped by migration `261`.
- Audit rows live in:
  - `trading_jsonb_contract_repair_audit`
  - `trading_jsonb_string_unwrap_audit`

## Remaining Storage Work

The startup migration intentionally defers heavy retention indexes on large
tables. Build them during a quiet maintenance window:

```powershell
python scripts\maintain_trading_storage.py --create-indexes --execute --target exit-parity
```

Then prune in bounded batches:

```powershell
python scripts\maintain_trading_storage.py --execute --target exit-parity --max-batches 20 --vacuum-analyze
python scripts\maintain_trading_storage.py --execute --target fast-orderbook --max-batches 20 --vacuum-analyze
```

Dry-run is the default. Without `--execute`, the script reports planned work and
does not mutate the database.

## Verification Queries

```sql
SELECT version_id, applied_at
FROM schema_version
WHERE version_id >= '259'
ORDER BY version_id;

SELECT table_name, column_name, rows_seen, sanitized_constant_rows, legacy_wrapped_rows
FROM trading_jsonb_contract_repair_audit
ORDER BY table_name, column_name;

SELECT table_name, column_name, eligible_string_rows_before,
       eligible_string_rows_after, unwrapped_rows
FROM trading_jsonb_string_unwrap_audit
ORDER BY table_name, column_name;
```
