# NEXT_TASK: f-phase5n-source-posture-watch

STATUS: QUEUED

## Goal

Turn the Phase 5M source-posture probe into a lightweight recurring guard so a
future manual restart cannot silently put live workers back on the dirty root.

## Current State

Phase 5M added `scripts/d-phase5m-source-posture-probe.py` and repaired the live
runtime source split. App/worker services now mount a clean worktree, and Phase
5K/5I probes remain `COMPLETE_POSITIVE`.

The root checkout remains very dirty and must not be used as a deployment
source.

## Recommended Work Shape

1. Add a tiny wrapper or scheduled task that runs:
   `python scripts/d-phase5m-source-posture-probe.py`.
2. Write output to a stable file under `scripts/` or `docs/RUNBOOKS/`.
3. Alert only on non-`COMPLETE_POSITIVE` verdict.
4. Include Phase 5K/5I probe instructions in the alert text, but do not run
   heavy probes every minute.

## Guardrails

- Do not restart Postgres.
- Do not flip Phase 5 flags.
- Do not mutate runtime unless the probe reports dirty-root usage.
- Do not clean or reset the dirty root checkout.

