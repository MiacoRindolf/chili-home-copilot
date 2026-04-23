# PostgreSQL database

CHILI uses **PostgreSQL only** for the relational database. Set `DATABASE_URL` in `.env` for local runs (see `.env.example`). The application **never** opens `data/chili.db` or any SQLite file at runtime—only optional one-off migration scripts do.

**Default for local development:** point at the **Docker Compose** Postgres instance (host port **5433**):

`postgresql://chili:chili@localhost:5433/chili`

That is the same database the `chili` container uses (`postgresql://chili:chili@postgres:5432/chili` on the Docker network).

## Staging database (`chili_staging`)

For **production-shaped** data (e.g. promoted/live `scan_patterns`, real trade depth) **without** hitting the live `chili` writer and **without** using `chili_test` (pytest **truncates** tables), use a separate database **`chili_staging`** on the same Postgres instance. It is **overwritten** on a schedule from a `pg_dump` of `chili` (or the latest backup file). Set `DATABASE_URL` to `postgresql://chili:chili@localhost:5433/chili_staging` when running dry-run scripts; optional `STAGING_DATABASE_URL` in `.env` records that URL for operators and future tooling.

**Full runbook:** [STAGING_DATABASE.md](STAGING_DATABASE.md) (one-time `CREATE DATABASE`, `scripts/refresh_staging_from_backup.ps1`, scheduled task, RDS notes).

## Docker Compose (bundled database)

The repo `docker-compose.yml` defines a **`postgres`** service and wires the **`chili`** app to it:

| Item | Value |
|------|--------|
| In-container URL (set on `chili`, `brain`, etc.) | `postgresql://chili:chili@postgres:5432/chili` |
| From your host (uvicorn, scripts, GUI, pytest) | `postgresql://chili:chili@localhost:5433/chili` |

- Port **5433** on the host maps to Postgres **5432** inside Compose so a separate PostgreSQL on **5432** does not conflict.
- The `chili` service **`environment`** entry for `DATABASE_URL` overrides any `DATABASE_URL` in `.env` when using Compose, so the app always resolves the `postgres` hostname on the Docker network.
- Data persists under host paths **`D:/CHILI-Docker/postgres`**, **`D:/CHILI-Docker/chili-data`**, and **`D:/CHILI-Docker/ollama`** (bind mounts in `docker-compose.yml`). `docker compose down` does not remove that data; deleting those folders would.

Use **`bash scripts/docker-setup.sh`** to start Postgres + Ollama, wait for health, pull models, start CHILI, and run RAG ingest.

## New environment checklist

1. **Start Postgres** (recommended: `docker compose up -d postgres` from this repo). The `chili` database and `chili` user are created by the image.
2. **Set `DATABASE_URL`** to a PostgreSQL URL, for example:
   - `postgresql://chili:chili@localhost:5433/chili` (host → Compose Postgres)
   - or `postgresql+psycopg2://chili:chili@localhost:5433/chili`
3. **Start the app once** (or run a process that imports `app.main`). The app runs SQLAlchemy `create_all` plus versioned migrations in `app/migrations.py` on startup.
4. **Optional — legacy SQLite (`data/chili.db`)**  
   Runtime always uses Postgres (`app/config.py` rejects non-PostgreSQL URLs). SQLite is **only** for one-time import.
   - **Preferred (keeps existing Postgres rows):** back up Postgres, then run  
     `python scripts/merge_sqlite_into_postgres.py --dry-run`  
     `python scripts/merge_sqlite_into_postgres.py`  
     Optional: `--archive-sqlite-after` renames `chili.db` (and `-wal`/`-shm` if present) after a clean run.
   - **Full replace (wipes target Postgres):** `python scripts/migrate_legacy_sqlite_to_postgres.py` — truncates all app tables, then copies from SQLite. Use only on an empty or disposable database.
   - **Docker:** from the host, mount the repo and use the Compose network, e.g.  
     `docker run --rm --entrypoint python -e DATABASE_URL=postgresql://chili:chili@postgres:5432/chili -v <repo>:/src -w /src --network <project>_default chili-app:local scripts/merge_sqlite_into_postgres.py`

### Merge behavior and “redundant” rows

- The merge script inserts with **`ON CONFLICT (primary key) DO NOTHING`**. Re-running it does not create **duplicate primary keys**; existing Postgres rows win for that `id`.
- Two different numeric ids can still represent the **same** pattern text/rules if historical data diverged; that is rare. To **list** them: `python scripts/audit_postgres_merge_redundancy.py`
- To **collapse** those groups (keep the **newest** row by `updated_at`, then `created_at`, then `id`; repoint FK-like columns; delete losers):  
  `python scripts/dedupe_scan_patterns_by_rules.py` then `python scripts/dedupe_scan_patterns_by_rules.py --apply`

## Running tests (`pytest`)

Tests need a **separate** PostgreSQL database so they do not wipe your dev data.

1. Create e.g. `chili_test` in the same Postgres instance (or another server).
2. Set **`TEST_DATABASE_URL`** (recommended) or **`DATABASE_URL`** before pytest, for example:

   ```bash
   set TEST_DATABASE_URL=postgresql://chili:chili@localhost:5433/chili_test
   pytest
   ```

   On Linux/macOS:

   ```bash
   export TEST_DATABASE_URL=postgresql://chili:chili@localhost:5433/chili_test
   pytest
   ```

   Create `chili_test` once in the same Postgres instance (e.g. `CREATE DATABASE chili_test;` as superuser).

3. `tests/conftest.py` copies `TEST_DATABASE_URL` into `DATABASE_URL` before importing the app, then **truncates** application tables (except `schema_version`) around each test for isolation.

### Ad-hoc Postgres (optional)

If you are not using Compose, you can run a standalone Postgres and set `DATABASE_URL` to match it (any port). The repo defaults assume Compose on **5433** (see above).

## Troubleshooting

- **`DATABASE_URL is required`** — Add a non-empty PostgreSQL URL to `.env`.
- **`DATABASE_URL must be a PostgreSQL URL`** — Use `postgresql://` or `postgresql+psycopg2://`, not SQLite or other drivers unless you extend validation in `app/config.py`.

### Windows: `No buffer space available` / Winsock `10055` (socket pool exhaustion)

This is a **host** limitation: too many sockets in `TIME_WAIT` or ephemeral ports exhausted (common with Docker Desktop, Git, IDEs, and many short-lived DB connections). It is **not** “Postgres is down” — the server may be fine while the **client** cannot open a new TCP connection to `127.0.0.1:5433` or `localhost:5433`.

**Try in order:**

1. **Restart Docker Desktop**, then retry.
2. **Reboot Windows** if restarts do not clear it.
3. Use **`127.0.0.1`** instead of **`localhost`** (avoids IPv6 `::1` split-brain). If both still fail with `10055`, the host pool is still exhausted—go to (4).
4. **Run the script inside the Compose network** (bypasses the Windows loopback path entirely). Use hostname **`postgres`** and port **`5432`** (in-container), not `localhost:5433`. From the repo root, with the `chili` service up:

   ```powershell
   docker compose exec -w /workspace -e DATABASE_URL=postgresql://chili:chili@postgres:5432/chili_staging chili python scripts/backfill_cpcv_metrics.py --dry-run
   ```

   Adjust database name (`chili` / `chili_staging` / `chili_test`) and script path as needed. Requires the `chili` image to contain the same Python dependencies as your script (e.g. `lightgbm` for CPCV backfill). If a package is missing in the image, use `docker compose run --rm` with a custom image, or install deps in `chili-env` on the host only **after** the machine recovers from `10055`.

CLI scripts `merge_sqlite_into_postgres.py`, `dedupe_scan_patterns_by_rules.py`, and `audit_postgres_merge_redundancy.py` retry once with IPv4 automatically when this error appears on `localhost`.
