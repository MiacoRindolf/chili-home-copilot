# NEXT_TASK: f-phase5m-deployment-source-posture

STATUS: QUEUED

## Goal

Make the live service source-of-truth posture explicit and boring before any
more Phase 5 runtime cutovers.

## Current State

Runtime is healthy, and Phase 5K/5I probes are green. Production services are
intentionally running from clean worktrees because the root checkout remains
very dirty and should not be used for edits, tests, deployment, commits, or
pushes.

The live web container currently mounts a clean Phase 5L worktree while the
root repository contains many unrelated modifications. That posture is safe
only if it is documented and verified before each deploy/restart.

## Recommended Work Shape

1. Identify the worktree currently mounted into each live app/worker container.
2. Verify it is based on the merged recovery branch and not the dirty root.
3. Add a small runbook or probe that reports:
   - mounted source path
   - git branch / commit
   - dirty/clean status
   - relevant Phase 5 flags
4. Keep the check read-only unless the runtime is clearly pointing at the wrong
   source.
5. Re-run Phase 5K and Phase 5I probes after any source-mount correction.

## Guardrails

- Do not reset, clean, or overwrite the dirty root checkout.
- Do not restart Postgres.
- Do not flip Phase 5 flags.
- Do not perform schema migrations.
- Prefer a clean worktree and a documentation/probe change over manual runtime
  surgery.

