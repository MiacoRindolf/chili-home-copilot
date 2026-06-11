# SCHEDULER SPLIT — exec-critical vs R&D plane

**Status: code SHIPPED 2026-06-11 night; topology deploy AFTER the 06-12 SpaceX
session (never re-architect containers hours before the most important open).**

## Why

4 scheduler deploys on 2026-06-11 alone, each restarting the ENTIRE trading
brain mid-session while it held live sessions: WS rails dropped, viability
freshness windows opened, order-lifecycle ticks paused. The ship-during-session
culture is the system's superpower — this split makes it safe.

## Roles (CHILI_SCHEDULER_ROLE)

| Role | Owns |
|---|---|
| `momentum_exec_only` | live runner batch + LiveRunnerLoop + TapeWsRecorder, auto-arm, NBBO sampler/prune, WS rails (Massive + Coinbase + price bus), broker session restore, heartbeat |
| `rnd_only` | everything `cron_only` had MINUS the exec set above and minus WS rails: scanners, viability bridge, miners, learning, paper runner, replay regression, prescreen, post-exit excursion, evolution… |
| `cron_only` / `all` / others | unchanged (safe fallback — running one container with `cron_only` keeps today's behavior exactly) |

Known coupling (v1, documented): the viability bridge lives in the R&D plane;
if that container is down >10min, live sessions hit viability-freshness blocks
(the #608 pin protects refresh PRIORITY, not refresh EXISTENCE). The freshness
watchdog alerts on this; do not leave R&D down during entry hours.

## Deploy runbook (evening of 2026-06-12)

```bash
# 1. Exec-critical container (deploy RARELY — only order/exit-path changes)
sed 's/^CHILI_SCHEDULER_ROLE=.*/CHILI_SCHEDULER_ROLE=momentum_exec_only/' \
  D:/CHILI-Docker/_sched_261364e.env > D:/CHILI-Docker/_sched_exec.env
docker run -d --name chili-clean-recovery-momentum-exec \
  --env-file D:/CHILI-Docker/_sched_exec.env \
  -v D:/CHILI-Docker/chili-data:/app/data \
  --network chili-home-copilot_default --restart unless-stopped \
  chili-app:main-clean-<sha> python scripts/scheduler_worker.py

# 2. Flip the existing scheduler container to the R&D plane
sed 's/^CHILI_SCHEDULER_ROLE=.*/CHILI_SCHEDULER_ROLE=rnd_only/' \
  D:/CHILI-Docker/_sched_261364e.env > D:/CHILI-Docker/_sched_rnd.env
docker rm -f chili-clean-recovery-scheduler
docker run -d --name chili-clean-recovery-scheduler \
  --env-file D:/CHILI-Docker/_sched_rnd.env \
  -v D:/CHILI-Docker/chili-data:/app/data \
  --network chili-home-copilot_default --restart unless-stopped \
  chili-app:main-clean-<sha> python scripts/scheduler_worker.py

# 3. Verify: exec has live_runner/auto_arm/nbbo jobs + WS; rnd has the rest, no WS
docker logs chili-clean-recovery-momentum-exec | grep -E "Massive WS|live runner|auto-arm"
docker logs chili-clean-recovery-scheduler | grep -E "rnd_only|skipping"
```

Rollback: kill both, relaunch one container with the original env (cron_only).
