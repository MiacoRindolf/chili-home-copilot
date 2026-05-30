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
