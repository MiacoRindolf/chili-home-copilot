"""Check PostgreSQL for duplicate primary keys (sanity) and duplicate logical scan patterns.

The SQLite merge uses ``ON CONFLICT (primary key) DO NOTHING``, so duplicate PKs should not
exist. This script optionally reports rows that share the same pattern name and rules hash
(different ids), which you may want to merge manually.

Usage (repo root, ``DATABASE_URL`` set)::

    conda run -n chili-env python scripts/audit_postgres_merge_redundancy.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
except Exception:
    pass

from sqlalchemy import text

_repo = Path(__file__).resolve().parents[1]
_scripts_dir = Path(__file__).resolve().parent
for _p in (_scripts_dir, _repo):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
from app.config import settings
from pg_connection import create_postgres_engine_connected


def main() -> int:
    url = (os.environ.get("DATABASE_URL") or settings.database_url or "").strip()
    low = url.lower()
    if not (
        low.startswith("postgresql://")
        or low.startswith("postgresql+psycopg2://")
        or low.startswith("postgresql+psycopg://")
    ):
        print("DATABASE_URL must be PostgreSQL.", file=sys.stderr)
        return 1

    eng = create_postgres_engine_connected(url)
    issues = 0

    with eng.connect() as conn:
        # Sanity: duplicate ids impossible if PK enforced — detect broken catalog
        r = conn.execute(
            text(
                """
                SELECT COUNT(*) - COUNT(DISTINCT id) FROM scan_patterns
                """
            )
        )
        dup_pk_sp = r.scalar() or 0
        if dup_pk_sp:
            print(f"UNEXPECTED: scan_patterns duplicate id count delta = {dup_pk_sp}")
            issues += 1
        else:
            print("scan_patterns: no duplicate primary keys (id).")

        r = conn.execute(
            text(
                """
                SELECT COUNT(*) - COUNT(DISTINCT id) FROM trading_insights
                """
            )
        )
        dup_pk_ti = r.scalar() or 0
        if dup_pk_ti:
            print(f"UNEXPECTED: trading_insights duplicate id count delta = {dup_pk_ti}")
            issues += 1
        else:
            print("trading_insights: no duplicate primary keys (id).")

        # Logical duplicates: same name + same rules content (different ids)
        rows = conn.execute(
            text(
                """
                SELECT trim(name), md5(rules_json::text) AS h, COUNT(*) AS c,
                       array_agg(id ORDER BY id) AS ids
                FROM scan_patterns
                GROUP BY trim(name), md5(rules_json::text)
                HAVING COUNT(*) > 1
                ORDER BY c DESC
                LIMIT 50
                """
            )
        ).fetchall()

        if not rows:
            print("scan_patterns: no duplicate (name, rules_json) groups (logical dup check).")
        else:
            print(
                f"scan_patterns: {len(rows)} group(s) with same name+rules hash "
                f"(different ids — review if unwanted):"
            )
            issues += len(rows)
            for name, _h, c, ids in rows:
                snip = (name or "")[:72] + ("…" if name and len(name) > 72 else "")
                print(f"  count={c} ids={ids} name={snip!r}")
            print(
                "  Fix: scripts/dedupe_scan_patterns_by_rules.py --apply "
                "(keeps newest row per group; see script docstring).",
                file=sys.stderr,
            )

    data_dir = Path(__file__).resolve().parents[1] / "data"
    legacy = data_dir / "chili.db"
    archived = list(data_dir.glob("chili.db.archived*"))
    if legacy.is_file():
        print(
            f"NOTE: {legacy} still exists — only migration scripts should open it; "
            "archive or remove after merge.",
            file=sys.stderr,
        )
        issues += 1
    elif archived:
        print(f"Legacy SQLite archived ({len(archived)} file(s)); app uses PostgreSQL only.")

    return 1 if issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
