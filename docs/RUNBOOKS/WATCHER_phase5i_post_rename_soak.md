# Phase 5I Post-Rename Soak Watcher

## Purpose

Monitor the first organic trading flow after Phase 5H physically renamed
`trading_trades` to `trading_management_envelopes` and left `trading_trades` as
a compatibility view.

The watcher closes the soak only after fresh post-mig-283 entries and closes
appear in the Phase 5B/5C read model without linkage or attribution drift.

## Scheduled Task

```text
CHILI-phase5i-post-rename-soak-probe
```

Cadence:

```text
every 30 minutes for 14 days
```

Output:

```text
scripts/dispatch-phase5i-post-rename-soak-probe-out.txt
```

## Manual Run

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\dispatch-phase5i-post-rename-soak-probe.ps1
Get-Content scripts\dispatch-phase5i-post-rename-soak-probe-out.txt -TotalCount 80
```

## Verdicts

```text
IN_FLIGHT
```

Awaiting at least one fresh decision, envelope, and close after mig 283.

```text
COMPLETE_POSITIVE
```

Fresh post-rename data is present and clean. Write the Phase 5I closeout and
queue Phase 5J selective reader cleanup.

```text
REGRESSION_SCHEMA
```

Unexpected relation kinds. Expected:

```text
trading_management_envelopes = r
trading_trades = v
trading_phase5b_decision_envelope_position = v
```

```text
BLOCKED_LINKAGE
```

Hard linkage issues reappeared in the Phase 5B view.

```text
BLOCKED_DRIFT
```

Decision-pattern attribution no longer matches envelope-pattern attribution.

```text
ALERT
```

The probe failed to connect or query.

## Extra Log Scan

The dispatcher also scans recent Docker logs for schema-specific errors:

```text
NoReferencedTableError
UndefinedTable
relation .*trading_
PendingRollbackError
cannot truncate
not a table
```

`LOG_SCHEMA_ERRORS=0` is expected.

## Rollback

Only use this if a rename-specific production issue appears:

```sql
DROP VIEW IF EXISTS trading_trades;
ALTER TABLE trading_management_envelopes RENAME TO trading_trades;
CREATE VIEW trading_management_envelopes AS
SELECT * FROM trading_trades;
DELETE FROM schema_version
 WHERE version_id = '283_position_identity_phase5h_physical_rename';
```

Then force-recreate affected workers and rerun the probe.
