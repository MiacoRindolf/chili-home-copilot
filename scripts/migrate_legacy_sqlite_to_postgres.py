"""One-time legacy import: copy rows from SQLite ``data/chili.db`` into PostgreSQL.

**Not used at runtime.** CHILI never opens SQLite during normal operation—only this script
and ``merge_sqlite_into_postgres.py`` touch legacy ``chili.db``. The app always uses
``DATABASE_URL`` (PostgreSQL); see ``app/config.py``.

**Warning:** The script truncates all application tables in the target Postgres
database before loading. Use a dedicated database or backup first.

To **merge** SQLite into Postgres without wiping existing rows (``ON CONFLICT DO NOTHING``),
use ``scripts/merge_sqlite_into_postgres.py`` instead.

Run from project root::

    python scripts/migrate_legacy_sqlite_to_postgres.py

Requires ``DATABASE_URL`` in ``.env`` pointing at PostgreSQL (see ``.env.example``).
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

# Load .env before app imports so DATABASE_URL is set
try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
except Exception:
    pass

# Ensure we use PostgreSQL for the target; SQLite path is fixed (legacy file only)
DATA_DIR = Path(__file__).resolve().parents[1] / "data"
SQLITE_PATH = DATA_DIR / "chili.db"

if not SQLITE_PATH.exists():
    print(f"SQLite DB not found: {SQLITE_PATH}")
    sys.exit(1)

database_url = (os.environ.get("DATABASE_URL") or "").strip()
_lower = database_url.lower()
if not (
    _lower.startswith("postgresql://")
    or _lower.startswith("postgresql+psycopg2://")
    or _lower.startswith("postgresql+psycopg://")
):
    print("Set DATABASE_URL to a PostgreSQL URL in .env and run again (see .env.example).")
    sys.exit(1)

from psycopg2.extras import execute_values
from sqlalchemy import create_engine, text

# Import app so all models are registered
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.db import Base
import app.models  # noqa: F401 - register all tables

pg_engine = create_engine(database_url)
Base.metadata.create_all(bind=pg_engine)

# Truncate so we can re-run the script cleanly (reverse order for FK safety)
with pg_engine.connect() as conn:
    conn.execute(text("SET session_replication_role = replica"))
    for table in reversed(list(Base.metadata.sorted_tables)):
        try:
            conn.execute(text(f'TRUNCATE TABLE "{table.name}" CASCADE'))
        except Exception:
            pass
    conn.execute(text("SET session_replication_role = DEFAULT"))
    conn.commit()

# Get table names from SQLite (only copy tables that exist in both)
sqlite_conn = sqlite3.connect(str(SQLITE_PATH))
sqlite_conn.row_factory = sqlite3.Row
cur = sqlite_conn.cursor()
cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
sqlite_tables = {row[0] for row in cur.fetchall()}
cur.close()
sqlite_conn.close()

# Tables in dependency order
tables = [t for t in Base.metadata.sorted_tables if t.name in sqlite_tables]
total_rows = 0

with pg_engine.connect() as conn:
    conn.execute(text("SET session_replication_role = replica"))
    conn.commit()


def _adapt_row(row, columns):
    """Convert row so PostgreSQL types match (e.g. SQLite 0/1 -> boolean)."""
    out = []
    for i, col in enumerate(columns):
        val = row[i] if i < len(row) else None
        if val is None:
            out.append(None)
            continue
        # Boolean: SQLite stores 0/1, PostgreSQL needs True/False
        if col.type.__class__.__name__ == "Boolean":
            out.append(bool(val) if isinstance(val, (int, float)) else val)
        else:
            out.append(val)
    return tuple(out)


for table in tables:
    name = table.name
    col_names = [c.name for c in table.c]
    cols = ", ".join(f'"{c}"' for c in col_names)

    sqlite_conn = sqlite3.connect(str(SQLITE_PATH))
    cur = sqlite_conn.cursor()
    try:
        cur.execute(f'PRAGMA table_info("{name}")')
        pragma = cur.fetchall()
        # Pragma: (cid, name, type, notnull, default, pk)
        sqlite_col_order = [r[1] for r in pragma]
        if set(sqlite_col_order) != set(col_names):
            missing = set(col_names) - set(sqlite_col_order)
            extra = set(sqlite_col_order) - set(col_names)
            if missing and not extra:
                print(f"  Skip {name}: SQLite missing columns {missing}")
            else:
                print(f"  Skip {name}: column mismatch")
            cur.close()
            sqlite_conn.close()
            continue
        # Map: for each col in table.c order, index in SQLite row (SELECT *)
        col_idx = [sqlite_col_order.index(c) for c in col_names]
        cur.execute(f'SELECT * FROM "{name}"')
        rows = cur.fetchall()
    except Exception as e:
        print(f"  Skip {name}: {e}")
        cur.close()
        sqlite_conn.close()
        continue
    cur.close()
    sqlite_conn.close()

    if not rows:
        print(f"  {name}: 0 rows")
        continue

    # Reorder each row to match table.c and adapt types
    reordered = [tuple(r[i] for i in col_idx) for r in rows]
    data = [_adapt_row(r, list(table.c)) for r in reordered]
    insert_sql = f'INSERT INTO "{name}" ({cols}) VALUES %s'

    try:
        with pg_engine.raw_connection() as raw:
            with raw.cursor() as pg_cur:
                execute_values(pg_cur, insert_sql, data, page_size=500)
            raw.commit()
        total_rows += len(data)
        print(f"  {name}: {len(data)} rows")
    except Exception as e:
        print(f"  {name}: ERROR {e}")

with pg_engine.connect() as conn:
    conn.execute(text("SET session_replication_role = DEFAULT"))
    conn.commit()

# Reset sequences
with pg_engine.connect() as conn:
    for table in tables:
        name = table.name
        pk = table.primary_key
        if not pk or len(pk.columns) != 1:
            continue
        col = list(pk.columns)[0]
        if col.autoincrement or col.type.__class__.__name__ in ("INTEGER", "BigInteger", "Serial"):
            try:
                conn.execute(
                    text(
                        f"SELECT setval(pg_get_serial_sequence('{name}', '{col.name}'), "
                        f'COALESCE((SELECT MAX("{col.name}") FROM "{name}"), 1))'
                    )
                )
            except Exception:
                pass
    conn.commit()

print(f"Done. Total rows copied: {total_rows}")
