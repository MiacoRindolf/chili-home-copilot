# NEXT_TASK: f-position-identity-phase-5i-post-rename-soak

STATUS: PENDING

## Goal

Soak the Phase 5H physical rename after production DDL.

The compatibility view must stay in place. This task is observation plus
selective cleanup planning, not another destructive schema change.

## Current Gate State

- Phase 5H migration applied: `283_position_identity_phase5h_physical_rename`
- Physical base table: `trading_management_envelopes` (`relkind='r'`)
- Legacy compatibility view: `trading_trades` (`relkind='v'`)
- Phase 5E compare after rename: `READY_FOR_RENAME_BRIEF`
- Fresh decisions: 3
- Fresh envelopes: 3
- Fresh closes: 7
- Hard linkage issues: 0
- 30d attribution mismatched rows: 0
- 30d attribution drift: $0.0000
- Live rollback smoke:
  - old SQL through `trading_trades`: green
  - new SQL through `trading_management_envelopes`: green
  - SQLAlchemy `Trade` flush through compatibility view: green
  - Phase 5A trigger created/linked 3/3 decisions: green
  - row count before/after rollback: 705 -> 705
- Schema-specific log scan: no rename-path errors.
- Phase 5I watcher installed: `CHILI-phase5i-post-rename-soak-probe`
  - cadence: every 30 minutes for 14 days
  - output: `scripts/dispatch-phase5i-post-rename-soak-probe-out.txt`
  - latest manual run: `IN_FLIGHT`, 0 fresh decisions/envelopes/closes, 0
    hard linkage issues, 0 schema-specific log errors

## Tasks

1. Let at least one fresh entry and one fresh close occur after mig 283. The
   scheduled watcher now checks this automatically.
2. Rerun manually when needed:

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\dispatch-phase5i-post-rename-soak-probe.ps1
   ```

3. Verify:
   - `HARD_LINKAGE_ISSUES=0`
   - `FRESH_CLOSE_MISMATCHES=0`
   - `MISMATCHED_ROWS=0`
   - `MISMATCHED_PNL=0.0000`
4. Scan worker logs for schema-specific errors:
   - `NoReferencedTableError`
   - `UndefinedTable`
   - `relation trading_* does not exist`
   - `PendingRollbackError`
   - `cannot truncate`
5. If clean, write Phase 5I closeout and queue Phase 5J selective reader cleanup:
   - Prefer `trading_management_envelopes` in new analytics/reporting SQL.
   - Keep `trading_trades` compatibility view.
   - Do not rename the Python `Trade` class yet.

## Acceptance

- Fresh post-mig-283 entry/close represented in read models.
- Phase 5E compare remains clean.
- No schema-specific worker errors.
- Compatibility view still works.
- No live trading behavior changed.

## Rollback

Only if a rename-specific production issue appears:

```sql
DROP VIEW IF EXISTS trading_trades;
ALTER TABLE trading_management_envelopes RENAME TO trading_trades;
CREATE VIEW trading_management_envelopes AS
SELECT * FROM trading_trades;
DELETE FROM schema_version
 WHERE version_id = '283_position_identity_phase5h_physical_rename';
```

Then force-recreate affected workers and rerun the Phase 5E compare.

## References

- `docs/STRATEGY/CC_REPORTS/2026-05-28_f-position-identity-phase-5h-production-physical-rename.md`
- `scripts/d-phase5e-reporting-soak-probe.py`
- `scripts/d-phase5g-rename-dry-run.py`
- `scripts/d-phase5i-post-rename-soak-probe.py`
- `docs/RUNBOOKS/WATCHER_phase5i_post_rename_soak.md`
