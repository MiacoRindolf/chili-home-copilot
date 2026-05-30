# NEXT_TASK: f-runtime-docker-postgres-recovery-then-phase5k-c-retry

STATUS: DONE

## Goal

Recover the local Docker/Postgres runtime, then retry the Phase 5K-C Coinbase
cap reader flag soak only if the runtime is healthy.

The Phase 5K-B code is already safe and default off. Phase 5K-C is now live
for the Coinbase venue-cap reader. `.env` currently has:

```text
CHILI_PHASE5K_COINBASE_CAP_USE_ENVELOPES=true
```

## Current State

- Phase 5K-B code: shipped and pushed.
- Phase 5K-A parity before the flip attempt: `COMPLETE_POSITIVE`.
- Phase 5I before the flip attempt: `COMPLETE_POSITIVE`.
- Phase 5K-C initial attempts: rolled back because Docker/Postgres became
  unhealthy during autotrader restart.
- Root cause isolated: stale live-runtime watchdog root
  (`D:\dev\chili-home-copilot-options-alpha-evidence-pr`) plus non-trading
  project-autonomy/Codex pytest jobs colliding with DB recovery.
- Recovery completed: Postgres healthy, project-autonomy agent scheduler off,
  stale pytest jobs stopped, live-runtime watchdog re-registered from
  `D:\dev\chili-home-copilot`.
- Phase 5K-C retry: promoted; autotrader sees
  `CHILI_PHASE5K_COINBASE_CAP_USE_ENVELOPES=true`.
- Phase 5K-A and Phase 5I probes remain `COMPLETE_POSITIVE`.

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
- `.env` flag true with post-flip probes green.
- Phase 5K-A remains `COMPLETE_POSITIVE`.
- Phase 5I remains `COMPLETE_POSITIVE`.

## Next Recommended Brief

Do not bulk-cut live paths yet. Let Phase 5K-C keep soaking, then ship the next
single-reader default-off flag from the Phase 5K parity set. Recommended order:

1. `f-position-identity-phase-5k-d-pdt-reader-flag`
2. promotion/pattern-quality realized aggregate readers
3. portfolio-risk open-exposure reader

Keep the same evidence-first pattern: old-vs-new parity probe, default-off
flag, focused tests, then a narrow live flag soak.
