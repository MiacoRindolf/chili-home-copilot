# WATCHER_phase5e_reporting_soak

The Windows scheduled task `CHILI-phase5e-reporting-soak-probe` runs daily at
18:20 local time. It executes:

```text
scripts/dispatch-phase5e-reporting-soak-probe.ps1
```

Latest output:

```text
scripts/dispatch-phase5e-reporting-soak-probe-out.txt
```

Machine-readable header:

```text
VERDICT_STATUS=<IN_FLIGHT|READY_FOR_RENAME_BRIEF|BLOCKED_LINKAGE|BLOCKED_DRIFT|ALERT>
VERDICT_REASON=<short reason>
```

## Verdicts

- `IN_FLIGHT`: Phase 5D is clean, but there are not yet fresh post-mig-275
  decisions/envelopes/closes.
- `READY_FOR_RENAME_BRIEF`: fresh post-mig-275 entries and closes exist, hard
  linkage is clean, and attribution drift is zero. Prepare the rename design
  brief; do not automatically rename.
- `BLOCKED_LINKAGE`: hard linkage issue reappeared in the Phase 5B view.
- `BLOCKED_DRIFT`: decision-vs-envelope pattern attribution drift reappeared.
- `ALERT`: the probe failed to connect or query.

## Commands

Manual run:

```powershell
.\scripts\dispatch-phase5e-reporting-soak-probe.ps1
Get-Content .\scripts\dispatch-phase5e-reporting-soak-probe-out.txt -Head 20
```

Install or refresh the scheduled task:

```powershell
.\scripts\setup-phase5e-reporting-soak-windows-task.ps1
```

Disable when Phase 5E is done:

```powershell
Disable-ScheduledTask -TaskName 'CHILI-phase5e-reporting-soak-probe'
```
