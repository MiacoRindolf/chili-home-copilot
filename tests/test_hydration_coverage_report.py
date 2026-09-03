"""Unit tests for the Phase 4 hydration coverage reporter.

Pure fakes: no database, no network.  The live lane is running while these
execute.

What these bind is the one way a coverage report can be actively harmful --
reporting coverage that is not there, or hiding a hole.  A report that
undercounts is annoying; a report that OVERCOUNTS gets a study run on a tape
with silent gaps in it, which is exactly the failure Phase 3 found in our own
recording.
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.hydration_coverage_report import DATASETS, build_report  # noqa: E402
from scripts import historical_tick_hydrator as hyd  # noqa: E402

D = date(2026, 8, 26)
D2 = date(2026, 8, 27)

# Observed first/last stand-ins. The trade tape's observed_at is naive-UTC and
# the NBBO tape's is aware, and the reporter has to render both as the same
# UTC instant -- getting that wrong is a silent four-hour lie.
_FIRST_NAIVE = datetime(2026, 8, 26, 8, 0, 0)
_LAST_NAIVE = datetime(2026, 8, 26, 23, 59, 56)
_FIRST_AWARE = _FIRST_NAIVE.replace(tzinfo=timezone.utc)
_LAST_AWARE = _LAST_NAIVE.replace(tzinfo=timezone.utc)


def _counts_tuple(n, *, aware: bool):
    """(rows, usable, first, last) from a test fixture value.

    An int means "all rows are usable"; a dict lets a test say otherwise, which
    is how the crossed/zero-quote case is expressed.
    """
    if isinstance(n, dict):
        rows, usable = int(n["rows"]), int(n["valid"])
    else:
        rows = usable = int(n)
    if rows == 0:
        return rows, usable, None, None
    return (rows, usable,
            _FIRST_AWARE if aware else _FIRST_NAIVE,
            _LAST_AWARE if aware else _LAST_NAIVE)


class FakeCursor:
    """Answers the three query shapes build_report issues, by keyword sniffing."""

    def __init__(self, counts, jobs, batches, size="123 MB"):
        self._counts, self._jobs, self._batches, self._size = counts, jobs, batches, size
        self._rows: list = []

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if "pg_database_size" in s:
            self._rows = [(self._size,)]
        elif "FROM hydration_jobs" in s:
            self._rows = list(self._jobs)
        elif "FROM hydration_batches" in s:
            self._rows = list(self._batches)
        else:  # the per-source row-count scan
            table = hyd.NBBO_TABLE if hyd.NBBO_TABLE in s else hyd.TRADES_TABLE
            source = params[0]
            self._rows = [
                (sym, day, *_counts_tuple(n, aware=table == hyd.NBBO_TABLE))
                for (tbl, src, sym, day), n in self._counts.items()
                if tbl == table and src == source
            ]
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
    def __init__(self, cursor):
        self._cursor = cursor
        self.closed = False

    def cursor(self):
        return self._cursor

    def close(self):
        self.closed = True


def run(monkeypatch, pairs, counts, jobs, batches=()):
    conn = FakeConn(FakeCursor(counts, jobs, batches))
    monkeypatch.setattr("scripts.hydration_coverage_report.connect",
                        lambda *a, **k: conn)
    return build_report(pairs, "chili_hydrated"), conn


def test_done_but_empty_is_a_failure_not_coverage(monkeypatch):
    """The defect this whole file exists to prevent.

    A job the hydrator marked ``done`` that produced ZERO rows is a hole, not
    coverage.  Trusting the ledger alone would count it as success and a study
    would replay a symbol-day with no tape under it.
    """
    jobs = [("LGCL", D, "trades", "iqfeed", "done", 0, None)]
    report, _ = run(monkeypatch, [("LGCL", D)], counts={}, jobs=jobs)

    assert report["coverage_by_source"].get("iqfeed_trades", 0) == 0
    assert report["has_trades"] == 0
    assert report["replayable"] == 0
    assert "LGCL 2026-08-26" in report["not_replayable_symbol_days"]
    assert any(f["dataset"] == "trades" and f["status"] == "uncovered"
               for f in report["failures"])


def test_canonicalized_away_source_is_not_a_failure(monkeypatch):
    """The false-alarm defect.

    Canonicalization DELETES the non-preferred source on purpose, leaving a job
    marked ``done`` with no rows behind it.  Calling that a failure raised ~250
    false alarms on the real corpus and buried the 6 genuine gaps.  Coverage is
    a property of the TABLE, not of a provider.
    """
    counts = {(hyd.TRADES_TABLE, hyd.SOURCE_IQFEED_TRADES, "LGCL", D): 310382,
              (hyd.NBBO_TABLE, hyd.SOURCE_POLYGON_NBBO, "LGCL", D): 103057}
    jobs = [("LGCL", D, "trades", "iqfeed", "done", 310382, None),
            # loaded, then dropped by canonicalize:
            ("LGCL", D, "trades", "polygon", "done", 310382, None),
            ("LGCL", D, "nbbo", "iqfeed", "done", 309000, None),
            ("LGCL", D, "nbbo", "polygon", "done", 103057, None)]
    report, _ = run(monkeypatch, [("LGCL", D)], counts, jobs)

    assert report["failure_count"] == 0
    assert report["replayable"] == 1


def test_no_data_from_every_provider_is_labelled_as_such(monkeypatch):
    """The 6 real gaps: REEMF/NLST/FMCC have no NBBO from EITHER provider.

    That is a different fact from "we did not try", and the label has to say so
    or someone will go looking for a bug that is not there.
    """
    counts = {(hyd.TRADES_TABLE, hyd.SOURCE_IQFEED_TRADES, "REEMF", D2): 3677}
    jobs = [("REEMF", D2, "trades", "iqfeed", "done", 3677, None),
            ("REEMF", D2, "nbbo", "iqfeed", "no_data", 0, None),
            ("REEMF", D2, "nbbo", "polygon", "no_data", 0, None)]
    report, _ = run(monkeypatch, [("REEMF", D2)], counts, jobs)

    assert report["has_trades"] == 1
    assert report["has_nbbo"] == 0
    # trades without a book is NOT replayable -- it is its own named tier
    assert report["replayable"] == 0
    assert report["tape_only_not_replayable"] == ["REEMF 2026-08-27"]
    assert report["failure_reasons"]["nbbo/no_data_from_any_provider"]["n"] == 1


def test_rows_present_count_as_coverage(monkeypatch):
    counts = {(hyd.TRADES_TABLE, hyd.SOURCE_IQFEED_TRADES, "LGCL", D): 310382,
              (hyd.NBBO_TABLE, hyd.SOURCE_POLYGON_NBBO, "LGCL", D): 103057}
    jobs = [("LGCL", D, "trades", "iqfeed", "done", 310382, None)]
    report, _ = run(monkeypatch, [("LGCL", D)], counts, jobs)

    assert report["has_trades"] == 1
    assert report["has_nbbo"] == 1
    assert report["replayable"] == 1
    assert report["not_replayable_symbol_days"] == []
    row = report["per_symbol_day"][0]
    assert row["iqfeed_trades"] == 310382
    assert row["polygon_nbbo"] == 103057


def test_one_provider_is_enough_to_be_replayable(monkeypatch):
    """IQFeed can legitimately have no data where Polygon does (and vice versa).

    Coverage is a property of the CORPUS, not of a provider, so a symbol-day
    Polygon covered is replayable even though the IQFeed job says no_data.
    """
    counts = {(hyd.TRADES_TABLE, hyd.SOURCE_POLYGON_TRADES, "CHGA", D2): 50000,
              (hyd.NBBO_TABLE, hyd.SOURCE_POLYGON_NBBO, "CHGA", D2): 40000}
    jobs = [("CHGA", D2, "trades", "iqfeed", "no_data", 0, None),
            ("CHGA", D2, "trades", "polygon", "done", 50000, None)]
    report, _ = run(monkeypatch, [("CHGA", D2)], counts, jobs)

    assert report["has_trades"] == 1
    assert report["replayable"] == 1
    # Polygon covered it, so the table is not a hole and nothing is flagged.
    assert report["failure_count"] == 0
    # ...but the IQFeed job status is still visible per symbol-day.
    assert report["per_symbol_day"][0]["iqfeed_trades_status"] == "no_data"


def test_failures_are_grouped_by_distinct_reason(monkeypatch):
    jobs = [("AAA", D, "trades", "iqfeed", "failed", 0, "IQFeedError: NO_DATA"),
            ("BBB", D, "trades", "iqfeed", "failed", 0, "TimeoutError: read"),
            ("CCC", D, "trades", "iqfeed", "failed", 0, "TimeoutError: read")]
    report, _ = run(monkeypatch, [("AAA", D), ("BBB", D), ("CCC", D)],
                    counts={}, jobs=jobs)

    assert report["failure_count"] >= 3
    key = "trades/uncovered"
    assert report["failure_reasons"][key]["n"] == 3
    # the per-provider error text survives into the detail line
    assert any("TimeoutError" in f["detail"] for f in report["failures"])
    assert report["not_replayable_symbol_days"] == ["AAA 2026-08-26",
                                                   "BBB 2026-08-26",
                                                   "CCC 2026-08-26"]


def test_missing_job_row_is_not_silently_covered(monkeypatch):
    """A symbol-day nobody ever attempted must show up, not vanish."""
    report, _ = run(monkeypatch, [("NEVR", D)], counts={}, jobs=[])
    assert report["corpus_symbol_days"] == 1
    assert report["has_trades"] == 0
    assert report["per_symbol_day"][0]["iqfeed_trades_status"] == "missing"
    assert "NEVR 2026-08-26" in report["not_replayable_symbol_days"]
    assert report["failure_reasons"]["trades/uncovered"]["n"] == 1


def test_cost_totals_sum_every_provider_dataset(monkeypatch):
    batches = [
        ("iqfeed", "trades", 3, 24, 18_400_000, 185_830, 0, 30.3, None, None),
        ("polygon", "nbbo", 2, 5, 25_000_000, 103_057, 0, 2.1, None, None),
    ]
    report, _ = run(monkeypatch, [("LGCL", D)], counts={}, jobs=[], batches=batches)

    assert report["cost_totals"]["requests"] == 29
    assert report["cost_totals"]["bytes"] == 43_400_000
    assert report["cost_totals"]["rows_loaded"] == 288_887
    assert report["cost_totals"]["provider_seconds"] == 32.4


def test_dataset_map_matches_the_hydrator_source_constants():
    """If the hydrator renames a source, this report must break loudly.

    A stale literal here would query a tag nothing was written under and return
    a clean zero -- indistinguishable from a genuine coverage hole.
    """
    sources = {src for _, src in DATASETS.values()}
    assert sources == set(hyd.HYDRATED_SOURCES)
    tables = {tbl for tbl, _ in DATASETS.values()}
    assert tables == {hyd.TRADES_TABLE, hyd.NBBO_TABLE}


def test_connection_is_closed_even_though_report_is_built(monkeypatch):
    _, conn = run(monkeypatch, [("LGCL", D)], counts={}, jobs=[])
    assert conn.closed is True


def test_session_bounds_use_zoneinfo_not_a_fixed_offset():
    """A hardcoded -4 would shift a whole session by an hour across the DST
    boundary, and the rows would still look well-formed -- the same class of
    landmine Phase 1 found in the lookup port's ET-naive timestamps."""
    from datetime import date as _date

    from scripts.hydration_coverage_report import et_session_bounds_utc

    # EDT (UTC-4): 04:00 ET -> 08:00 UTC
    lo, hi = et_session_bounds_utc(_date(2026, 8, 26))
    assert lo.isoformat() == "2026-08-26T08:00:00+00:00"
    assert hi.isoformat() == "2026-08-27T00:00:00+00:00"

    # EST (UTC-5): 04:00 ET -> 09:00 UTC. Same code, different offset.
    lo2, hi2 = et_session_bounds_utc(_date(2026, 3, 6))
    assert lo2.isoformat() == "2026-03-06T09:00:00+00:00"
    assert hi2.isoformat() == "2026-03-07T01:00:00+00:00"


def test_crossed_or_zero_quotes_are_not_coverage(monkeypatch):
    """Rows are not a book.

    ``load_nbbo_tape`` filters on ``bid > 0 AND ask > 0 AND ask >= bid``.  A
    tape of a million crossed quotes returns ZERO ticks to the replay, so
    counting the rows would report coverage the consumer cannot use -- the
    overcount failure mode, dressed as a full table.
    """
    counts = {
        (hyd.TRADES_TABLE, hyd.SOURCE_IQFEED_TRADES, "XCRS", D): 5000,
        (hyd.NBBO_TABLE, hyd.SOURCE_POLYGON_NBBO, "XCRS", D): {"rows": 900_000,
                                                               "valid": 0},
    }
    report, _ = run(monkeypatch, [("XCRS", D)], counts, jobs=[])

    assert report["per_symbol_day"][0]["polygon_nbbo"] == 900_000
    assert report["per_symbol_day"][0]["polygon_nbbo_usable"] == 0
    assert report["has_nbbo"] == 0
    assert report["replayable"] == 0
    assert report["tape_only_not_replayable"] == ["XCRS 2026-08-26"]


def test_mixed_vendor_quote_seam_is_named_not_implied(monkeypatch):
    """The corpus is IQFeed trades + Polygon quotes on EVERY symbol-day.

    ``load_trade_tape`` puts ``iqfeed_trade_ticks.bid/ask`` on every tick it
    returns, so a spread computed off a trade tick and a spread read from the
    NBBO tape at the same instant come from different vendors.  That is a
    deliberate choice; it must not be an invisible one.
    """
    counts = {(hyd.TRADES_TABLE, hyd.SOURCE_IQFEED_TRADES, "LGCL", D): 310382,
              (hyd.NBBO_TABLE, hyd.SOURCE_POLYGON_NBBO, "LGCL", D): 103057}
    report, _ = run(monkeypatch, [("LGCL", D)], counts, jobs=[])

    row = report["per_symbol_day"][0]
    assert row["trade_quote_vendor"] == "iqfeed"
    assert row["trade_quote_derivation"] == "native_at_trade_bid_ask"
    assert row["nbbo_vendor"] == "polygon"
    assert row["mixed_vendor_quote_seam"] is True
    assert report["mixed_vendor_quote_seam"] == 1


def test_polygon_trade_quotes_are_named_as_a_reconstruction(monkeypatch):
    """A Polygon-sourced trade row's bid/ask is an as-of MERGE, not a reading.

    Same columns, different epistemic status.  A consumer computing a spread off
    that tick is holding a reconstruction, and the artifact has to say so.
    """
    counts = {(hyd.TRADES_TABLE, hyd.SOURCE_POLYGON_TRADES, "CHGA", D2): 50000,
              (hyd.NBBO_TABLE, hyd.SOURCE_POLYGON_NBBO, "CHGA", D2): 40000}
    report, _ = run(monkeypatch, [("CHGA", D2)], counts, jobs=[])

    row = report["per_symbol_day"][0]
    assert row["trade_quote_vendor"] == "polygon"
    assert row["trade_quote_derivation"] == "as_of_merge_from_v3_quotes"
    # Both tapes are Polygon here, so there is no seam to warn about.
    assert row["mixed_vendor_quote_seam"] is False


def test_observed_bounds_are_utc_from_both_column_types(monkeypatch):
    """Measured extended-hours coverage, not inferred from the request window.

    ``since_utc``/``until_utc`` are what was ASKED for; only the observed first
    tick says whether premarket actually arrived.  The trade tape's column is
    naive-UTC and the NBBO tape's is aware -- rendering the naive one straight
    through would publish a timestamp that reads four hours wrong.
    """
    counts = {(hyd.TRADES_TABLE, hyd.SOURCE_IQFEED_TRADES, "LGCL", D): 310382,
              (hyd.NBBO_TABLE, hyd.SOURCE_POLYGON_NBBO, "LGCL", D): 103057}
    report, _ = run(monkeypatch, [("LGCL", D)], counts, jobs=[])

    row = report["per_symbol_day"][0]
    assert row["iqfeed_trades_first_utc"] == "2026-08-26T08:00:00+00:00"
    assert row["polygon_nbbo_first_utc"] == "2026-08-26T08:00:00+00:00"
    assert row["iqfeed_trades_last_utc"] == "2026-08-26T23:59:56+00:00"
    # 08:00Z is 04:00 ET -- the first second of premarket, which is the claim
    # the corpus makes and which the REQUESTED bounds cannot substantiate.
    assert row["since_utc"] == "2026-08-26T08:00:00+00:00"


def test_uncovered_symbol_day_has_no_vendor_claims(monkeypatch):
    """Absent data must not acquire a vendor by default."""
    report, _ = run(monkeypatch, [("NEVR", D)], counts={}, jobs=[])
    row = report["per_symbol_day"][0]
    assert row["trade_quote_vendor"] is None
    assert row["nbbo_vendor"] is None
    assert row["mixed_vendor_quote_seam"] is False
    assert row["iqfeed_trades_first_utc"] is None
