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
