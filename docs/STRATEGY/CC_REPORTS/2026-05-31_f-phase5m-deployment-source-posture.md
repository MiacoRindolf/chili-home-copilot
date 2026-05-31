# Phase 5M - Deployment Source Posture

Date: 2026-05-31

## Summary

Phase 5M adds a read-only deployment source-posture probe and uses it to repair
an actual runtime split: the web service was mounted from a clean Phase 5
worktree, while autotrader/scheduler/broker-sync were still mounted from the
dirty root checkout.

No Postgres restart, schema migration, or Phase 5 flag flip was performed.

## What Landed

- `scripts/d-phase5m-source-posture-probe.py`
  - reports live app/worker source roots
  - detects dirty-root usage
  - detects dirty live worktrees
  - checks whether source commits are ancestors of the merged recovery branch
  - checks Phase 5 flag consistency across the app/worker containers
- `tests/test_phase5m_source_posture_probe.py`
- `docs/RUNBOOKS/PHASE5M_DEPLOYMENT_SOURCE_POSTURE.md`

## Runtime Repair

Initial probe:

```text
USING_DIRTY_ROOT=true
DIRTY_WORKTREES=1
NON_ANCESTOR_ROOTS=1
```

The app/worker containers were recreated from the clean Phase 5M worktree with
`--no-deps`, leaving Postgres untouched. A local ignored `.env` copy was placed
in the worktree before the final recreate so runtime secrets and explicit Phase
5 flags stayed present.

Post-repair, all four app/worker containers point at the clean Phase 5M
worktree and the Phase 5 flags are consistent.

## Verification

```text
tests/test_phase5m_source_posture_probe.py -> 2 passed
Phase 5K live-path parity probe           -> COMPLETE_POSITIVE
Phase 5I post-rename soak probe           -> COMPLETE_POSITIVE
```

Final source-posture probe is expected to become `COMPLETE_POSITIVE` after this
Phase 5M commit is created, because the only remaining alert before commit was
the intentionally dirty worktree containing this probe/report change.

## Architect Verdict

This was worth doing before more cutover work. The system had a quiet split
between clean-web and dirty-worker source, which could have made trading
behavior diverge from route/probe behavior. The repair makes the runtime source
posture explicit, testable, and recoverable.

