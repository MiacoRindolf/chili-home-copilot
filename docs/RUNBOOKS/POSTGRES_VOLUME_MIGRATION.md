# RUNBOOK: Migrate Postgres off the Windows bind mount

**Status:** PLANNED (not yet executed) — created 2026-06-04, revised same day with
environment-specific facts. Needs a maintenance window + verified backup. Do **not**
run ad hoc.

## Why

The live `chili` database (~**75 GB**) stores its data dir on a **Windows bind mount**
(`D:/CHILI-Docker/postgres`), where `fsync()` is pathologically slow through Docker
Desktop's host-mount translation layer. Observed 2026-06-04:

- Checkpoint sync phases of **26–170 s** (e.g. 08:29 checkpoint: `sync=169.7s`,
  `longest=39.7s` for one file, `sync files=223`).
- That stall trips the app's per-session idle/statement timeout →
  `FATAL: terminating connection due to idle-in-transaction timeout` →
  `server closed the connection unexpectedly` → poisoned session → a 210 s autotrader
  tick that failed (3 ticks skipped) + chronic 20–42 s slow ticks.

A reloadable palliative was applied 2026-06-04 (`checkpoint_timeout=1800s`,
`max_wal_size=4GB`, `wal_compression=on`, `synchronous_commit=off`). Effect measured:
checkpoints went from ~5 min to ~30 min apart, and a subsequent **169 s** checkpoint
caused **0** tick failures (vs. the earlier kill) because commits no longer wait on
fsync. But each checkpoint still pays the slow-fsync tax (169 s) — the palliative
reduces frequency/blast-radius, it does **not** fix the storage. This migration does.

A Docker **named volume** lives on the Docker Desktop VM's ext4 filesystem, where
`fsync()` is milliseconds — no host-mount translation.

## Environment facts (verified 2026-06-04)

- Postgres: **16.13**, image `postgres:16-alpine`, service runs as `user: postgres`
  → data files owned by **uid 70** (Alpine postgres), *not* 999. `cp -a` preserves it.
- Docker VM disk (`/`, where volumes live): **914 GB available** of 1007 GB.
- Docker VM disk image (`ext4.vhdx`) is on **D:** (`D:\CHILI-Docker\docker-desktop-wsl\main`),
  and D: has ~218 GB free → room for the vhdx to grow by ~80 GB.
- `docker-compose.yml` is **already wired** for this migration:
  - line 46: `${CHILI_POSTGRES_DATA_SOURCE:-D:/CHILI-Docker/postgres}:/var/lib/postgresql/data`
  - line 945: top-level named volume `chili-postgres-data` declared (local driver).
  - So the switch is **one `.env` line** — no compose edit.
- Compose project name is `chili-home-copilot`, so the named volume materializes as
  **`chili-home-copilot_chili-postgres-data`**.
- `.env` currently has **no** `CHILI_POSTGRES_DATA_SOURCE` (on the bind-mount default).
- `postgres` service has `stop_grace_period: 180s` and a `recovery_init_sync_method=syncfs`
  command flag — leave both as-is.

## Preconditions

- Scheduled window. Postgres downtime takes the whole stack down. Prefer markets
  closed / low activity (weekend or overnight ET).
- **Kill switch ON** first: `CHILI_AUTOTRADER_KILL_SWITCH=1` (halts both venues ~30 s).
- A **verified** backup you've confirmed restores.
- No active pytest/other writers hammering the shared Postgres (they amplify the copy).
- Confirm D: free space ≥ (data-dir size + headroom). Data dir ≈ `chili` 75 GB +
  `chili_test`/`chili_staging`/`postgres` + WAL. Measure exact size during step 2.

## Procedure (cold physical copy — exact, safest for 75 GB)

The old bind-mount dir is the rollback artifact; it is opened read-only (`:ro`) and
never modified.

1. **Quiesce.** Set the kill switch, then stop every DB-touching container (app,
   workers, and the `chili-clean-recovery-*` containers). Leave Postgres up briefly
   for step 2–3.

2. **Record pre-migration truth (compare after):**
   ```sql
   SELECT 'envelopes', count(*) FROM trading_management_envelopes
   UNION ALL SELECT 'exec_events', count(*) FROM trading_execution_events
   UNION ALL SELECT 'decisions',  count(*) FROM trading_decisions
   UNION ALL SELECT 'paper',      count(*) FROM trading_paper_trades;
   ```
   And size: `docker exec chili-home-copilot-postgres-1 du -sh /var/lib/postgresql/data`.

3. **Stop Postgres cleanly** (so the data dir is consistent for a physical copy):
   ```
   docker compose stop postgres        # honors stop_grace_period 180s
   ```

4. **Create the named volume with the exact compose name:**
   ```
   docker volume create chili-home-copilot_chili-postgres-data
   ```

5. **Copy the data dir into the volume** — `cp -a` preserves uid 70 / perms / symlinks.
   Read from the bind mount **read-only**:
   ```
   docker run --rm \
     -v D:/CHILI-Docker/postgres:/src:ro \
     -v chili-home-copilot_chili-postgres-data:/dst \
     alpine sh -c "cp -a /src/. /dst/ && echo COPIED && ls -ld /dst/PG_VERSION /dst/base && stat -c '%u:%g %n' /dst/PG_VERSION"
   ```
   Confirm `PG_VERSION` exists in `/dst` and is owned by `70:70`.

6. **Flip the source** — add one line to `.env`:
   ```
   CHILI_POSTGRES_DATA_SOURCE=chili-postgres-data
   ```

7. **Start Postgres only, validate before anything else connects:**
   ```
   docker compose up -d postgres
   docker exec chili-home-copilot-postgres-1 pg_isready -U chili
   docker inspect chili-home-copilot-postgres-1 \
     --format '{{range .Mounts}}{{.Type}} {{.Name}}{{.Source}} -> {{.Destination}}{{println}}{{end}}'
   # MUST show:  volume chili-home-copilot_chili-postgres-data -> /var/lib/postgresql/data
   ```
   - Row counts match step 2 **exactly**.
   - Watch one checkpoint — `sync` should now be **sub-second to low-seconds**, not 169 s:
     `docker logs chili-home-copilot-postgres-1 --since <ts> | grep "checkpoint complete"`

8. **Bring the stack back up.** Start app + workers. Confirm `/trading` 200, autotrader
   tick time back under budget, **no** idle-in-transaction FATALs, broker-sync
   reconciliation running. Only then reset the kill switch.

9. **Retain rollback.** Keep `D:/CHILI-Docker/postgres` untouched for N days. Once the
   named volume is proven under a full learning + tick cycle, archive/delete it to
   reclaim ~75 GB on D:.

## Rollback (fast — no data copy needed)

The bind-mount dir was never modified. Revert is just the env flip:
```
docker compose stop postgres
# remove or comment CHILI_POSTGRES_DATA_SOURCE in .env  (back to the D: default)
docker compose up -d postgres
```

## Downtime estimate

Dominated by the cold copy in step 5: reading ~75–100 GB off the slow bind mount.
Budget **~30–120 min** for the copy + ~15 min validation; measure with the step-2 `du`
and, if you want a tighter number, time a copy of just `base/` first. (A lower-downtime
alternative is `pg_basebackup` into the volume while PG runs, then a short stop +
final catch-up — more moving parts; only worth it if the cold-copy downtime is too long.)

## Validation checklist

- [ ] `docker inspect` shows the named volume mounted at the data dir.
- [ ] Key table row counts match pre-migration.
- [ ] Checkpoint `sync` time < a few seconds.
- [ ] No `idle-in-transaction` / `server closed the connection` events.
- [ ] Autotrader tick time under the 15 s advisory budget.
- [ ] `/trading` 200; broker-sync reconciliation running.
- [ ] Kill switch reset; a full learning + tick cycle completes clean.

## Notes

- Infra only — no trading-logic, schema, or authority-contract change.
- If a restart is being taken anyway, clear the stale pending `max_connections` change
  in `postgresql.auto.conf` (a config reload currently logs
  `parameter "max_connections" cannot be changed without restarting`). The live value
  comes from the compose `command` flag (`max_connections=350`).
