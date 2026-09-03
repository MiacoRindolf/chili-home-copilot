"""Unit tests for the mixed-vendor quote seam checker.

Pure fakes: no database, no network.  The live lane is running while these
execute.

What they bind is the arithmetic and the accounting of a measurement whose whole
purpose is to turn an inference into a number.  The dangerous direction here is
FLATTERY: a comparator that quietly drops the cases it cannot compare, or that
counts a sub-cent rounding artefact as a disagreement, produces a reassuring
figure that nobody can act on.
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import hydration_quote_seam_check as seam  # noqa: E402
from scripts import historical_tick_hydrator as hyd  # noqa: E402

D = date(2026, 8, 26)


def _ts(sec: float) -> datetime:
    return datetime(2026, 8, 26, 14, 0, 0, tzinfo=timezone.utc).replace(
        microsecond=0) + __import__("datetime").timedelta(seconds=sec)


class FakeCursor:
    """Answers the four query shapes ``compare_symbol_day`` issues."""

    def __init__(self, trades_with_quote, nbbo_valid, trade_sources,
                 nbbo_source, samples) -> None:
        self._n_t = trades_with_quote
        self._n_q = nbbo_valid
        self._trade_sources = trade_sources
        self._nbbo_source = nbbo_source
        self._samples = samples
        self._rows: list = []

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if "LEFT JOIN LATERAL" in s:
            self._rows = list(self._samples)
        elif "SELECT DISTINCT source" in s:
            self._rows = [(x,) for x in self._trade_sources]
        elif hyd.NBBO_TABLE in s and "count(*), min(source)" in s:
            self._rows = [(self._n_q, self._nbbo_source)]
        else:
            self._rows = [(self._n_t, len(self._trade_sources))]
        return self

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConn:
    def __init__(self, cur) -> None:
        self._cur = cur
        self.closed = False

    def cursor(self):
        return self._cur

    def close(self):
        self.closed = True


def compare(samples, *, n_t=1000, n_q=1000,
            trade_sources=(hyd.SOURCE_IQFEED_TRADES,),
            nbbo_source=hyd.SOURCE_POLYGON_NBBO):
    cur = FakeCursor(n_t, n_q, list(trade_sources), nbbo_source, samples)
    return seam.compare_symbol_day(FakeConn(cur), "LGCL", D, sample=100)


def test_the_mixed_vendor_seam_is_detected_from_the_source_tags():
    """IQFeed trades + Polygon quotes is the canonical corpus's shape on EVERY
    symbol-day, because the canonicalization preference INVERTS between the two
    tables. The checker must name it, not assume it."""
    rec = compare([(_ts(0), 1.00, 1.02, 1.00, 1.02, _ts(0))])
    assert rec["mixed_vendor"] is True
    assert rec["trade_sources"] == [hyd.SOURCE_IQFEED_TRADES]
    assert rec["nbbo_source"] == hyd.SOURCE_POLYGON_NBBO


def test_a_single_vendor_symbol_day_is_not_flagged():
    rec = compare([(_ts(0), 1.00, 1.02, 1.00, 1.02, _ts(0))],
                  trade_sources=(hyd.SOURCE_POLYGON_TRADES,))
    assert rec["mixed_vendor"] is False


def test_agreement_is_exact_at_cent_resolution_not_bit_resolution():
    """Both vendors quote in cents. A 1e-9 float difference is a wire-format
    rounding artefact; calling it a disagreement would manufacture a seam that
    is not there."""
    rec = compare([
        (_ts(0), 1.00, 1.02, 1.00 + 1e-9, 1.02 - 1e-9, _ts(0)),
        (_ts(1), 1.00, 1.02, 1.01, 1.02, _ts(1)),          # a real 1c gap
    ])
    assert rec["compared"] == 2
    assert rec["exact_agreements"] == 1
    assert rec["exact_rate"] == 0.5
    assert rec["bid_abs_max"] == 0.01


def test_a_trade_with_no_prior_quote_is_counted_not_dropped():
    """Dropping it would flatter the agreement rate: the cases with no book are
    exactly the ones a consumer most needs to know about."""
    rec = compare([
        (_ts(0), 1.00, 1.02, None, None, None),
        (_ts(1), 1.00, 1.02, 1.00, 1.02, _ts(1)),
    ])
    assert rec["sampled"] == 2
    assert rec["compared"] == 1          # only the one with a quote
    assert rec["no_prior_quote"] == 1
    assert rec["exact_rate"] == 1.0      # ...and the rate is over compared only


def test_quote_age_measures_how_stale_the_asof_row_was():
    """A large median age means the two vendors sampled the book at different
    moments -- a different diagnosis from disagreeing about its level, and it
    points at a different fix."""
    rec = compare([
        (_ts(10), 1.00, 1.02, 1.00, 1.02, _ts(8)),
        (_ts(20), 1.00, 1.02, 1.00, 1.02, _ts(14)),
    ])
    assert rec["quote_age_s_p50"] in (2.0, 6.0)
    assert rec["quote_age_s_p90"] == 6.0


def test_no_overlap_is_its_own_status_not_a_zero_agreement():
    """A symbol-day with no book is a coverage hole, not a 0% agreement -- and
    reporting it as the latter would put a fabricated disagreement into the
    summary statistics."""
    rec = compare([], n_q=0)
    assert rec["status"] == "no_overlap"
    assert "exact_rate" not in rec


def test_stride_samples_across_the_session_rather_than_taking_a_prefix():
    """A prefix would measure only the open. The stride is what makes the sample
    span premarket through the close."""
    cur = FakeCursor(10_000, 5_000, [hyd.SOURCE_IQFEED_TRADES],
                     hyd.SOURCE_POLYGON_NBBO,
                     [(_ts(0), 1.0, 1.02, 1.0, 1.02, _ts(0))])
    rec = seam.compare_symbol_day(FakeConn(cur), "LGCL", D, sample=100)
    assert rec["stride"] == 100


def test_session_bounds_use_zoneinfo_not_a_fixed_offset():
    lo, hi = seam._session_bounds(date(2026, 8, 26))        # EDT
    assert lo.isoformat() == "2026-08-26T08:00:00+00:00"
    lo2, _ = seam._session_bounds(date(2026, 3, 6))         # EST
    assert lo2.isoformat() == "2026-03-06T09:00:00+00:00"


def test_percentiles_are_stable_on_tiny_and_empty_samples():
    assert seam._pct([], 0.5) is None
    assert seam._pct([1.0], 0.99) == 1.0
    assert seam._pct([0.0, 1.0], 0.0) == 0.0
    assert seam._pct([0.0, 1.0], 1.0) == 1.0
