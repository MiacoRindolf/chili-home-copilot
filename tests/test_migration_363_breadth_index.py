"""MIG 363 — ang index na nagdedefang sa 100-180s na breadth scan.

Ang scan predicate: `live_eligible = true AND symbol NOT LIKE '%-USD%'` sa
30-araw na saklaw ng observed_at sa 31M-row/11GB table. Dati: heap fetch bawat
row sa saklaw. Ngayon: index-only scan sa maliit na partial index.
"""
from __future__ import annotations

from sqlalchemy import text

from app.migrations import (
    MIGRATIONS,
    _migration_363_breadth_scan_partial_covering_index,
)


def test_migration_registered_exactly_once():
    ids = [m[0] for m in MIGRATIONS]
    assert ids.count("363_breadth_scan_partial_covering_index") == 1


def test_creates_the_partial_covering_index(db):
    conn = db.connection()
    _migration_363_breadth_scan_partial_covering_index(conn)
    row = db.execute(text(
        "select indexdef from pg_indexes "
        "where indexname='ix_mvh_live_eligible_observed'"
    )).fetchone()
    assert row is not None
    idxdef = row[0]
    assert "WHERE" in idxdef and "live_eligible" in idxdef
    assert "INCLUDE" in idxdef and "viability_score" in idxdef
    # IDEMPOTENT
    _migration_363_breadth_scan_partial_covering_index(conn)


def test_migration_364_registered_and_creates_the_sessions_index(db):
    from app.migrations import _migration_364_active_sessions_partial_index

    ids = [m[0] for m in MIGRATIONS]
    assert ids.count("364_active_sessions_partial_index") == 1
    conn = db.connection()
    _migration_364_active_sessions_partial_index(conn)
    row = db.execute(text(
        "select indexdef from pg_indexes where indexname='ix_tas_live_active'"
    )).fetchone()
    assert row is not None
    assert "WHERE" in row[0] and "live_arm_expired" in row[0]
    # IDEMPOTENT
    _migration_364_active_sessions_partial_index(conn)
