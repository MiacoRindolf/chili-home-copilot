# Staging database (`chili_staging`)

A **full logical copy** of the production `chili` database (schema + data), refreshed on a schedule so operators can run **read-only** or **dry-run** maintenance scripts (CPCV backfill, regime rehearsal, etc.) against **production-shaped** data without using `chili_test` (pytest truncates tables).

**Not for pytest.** Use `chili_test` + `TEST_DATABASE_URL` for automated tests. Use `chili_staging` when you need real promoted/live rows and full history.

## Security and data handling

- Staging may contain the same **secrets, API echoes, and PII** as production. Treat it like production for access control, log redaction, and **do not** share dumps or connection strings casually.
- **Never** point a production write path or the live trading stack at `chili_staging` unless you have an explicit, separate runbook.
- **Lag:** data is at most ~24h behind production (or behind your last successful backup), depending on schedule.

## One-time: create the database

On the same PostgreSQL instance as `chili` (e.g. Docker Compose `postgres` on `localhost:5433`):

```sql
-- connect as a superuser or a role with CREATEDB (the default `chili` user in the official image can do this)
CREATE DATABASE chili_staging;
```

From the host, with Compose:

```powershell
docker exec -i chili-home-copilot-postgres-1 psql -U chili -d postgres -c "CREATE DATABASE chili_staging;"
```

(Replace the container name with `docker ps` output if your project name differs.)

## Point scripts at staging

Most CLI scripts use `SessionLocal()` and read `DATABASE_URL` from the environment. For a production-shape dry-run:

**PowerShell (session only):**

```powershell
$env:DATABASE_URL = "postgresql://chili:chili@localhost:5433/chili_staging"
conda run -n chili-env python scripts/backfill_cpcv_metrics.py --dry-run
```

Or set `STAGING_DATABASE_URL` in `.env` and copy it when running scripts (the app may expose `chili_staging_url` in settings for future UX; it is not required for `DATABASE_URL`-driven scripts).

**Do not** set `TEST_DATABASE_URL` to `chili_staging` — the test harness requires a database name ending in `_test` and **truncates** tables.

## Daily refresh (Docker + Windows)

| Script | Role |
|--------|------|
| [`scripts/backup_chili_db.ps1`](../scripts/backup_chili_db.ps1) | `pg_dump` of `chili` → `D:\CHILI-Docker\backup\chili_YYYYMMDD_HHmm.dump` |
| [`scripts/refresh_staging_from_backup.ps1`](../scripts/refresh_staging_from_backup.ps1) | `pg_restore` of the **newest** `chili_*.dump` into a **recreated** `chili_staging` |
| [`scripts/refresh_staging_from_live.ps1`](../scripts/refresh_staging_from_live.ps1) (optional) | `pg_dump` of live `chili` → `pg_restore` into `chili_staging` when no fresh backup file exists |
| [`scripts/backup_and_refresh_staging.ps1`](../scripts/backup_and_refresh_staging.ps1) | Runs **backup** then **staging refresh** in one invocation (single scheduled task) |

**Order of operations:** run **backup** first, then **staging refresh** (e.g. 03:30 backup, 04:00 refresh). Logs: `D:\CHILI-Docker\backup\logs\` (backup and `staging_refresh_*.log`).

**Scheduled task (example — adjust paths and container name):**

```powershell
schtasks /Create /TN "CHILI backup and staging" /SC DAILY /ST 03:45 /RL HIGHEST /F /TR "powershell -ExecutionPolicy Bypass -File C:\dev\chili-home-copilot\scripts\backup_and_refresh_staging.ps1"
```

Or keep two tasks (03:30 backup, 04:00 `refresh_staging_from_backup.ps1` only).

### Failure behavior

- On **restore failure** (non-zero `pg_restore`), the script **exits with code 1**. The previous `chili_staging` is already **dropped** in the same run — the database may be **empty or missing** until a successful re-run. Re-run the script or restore manually from a known-good `.dump` if needed.
- A **successful** run always replaces the entire contents of `chili_staging` with the source dump.

## Hosted / RDS or other production

The automation above assumes **Postgres in Docker** on the same host and the same `pg_dump` flags as `backup_chili_db.ps1` (`-Fc --no-owner --no-privileges`).

For **RDS** or a remote server:

1. Run `pg_dump` from a bastion or CI (or download an automated backup) to a **custom-format** file on disk.
2. Copy that file into the same layout `refresh_staging_from_backup.ps1` expects **or** copy it to `/tmp` in the **staging** Postgres and run the same `DROP DATABASE` / `CREATE DATABASE` / `pg_restore` pattern with `psql` / `pg_restore` from that environment.
3. Network, IAM, and credentials are **operator-specific**; this repo only documents the **pattern** (format + full replace by drop/create).

## See also

- [DATABASE_POSTGRES.md](DATABASE_POSTGRES.md) — dev, test, and staging URLs
- [CPCV_PROMOTION_GATE_RUNBOOK.md](CPCV_PROMOTION_GATE_RUNBOOK.md) — production-shape dry-run
- [REGIME_CLASSIFIER_RUNBOOK.md](REGIME_CLASSIFIER_RUNBOOK.md) — rehearsal on staging
