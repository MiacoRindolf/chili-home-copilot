# f-position-identity-phase-5k-c-coinbase-cap-flag-soak-attempt

## Summary

Phase 5K-C was attempted but not promoted. The code gate is sound and the
pre-flip probes were green, but the local Docker stack became unhealthy during
the autotrader-only restart.

The live flag was rolled back to false in `.env`:

```text
CHILI_PHASE5K_COINBASE_CAP_USE_ENVELOPES=false
```

No source-code rollback is needed because Phase 5K-B defaults off and remains
safe.

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
- Phase 5K-C live soak is **not complete**.
- `.env` is conservative again (`false`).
- Docker/Postgres local runtime needs recovery before retrying the soak.

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

## Architect Read

This was an infrastructure failure, not a data-model or code-path failure. The
pre-flip parity evidence was exactly what we wanted, and the default-off code
still protects the live path.

Do not retry the flag flip until Docker is healthy and Postgres can complete
startup cleanly. The next attempt should:

1. confirm Docker API health
2. confirm Postgres is healthy
3. run Phase 5K-A and Phase 5I probes
4. flip the one flag
5. restart only autotrader
6. verify fresh autotrader logs after Postgres is already healthy

No live trading behavior should be assumed changed from this attempt.
