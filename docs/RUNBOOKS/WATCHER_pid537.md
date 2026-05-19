# Watcher runbook — pid 537 + Tier A health

## What this watches

The Windows scheduled task `CHILI-pid537-watcher` fires **daily at 18:00 local time** and dispatches `scripts/dispatch-pid537-watcher.ps1` via the dev daemon. The probe (`scripts/d-pid537-watcher.py`) is read-only against the chili DB and emits a structured verdict.

### The two things being watched

1. **Pid 537 maturation.** Operator chose Path A on 2026-05-18 to promote 537 to `pilot_promoted` despite the brain's CPCV evidence being weak (Sharpe 0.626, below the 1.0 floor) and the realized sample being effectively ~3 distinct ideas over 10 days. The watcher tracks the post-promote realized stats until n hits **15** and renders a verdict.

2. **Tier A protection regression.** The payoff-ratio gate (commit `23bde18`) is load-bearing for both pattern 585 (the system's only proven alpha) and pid 537. If the protection is silently disabled — e.g. someone flips `CHILI_PATTERN_DEMOTE_PAYOFF_RATIO_FLOOR=1e9` in `.env` and force-recreates workers — the watcher flags REGRESSION.

## Output schema

`scripts/dispatch-pid537-watcher-out.txt` is overwritten on each run. The machine-readable header:

```
VERDICT_STATUS=<one of: IN_FLIGHT, COMPLETE_POSITIVE, COMPLETE_NEGATIVE, ALERT, REGRESSION>
VERDICT_REASON=<short text>
PID_537_N=<int>
PID_537_WR=<float or NULL>
PID_537_PAYOFF=<float or NULL>
PID_537_STAGE=<string>
PID_585_STAGE=<string>
TIER_A_PROTECTED=<int>
TIER_A_SCORED=<int>
TIER_A_WITH_N5=<int>
```

Followed by `--- details ---` and the full pid 537 row state.

### What each VERDICT_STATUS means

| Status | When | Operator action |
|---|---|---|
| **IN_FLIGHT** | Healthy, n<15, no degradation, no regression | None. Read the numbers if curious. |
| **COMPLETE_POSITIVE** | n≥15 AND WR≥0.50 AND payoff≥3.0 | Elevate pid 537 from `shadow_promoted` to `promoted` if desired. Disable watcher (job done). |
| **COMPLETE_NEGATIVE** | n≥15 AND (WR<0.50 OR payoff<3.0) | Re-demote pid 537 to `challenged`. The original promotion was thin-sample artifact. Disable watcher. |
| **ALERT** | Mid-flight degradation OR pid 537 unexpectedly demoted | Investigate. The brain may have demoted via a path the Tier A protection doesn't cover. |
| **REGRESSION** | Tier A protection count is 0 OR composite floor isn't firing | URGENT. The protection may have been silently disabled. Check `.env` for `CHILI_PATTERN_DEMOTE_PAYOFF_RATIO_FLOOR` and `CHILI_COMPOSITE_MIN_REALIZED_TRADES`. Rollback path: `git revert 23bde18` or restore correct env values. |

## Operating the watcher

### Read current verdict

```powershell
Get-Content C:\dev\chili-home-copilot\scripts\dispatch-pid537-watcher-out.txt -Head 15
```

### Manually trigger between scheduled runs

```powershell
Start-ScheduledTask -TaskName 'CHILI-pid537-watcher'
# Wait ~10 seconds, then read the output file (above).
```

Or via the dev daemon (which is the path the scheduled task itself uses):

```
TIMEOUT=60s .\scripts\dispatch-pid537-watcher.ps1
```

### Disable

```powershell
Disable-ScheduledTask -TaskName 'CHILI-pid537-watcher'
# Or remove entirely:
Unregister-ScheduledTask -TaskName 'CHILI-pid537-watcher' -Confirm:$false
```

### Verify it's still registered

```powershell
Get-ScheduledTask -TaskName 'CHILI-pid537-watcher' | Select-Object TaskName, State, @{N='NextRun';E={(Get-ScheduledTaskInfo $_).NextRunTime}}
```

### Re-install (idempotent)

```powershell
.\scripts\setup-pid537-watcher-windows-task.ps1
```

## How the pipeline works under the hood

```
Windows Task Scheduler                       Dev Daemon                Probe
──────────────────────                       ──────────                ──────
[18:00 daily]
     │
     ├─→ powershell.exe writes line to scripts/_claude_pending.txt
     │
     │                                 ┌── polls scripts/_claude_pending.txt every 2s
     │                                 │
     │                                 ├── sees line, atomic-consumes to lock file
     │                                 │
     │                                 ├── runs `TIMEOUT=60s .\scripts\dispatch-pid537-watcher.ps1`
     │                                 │
     │                                 │            ┌── conda run -n chili-env python scripts/d-pid537-watcher.py
     │                                 │            │
     │                                 │            ├── reads chili DB (read-only)
     │                                 │            │
     │                                 │            ├── emits VERDICT_* lines + details
     │                                 │            │
     │                                 │            └── exit code 0/2/3
     │                                 │
     │                                 └── writes output to scripts/dispatch-pid537-watcher-out.txt
     │
     └─→ next fire: tomorrow 18:00 local
```

## When to retire the watcher

Disable after one of:

- Pid 537 reaches n=15 AND a verdict (POSITIVE or NEGATIVE) is acted on.
- Operator decides the watch is no longer useful (e.g. the brain has decisively moved 537 to `promoted` or `retired` and there's nothing left to learn).
- Tier A architecture changes meaningfully (e.g. payoff_ratio gate replaced by something else); rewrite the watcher first.

## Files

| Path | Purpose |
|---|---|
| `scripts/d-pid537-watcher.py` | Read-only probe + verdict logic |
| `scripts/dispatch-pid537-watcher.ps1` | Daemon dispatch wrapper |
| `scripts/dispatch-pid537-watcher-out.txt` | Latest output (overwritten each run) |
| `scripts/setup-pid537-watcher-windows-task.ps1` | Idempotent installer for the Windows task |
| `scripts/dispatch-setup-pid537-watcher.ps1` | Dispatches the installer via daemon |
| `scripts/dispatch-trigger-pid537-watcher.ps1` | Manually fires the Windows task via daemon |
| `docs/runbooks/WATCHER_pid537.md` | This file |

## Companion memory entries

- `project_2026_05_18_pid537_path_a.md` — full backstory of the Path A decision
- `project_2026_05_18_tier_a_eval_fix.md` — Tier A architecture
- `reference_payoff_ratio_protection.md` — how the demote-protection gate works
