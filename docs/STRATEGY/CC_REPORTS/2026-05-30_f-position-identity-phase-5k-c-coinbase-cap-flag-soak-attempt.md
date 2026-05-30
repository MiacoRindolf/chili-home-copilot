# f-position-identity-phase-5k-c-coinbase-cap-flag-soak-attempt

## Summary

Phase 5K-C was initially attempted and rolled back, then retried successfully
after the local runtime root cause was isolated.

The live flag is now enabled in `.env` and visible inside the autotrader
container:

```text
CHILI_PHASE5K_COINBASE_CAP_USE_ENVELOPES=true
```

The Coinbase venue-cap reader is now using the physical
`trading_management_envelopes` base table instead of the legacy
`trading_trades` compatibility view.

## Pre-Flip Evidence

Before the flag flip:

```text
Phase 5K-A parity probe: COMPLETE_POSITIVE
PARITY_CHECKS=6
PARITY_MISMATCHES=0

Phase 5I post-rename probe: COMPLETE_POSITIVE
FRESH_DECISIONS=20
FRESH_ENVELOPES=20
FRESH_CLOSES=10
HARD_LINKAGE_ISSUES=0
MISMATCHED_ROWS=0
```

## What Happened

1. `.env` was updated to:

   ```text
   CHILI_PHASE5K_COINBASE_CAP_USE_ENVELOPES=true
   ```

2. `docker compose up -d --force-recreate autotrader-worker` partially failed:
   - the autotrader container was removed/recreated
   - stale generated-name containers were left behind
   - Postgres was recreated and entered a long crash-recovery/fsync window

3. Postgres stayed in `health: starting` and emitted repeated:

   ```text
   FATAL: the database system is starting up
   ```

4. Autotrader started while Postgres was unavailable and logged DB connection
   failures plus temporary kill-switch restore warnings.

5. Docker Desktop then stopped answering cleanly:

   ```text
   failed to connect to the docker API at npipe:////./pipe/dockerDesktopLinuxEngine
   request returned 500 Internal Server Error
   ```

6. The flag was rolled back in `.env` to false so the next clean Docker start
   returns to compatibility-view behavior.

## Status

- Phase 5K-B source code remains pushed and default-off safe.
- Phase 5K-C live soak is **promoted**.
- `.env` now has `CHILI_PHASE5K_COINBASE_CAP_USE_ENVELOPES=true`.
- Postgres is healthy.
- Autotrader is running with the Phase 5K flag set to true.
- The live-runtime watchdog was re-registered from the correct
  `D:\dev\chili-home-copilot` root and reports `runtime_ok=true`.

## Retry Note

After Docker recovered, Postgres became healthy and autotrader was recreated
with the flag false. The Phase 5K-A probe initially exposed a probe-only
psycopg2 formatting bug in checks whose SQL includes literal `%option%`
patterns through the shared return-math expression. The probe was fixed so
parameterless SELECTs call `cursor.execute(sql)` without an empty params tuple.

Post-fix verification:

```text
Phase 5K-A parity probe: COMPLETE_POSITIVE
PARITY_CHECKS=6
PARITY_MISMATCHES=0

Focused probe tests: 8 passed
```

The flag was retried using `docker compose up -d --no-deps --force-recreate
autotrader-worker` to avoid touching Postgres. Autotrader started and showed
`CHILI_PHASE5K_COINBASE_CAP_USE_ENVELOPES=true`, but host-side probes then saw
Postgres close the connection and the database container re-entered
`health: starting` recovery. The flag was rolled back to false again.

The repeated failure pattern is now runtime/Docker/Postgres instability, not
the Phase 5K-B code path.

## Successful Retry

The later retry succeeded after isolating two runtime causes:

1. The `CHILI-live-runtime-watchdog` Windows task was registered against the
   stale root `D:\dev\chili-home-copilot-options-alpha-evidence-pr`. While
   Postgres was recovering, it kept treating the real runtime as a wrong
   worktree and recreated Compose containers under the same project name.
2. The project-autonomy agent scheduler and already-launched Codex pytest jobs
   were colliding with the live database immediately after recovery. The
   project-autonomy scheduler was disabled in `.env`:

   ```text
   PROJECT_AUTONOMY_AGENT_SCHEDULER_ENABLED=false
   ```

After Postgres completed recovery:

```text
Postgres: healthy
Autotrader: running
CHILI_PHASE5K_COINBASE_CAP_USE_ENVELOPES=true
PROJECT_AUTONOMY_AGENT_SCHEDULER_ENABLED=false
```

Post-flip validation:

```text
Phase 5K-A parity probe: COMPLETE_POSITIVE
PARITY_CHECKS=6
PARITY_MISMATCHES=0

Phase 5I post-rename probe: COMPLETE_POSITIVE
FRESH_DECISIONS=20
FRESH_ENVELOPES=20
FRESH_CLOSES=10
HARD_LINKAGE_ISSUES=0
MISMATCHED_ROWS=0

Live-runtime watchdog: runtime_ok=true
services_wrong_worktree=[]
services_to_start=[]
action=noop
```

## Architect Read

The original rollback was an infrastructure failure, not a data-model or
code-path failure. Once the stale watchdog root and non-trading test launchers
were removed from the equation, the Phase 5K-C flag behaved as expected.

Keep this as a narrow soak before cutting over more live readers. The right
next Phase 5K move is another default-off reader flag plus a parity probe, not a
bulk live-path rename.

Operational guardrails from the retry:

1. Keep `PROJECT_AUTONOMY_AGENT_SCHEDULER_ENABLED=false` during live trading
   work unless explicitly testing project-autonomy behavior.
2. Keep the live-runtime watchdog registered from
   `D:\dev\chili-home-copilot`.
3. Do not run DB-backed pytest jobs against the live Docker Postgres while
   trading workers are active.
