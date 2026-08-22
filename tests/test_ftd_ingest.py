"""SEC FTD ingestion + squeeze-fuel delta (Ross "Forcing a Crash" 08-21).

Runnable: pytest tests/test_ftd_ingest.py -v
"""
from __future__ import annotations

import inspect
from datetime import date, timedelta
from unittest.mock import MagicMock

from app.migrations import MIGRATIONS, _migration_368_sec_fails_to_deliver
from app.services.trading.momentum_neural.ftd_ingest import (
    _candidate_periods,
    ftd_squeeze_viability_delta,
)


def test_migration_registered_exactly_once():
    ids = [m[0] for m in MIGRATIONS]
    assert ids.count("368_sec_fails_to_deliver") == 1
    # 366/367 ay nakalaan sa captured-paper branch — hindi dapat naririto.
    assert not any(i.startswith(("366_", "367_")) for i in ids)


def test_candidate_periods_recent_first():
    ps = _candidate_periods(date(2026, 8, 21))
    assert ps[0] == ("202608", "b") and ps[1] == ("202608", "a")
    assert ("202607", "b") in ps and ("202606", "a") in ps


def _db_with_ftd(settlement: date | None, qty: float):
    db = MagicMock()
    db.execute.return_value.fetchone.return_value = (
        (settlement, qty) if settlement else None
    )
    if settlement is None:
        db.execute.return_value.fetchone.return_value = None
    return db


def test_strong_evidence_delta():
    db = _db_with_ftd(date.today() - timedelta(days=10), 600_000)
    # 600k fails sa 10M float = 6% -> strong +0.05
    assert ftd_squeeze_viability_delta("LNAI", float_shares=10_000_000, db=db) == 0.05


def test_notable_evidence_delta():
    db = _db_with_ftd(date.today() - timedelta(days=10), 150_000)
    # 1.5% ng float -> +0.03
    assert ftd_squeeze_viability_delta("LNAI", float_shares=10_000_000, db=db) == 0.03


def test_small_or_stale_or_missing_is_zero():
    db = _db_with_ftd(date.today() - timedelta(days=10), 50_000)
    assert ftd_squeeze_viability_delta("X", float_shares=10_000_000, db=db) == 0.0
    stale = _db_with_ftd(date.today() - timedelta(days=90), 600_000)
    assert ftd_squeeze_viability_delta("X", float_shares=10_000_000, db=stale) == 0.0
    empty = _db_with_ftd(None, 0)
    assert ftd_squeeze_viability_delta("X", float_shares=10_000_000, db=empty) == 0.0
    assert ftd_squeeze_viability_delta("BTC-USD", float_shares=1e7, db=db) == 0.0
    assert ftd_squeeze_viability_delta("X", float_shares=None, db=db) == 0.0


def test_scorer_block_is_meta_driven_and_core_import_free():
    from app.services.trading.momentum_neural import viability

    src = inspect.getsource(viability.score_viability_explicit)
    assert "ftd_squeeze_deltas" in src
    import ast

    tree = ast.parse(src)
    assert not any(
        isinstance(n, (ast.Import, ast.ImportFrom)) for n in ast.walk(tree)
    ), "core must stay import-free"
