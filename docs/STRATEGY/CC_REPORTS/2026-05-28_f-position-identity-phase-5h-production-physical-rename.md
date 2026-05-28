# f-position-identity-phase-5h-production-physical-rename

Date: 2026-05-28
Status: SHIPPED
Branch: `main`

## Executive Summary

Phase 5H executed the compatibility-first production physical rename:

- physical base table: `trading_management_envelopes`
- legacy compatibility view: `trading_trades`

No columns, constraints, close-reason strings, `trade_id` fields, or
`source_trade_id` fields were dropped. The Python ORM class remains `Trade` and
continues to bind to `trading_trades`, now a simple updatable view.

## Preflight

Final gates before the live migration:

```text
Phase 5G dry-run on chili_test: ok=true
Phase 5E live soak: READY_FOR_RENAME_BRIEF
Fresh decisions: 3
Fresh envelopes: 3
Fresh closes: 7
Fresh close mismatches: 0
Hard linkage issues: 0
30d mismatched rows: 0
30d mismatched pnl: 0.0000
```

`STAGING_DATABASE_URL` was not configured locally, so staging rehearsal remained
unavailable. The `_test` dry run plus live read-model soak were the gating
evidence.

## Migration

Migration:

```text
283_position_identity_phase5h_physical_rename
```

DDL shape:

```sql
DROP VIEW IF EXISTS trading_management_envelopes;
ALTER TABLE trading_trades RENAME TO trading_management_envelopes;
CREATE VIEW trading_trades AS
SELECT * FROM trading_management_envelopes;
```

The migration is idempotent for the already-renamed shape
(`trading_trades` = view, `trading_management_envelopes` = table).

## Live Verification

Relation state after migration:

```text
trading_management_envelopes: r
trading_phase5b_decision_envelope_position: v
trading_trades: v
```

Applied schema versions:

```text
282_autotrader_imminent_selector_indexes: 2026-05-28 17:06:13 UTC
283_position_identity_phase5h_physical_rename: 2026-05-28 17:06:51 UTC
```

Live rollback smoke, inside one transaction:

```text
old SQL insert via trading_trades view: visible in trading_management_envelopes
new SQL insert via trading_management_envelopes table: visible in trading_trades
SQLAlchemy Trade flush through trading_trades view: visible in base table
Phase 5A trigger created decisions: 3/3
Phase 5A trigger linked envelopes: 3/3
row counts before/after rollback: 705 -> 705
```

Phase 5E compare after rename:

```text
VERDICT_STATUS=READY_FOR_RENAME_BRIEF
FRESH_DECISIONS=3
FRESH_ENVELOPES=3
FRESH_CLOSES=7
FRESH_CLOSE_MISMATCHES=0
HARD_LINKAGE_ISSUES=0
CLOSED_ROWS=310
MISMATCHED_ROWS=0
MISMATCHED_PNL=0.0000
```

Container log scan found no schema-specific errors:

```text
NoReferencedTableError: 0
UndefinedTable / relation trading_* missing: 0
PendingRollbackError from rename: 0
cannot truncate view/table: 0
```

Unrelated provider noise remains in logs (Coinbase 429s, yfinance 401s), but no
rename-path errors appeared.

## Tests

```text
powershell -ExecutionPolicy Bypass -File scripts\verify-migration-ids.ps1
python -m py_compile app\migrations.py tests\conftest.py tests\test_position_identity_phase5h.py scripts\d-phase5g-rename-dry-run.py
python scripts\d-phase5g-rename-dry-run.py
pytest tests\test_position_identity_phase5h.py tests\test_position_identity_phase5a.py tests\test_position_sizer_writer.py::TestModeGate::test_off_mode_is_noop_and_returns_none -vv -p no:asyncio
```

Result:

```text
Migration IDs: PASS
Phase 5G post-rename smoke: ok=true
Pytest: 10 passed
```

## Architect/Data-Science Read

The rename is intentionally boring: all live behavior still flows through the
same columns and most runtime code still uses the old `Trade` semantic surface.
The value is architectural clarity: the database now names the mutable trade row
for what it actually is, a management envelope, while the immutable decision
layer and broker-authoritative position layer remain separate.

The next value step is not more DDL. It is a short post-rename soak, then a
selective reader cleanup where new analytics/reporting code starts preferring
`trading_management_envelopes` directly and old `trading_trades` references stay
as compatibility debt until they are cheap to remove.

## Rollback

If a rename-specific production issue appears:

```sql
DROP VIEW IF EXISTS trading_trades;
ALTER TABLE trading_management_envelopes RENAME TO trading_trades;
CREATE VIEW trading_management_envelopes AS
SELECT * FROM trading_trades;
DELETE FROM schema_version
 WHERE version_id = '283_position_identity_phase5h_physical_rename';
```

Then force-recreate affected workers and rerun the Phase 5E compare.
