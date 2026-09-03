"""Unit tests for the Phase 4 hydrated-source canonicalizer.

Pure fakes: no database, no network.  The live lane is running.

The property under test is the one whose failure is INVISIBLE: the replay's
non-strict read has no ``source`` predicate, so two providers on one symbol-day
double every print without producing a single malformed row.  Measured on TMCR
2026-08-24 -- 16,933 + 16,933 rows in the table, 33,866 ticks out of
``load_trade_tape``.  These tests bind the rule that prevents it, and equally
bind the case where dropping would DESTROY coverage rather than protect it.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import historical_tick_hydrator as hyd  # noqa: E402
from scripts.hydration_canonicalize import TABLES, plan  # noqa: E402

D = date(2026, 8, 24)
D2 = date(2026, 8, 19)

TRADE_PREFS = TABLES[hyd.TRADES_TABLE][1]
NBBO_PREFS = TABLES[hyd.NBBO_TABLE][1]


def test_the_tmcr_duplication_is_planned_away():
    """The exact measured defect: both providers on one symbol-day."""
    rows = [("TMCR", D, hyd.SOURCE_IQFEED_TRADES, 16933),
            ("TMCR", D, hyd.SOURCE_POLYGON_TRADES, 16933)]
    drops = plan(rows, TRADE_PREFS)

    assert len(drops) == 1
    assert drops[0]["keep"] == hyd.SOURCE_IQFEED_TRADES
    assert drops[0]["drop"] == hyd.SOURCE_POLYGON_TRADES
    assert drops[0]["rows"] == 16933
    assert drops[0]["kept_rows"] == 16933


def test_single_source_symbol_day_is_left_alone():
    rows = [("LGCL", D, hyd.SOURCE_IQFEED_TRADES, 310382)]
    assert plan(rows, TRADE_PREFS) == []


def test_fallback_standing_alone_is_never_dropped():
    """Coverage must survive canonicalization.

    IQFeed returned no top-of-book at all for OTC names (REEMF 2026-08-19,
    NLST 2026-08-20) whose trades loaded fine.  Symmetrically, a symbol-day
    covered ONLY by the lower-preference source is the only tape there is --
    dropping it would turn a working symbol-day into a hole.
    """
    rows = [("REEMF", D2, hyd.SOURCE_POLYGON_TRADES, 3677)]
    assert plan(rows, TRADE_PREFS) == []

    nbbo_rows = [("REEMF", D2, hyd.SOURCE_IQFEED_NBBO, 1200)]
    assert plan(nbbo_rows, NBBO_PREFS) == []


def test_nbbo_preference_is_polygon_not_iqfeed():
    """The Phase 3 verdict, inverted relative to trades, and easy to get wrong.

    IQFeed lookup carries only the quote attached to each print, so it cannot
    represent a quote that MOVES between trades -- exactly what spread floors
    and stale-BBO vetoes read.
    """
    rows = [("CANF", D, hyd.SOURCE_IQFEED_NBBO, 185430),
            ("CANF", D, hyd.SOURCE_POLYGON_NBBO, 103057)]
    drops = plan(rows, NBBO_PREFS)

    assert len(drops) == 1
    assert drops[0]["keep"] == hyd.SOURCE_POLYGON_NBBO
    assert drops[0]["drop"] == hyd.SOURCE_IQFEED_NBBO
    # ...and preference is NOT "whichever has more rows".
    assert drops[0]["kept_rows"] < drops[0]["rows"]


def test_zero_row_source_does_not_count_as_present():
    """A source with 0 rows must not win, nor trigger a pointless delete."""
    rows = [("AAA", D, hyd.SOURCE_IQFEED_TRADES, 0),
            ("AAA", D, hyd.SOURCE_POLYGON_TRADES, 5000)]
    assert plan(rows, TRADE_PREFS) == []


def test_each_symbol_day_is_planned_independently():
    rows = [
        ("TMCR", D, hyd.SOURCE_IQFEED_TRADES, 16933),
        ("TMCR", D, hyd.SOURCE_POLYGON_TRADES, 16933),
        ("LGCL", D, hyd.SOURCE_IQFEED_TRADES, 310382),
        ("REEMF", D2, hyd.SOURCE_POLYGON_TRADES, 3677),
    ]
    drops = plan(rows, TRADE_PREFS)
    assert [d["symbol"] for d in drops] == ["TMCR"]


def test_same_symbol_different_days_are_separate_slices():
    """A day-scoped delete must not take the other day with it."""
    rows = [
        ("CANF", D, hyd.SOURCE_IQFEED_TRADES, 100),
        ("CANF", D, hyd.SOURCE_POLYGON_TRADES, 100),
        ("CANF", D2, hyd.SOURCE_IQFEED_TRADES, 200),
        ("CANF", D2, hyd.SOURCE_POLYGON_TRADES, 200),
    ]
    drops = plan(rows, TRADE_PREFS)
    assert len(drops) == 2
    assert {d["day"] for d in drops} == {str(D), str(D2)}
    assert all(d["drop"] == hyd.SOURCE_POLYGON_TRADES for d in drops)


def test_preference_lists_cover_exactly_the_hydrated_sources():
    """A source the hydrator writes but this file does not rank would be
    invisible to the guard -- it would duplicate silently, which is the whole
    failure mode."""
    ranked = set(TRADE_PREFS) | set(NBBO_PREFS)
    assert ranked == set(hyd.HYDRATED_SOURCES)
    assert set(TABLES) == {hyd.TRADES_TABLE, hyd.NBBO_TABLE}


class RecordingCursor:
    def __init__(self):
        self.sql = []
        self.params = []
        self.rowcount = 7

    def execute(self, sql, params=None):
        self.sql.append(" ".join(sql.split()))
        self.params.append(params)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class RecordingConn:
    def __init__(self):
        self.cur = RecordingCursor()
        self.commits = 0

    def cursor(self):
        return self.cur

    def commit(self):
        self.commits += 1


def test_delete_is_index_friendly_and_day_scoped():
    """The delete must key on (source, symbol, observed_at range).

    Both tables carry a (source, symbol, observed_at) btree. Keying the delete
    on the ET-day EXPRESSION this module groups by would be correct but could
    not use that index -- one sequential scan of a multi-million-row table per
    slice, while a load may be running against it.
    """
    from scripts.hydration_canonicalize import apply_drops

    conn = RecordingConn()
    drops = [{"symbol": "TMCR", "day": "2026-08-24",
              "keep": hyd.SOURCE_IQFEED_TRADES,
              "drop": hyd.SOURCE_POLYGON_TRADES, "rows": 16933, "kept_rows": 16933}]
    removed = apply_drops(conn, hyd.TRADES_TABLE, drops)

    sql = conn.cur.sql[0]
    assert "source = %s AND symbol = %s" in sql
    assert "observed_at >= %s AND observed_at < %s" in sql
    assert "AT TIME ZONE" not in sql  # no expression predicate
    assert removed == 7
    assert conn.commits == 1

    src, sym, lo, hi = conn.cur.params[0]
    assert (src, sym) == (hyd.SOURCE_POLYGON_TRADES, "TMCR")
    # ET calendar day 2026-08-24 (EDT) == [04:00Z, 04:00Z next day), and the
    # trade tape is naive-UTC so the bounds must carry no tzinfo.
    assert lo.tzinfo is None and hi.tzinfo is None
    assert lo.isoformat() == "2026-08-24T04:00:00"
    assert hi.isoformat() == "2026-08-25T04:00:00"


def test_nbbo_delete_bounds_stay_timezone_aware():
    """The NBBO tape is TIMESTAMPTZ; stripping the zone would compare against
    server-local time and delete the wrong rows -- or none."""
    from scripts.hydration_canonicalize import apply_drops

    conn = RecordingConn()
    drops = [{"symbol": "CANF", "day": "2026-09-02",
              "keep": hyd.SOURCE_POLYGON_NBBO,
              "drop": hyd.SOURCE_IQFEED_NBBO, "rows": 100, "kept_rows": 50}]
    apply_drops(conn, hyd.NBBO_TABLE, drops)

    _, _, lo, hi = conn.cur.params[0]
    assert lo.tzinfo is not None and hi.tzinfo is not None
    assert lo.isoformat() == "2026-09-02T04:00:00+00:00"


def test_check_flag_exists_and_is_exclusive_with_apply(capsys):
    """The runbook and the handover both tell people to run --check.

    A documented flag that argparse rejects is a defect, and the failure lands
    on whoever is trying to gate a study on the invariant.
    """
    import pytest

    from scripts.hydration_canonicalize import main

    with pytest.raises(SystemExit) as exc:
        main(["--check", "--apply"])
    assert exc.value.code == 2  # argparse usage error, not a crash
    assert "mutually exclusive" in capsys.readouterr().err
