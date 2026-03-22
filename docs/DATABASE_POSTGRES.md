# PostgreSQL database

CHILI uses **PostgreSQL only** for the relational database. Set `DATABASE_URL` in `.env` for local runs (see `.env.example`).

**Default for local development:** point at the **Docker Compose** Postgres instance (host port **5433**):

`postgresql://chili:chili@localhost:5433/chili`

That is the same database the `chili` container uses (`postgresql://chili:chili@postgres:5432/chili` on the Docker network).

## Docker Compose (bundled database)

The repo `docker-compose.yml` defines a **`postgres`** service and wires the **`chili`** app to it:

| Item | Value |
|------|--------|
| In-container URL (set on `chili`, `brain`, etc.) | `postgresql://chili:chili@postgres:5432/chili` |
| From your host (uvicorn, scripts, GUI, pytest) | `postgresql://chili:chili@localhost:5433/chili` |

- Port **5433** on the host maps to Postgres **5432** inside Compose so a separate PostgreSQL on **5432** does not conflict.
- The `chili` service **`environment`** entry for `DATABASE_URL` overrides any `DATABASE_URL` in `.env` when using Compose, so the app always resolves the `postgres` hostname on the Docker network.
- Data persists in the **`postgres_data`** volume until you run `docker compose down -v`.

Use **`bash scripts/docker-setup.sh`** to start Postgres + Ollama, wait for health, pull models, start CHILI, and run RAG ingest.

## New environment checklist

1. **Start Postgres** (recommended: `docker compose up -d postgres` from this repo). The `chili` database and `chili` user are created by the image.
2. **Set `DATABASE_URL`** to a PostgreSQL URL, for example:
   - `postgresql://chili:chili@localhost:5433/chili` (host → Compose Postgres)
   - or `postgresql+psycopg2://chili:chili@localhost:5433/chili`
3. **Start the app once** (or run a process that imports `app.main`). The app runs SQLAlchemy `create_all` plus versioned migrations in `app/migrations.py` on startup.
4. **Optional — legacy SQLite (`data/chili.db`)**  
   If you have an old SQLite file and want to move data into Postgres:
   - Use a **dedicated** Postgres database or **backup** the target first.
   - Run: `python scripts/migrate_legacy_sqlite_to_postgres.py`  
     This **truncates** all application tables in the target DB, then copies rows from `data/chili.db`.
   - Verify row counts / spot-check important tables, then archive or remove the local `chili.db` if you no longer need it.

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
