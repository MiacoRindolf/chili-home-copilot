# RUNBOOK: Move Postgres onto a fast disk (new 1 TB drive)

**Status:** PLANNED — awaiting new disk (operator ordered a 1 TB drive, ETA ~2026-06-06).
Needs a maintenance window + kill switch. Created 2026-06-04; supersedes the earlier
"cold-copy to a Docker named volume on D:" plan (proven infeasible — see below).

## Root cause (confirmed 2026-06-04)

**D: is a slow 7200 rpm HDD** — `ST1000DM003` (Seagate Barracuda). The live `chili`
Postgres data dir lives there (`D:/CHILI-Docker/postgres`), and the Docker Desktop WSL
vhdx is *also* on D: (`D:/CHILI-Docker/docker-desktop-wsl`). Postgres' fsync-heavy
checkpoints on a spinning disk took **80–237 s**, stalling the autotrader's transaction
→ `idle-in-transaction` connection kills → `PendingRollbackError` / 210 s "tick failed".

There is an SSD (`C:`, Samsung 850 EVO) but it had only ~57 GB free vs a 75 GB DB —
hence the new 1 TB drive.

Measured disk facts (so future readers don't re-test):
- D: HDD native write under live load ≈ **6.5 MB/s**; 9p single-big-file read ≈ 34 MB/s.
- **Many-small-files copy through the 9p bind mount ≈ 0.2 MB/s** — the DB is tens of
  thousands of small files, so a file-by-file `cp`/`tar` of the data dir is INFEASIBLE
  (80 GB would take ~105 h). A cold-copy migration was attempted 2026-06-04 and
  aborted/rolled back cleanly (~15 min downtime, no data loss).
- A Docker **named volume does NOT escape D:** — its vhdx is on the same HDD. So the
  old "move to a named volume" plan would not have fixed fsync.

## What's already done (no downtime)

- **Palliative live** (reloadable, persisted in `postgresql.auto.conf`):
  `checkpoint_timeout=1800s`, `max_wal_size=4GB`, `wal_compression=on`,
  `synchronous_commit=off`. Effect: fewer, survivable checkpoint storms.
- **Cleanup (2026-06-04): chili 65 GB → 29 GB**, data dir ~80 GB → ~31 GB, D: free
  217 → 266 GB:
  - Dropped 83 stale DBs (kept only `chili`, `chili_test`, `chili_staging`, `postgres`,
    `template0/1`).
  - TRUNCATEd `fast_orderbook_default` (37 GB, fast-path paused).
  - **Still pending:** prune `trading_exit_parity_log` (~17 GB) to **14 days** — do this
    as part of the migration (a fresh `pg_dump`/restore naturally drops old rows if dumped
    after a `DELETE`, or run the DELETE + a `VACUUM FULL`/repack on the fast disk where
    it's cheap).

## Migration plan (when the 1 TB disk arrives)

Even at 29 GB, the data dir is still many small files, so the per-file 9p `cp` wall still
applies — **do NOT use a Docker file-copy.** Use a method that bypasses per-file 9p:
either Windows-native `robocopy` (physical) or `pg_dump`/`pg_restore` (logical).

### 0. Prep
- Install + format the new drive (NTFS), assign a letter — assume **`E:`** below.
- Decide the end-state (recommended: **Docker data root on the SSD** so named volumes get
  fast ext4 fsync with no 9p; simpler interim: a bind mount on the SSD, which is fast even
  with 9p because the disk is fast). Both are fine; pick one.
- Kill switch ON: `CHILI_AUTOTRADER_KILL_SWITCH=1`. Stop the DB-touching containers
  (the `chili-clean-recovery-*` set + fast-scan), then `docker stop chili-home-copilot-postgres-1`
  (clean shutdown — it exits 0).
- Record pre-counts: `SELECT count(*) FROM trading_management_envelopes;` etc.

### Method A — Windows-native physical copy (simplest, exact)
```
robocopy D:\CHILI-Docker\postgres E:\CHILI-Docker\postgres /MIR /COPY:DAT /DCOPY:DAT /R:1 /W:1
```
Native NTFS many-small-file copy on a fast SSD target — no 9p, far faster than the 0.2 MB/s
Docker path. Then point Postgres at the new path: set
`CHILI_POSTGRES_DATA_SOURCE=E:/CHILI-Docker/postgres` in `.env` (the compose volume is
parameterized: `${CHILI_POSTGRES_DATA_SOURCE:-D:/CHILI-Docker/postgres}`), and ensure file
ownership stays uid 70 (Alpine postgres) — `cp -a`/robocopy preserve; if PG won't start,
`docker run --rm -v E:/CHILI-Docker/postgres:/d alpine chown -R 70:70 /d`.

### Method B — logical dump/restore (also debloats; drops old exit-parity rows)
```
# dump (PG still running on D:):
docker exec chili-home-copilot-postgres-1 pg_dump -U chili -d chili -Fc -f /tmp/chili.dump
docker cp chili-home-copilot-postgres-1:/tmp/chili.dump E:\chili.dump
# new PG on the SSD (fresh data dir on E:), then:
pg_restore -U chili -d chili --no-owner -j 4 E:\chili.dump
```
Slower to dump (PG reads the HDD) but yields a compact, freshly-packed DB on the SSD and is
the natural place to apply the exit-parity 14-day retention (DELETE old rows before dump).

### Restore & validate
- Start Postgres on the new disk; `pg_isready`; `docker inspect` shows the new data source.
- Row counts match pre-migration.
- **Watch one checkpoint — `sync` should be sub-second** (the whole point).
- Revert the brain-worker lean-cycle interval to the default (5 min) once on the SSD.
- Bring the workers back; `/trading` 200; autotrader tick under budget; reset kill switch.
- Keep `D:\CHILI-Docker\postgres` as rollback for N days, then reclaim.

## Also at migration time — consolidate the manual recovery containers

The live stack currently runs as **manual `docker run` containers** named
`chili-clean-recovery-*` (web, autotrader, broker-sync, scheduler, brain, market-snapshot)
+ a clean fast-scan — all from `chili-app:main-clean-<sha>` images, mounting only
`/app/data` (no dirty code). They have `restart=unless-stopped` but are NOT compose-managed:
**do not `docker compose up` those services** (it creates duplicates). The migration window
is the right time to fold them back into compose (once the dirty checkout is resolved or the
app is rebuilt from clean main).

## Rollback
Stop PG, set `CHILI_POSTGRES_DATA_SOURCE` back to the D: default (or unset), restart. The
D: data dir is untouched by a `robocopy /MIR` *to* E: (it only reads D:).
