# NEXT_TASK: f-position-identity-phase-5g-physical-rename-dry-run

STATUS: PENDING

## Goal

Prototype the physical rename path in a test/dry-run environment before any
production migration.

Do not rename production `trading_trades` yet. Phase 5E proved the read model is
clean; Phase 5F proved the rename surface is broad enough that the safe next
move is a compatibility-first dry run.

## Current Gate State

- Phase 5E soak: `READY_FOR_RENAME_BRIEF`
- Fresh post-mig-275 data represented:
  - fresh decisions: 3
  - fresh envelopes: 3
  - fresh closes: 7
- Hard linkage issues: 0
- Fresh close mismatches: 0
- 30d attribution mismatched rows: 0
- 30d attribution drift: $0.0000
- Phase 5F audit:
  - runtime files with literal `trading_trades`: 35
  - runtime files with `Trade` ORM-symbol references: 101

## Tasks

1. Create a test-only dry-run migration prototype:
   - `ALTER TABLE trading_trades RENAME TO trading_management_envelopes`
   - `CREATE VIEW trading_trades AS SELECT * FROM trading_management_envelopes`
   - Preserve all existing columns, constraints, indexes, and close-reason
     strings.
   - Do not drop `trade_id`, `source_trade_id`, or any compatibility fields.
2. Keep the Python `Trade` ORM class temporarily and prove SQLAlchemy metadata
   import/flush does not raise `NoReferencedTableError` or FK-resolution errors.
3. Add or run tests that prove:
   - old raw SQL against `trading_trades` still works through the view
   - new raw SQL against `trading_management_envelopes` works
   - Phase 5B/5C/5E attribution compare remains clean
4. Smoke selected writer/reader paths in dry-run:
   - autotrader entry creation
   - Coinbase sync
   - Robinhood broker sync
   - bracket writer/reconcile
   - stop engine
   - attribution endpoint with `phase5b_compare=true`
5. If and only if the dry run is green, write Phase 5H production migration
   brief with an exact migration, rollback SQL, and a post-deploy smoke plan.

## Acceptance

- No production physical rename in this task.
- Dry-run migration succeeds on a test database.
- Compatibility view keeps legacy SQL readers alive.
- SQLAlchemy metadata and at least one insert/update flush are clean.
- Phase 5E compare remains clean after the dry-run prototype.

## Rollback

Dry-run rollback SQL:

```sql
DROP VIEW IF EXISTS trading_trades;
ALTER TABLE trading_management_envelopes RENAME TO trading_trades;
```

Production rollback is not applicable until a later Phase 5H task is explicitly
approved.

## References

- `docs/STRATEGY/CC_REPORTS/2026-05-28_f-position-identity-phase-5e-soak-closeout.md`
- `docs/STRATEGY/CC_REPORTS/2026-05-28_f-position-identity-phase-5f-rename-audit.md`
- `scripts/d-phase5f-rename-audit.py`
- Migrations: 274, 275
