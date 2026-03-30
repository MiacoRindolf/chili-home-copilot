"""Copy ScanPattern-related rows from an older Postgres (e.g. 15) into the current DB (e.g. 16).

Only inserts rows whose primary key **id** is not already present in the target. If the same
numeric id means different logical rows in old vs new, resolve that manually first — this
script assumes ids are comparable (e.g. new DB was mostly empty or continued the same lineage).

**Typical flow**

1. Find the **volume** that held Postgres 15 data (``docker volume ls``). The image tag alone
   does not store your data; the named volume does.

2. Start a **temporary** Postgres 15 container on another host port, mounting that volume::

       docker run -d --name pg15-migrate ^
         -e POSTGRES_USER=chili -e POSTGRES_PASSWORD=chili -e POSTGRES_DB=chili ^
         -v YOUR_OLD_VOLUME_NAME:/var/lib/postgresql/data ^
         -p 5434:5432 postgres:15-alpine

3. Set URLs and run (from repo root, ``chili-env``)::

       set SOURCE_DATABASE_URL=postgresql://chili:chili@localhost:5434/chili
       set DATABASE_URL=postgresql://chili:chili@localhost:5433/chili
       conda run -n chili-env python scripts/merge_scan_patterns_from_old_postgres.py

4. Optional: ``--with-evidence`` to also merge ``trading_insight_evidence`` for copied insights.

5. Stop/remove ``pg15-migrate`` when done. You may then ``docker image rm`` the old image if unused.

**Backup target first** (pg_dump or snapshot) before merging.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
except Exception:
    pass

import psycopg2
from psycopg2 import sql
from psycopg2.extras import execute_values


def _connect(url: str):
    if not url or not url.strip():
        raise SystemExit("Missing database URL.")
    return psycopg2.connect(url)


def _table_columns(conn, table: str) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = %s
            ORDER BY ordinal_position
            """,
            (table,),
        )
        return [r[0] for r in cur.fetchall()]


def _existing_ids(conn, table: str) -> set[int]:
    with conn.cursor() as cur:
        cur.execute(sql.SQL("SELECT id FROM {}").format(sql.Identifier(table)))
        return {r[0] for r in cur.fetchall()}


def _fetch_rows_to_merge(
    old_conn,
    table: str,
    columns: list[str],
    skip_ids: set[int],
) -> list[tuple]:
    if not columns:
        return []
    select = sql.SQL("SELECT {} FROM {}").format(
        sql.SQL(", ").join(sql.Identifier(c) for c in columns),
        sql.Identifier(table),
    )
    with old_conn.cursor() as cur:
        cur.execute(select)
        rows = []
        for row in cur.fetchall():
            rid = row[0]
            if rid not in skip_ids:
                rows.append(row)
        return rows


def _insert_rows(
    new_conn,
    table: str,
    columns: list[str],
    rows: list[tuple],
    dry_run: bool,
) -> int:
    if not rows:
        return 0
    if dry_run:
        print(f"  [dry-run] would insert {len(rows)} row(s) into {table}")
        return len(rows)
    cols_sql = sql.SQL(", ").join(sql.Identifier(c) for c in columns)
    insert = sql.SQL("INSERT INTO {} ({}) VALUES %s").format(
        sql.Identifier(table),
        cols_sql,
    )
    with new_conn.cursor() as cur:
        execute_values(cur, insert.as_string(new_conn), rows)
    new_conn.commit()
    return len(rows)


def _set_sequence(new_conn, table: str, col: str = "id") -> None:
    # Table names are fixed literals in this script (not user input).
    with new_conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT setval(
                pg_get_serial_sequence('{table}', '{col}'),
                (SELECT COALESCE(MAX({col}), 1) FROM {table})
            )
            """
        )
    new_conn.commit()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("**Typical flow**")[0].strip())
    parser.add_argument(
        "--source",
        default=os.environ.get("SOURCE_DATABASE_URL", "").strip(),
        help="Old Postgres URL (or set SOURCE_DATABASE_URL)",
    )
    parser.add_argument(
        "--target",
        default=os.environ.get("DATABASE_URL", "").strip(),
        help="New Postgres URL (default: DATABASE_URL)",
    )
    parser.add_argument(
        "--with-evidence",
        action="store_true",
        help="Also merge trading_insight_evidence for merged insights",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not args.source:
        print("Set --source or SOURCE_DATABASE_URL to the old database.", file=sys.stderr)
        return 1
    if not args.target:
        print("Set --target or DATABASE_URL to the new database.", file=sys.stderr)
        return 1

    old = _connect(args.source)
    new = _connect(args.target)

    try:
        # --- scan_patterns ---
        tgt_sp = _table_columns(new, "scan_patterns")
        cols_sp = [c for c in _table_columns(old, "scan_patterns") if c in tgt_sp]
        if not cols_sp:
            print("scan_patterns: no common columns; abort.")
            return 1
        if len(cols_sp) < len(tgt_sp):
            print(
                f"Note: scan_patterns merge uses {len(cols_sp)}/{len(tgt_sp)} columns "
                "(new DB has extra columns; defaults apply).",
                file=sys.stderr,
            )
        existing_sp = _existing_ids(new, "scan_patterns")
        rows_sp = _fetch_rows_to_merge(old, "scan_patterns", cols_sp, existing_sp)
        n_sp = _insert_rows(new, "scan_patterns", cols_sp, rows_sp, args.dry_run)
        print(
            f"scan_patterns: inserted {n_sp} row(s) "
            f"({len(existing_sp)} id(s) already existed in target and were not overwritten)."
        )
        if n_sp and not args.dry_run:
            _set_sequence(new, "scan_patterns")

        # --- trading_insights (FK scan_patterns.id) ---
        cols_ti = [
            c
            for c in _table_columns(old, "trading_insights")
            if c in _table_columns(new, "trading_insights")
        ]
        if not cols_ti:
            print("trading_insights: no common columns; abort.")
            return 1

        existing_ti = _existing_ids(new, "trading_insights")
        valid_pattern_ids = set(_existing_ids(new, "scan_patterns"))
        if args.dry_run and rows_sp:
            valid_pattern_ids |= {row[0] for row in rows_sp}
        idx_iid = cols_ti.index("id")
        idx_spid = cols_ti.index("scan_pattern_id")
        select = sql.SQL("SELECT {} FROM {}").format(
            sql.SQL(", ").join(sql.Identifier(c) for c in cols_ti),
            sql.Identifier("trading_insights"),
        )
        rows_ti: list[tuple] = []
        with old.cursor() as cur:
            cur.execute(select)
            for row in cur.fetchall():
                rid = row[idx_iid]
                spid = row[idx_spid]
                if rid in existing_ti:
                    continue
                if spid not in valid_pattern_ids:
                    continue
                rows_ti.append(row)
        n_ti = _insert_rows(new, "trading_insights", cols_ti, rows_ti, args.dry_run)
        print(
            f"trading_insights: inserted {n_ti} row(s) "
            f"(skipped existing ids or orphan scan_pattern_id vs target)."
        )
        if n_ti and not args.dry_run:
            _set_sequence(new, "trading_insights")

        if args.with_evidence:
            cols_ev = [
                c
                for c in _table_columns(old, "trading_insight_evidence")
                if c in _table_columns(new, "trading_insight_evidence")
            ]
            if cols_ev:
                existing_ev = _existing_ids(new, "trading_insight_evidence")
                valid_insight_ids = _existing_ids(new, "trading_insights")
                select_ev = sql.SQL("SELECT {} FROM {}").format(
                    sql.SQL(", ").join(sql.Identifier(c) for c in cols_ev),
                    sql.Identifier("trading_insight_evidence"),
                )
                idx_eid = cols_ev.index("id")
                idx_insight = cols_ev.index("insight_id")
                if args.dry_run and rows_ti:
                    valid_insight_ids |= {row[idx_iid] for row in rows_ti}
                rows_ev: list[tuple] = []
                with old.cursor() as cur:
                    cur.execute(select_ev)
                    for row in cur.fetchall():
                        rid = row[idx_eid]
                        iid = row[idx_insight]
                        if rid in existing_ev:
                            continue
                        if iid not in valid_insight_ids:
                            continue
                        rows_ev.append(row)
                n_ev = _insert_rows(
                    new, "trading_insight_evidence", cols_ev, rows_ev, args.dry_run
                )
                print(f"trading_insight_evidence: inserted {n_ev} row(s).")
                if n_ev and not args.dry_run:
                    _set_sequence(new, "trading_insight_evidence")
            else:
                print("trading_insight_evidence: no common columns; skip.")

        if args.dry_run:
            new.rollback()
        return 0
    finally:
        old.close()
        new.close()


if __name__ == "__main__":
    raise SystemExit(main())
