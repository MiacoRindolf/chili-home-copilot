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
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.hydration_coverage_report import DATASETS, build_report  # noqa: E402
from scripts import historical_tick_hydrator as hyd  # noqa: E402

D = date(2026, 8, 26)
D2 = date(2026, 8, 27)


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
            self._rows = [(sym, day, n)
                          for (tbl, src, sym, day), n in self._counts.items()
                          if tbl == table and src == source]
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
    assert report["replayable_trades"] == 0
    assert "LGCL 2026-08-26" in report["not_replayable"]
    labels = [f["status"] for f in report["failures"]]
    assert "done_but_empty" in labels


def test_rows_present_count_as_coverage(monkeypatch):
    counts = {(hyd.TRADES_TABLE, hyd.SOURCE_IQFEED_TRADES, "LGCL", D): 310382,
              (hyd.NBBO_TABLE, hyd.SOURCE_POLYGON_NBBO, "LGCL", D): 103057}
    jobs = [("LGCL", D, "trades", "iqfeed", "done", 310382, None)]
    report, _ = run(monkeypatch, [("LGCL", D)], counts, jobs)

    assert report["replayable_trades"] == 1
    assert report["replayable_nbbo"] == 1
    assert report["replayable_both"] == 1
    assert report["not_replayable"] == []
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

    assert report["replayable_trades"] == 1
    assert report["replayable_both"] == 1
    # ...but the IQFeed hole is still REPORTED rather than papered over.
    assert any(f["provider"] == "iqfeed" and f["status"] == "no_data"
               for f in report["failures"])


def test_failures_are_grouped_by_distinct_reason(monkeypatch):
    jobs = [("AAA", D, "trades", "iqfeed", "failed", 0, "IQFeedError: NO_DATA"),
            ("BBB", D, "trades", "iqfeed", "failed", 0, "TimeoutError: read"),
            ("CCC", D, "trades", "iqfeed", "failed", 0, "TimeoutError: read")]
    report, _ = run(monkeypatch, [("AAA", D), ("BBB", D), ("CCC", D)],
                    counts={}, jobs=jobs)

    assert report["failure_count"] >= 3
    key = "iqfeed/trades/failed"
    assert report["failure_reasons"][key]["n"] == 3
    assert report["not_replayable"] == ["AAA 2026-08-26", "BBB 2026-08-26",
                                        "CCC 2026-08-26"]


def test_missing_job_row_is_not_silently_covered(monkeypatch):
    """A symbol-day nobody ever attempted must show up, not vanish."""
    report, _ = run(monkeypatch, [("NEVR", D)], counts={}, jobs=[])
    assert report["corpus_symbol_days"] == 1
    assert report["replayable_trades"] == 0
    assert report["per_symbol_day"][0]["iqfeed_trades_status"] == "missing"
    assert "NEVR 2026-08-26" in report["not_replayable"]


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
