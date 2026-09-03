"""Unit tests for the counterfactual preflight.

Pure fakes: no database, no network, no app import.  The live lane is running
while these execute.

What they bind is the failure mode that survived four phases of acceptance: a
hydrated symbol-day that loads perfectly and produces an EMPTY study.  Every
earlier acceptance drove the tape loaders, and the loaders were never the
problem -- they return ticks correctly.  Admission is downstream of them, and a
clean empty result is the most expensive kind of wrong answer because it looks
like a finding.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import hydration_preflight as pf  # noqa: E402
from scripts import historical_tick_hydrator as hyd  # noqa: E402

D = date(2026, 8, 26)


class FakeCursor:
    def __init__(self, trades: int, nbbo: int) -> None:
        self._trades, self._nbbo, self._row = trades, nbbo, None

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self._row = ((self._nbbo,) if hyd.NBBO_TABLE in s else (self._trades,))
        return self

    def fetchone(self):
        return self._row

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConn:
    def __init__(self, trades: int, nbbo: int) -> None:
        self._cur = FakeCursor(trades, nbbo)
        self.closed = False

    def cursor(self):
        return self._cur

    def close(self):
        self.closed = True


def check(monkeypatch, *, trades: int, nbbo: int, events: int,
          bypass: bool = False):
    monkeypatch.setattr(pf, "_load_source_events", lambda *a, **k: events)
    return pf.check_symbol_day(FakeConn(trades, nbbo), "LGCL", D,
                               source_gate_bypassed=bypass)


def test_an_empty_source_gate_blocks_and_says_why(monkeypatch):
    """THE hole. load_ross_source_events reads July-era JSONL files; the corpus
    is August/September. Every candidate is skipped with
    ``no_ross_source_before_entry``, the run exits 0, and it prints a replay
    with zero trades and no hint that a flag was needed."""
    rec = check(monkeypatch, trades=310382, nbbo=103057, events=0)

    assert rec["ok"] is False
    assert any(b.startswith("no_source_events") for b in rec["blockers"])
    blocker = next(b for b in rec["blockers"] if b.startswith("no_source_events"))
    # The refusal has to name the escape hatch, or an operator hits a wall.
    assert "--allow-pre-source-entries" in blocker
    assert "--no-live-admission-mode" in blocker


def test_the_bypass_flag_clears_the_source_blocker_only(monkeypatch):
    rec = check(monkeypatch, trades=310382, nbbo=103057, events=0, bypass=True)
    assert rec["ok"] is True
    assert rec["blockers"] == []
    assert rec["source_gate_bypassed"] is True


def test_trades_without_a_book_is_blocked_not_passed(monkeypatch):
    """REEMF/NLST/FMCC: trades from IQFeed, no book from EITHER vendor.

    An earlier coverage report called these "replayable on trades". They are
    not: ``_confidence`` returns ``no_tape`` on an empty NBBO tape and
    bar-candidate generation is guarded on NBBO ticks, so trades alone generate
    ZERO candidates.
    """
    rec = check(monkeypatch, trades=3677, nbbo=0, events=5)

    assert rec["ok"] is False
    blocker = next(b for b in rec["blockers"] if b.startswith("no_nbbo_tape"))
    assert "ZERO candidates" in blocker
    # The bypass flag must NOT rescue a missing book -- different failure.
    rec2 = check(monkeypatch, trades=3677, nbbo=0, events=0, bypass=True)
    assert rec2["ok"] is False
    assert [b for b in rec2["blockers"] if b.startswith("no_nbbo_tape")]


def test_a_fully_covered_symbol_day_with_a_catalyst_passes(monkeypatch):
    rec = check(monkeypatch, trades=310382, nbbo=103057, events=3)
    assert rec["ok"] is True
    assert rec["blockers"] == []
    assert rec["trade_ticks"] == 310382
    assert rec["nbbo_ticks"] == 103057


def test_an_empty_trade_tape_is_its_own_blocker(monkeypatch):
    rec = check(monkeypatch, trades=0, nbbo=0, events=0)
    kinds = {b.split(":", 1)[0] for b in rec["blockers"]}
    assert kinds == {"no_trade_tape", "no_nbbo_tape", "no_source_events"}


def test_report_exits_blocked_and_names_the_symbol_days(monkeypatch):
    monkeypatch.setattr(pf, "_load_source_events", lambda *a, **k: 0)
    conn = FakeConn(3677, 0)
    monkeypatch.setattr(pf, "connect", lambda *a, **k: conn)

    report = pf.build_report([("REEMF", D)], "chili_hydrated")

    assert report["blocked"] == 1
    assert report["ok"] == 0
    assert report["blocker_counts"]["no_nbbo_tape"] == 1
    assert report["blocked_symbol_days"] == [
        "REEMF 2026-08-26: no_nbbo_tape, no_source_events"]
    assert conn.closed is True


def test_session_bounds_use_zoneinfo_not_a_fixed_offset():
    """Same landmine as everywhere else in this stack: the corpus straddles the
    DST boundary and a hardcoded -4 shifts a whole session by an hour."""
    lo, hi = pf.session_bounds_utc(date(2026, 8, 26))       # EDT
    assert lo.isoformat() == "2026-08-26T08:00:00+00:00"
    assert hi.isoformat() == "2026-08-27T00:00:00+00:00"

    lo2, hi2 = pf.session_bounds_utc(date(2026, 3, 6))      # EST
    assert lo2.isoformat() == "2026-03-06T09:00:00+00:00"
    assert hi2.isoformat() == "2026-03-07T01:00:00+00:00"
