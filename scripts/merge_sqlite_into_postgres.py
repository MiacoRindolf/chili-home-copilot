"""Merge legacy SQLite ``data/chili.db`` into PostgreSQL without truncating the target.

Unlike ``migrate_legacy_sqlite_to_postgres.py`` (full replace), this script **only inserts**
rows whose primary key is **not already present** in Postgres (`ON CONFLICT … DO NOTHING`).
Re-running the script does **not** add a second row for the same primary key, so there is no
PK-level redundancy from the merge. For ``scan_patterns``, rows whose **name + rules_json**
already exist in Postgres (same hash as ``md5(rules_json::text)``) are **skipped** so the
current database wins; within SQLite-only duplicates, **newest** ``updated_at`` then ``id``
is kept. Residual duplicates: ``scripts/dedupe_scan_patterns_by_rules.py --apply``.

Uses ``session_replication_role = replica`` for the session (same idea as the full migrate)
so foreign-key order matches the bulk import path when SQLite references rows not yet in PG.

**Backup Postgres first** (``pg_dump``). Run from repo root with ``DATABASE_URL`` set::

    conda run -n chili-env python scripts/merge_sqlite_into_postgres.py --dry-run
    conda run -n chili-env python scripts/merge_sqlite_into_postgres.py

Optional: ``--sqlite-path path/to/chili.db`` if the file is not under ``data/chili.db``.

**Caveat:** If the same numeric ``id`` in SQLite and Postgres refers to **different** logical
rows (two databases evolved separately), you get skipped inserts for those ids; resolve manually.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
except Exception:
    pass

from psycopg2.extras import Json, execute_values
from sqlalchemy import String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import JSON

_repo_root = Path(__file__).resolve().parents[1]
_scripts_dir = Path(__file__).resolve().parent
for _p in (_scripts_dir, _repo_root):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
from app.db import Base
import app.models  # noqa: F401 - register metadata
from pg_connection import create_postgres_engine_connected

DATA_DIR = Path(__file__).resolve().parents[1] / "data"


def _adapt_row(row: tuple, columns: list) -> tuple:
    out = []
    for i, col in enumerate(columns):
        val = row[i] if i < len(row) else None
        if val is None:
            out.append(None)
            continue
        tname = col.type.__class__.__name__
        if tname == "Boolean":
            out.append(bool(val) if isinstance(val, (int, float)) else val)
            continue
        if isinstance(col.type, (JSON, JSONB)) or tname in ("JSON", "JSONB"):
            if isinstance(val, (dict, list)):
                out.append(Json(val))
            else:
                out.append(val)
            continue
        if isinstance(col.type, String) and col.type.length:
            ln = int(col.type.length)
            if isinstance(val, str) and len(val) > ln:
                val = val[:ln]
        out.append(val)
    return tuple(out)


def _pk_names(table) -> list[str] | None:
    pk = table.primary_key
    if pk is None or len(pk.columns) == 0:
        return None
    return [c.name for c in pk.columns]


def _archive_sqlite_bundle(db_path: Path) -> None:
    """Rename chili.db and optional -wal / -shm siblings so nothing uses the legacy file."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    parent = db_path.parent
    base_name = db_path.name
    for src in (db_path, parent / f"{base_name}-wal", parent / f"{base_name}-shm"):
        if src.is_file():
            dest = parent / f"{src.name}.archived.{ts}"
            src.rename(dest)
            print(f"Archived {src.name} -> {dest.name}")


def _scan_pattern_logical_key(name: object, rules_json: object) -> tuple[str, str]:
    """Match PostgreSQL ``(name, md5(rules_json::text))`` for duplicate detection."""
    n = (str(name) if name is not None else "").strip()
    r = rules_json if isinstance(rules_json, str) else (
        str(rules_json) if rules_json is not None else ""
    )
    h = hashlib.md5(r.encode("utf-8")).hexdigest()
    return (n, h)


def _existing_pk_tuples(pg_cur, table_name: str, pk_cols: list[str]) -> set:
    cols = ", ".join(f'"{c}"' for c in pk_cols)
    pg_cur.execute(f'SELECT {cols} FROM "{table_name}"')
    rows = pg_cur.fetchall()
    if len(pk_cols) == 1:
        return {r[0] for r in rows}
    return {tuple(r) for r in rows}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("**Backup")[0].strip())
    parser.add_argument(
        "--sqlite-path",
        type=Path,
        default=DATA_DIR / "chili.db",
        help="Path to legacy SQLite file (default: data/chili.db)",
    )
    parser.add_argument(
        "--database-url",
        default=(os.environ.get("DATABASE_URL") or "").strip(),
        help="Target PostgreSQL URL (default: DATABASE_URL)",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--archive-sqlite-after",
        action="store_true",
        help="After a successful run (no per-table errors), rename legacy SQLite files on disk",
    )
    args = parser.parse_args()

    sqlite_path: Path = args.sqlite_path
    if not sqlite_path.is_file():
        print(f"SQLite file not found: {sqlite_path}")
        return 1

    db_url = args.database_url
    low = db_url.lower()
    if not (
        low.startswith("postgresql://")
        or low.startswith("postgresql+psycopg2://")
        or low.startswith("postgresql+psycopg://")
    ):
        print("Set DATABASE_URL or --database-url to PostgreSQL.")
        return 1

    sqlite_conn = sqlite3.connect(str(sqlite_path))
    sqlite_conn.row_factory = sqlite3.Row
    sq = sqlite_conn.cursor()
    sq.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )
    sqlite_tables = {row[0] for row in sq.fetchall()}

    pg_engine = create_postgres_engine_connected(db_url)
    Base.metadata.create_all(bind=pg_engine)

    tables = [t for t in Base.metadata.sorted_tables if t.name in sqlite_tables]
    total_inserted = 0
    table_errors = 0

    with pg_engine.raw_connection() as raw:
        raw.autocommit = False
        pg_cur = raw.cursor()
        pg_cur.execute("SET session_replication_role = replica")

        for table in tables:
            name = table.name
            pk_cols = _pk_names(table)
            if not pk_cols:
                print(f"  {name}: skip (no primary key in model)")
                continue

            col_names = [c.name for c in table.c]
            sq.execute(f'PRAGMA table_info("{name}")')
            pragma = sq.fetchall()
            sqlite_col_order = [r[1] for r in pragma]
            sqlite_set = set(sqlite_col_order)
            insert_cols = [c for c in col_names if c in sqlite_set]
            if not insert_cols:
                print(f"  {name}: skip — no overlapping columns")
                continue
            if not all(c in sqlite_set for c in pk_cols):
                print(f"  {name}: skip — SQLite missing primary key column(s)")
                continue

            try:
                col_idx = [sqlite_col_order.index(c) for c in insert_cols]
            except ValueError as e:
                print(f"  {name}: skip — column index {e}")
                continue

            try:
                logical_existing: set[tuple[str, str]] | None = None
                logical_seen: set[tuple[str, str]] | None = None
                skip_logical = 0
                if name == "scan_patterns":
                    pg_cur.execute(
                        "SELECT TRIM(name), md5(rules_json::text) FROM scan_patterns"
                    )
                    logical_existing = set(pg_cur.fetchall())
                    logical_seen = set()
                    try:
                        sq.execute(
                            'SELECT * FROM "scan_patterns" '
                            "ORDER BY updated_at DESC, id DESC"
                        )
                    except sqlite3.OperationalError:
                        sq.execute('SELECT * FROM "scan_patterns"')
                else:
                    sq.execute(f'SELECT * FROM "{name}"')
                existing = _existing_pk_tuples(pg_cur, name, pk_cols)
                sa_cols = [table.c[c] for c in insert_cols]

                batch: list[tuple] = []
                would_skip = 0
                table_new = 0

                def flush() -> None:
                    nonlocal batch, total_inserted, table_new
                    if not batch:
                        return
                    if args.dry_run:
                        table_new += len(batch)
                        batch.clear()
                        return
                    cols_sql = ", ".join(f'"{c}"' for c in insert_cols)
                    conflict = ", ".join(f'"{c}"' for c in pk_cols)
                    ins = (
                        f'INSERT INTO "{name}" ({cols_sql}) VALUES %s '
                        f"ON CONFLICT ({conflict}) DO NOTHING"
                    )
                    execute_values(pg_cur, ins, batch, page_size=800)
                    total_inserted += len(batch)
                    table_new += len(batch)
                    batch.clear()

                while True:
                    row = sq.fetchone()
                    if row is None:
                        break
                    tup = tuple(row[i] for i in col_idx)
                    pk_tuple = tuple(
                        tup[insert_cols.index(pk_cols[j])] for j in range(len(pk_cols))
                    )
                    if len(pk_cols) == 1:
                        pk_tuple = pk_tuple[0]
                    if pk_tuple in existing:
                        would_skip += 1
                        continue
                    if (
                        name == "scan_patterns"
                        and logical_existing is not None
                        and logical_seen is not None
                        and "name" in insert_cols
                        and "rules_json" in insert_cols
                    ):
                        lk = _scan_pattern_logical_key(
                            tup[insert_cols.index("name")],
                            tup[insert_cols.index("rules_json")],
                        )
                        if lk in logical_existing:
                            skip_logical += 1
                            continue
                        if lk in logical_seen:
                            skip_logical += 1
                            continue
                        logical_seen.add(lk)
                    if name == "trading_insights" and "scan_pattern_id" in insert_cols:
                        _spi = insert_cols.index("scan_pattern_id")
                        if tup[_spi] is None:
                            would_skip += 1
                            continue
                    existing.add(pk_tuple)
                    batch.append(_adapt_row(tup, sa_cols))
                    if len(batch) >= 800:
                        flush()

                flush()
                extra = (
                    f", {skip_logical} skipped (same name+rules as Postgres or older SQLite row)"
                    if name == "scan_patterns" and skip_logical
                    else ""
                )
                print(
                    f"  {name}: {'dry-run ' if args.dry_run else ''}"
                    f"{table_new} row(s) to insert, {would_skip} PK(s) already in Postgres{extra}"
                )
                if not args.dry_run:
                    raw.commit()
            except Exception as e:
                table_errors += 1
                print(f"  {name}: ERROR — {e}", file=sys.stderr)
                if not args.dry_run:
                    raw.rollback()

        pg_cur.execute("SET session_replication_role = DEFAULT")
        if not args.dry_run:
            raw.commit()
        pg_cur.close()

    sqlite_conn.close()

    if not args.dry_run:
        with pg_engine.connect() as conn:
            for table in tables:
                name = table.name
                pk_cols = _pk_names(table)
                if not pk_cols or len(pk_cols) != 1:
                    continue
                col = pk_cols[0]
                try:
                    conn.execute(
                        text(
                            f"SELECT setval(pg_get_serial_sequence('{name}', '{col}'), "
                            f'COALESCE((SELECT MAX("{col}") FROM "{name}"), 1))'
                        )
                    )
                except Exception:
                    pass
            conn.commit()

    print(
        f"Done. dry_run={args.dry_run} "
        f"insert_batches_rows≈{total_inserted if not args.dry_run else 'n/a'} "
        f"errors={table_errors}"
    )

    if (
        args.archive_sqlite_after
        and not args.dry_run
        and table_errors == 0
        and sqlite_path.is_file()
    ):
        _archive_sqlite_bundle(sqlite_path)
    elif args.archive_sqlite_after and table_errors:
        print("Skipping --archive-sqlite-after because one or more tables failed.", file=sys.stderr)

    return 1 if table_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
