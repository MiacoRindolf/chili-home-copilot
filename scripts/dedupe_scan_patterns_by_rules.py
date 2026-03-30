"""Collapse logical duplicate ``scan_patterns`` rows (same name + same ``rules_json``).

For each duplicate group, **keeps the newest row** in the current database (highest
``updated_at``, then ``created_at``, then ``id``). All foreign references to the
older ids are repointed to the winner, then loser rows are deleted.

**Backup Postgres first.** Run::

    conda run -n chili-env python scripts/dedupe_scan_patterns_by_rules.py --dry-run
    conda run -n chili-env python scripts/dedupe_scan_patterns_by_rules.py --apply

Uses ``DATABASE_URL`` (PostgreSQL only).
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

from sqlalchemy import text

_scripts_dir = Path(__file__).resolve().parent
if str(_scripts_dir) not in sys.path:
    sys.path.insert(0, str(_scripts_dir))
from pg_connection import create_postgres_engine_connected

# (table, column) — int columns referencing scan_patterns.id (no FK in ORM does not matter)
_REF_COLUMNS: list[tuple[str, str]] = [
    ("trading_insights", "scan_pattern_id"),
    ("trading_trades", "scan_pattern_id"),
    ("trading_backtests", "scan_pattern_id"),
    ("trading_alerts", "scan_pattern_id"),
    ("trading_proposals", "scan_pattern_id"),
    ("trading_pattern_trades", "scan_pattern_id"),
    ("trading_pattern_evidence_hypotheses", "scan_pattern_id"),
]


def _dup_groups(conn):
    rows = conn.execute(
        text(
            """
            SELECT
              trim(name) AS name_norm,
              md5(rules_json::text) AS rules_md5,
              array_agg(id ORDER BY updated_at DESC, created_at DESC, id DESC) AS ids
            FROM scan_patterns
            GROUP BY trim(name), md5(rules_json::text)
            HAVING COUNT(*) > 1
            """
        )
    ).fetchall()
    out = []
    for name, _h, ids in rows:
        if not ids:
            continue
        winner = int(ids[0])
        losers = [int(x) for x in ids[1:]]
        out.append((name, winner, losers))
    return out


def _table_exists(conn, table: str) -> bool:
    r = conn.execute(
        text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = :t LIMIT 1"
        ),
        {"t": table},
    ).scalar()
    return r is not None


def _column_exists(conn, table: str, col: str) -> bool:
    r = conn.execute(
        text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = :t AND column_name = :c LIMIT 1"
        ),
        {"t": table, "c": col},
    ).scalar()
    return r is not None


def _repoint(conn, table: str, col: str, loser: int, winner: int) -> int:
    if not _table_exists(conn, table) or not _column_exists(conn, table, col):
        return 0
    r = conn.execute(
        text(f'UPDATE "{table}" SET "{col}" = :w WHERE "{col}" = :l'),
        {"w": winner, "l": loser},
    )
    return r.rowcount or 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("**Backup")[0].strip())
    ap.add_argument(
        "--apply",
        action="store_true",
        help="Perform repoint + delete (default is dry-run: print plan only)",
    )
    args = ap.parse_args()
    dry = not args.apply

    url = (os.environ.get("DATABASE_URL") or "").strip()
    low = url.lower()
    if not (
        low.startswith("postgresql://")
        or low.startswith("postgresql+psycopg2://")
        or low.startswith("postgresql+psycopg://")
    ):
        print("Set DATABASE_URL to PostgreSQL.", file=sys.stderr)
        return 1

    eng = create_postgres_engine_connected(url)

    with eng.connect() as conn:
        groups = _dup_groups(conn)
        if not groups:
            print("No logical duplicate scan_patterns groups (name + rules_json hash).")
            return 0

        total_losers = sum(len(ls) for _, _, ls in groups)
        print(f"Found {len(groups)} duplicate group(s), {total_losers} loser row(s) to remove.")

        for name, winner, losers in groups:
            snip = (name or "")[:60] + ("…" if name and len(name) > 60 else "")
            print(f"  keep id={winner} drop {losers} name={snip!r}")

        if dry:
            for name, winner, losers in groups:
                for loser in losers:
                    n_win = conn.execute(
                        text(
                            "SELECT COUNT(*) FROM scan_patterns "
                            "WHERE id = :w AND parent_id = :l"
                        ),
                        {"w": winner, "l": loser},
                    ).scalar()
                    if n_win:
                        print(
                            f"    [dry-run] would clear winner.parent_id "
                            f"(winner {winner} pointed at loser {loser})"
                        )
                    n_sp = conn.execute(
                        text("SELECT COUNT(*) FROM scan_patterns WHERE parent_id = :l"),
                        {"l": loser},
                    ).scalar()
                    if n_sp:
                        print(
                            f"    [dry-run] would repoint scan_patterns.parent_id "
                            f"{loser}->{winner} ({n_sp} row(s))"
                        )
                    for tbl, col in _REF_COLUMNS:
                        if not _table_exists(conn, tbl) or not _column_exists(conn, tbl, col):
                            continue
                        c = conn.execute(
                            text(f'SELECT COUNT(*) FROM "{tbl}" WHERE "{col}" = :l'),
                            {"l": loser},
                        ).scalar()
                        if c:
                            print(f"    [dry-run] would repoint {tbl}.{col} {loser}->{winner} ({c} rows)")
            print("Dry-run only. Pass --apply to execute.")
            return 0

    # apply — one transaction
    with eng.begin() as conn:
        groups = _dup_groups(conn)
        for name, winner, losers in groups:
            for loser in sorted(losers):
                conn.execute(
                    text(
                        "UPDATE scan_patterns SET parent_id = NULL "
                        "WHERE id = :w AND parent_id = :l"
                    ),
                    {"w": winner, "l": loser},
                )
                conn.execute(
                    text("UPDATE scan_patterns SET parent_id = :w WHERE parent_id = :l"),
                    {"w": winner, "l": loser},
                )
                for tbl, col in _REF_COLUMNS:
                    n = _repoint(conn, tbl, col, loser, winner)
                    if n:
                        print(f"    repointed {tbl}.{col}: {n} row(s) {loser}->{winner}")
                conn.execute(text("DELETE FROM scan_patterns WHERE id = :l"), {"l": loser})
            print(f"  deduped group name={name!r} -> winner id={winner}")

    print("Done. Re-run scripts/audit_postgres_merge_redundancy.py to verify.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
