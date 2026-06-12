"""ARM-time tape freshness gate (2026-06-12 IPO morning).

Quiet mid-caps with hours-old quotes (RYAM/BBD/BMA/ACAD) consumed live slots
and sat behind stale_bbo while the real movers (ASBP/UBXG) ran with live
tape. No fresh tape row = not actually trading = never armable.
"""

from datetime import datetime, timedelta

from sqlalchemy import text

from app.services.trading.momentum_neural.auto_arm import _filter_fresh_tape


class _Row:
    def __init__(self, symbol):
        self.symbol = symbol


def _seed_tape(db, symbol, age_sec):
    db.execute(text(
        "INSERT INTO momentum_nbbo_spread_tape (symbol, observed_at, source) "
        "VALUES (:s, (now() at time zone 'utc') - make_interval(secs => :a), 'test')"
    ), {"s": symbol, "a": age_sec})
    db.commit()


def test_only_fresh_tape_names_survive(db):
    _seed_tape(db, "ASBP", 30)
    _seed_tape(db, "RYAM", 3600)
    rows = [_Row("ASBP"), _Row("RYAM"), _Row("NOTAPE")]
    out = _filter_fresh_tape(rows, max_age_sec=180.0)
    assert [r.symbol for r in out] == ["ASBP"]


def test_zero_cap_disables_gate(db):
    rows = [_Row("ANYTHING")]
    assert _filter_fresh_tape(rows, max_age_sec=0.0) == rows
