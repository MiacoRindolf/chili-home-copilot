# NEXT_TASK: f-position-identity-phase-5h-production-rename-brief-and-preflight

STATUS: PENDING

## Goal

Prepare the final production migration brief and preflight for the physical
rename from `trading_trades` to `trading_management_envelopes`.

Do not mix semantic cleanup into this phase. The only acceptable production
change is the compatibility-first physical rename that Phase 5G proved in a
transactional dry run.

## Current Gate State

- Phase 5E soak: `READY_FOR_RENAME_BRIEF`
- Fresh post-mig-275 data represented:
  - fresh decisions: 3
  - fresh envelopes: 3
  - fresh closes: 7
- Hard linkage issues: 0
- Fresh close mismatches: 0
- 30d attribution drift: $0.0000
- Phase 5F audit:
  - runtime files with literal `trading_trades`: 35
  - runtime files with `Trade` ORM-symbol references: 101
- Phase 5G dry-run on `chili_test`: green
  - old SQL through `trading_trades` compatibility view: green
  - new SQL through `trading_management_envelopes` base table: green
  - SQLAlchemy `Trade` flush through compatibility view: green
  - Phase 5B view survived: green
  - rollback restored original schema: green
- `STAGING_DATABASE_URL` is not configured locally; staging rehearsal did not
  run.

## Tasks

1. Run final preflight:
   - rerun `python scripts\d-phase5g-rename-dry-run.py`
   - rerun Phase 5E reporting compare against live read models
   - verify no open broker envelopes missing `position_id`
   - verify no new hard linkage issues
2. If a staging URL becomes available, run:

   ```powershell
   $env:PHASE5G_DRY_RUN_DATABASE_URL = "<staging-url>"
   python scripts\d-phase5g-rename-dry-run.py --allow-staging
   ```

3. Write the production migration exactly as:

   ```sql
   DROP VIEW IF EXISTS trading_management_envelopes;
   ALTER TABLE trading_trades RENAME TO trading_management_envelopes;
   CREATE VIEW trading_trades AS
   SELECT * FROM trading_management_envelopes;
   ```

4. After deploy, smoke:
   - `SELECT COUNT(*) FROM trading_trades`
   - `SELECT COUNT(*) FROM trading_management_envelopes`
   - old raw SQL through `trading_trades`
   - new raw SQL through `trading_management_envelopes`
   - SQLAlchemy `Trade` flush
   - `/api/trading/attribution/live-vs-research?phase5b_compare=true`
   - autotrader, Coinbase sync, Robinhood broker sync, bracket reconcile, stop
     engine logs for tracebacks

## Acceptance

- Final preflight remains green.
- Production migration is the tiny compatibility rename only.
- No columns, constraints, indexes, close-reason strings, `trade_id`, or
  `source_trade_id` fields are dropped.
- Post-deploy smoke shows old and new names both usable.
- Phase 5E attribution compare remains clean after the rename.

## Rollback

```sql
DROP VIEW IF EXISTS trading_trades;
ALTER TABLE trading_management_envelopes RENAME TO trading_trades;
CREATE VIEW trading_management_envelopes AS
SELECT * FROM trading_trades;
```

Then force-recreate affected workers and rerun the Phase 5E compare.

## References

- `docs/STRATEGY/CC_REPORTS/2026-05-28_f-position-identity-phase-5e-soak-closeout.md`
- `docs/STRATEGY/CC_REPORTS/2026-05-28_f-position-identity-phase-5f-rename-audit.md`
- `docs/STRATEGY/CC_REPORTS/2026-05-28_f-position-identity-phase-5g-physical-rename-dry-run.md`
- `scripts/d-phase5f-rename-audit.py`
- `scripts/d-phase5g-rename-dry-run.py`
