# f-position-identity-phase-5g-physical-rename-dry-run

Date: 2026-05-28
Status: SHIPPED
Branch: `main`

## Executive Summary

Phase 5G built and ran a transactional dry-run for the physical rename from
`trading_trades` to `trading_management_envelopes`.

The result is green on `chili_test`: the compatibility-first rename shape works
for old raw SQL, new raw SQL, and a real SQLAlchemy `Trade` flush. The entire
DDL/write test rolls back, leaving the database schema and rows unchanged.

No production rename was executed.

## Tooling

New dry-run script:

```text
scripts/d-phase5g-rename-dry-run.py
```

Default safety behavior:

- Uses `PHASE5G_DRY_RUN_DATABASE_URL`, `TEST_DATABASE_URL`, or `DATABASE_URL`.
- Refuses to run unless the database name ends in `_test`.
- `--allow-staging` permits an explicitly isolated staging database.
- Never commits the rename transaction.

## Dry-Run Shape

Inside one transaction:

```sql
DROP VIEW IF EXISTS trading_management_envelopes;
ALTER TABLE trading_trades RENAME TO trading_management_envelopes;
CREATE VIEW trading_trades AS
SELECT * FROM trading_management_envelopes;
```

Then the script exercises:

- old SQL insert via `trading_trades` compatibility view
- new SQL insert via `trading_management_envelopes` base table
- SQLAlchemy `Trade` ORM flush through the legacy `trading_trades` mapping
- Phase 5B read-model survival
- rollback to the original schema

## Verification

Run:

```powershell
python scripts\d-phase5g-rename-dry-run.py
```

Result on `chili_test`:

```text
ok: true
before.trading_trades: r
before.trading_management_envelopes: v
after_rename.trading_trades: v
after_rename.trading_management_envelopes: r
old_sql_inserted_through_trading_trades_view: true
new_sql_inserted_through_management_envelopes_table: true
orm_trade_flush_through_trading_trades_view: true
phase5b_view_survived: true
phase5b_hard_issues_unchanged: true
rollback_state.trading_trades: r
rollback_state.trading_management_envelopes: v
```

`STAGING_DATABASE_URL` is not configured locally, so the production-shaped
staging rehearsal did not run in this pass.

## Architect/Data-Science Read

The key unknown was not whether PostgreSQL could rename the table. It was
whether the compatibility strategy preserved the old semantic surface while the
new name became the physical base table.

This dry-run answers yes for the critical low-level mechanics:

- A simple `SELECT *` compatibility view is auto-updatable for this table shape.
- Direct legacy SQL can keep writing through `trading_trades`.
- New SQL can use `trading_management_envelopes`.
- The existing `Trade` ORM class can flush through the compatibility view.
- Phase 5B read-model views survive the rename because their dependency follows
  the renamed base relation.

The remaining production question is operational, not conceptual: run the same
dry-run against a production-shaped staging clone if available, then ship a tiny
production migration with a smoke plan and an immediate rollback command.

## Acceptance

- No production physical rename: yes.
- Dry-run migration succeeds on `_test`: yes.
- Compatibility view keeps legacy SQL alive: yes.
- SQLAlchemy metadata/flush clean: yes.
- Phase 5B read-model still queryable after rename: yes.
- Transaction rollback restores original schema: yes.

## Follow-Up

Proceed to Phase 5H: write and execute the production migration only after one
final preflight. The production migration should do exactly the proven shape and
nothing semantic:

1. drop the old Phase 5B `trading_management_envelopes` view
2. rename base `trading_trades` to `trading_management_envelopes`
3. create `trading_trades` compatibility view over the renamed base table
4. run the Phase 5E reporting compare and selected writer/reader smoke checks

Rollback:

```sql
DROP VIEW IF EXISTS trading_trades;
ALTER TABLE trading_management_envelopes RENAME TO trading_trades;
CREATE VIEW trading_management_envelopes AS
SELECT * FROM trading_trades;
```
