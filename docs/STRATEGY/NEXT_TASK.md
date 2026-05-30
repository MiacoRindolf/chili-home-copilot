# NEXT_TASK: f-runtime-docker-postgres-recovery-then-phase5k-c-retry

STATUS: PENDING

## Goal

Recover the local Docker/Postgres runtime, then retry the Phase 5K-C Coinbase
cap reader flag soak only if the runtime is healthy.

The Phase 5K-B code is already safe and default off. `.env` currently has:

```text
CHILI_PHASE5K_COINBASE_CAP_USE_ENVELOPES=false
```

## Current State

- Phase 5K-B code: shipped and pushed.
- Phase 5K-A parity before the flip attempt: `COMPLETE_POSITIVE`.
- Phase 5I before the flip attempt: `COMPLETE_POSITIVE`.
- Phase 5K-C attempt: rolled back because Docker/Postgres became unhealthy
  during autotrader restart.
- Source code does not need rollback.

## Recovery Steps

1. Confirm Docker API is healthy:

   ```powershell
   docker version
   docker ps
   ```

2. Confirm Postgres is healthy:

   ```powershell
   docker compose ps postgres
   docker compose logs --tail=80 postgres
   ```

3. If Postgres is still in crash recovery, wait; do not start autotrader until
   Postgres is healthy.

4. Once Docker and Postgres are healthy, start/restart autotrader with the flag
   still false:

   ```powershell
   docker compose up -d autotrader-worker
   ```

5. Run:

   ```powershell
   python scripts\d-phase5k-live-path-parity-probe.py
   python scripts\d-phase5i-post-rename-soak-probe.py
   ```

6. Only if both are green, retry Phase 5K-C:

   - set `CHILI_PHASE5K_COINBASE_CAP_USE_ENVELOPES=true`
   - restart only autotrader
   - verify flag is present inside the autotrader container
   - check fresh autotrader logs for cap-query failures
   - rerun Phase 5K-A and Phase 5I probes

## Rollback

Set:

```text
CHILI_PHASE5K_COINBASE_CAP_USE_ENVELOPES=false
```

Then restart only the autotrader worker after Postgres is healthy.

## Guardrails

- Do not touch broker/order/stop/reconcile paths.
- Do not run migrations.
- Do not change any other flag.
- Do not assume the flag soak succeeded until Docker/Postgres are healthy and
  post-flip probes pass.

## Acceptance

- Docker API healthy.
- Postgres healthy.
- Autotrader running.
- `.env` flag either safely false, or true with post-flip probes green.
- Phase 5K-A remains `COMPLETE_POSITIVE`.
- Phase 5I remains `COMPLETE_POSITIVE`.
