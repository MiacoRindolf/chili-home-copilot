# RUNBOOK: Migrate Postgres off the Windows bind mount

**Status:** PLANNED (not yet executed) — created 2026-06-04 during a live-ops pass.
**Owner decision required:** needs a maintenance window + verified backup. Do **not** run ad hoc.

## Why

The live `chili` database (~**75 GB**) stores its data dir on a **Windows bind mount**:

```
bind | src=D:/CHILI-Docker/postgres | dst=/var/lib/postgresql/data
```

Docker Desktop on Windows routes bind-mount I/O through a translation layer where
`fsync()` is pathologically slow. Observed on 2026-06-04:

- Checkpoints taking **80–237 s**, almost entirely in the `sync` phase
  (`sync=94.8s`, `longest=36.6s` for a **single** file fsync; `sync files=168`).
- `FATAL: terminating connection due to idle-in-transaction timeout` — the
  autotrader's transaction stalls behind the multi-minute fsync, trips the app's
  per-session idle/statement timeout, and Postgres kills the connection
  → `server closed the connection unexpectedly` → poisoned SQLAlchemy session
  → one 210 s autotrader tick that failed (3 ticks skipped) + chronic 20–42 s slow ticks.

A reloadable palliative was applied 2026-06-04 (see
`docs/STRATEGY/CC_REPORTS/2026-06-04_live-ops-postgres-checkpoint-io.md`):
`checkpoint_timeout=1800s`, `max_wal_size=4GB`, `wal_compression=on`. That reduces
checkpoint **frequency** but every checkpoint still pays the slow-fsync tax. The
durable fix is to move the data dir onto a **Docker named volume**, which lives on
the Docker Desktop WSL2/VM ext4 filesystem where `fsync()` is milliseconds.

## Preconditions

- Scheduled maintenance window. Postgres downtime takes down the whole stack
  (web, autotrader, workers).
- **Kill switch ON** for the duration: `CHILI_AUTOTRADER_KILL_SWITCH=1` (halts both
  venues in ~30 s). No automated trades while the DB is moving.
- A **verified** backup that you have confirmed restores.
- Enough free space in the Docker Desktop VM disk for 75 GB + WAL headroom
  (check the WSL2 `.vhdx` has room, or expand it first).

## Procedure (cold physical copy — safest for 75 GB)

A cold file copy of a stopped Postgres data dir is exact and far faster than
`pg_dump`/restore at this size. The old bind-mount dir is retained as the rollback
artifact until the new volume is validated.

1. **Quiesce.** Set the kill switch, then stop every container that touches the DB
   (app, workers, and the `chili-clean-recovery-*` containers). Leave Postgres for
   the moment.

2. **Final sanity counts (pre-migration), record them:**
   ```sql
   SELECT 'envelopes', count(*) FROM trading_management_envelopes
   UNION ALL SELECT 'exec_events', count(*) FROM trading_execution_events
   UNION ALL SELECT 'decisions',  count(*) FROM trading_decisions
   UNION ALL SELECT 'paper',      count(*) FROM trading_paper_trades;
   ```

3. **Stop Postgres** (clean shutdown so the data dir is consistent):
   ```
   docker compose stop postgres
   ```

4. **Create the named volume:**
   ```
   docker volume create chili_pgdata
   ```

5. **Copy the data dir into the volume** (`cp -a` preserves the numeric uid/gid —
   Postgres runs as uid 999; ownership MUST be preserved or PG won't start):
   ```
   docker run --rm \
     -v D:/CHILI-Docker/postgres:/src:ro \
     -v chili_pgdata:/dst \
     alpine sh -c "cp -a /src/. /dst/ && ls -la /dst | head"
   ```
   Confirm files are owned by `999:999` in the output.

6. **Repoint compose** — change the postgres service volume from the bind mount to
   the named volume, and declare the volume:
   ```yaml
   services:
     postgres:
       volumes:
         - chili_pgdata:/var/lib/postgresql/data   # was D:/CHILI-Docker/postgres:/var/lib/postgresql/data
   volumes:
     chili_pgdata:
       external: true
   ```

7. **Start Postgres only** and validate:
   ```
   docker compose up -d postgres
   docker exec chili-home-copilot-postgres-1 pg_isready -U chili
   ```
   - Row counts match step 2 exactly.
   - `pg_controldata` shows a clean shutdown / valid state.
   - Watch one checkpoint — `sync` should now be **sub-second to low-seconds**, not 80 s:
     ```
     docker logs chili-home-copilot-postgres-1 --since <ts> | grep checkpoint
     ```

8. **Bring the stack back up.** Start app + workers. Confirm `/trading` = 200,
   autotrader tick time is back under budget, no idle-in-transaction FATALs, and
   broker reconciliation resumes. Only then reset the kill switch.

9. **Retain rollback.** Keep `D:/CHILI-Docker/postgres` untouched for N days. Once
   the named volume is proven, archive/delete it to reclaim space.

## Rollback

Stop Postgres, repoint the compose volume back to
`D:/CHILI-Docker/postgres:/var/lib/postgresql/data`, restart. (The bind-mount dir
was never modified by the copy — `:ro`.)

## Validation checklist

- [ ] Key table row counts match pre-migration.
- [ ] Checkpoint `sync` time < a few seconds.
- [ ] No `idle-in-transaction` / `server closed the connection` events.
- [ ] Autotrader tick time under the 15 s advisory budget.
- [ ] `/trading` 200; broker-sync reconciliation running again.
- [ ] Kill switch reset; a full learning + tick cycle completes clean.

## Notes

- This is infra only — no trading-logic, schema, or authority-contract change.
- Also clear the stale pending `max_connections` change in `postgresql.auto.conf`
  if a restart is being taken anyway (a reload currently logs
  `parameter "max_connections" cannot be changed without restarting`).
