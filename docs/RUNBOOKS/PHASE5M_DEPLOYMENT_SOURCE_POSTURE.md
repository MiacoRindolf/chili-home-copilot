# Phase 5M Deployment Source Posture

Use this runbook before Phase 5 flag flips, worker restarts, or live-path
cutovers.

## Probe

Run from a clean worktree:

```powershell
python scripts/d-phase5m-source-posture-probe.py
```

Expected healthy result:

```text
VERDICT_STATUS=COMPLETE_POSITIVE
USING_DIRTY_ROOT=false
DIRTY_WORKTREES=0
NON_ANCESTOR_ROOTS=0
FLAG_MISMATCHES=0
```

## What It Checks

- `chili`
- `autotrader-worker`
- `scheduler-worker`
- `broker-sync-worker`

For each service it reports:

- mounted source root
- container status / health
- git branch and commit
- whether the worktree is dirty
- whether the source commit is an ancestor of
  `origin/codex/brain-work-done-marker-recovery`
- Phase 5 route/cap flag consistency

## Repair Pattern

If services point at `D:\dev\chili-home-copilot`, do not clean or reset that
root. Instead, run the app services from a clean worktree and leave Postgres
alone:

```powershell
Copy-Item -LiteralPath D:\dev\chili-home-copilot\.env `
  -Destination D:\dev\chili-home-copilot\project_ws\_worktrees\<clean-worktree>\.env `
  -Force

docker compose -p chili-home-copilot `
  -f D:\dev\chili-home-copilot\project_ws\_worktrees\<clean-worktree>\docker-compose.yml `
  up -d --no-deps --force-recreate `
  chili autotrader-worker scheduler-worker broker-sync-worker
```

Then re-run:

```powershell
python scripts/d-phase5m-source-posture-probe.py
$env:DATABASE_URL='postgresql://chili:chili@localhost:5433/chili'
python scripts/d-phase5k-live-path-parity-probe.py
python scripts/d-phase5i-post-rename-soak-probe.py
```

## Guardrails

- Do not restart Postgres for source-posture repair.
- Do not use or clean the dirty root as a deployment source.
- Do not flip Phase 5 flags while repairing source posture.
- Ensure `.env` is present in the clean worktree before recreating services, or
  runtime secrets and explicit flags may silently disappear.

