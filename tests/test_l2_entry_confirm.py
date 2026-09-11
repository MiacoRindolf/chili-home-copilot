"""L2 entry CONFIRMER (DEFER-only) — docs/DESIGN/L2_PRIMARY_SIGNAL.md.

The confirmer runs at the live entry seam AFTER the chart trigger fires AND AFTER the
runner's entry vetoes pass — a veto ALWAYS wins (the gates' _l2_entry_veto is RETIRED,
[2] review 2026-09-11). It reads
the last N PRINTS ([29]) and decides on ONE tape feature, ``buy_share_delta``
(c92bf49ca): carrying ⇒ confirm (``l2_confirm_tape_thrust``); not carrying ⇒ confirm
only if a readable book agrees (``l2_confirm_secondary_override``), else DEFER
(``l2_confirm_buying_not_carrying``). ``signed_tape_accel`` / ``tick_rate`` are reported,
never decisive.

These tests pin the pure pieces (no real DB — a tiny fake `db` returns canned rows /
a canned LadderRead via a monkeypatched read_ladder_distribution):

  TAPE HELPER (_signed_tape_features):
    (1) rising aggressor-signed buy tape -> signed_tape_accel > 0.
    (2) dead/negative tape -> signed_tape_accel <= 0.
    (3) too-few / empty ticks -> None (=> caller fails open).

  _l2_entry_confirm:
    (4) DISABLED (flag False) -> ("confirm", reason=l2_confirm_disabled) before any I/O.
    (5) the buy_share_delta predicate (carrying / fading / book override / stale book).
    (6) FAIL-OPEN, NAMED ([2] [c], 2026-09-11): an empty tape (l2_confirm_no_tape), a
        FAILED tape read (l2_confirm_tape_error, why/error/where=query), and a CODE fault
        (l2_confirm_error + where + WARNING — a feature/query-build bug is NOT a read
        error, [2] review) are different receipts — before, all were
        `l2_confirm_no_data`. `l2_confirm_no_data` now means only "db None / blank
        symbol". Every one of them still CONFIRMS, with `fallback=fail_open_confirm`.
    (7) THE REAL READER ([2] review): a failed depth read is named (`book_error`), never
        booked as an empty book; the book is read over the SAME prints the tape decided
        on (window = age of the oldest decided print, current = newer than the back
        half's first print) — the 30-s window and 10-s ceiling no longer decide.
"""

from __future__ import annotations

import logging
from datetime import datetime

import pytest

from app.config import settings
from app.services.trading.momentum_neural import entry_gates
from app.services.trading.momentum_neural.entry_gates import (
    _l2_entry_confirm,
    _signed_tape_features,
)
from app.services.trading.momentum_neural.pipeline import LadderRead


# ── helpers ───────────────────────────────────────────────────────────────────

def _tick(price, size, bid, ask, ts):
    """(price, size, bid, ask, ts_seconds) as the SQL row shape the helper consumes."""
    return (price, size, bid, ask, ts)


class _FakeDB:
    """Returns canned tape rows for the iqfeed_trade_ticks query the live wrapper runs.
    The confirmer's book read is monkeypatched separately, so this only feeds the tape.

    THE ROWS ARE ANCHORED TO "NOW" ([29] review fix, 2026-09-11). The print window has
    no lower time bound (``LIMIT :n``), so ``_l2_entry_confirm`` now measures the AGE of
    the newest print against the window's own bound and fails OPEN on a stale tape,
    exactly as its docstring always promised ("Never defers on missing / thin / stale
    data"). These fixtures were written with epoch seconds 1.0-15.0 — i.e. January 1970
    — which is a legitimately stale tape. Shifting the whole canned window so its newest
    print lands one second before the decision instant keeps every fixture's SHAPE
    (all the relative gaps are preserved) while letting the tests exercise the tape
    legs they were written for rather than the freshness leg."""

    def __init__(self, rows):
        rows = list(rows or [])
        tss = [r[4] for r in rows if len(r) > 4 and r[4] is not None]
        if tss:
            now = (datetime.utcnow() - datetime(1970, 1, 1)).total_seconds()
            shift = (now - 1.0) - max(float(t) for t in tss)
            rows = [
                (r[0], r[1], r[2], r[3], float(r[4]) + shift) if r[4] is not None else r
                for r in rows
            ]
        self._rows = rows

    def execute(self, *_a, **_k):
        rows = self._rows

        class _R:
            def fetchall(self_inner):
                return rows

        return _R()


def _ladder(*, ofi, micro, pctile, age, n_snaps=6):
    return LadderRead(
        depth_imbal=None,
        depth_imbal_pctile=pctile,
        ofi=ofi,
        micro_edge=micro,
        bid_refill=None,
        ask_build=None,
        spread_bps=None,
        snapshot_age_s=age,
        n_snaps=n_snaps,
    )


@pytest.fixture
def confirm_on():
    """Enable the confirmer (kill-switch ON) for the duration of the test."""
    prev = getattr(settings, "chili_momentum_l2_confirm_enabled", False)
    settings.chili_momentum_l2_confirm_enabled = True
    try:
        yield
    finally:
        settings.chili_momentum_l2_confirm_enabled = prev


# ── (1)-(3) pure tape helper ────────────────────────────────────────────────────

def test_tape_helper_rising_buy_tape_positive_accel():
    # front half (ts 0-7): mostly bid-side sells; back half (ts 8-15): aggressive ask-lifts.
    rows = [
        _tick(10.00, 100, 9.99, 10.00, 1.0),
        _tick(9.99, 100, 9.99, 10.00, 3.0),
        _tick(10.00, 50, 9.99, 10.00, 5.0),
        _tick(10.01, 400, 10.00, 10.01, 9.0),   # px>=ask -> buy
        _tick(10.02, 500, 10.01, 10.02, 12.0),  # px>=ask -> buy
        _tick(10.03, 600, 10.02, 10.03, 15.0),  # px>=ask -> buy
    ]
    out = _signed_tape_features(
        rows,
        window_s=15.0,
        tick_rate_floor_pctile=0.0,
    )
    assert out is not None
    assert out["signed_tape_accel"] > 0.0
    assert out["n_ticks"] == 6
    assert out["tick_rate"] >= 0.0


def test_tape_helper_epoch_midpoint_is_stable_across_microsecond_offsets():
    """An exactly centered millisecond print always belongs to the back half."""

    epoch = 1_784_100_000.0
    for microsecond_offset in range(1_000):
        decision = epoch + microsecond_offset / 1_000_000.0
        rows = [
            _tick(10.00, 100, 9.99, 10.00, decision - 0.030),
            _tick(10.01, 200, 10.00, 10.01, decision - 0.020),
            _tick(10.02, 300, 10.01, 10.02, decision - 0.010),
        ]
        out = _signed_tape_features(
            rows,
            window_s=0.05,
            tick_rate_floor_pctile=0.0,
        )
        assert out is not None
        assert out["signed_tape_accel"] == 400.0


def test_tape_helper_dead_negative_tape_nonpositive_accel():
    # back half is bid-hitting sells; no back-half buy volume -> accel <= 0.
    rows = [
        _tick(10.01, 400, 10.00, 10.01, 1.0),   # front: buy
        _tick(10.02, 500, 10.01, 10.02, 4.0),   # front: buy
        _tick(10.00, 300, 10.00, 10.01, 9.0),   # back: px<=bid -> sell
        _tick(9.99, 400, 9.99, 10.00, 12.0),    # back: sell
        _tick(9.98, 500, 9.98, 9.99, 15.0),     # back: sell
    ]
    out = _signed_tape_features(
        rows,
        window_s=15.0,
        tick_rate_floor_pctile=0.0,
    )
    assert out is not None
    assert out["signed_tape_accel"] <= 0.0


def test_tape_helper_too_few_ticks_returns_none():
    assert (
        _signed_tape_features(
            [],
            window_s=15.0,
            tick_rate_floor_pctile=0.0,
        )
        is None
    )
    assert (
        _signed_tape_features(
            [_tick(10.0, 100, 9.99, 10.0, 1.0)],
            window_s=15.0,
            tick_rate_floor_pctile=0.0,
        )
        is None
    )


# ── (4) DISABLED = confirm before any I/O ────────────────────────────────────────

def test_disabled_confirms_before_io():
    # flag False (default) -> confirm immediately; a None db proves no I/O is attempted.
    prev = getattr(settings, "chili_momentum_l2_confirm_enabled", False)
    settings.chili_momentum_l2_confirm_enabled = False
    try:
        decision, dbg = _l2_entry_confirm("ABCD", db=None, settings=settings)
        assert decision == "confirm"
        assert dbg["reason"] == "l2_confirm_disabled"
    finally:
        settings.chili_momentum_l2_confirm_enabled = prev


# ── (5) rising tape -> confirm ───────────────────────────────────────────────────

def test_rising_tape_confirms(confirm_on, monkeypatch):
    rows = [
        _tick(10.00, 100, 9.99, 10.00, 1.0),
        _tick(9.99, 100, 9.99, 10.00, 4.0),
        _tick(10.01, 400, 10.00, 10.01, 9.0),
        _tick(10.02, 500, 10.01, 10.02, 12.0),
        _tick(10.03, 600, 10.02, 10.03, 15.0),
    ]
    db = _FakeDB(rows)
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.pipeline.read_ladder_distribution",
        lambda *a, **k: _ladder(ofi=0.4, micro=1.0, pctile=0.7, age=2.0),
    )
    decision, dbg = _l2_entry_confirm("ABCD", db=db, settings=settings)
    assert decision == "confirm"
    assert dbg["reason"] == "l2_confirm_tape_thrust"
    assert dbg["signed_tape_accel"] > 0.0


# ── (6) dead/negative tape + selling OFI + no secondary -> DEFER ─────────────────


def test_empty_tape_fails_open_to_confirm(confirm_on, monkeypatch):
    """The read SUCCEEDED and returned nothing — an absence, named as one. (Was
    `l2_confirm_no_data`, the same string a failed read and a bug produced.)"""
    db = _FakeDB([])  # no ticks -> tape helper None, no exception -> fail-open
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.pipeline.read_ladder_distribution",
        lambda *a, **k: _ladder(ofi=-0.9, micro=-3.0, pctile=0.05, age=2.0),
    )
    decision, dbg = _l2_entry_confirm("ABCD", db=db, settings=settings)
    assert decision == "confirm"
    assert dbg["reason"] == "l2_confirm_no_tape"
    assert dbg["fallback"] == "fail_open_confirm"
    assert "why" not in dbg and "error" not in dbg, "an empty tape is not an error"
    assert isinstance(dbg["tape_read_ms"], float) and dbg["tape_read_ms"] >= 0.0


# ── (6) FAIL-OPEN, NAMED: absence vs failure vs bug ──────────────────────────────


class _RaisingDB:
    """A session whose tape read raises — the cold print-form read that ran past its
    statement_timeout (SWVL 2026-09-10 17:10:51), or any other failed read."""

    def __init__(self, exc: BaseException):
        self.exc = exc
        self.calls = 0

    def execute(self, *_a, **_k):
        self.calls += 1
        raise self.exc


def _timeout_exc():
    """What SQLAlchemy raises when Postgres cancels on statement_timeout: an
    OperationalError whose ``.orig`` is psycopg2's QueryCanceled."""
    from psycopg2 import errors as pg_errors
    from sqlalchemy.exc import OperationalError

    orig = pg_errors.QueryCanceled("canceling statement due to statement timeout")
    return OperationalError("SELECT ... FROM iqfeed_trade_ticks", {}, orig)


def test_db_none_or_blank_symbol_is_the_only_no_data(confirm_on):
    """`l2_confirm_no_data` survives for exactly one case: nothing to read with."""
    for sym, db in (("ABCD", None), ("", _FakeDB([])), (None, _FakeDB([]))):
        decision, dbg = _l2_entry_confirm(sym, db=db, settings=settings)
        assert decision == "confirm"
        assert dbg["reason"] == "l2_confirm_no_data"
        assert dbg["fallback"] == "fail_open_confirm"
        assert "tape_read_ms" not in dbg, "no read was attempted"


def test_a_tape_read_that_times_out_is_an_error_not_an_empty_tape(confirm_on, monkeypatch):
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.pipeline.read_ladder_distribution",
        lambda *a, **k: None,
    )
    db = _RaisingDB(_timeout_exc())
    decision, dbg = _l2_entry_confirm("SWVL", db=db, settings=settings)
    assert db.calls >= 1, "the tape read must actually have been attempted"
    assert decision == "confirm", "a failed read must never manufacture a refusal"
    assert dbg["reason"] == "l2_confirm_tape_error"
    assert dbg["why"] == "timeout"
    assert dbg["error"] == "OperationalError"
    assert dbg["where"] == "query"
    assert dbg["fallback"] == "fail_open_confirm"
    assert isinstance(dbg["tape_read_ms"], float) and dbg["tape_read_ms"] >= 0.0


def test_a_tape_read_that_raises_anything_else_is_an_error_too(confirm_on):
    db = _RaisingDB(RuntimeError("server closed the connection unexpectedly"))
    decision, dbg = _l2_entry_confirm("ABCD", db=db, settings=settings)
    assert decision == "confirm"
    assert dbg["reason"] == "l2_confirm_tape_error"
    assert dbg["why"] == "error"
    assert dbg["error"] == "RuntimeError"
    assert dbg["where"] == "query"


def test_a_feature_computation_that_raises_is_a_bug_not_a_read_error(
        confirm_on, monkeypatch, caplog):
    """[2] review, 2026-09-11. The rows CAME BACK; computing the features on them raised
    (a ZeroDivisionError on valid rows). That is a code fault in the tape path every
    reader shares, not a failed read: it is booked as `l2_confirm_error` with
    `where=features`, and the helper leaves a WARNING with the traceback. Before this
    fix it read `l2_confirm_tape_error` with no log line — a bug hiding as a read."""
    rows = _tape([(10.00, 600, True), (10.01, 600, True),
                  (10.02, 400, False), (10.01, 400, False)])

    def _boom(*_a, **_k):
        raise ZeroDivisionError("bad window")

    monkeypatch.setattr(entry_gates, "_signed_tape_features", _boom)
    with caplog.at_level(logging.WARNING, logger=entry_gates.__name__):
        decision, dbg = _l2_entry_confirm("ABCD", db=_FakeDB(rows), settings=settings)
    assert decision == "confirm"
    assert dbg["reason"] == "l2_confirm_error"
    assert dbg["error_type"] == "ZeroDivisionError"
    assert dbg["where"] == "features"
    assert dbg["fallback"] == "fail_open_confirm"
    assert "why" not in dbg and "error" not in dbg, "the read-error fields are for reads"
    warned = [r for r in caplog.records
              if r.levelno == logging.WARNING and r.getMessage().startswith("[entry_gates]")
              and "ZeroDivisionError" in r.getMessage()]
    assert warned, "a code fault in the tape path must leave a [entry_gates] WARNING"
    assert warned[0].exc_info is not None, "…with the traceback"


def test_a_query_that_cannot_even_be_built_is_a_bug_not_a_read_error(
        confirm_on, monkeypatch, caplog):
    """Building the query raised before any row was requested (an import / clock /
    contract fault) — `where=query_build`, `l2_confirm_error`, WARNING. Not a read."""
    from app.services.trading.momentum_neural import tape_selection

    def _boom(*_a, **_k):
        raise ImportError("held_evaluation_audit moved")

    monkeypatch.setattr(tape_selection, "signed_tape_query", _boom)
    db = _RaisingDB(AssertionError("no query may be executed"))
    with caplog.at_level(logging.WARNING, logger=entry_gates.__name__):
        decision, dbg = _l2_entry_confirm("ABCD", db=db, settings=settings)
    assert db.calls == 0, "nothing was read"
    assert decision == "confirm"
    assert dbg["reason"] == "l2_confirm_error"
    assert dbg["error_type"] == "ImportError"
    assert dbg["where"] == "query_build"
    assert any("query_build" in r.getMessage() for r in caplog.records
               if r.levelno == logging.WARNING)


def test_an_exception_after_the_read_is_named_and_logged(confirm_on, monkeypatch, caplog):
    """A bug past the tape read (here: a non-numeric feature) is `l2_confirm_error` with
    its class on the receipt and a WARNING in the log — never `no_data`, never a defer."""
    monkeypatch.setattr(
        entry_gates, "signed_tape_accel_features",
        lambda *a, **k: {"signed_tape_accel": "not-a-number"},
    )
    with caplog.at_level(logging.WARNING, logger=entry_gates.__name__):
        decision, dbg = _l2_entry_confirm("ABCD", db=_FakeDB([]), settings=settings)
    assert decision == "confirm"
    assert dbg["reason"] == "l2_confirm_error"
    assert dbg["error_type"] == "ValueError"
    assert dbg["where"] == "confirmer"
    assert dbg["fallback"] == "fail_open_confirm"
    assert "tape_read_ms" in dbg, "the read completed before the bug"
    warned = [r for r in caplog.records
              if r.levelno == logging.WARNING and r.getMessage().startswith("[entry_gates]")]
    assert warned, "an exception in the confirmer must leave a [entry_gates] WARNING"
    assert "ValueError" in warned[0].getMessage()


def test_a_reader_that_raises_is_named_and_the_tape_still_decides(confirm_on, monkeypatch):
    """The reader ITSELF raising only happens with a bound replay provider rejecting the
    read (live readers swallow; see the real-reader tests below). Still named: `book_error`
    with `book_error_where=reader`, no override, the fresh tape decides."""
    rows = _tape([(10.00, 600, True), (10.01, 600, True),
                  (10.02, 400, False), (10.01, 400, False)])

    def _raise(*_a, **_k):
        raise RuntimeError("provider rejected the read")

    monkeypatch.setattr(
        "app.services.trading.momentum_neural.pipeline.read_ladder_distribution", _raise)
    decision, dbg = _l2_entry_confirm("ABCD", db=_FakeDB(rows), settings=settings)
    assert dbg["book_error"] == "RuntimeError"
    assert dbg["book_error_where"] == "reader"
    assert dbg["book_unreadable_why"] == "error"
    assert dbg["book_readable"] is False
    assert dbg["buy_share_delta"] < 0
    assert decision == "defer" and dbg["reason"] == "l2_confirm_buying_not_carrying"
    assert "fallback" not in dbg, "a tape-decided defer is a decision, not a fallback"


# ── THE REAL READER: a failed depth read, and the book's clocks ([2] review) ────────
# Everything below drives the REAL `_l2_entry_confirm` AND the REAL
# `read_ladder_distribution` / `_ladder_equity` / `_live_ofi_microprice` — only the
# session is fake. The reviewer's point was that monkeypatching the reader to raise
# tests a layer that never raises in live: the live readers catch every exception and
# return an empty book.

_AS_OF = datetime(2026, 9, 10, 14, 20, 0)
_AS_OF_EPOCH = (_AS_OF - datetime(1970, 1, 1)).total_seconds()


def _fading_rows(n=40, step_s=1.0, newest_age_s=0.5):
    """Front half lifts the ask, back half hits the bid -> buy_share_delta < 0.
    Oldest-first (price, size, bid, ask, ts)."""
    rows = []
    for i in range(n):
        ts = _AS_OF_EPOCH - newest_age_s - (n - 1 - i) * step_s
        px = 10.00 + 0.001 * i
        if i < n // 2:
            rows.append((px, 500.0, px - 0.01, px, ts))
        else:
            rows.append((px, 500.0, px, px + 0.01, ts))
    return rows


def _rising_book(newest_age_s, n=6, spacing_s=2.0):
    """newest-first iqfeed_depth_snapshots rows; imbalance RISES into the newest (the
    newest ranks 6/6 = 1.0, so the depth leg agrees)."""
    from datetime import timedelta

    out = []
    for i in range(n):
        t = _AS_OF - timedelta(seconds=newest_age_s + spacing_s * i)
        out.append((t, 10.0, 10.01, 800.0, 400.0, 5000.0, 3000.0, 0.6 - 0.1 * i))
    return out


class _TapeAndBookDB:
    """Serves the three reads the confirmer makes, honouring each query's OWN window:
    the tape (iqfeed_trade_ticks), the ladder (iqfeed_depth_snapshots, newest-first,
    `observed_at > as_of - w`, LIMIT k) and the OFI read (bids_json -> no rows here)."""

    def __init__(self, tape, depth, *, depth_exc=None):
        self.tape, self.depth, self.depth_exc = tape, depth, depth_exc
        self.depth_params = []

    def execute(self, stmt, params=None):
        from datetime import timedelta

        q, p = str(stmt), dict(params or {})
        if "iqfeed_trade_ticks" in q:
            rows = self.tape
        elif "bids_json" in q:
            rows = []                          # the OFI read: no OFI / micro here
        elif "iqfeed_depth_snapshots" in q:
            self.depth_params.append(p)
            if self.depth_exc is not None:
                raise self.depth_exc
            lo = p["as_of"] - timedelta(seconds=float(p["w"]))
            rows = [r for r in self.depth if lo < r[0] <= p["as_of"]][: int(p["k"])]
        else:
            raise AssertionError("unexpected query: " + q[:80])

        class _R:
            def fetchall(self_inner):
                return rows

        return _R()


def test_a_failed_depth_read_is_named_not_booked_as_an_empty_book(confirm_on):
    """Probe A of the review, on the real reader: the depth query raises (a dropped
    column). Before: `book_readable False, n_snaps 0`, no `book_error` — byte-identical
    to a genuinely empty book. Now the receipt says the READ failed, and where."""
    broken = _TapeAndBookDB(
        _fading_rows(), _rising_book(2.0),
        depth_exc=RuntimeError('column "imbalance5" does not exist'))
    d_err, dbg_err = _l2_entry_confirm("ABCD", db=broken, settings=settings, l2_as_of=_AS_OF)
    empty = _TapeAndBookDB(_fading_rows(), [])
    d_empty, dbg_empty = _l2_entry_confirm("ABCD", db=empty, settings=settings, l2_as_of=_AS_OF)

    assert broken.depth_params, "the real reader must actually have queried depth"
    assert dbg_err["book_error"] == "RuntimeError"
    assert dbg_err["book_error_why"] == "error"
    assert dbg_err["book_error_where"] == "depth"
    assert dbg_err["book_unreadable_why"] == "error"
    assert "book_error" not in dbg_empty
    assert dbg_empty["book_unreadable_why"] == "empty"
    # The two receipts can now be told apart; the DECISION is the tape's in both — a
    # failed secondary read removes only the book's release, it cannot switch the
    # tape's refusal off (a dead depth table would otherwise disable the gate).
    assert dbg_err["buy_share_delta"] < 0 and dbg_empty["buy_share_delta"] < 0
    assert d_err == d_empty == "defer"
    assert dbg_err["reason"] == dbg_empty["reason"] == "l2_confirm_buying_not_carrying"


def test_a_timed_out_depth_read_says_timeout(confirm_on):
    db = _TapeAndBookDB(_fading_rows(), _rising_book(2.0), depth_exc=_timeout_exc())
    _d, dbg = _l2_entry_confirm("ABCD", db=db, settings=settings, l2_as_of=_AS_OF)
    assert dbg["book_error"] == "OperationalError"
    assert dbg["book_error_why"] == "timeout"


def test_the_book_clock_no_longer_decides(confirm_on):
    """Probe of the review: SAME fading tape, SAME rising book, newest snapshot 9.9 s vs
    11.0 s old. The old 10-s ceiling CONFIRMED the first and DEFERRED the second — the
    wall clock decided. The bound is now the tape's: this tape's back half began 19.5 s
    before the decision, so both snapshots saw it and both confirm."""
    out = {}
    for age in (9.9, 11.0):
        db = _TapeAndBookDB(_fading_rows(step_s=1.0), _rising_book(age))
        out[age] = _l2_entry_confirm("ABCD", db=db, settings=settings, l2_as_of=_AS_OF)
    for age, (d, dbg) in out.items():
        assert dbg["book_current_within_s"] == pytest.approx(19.5, abs=1e-6), dbg
        assert dbg["book_readable"] is True, (age, dbg)
        assert dbg["depth_rising"] is True
        assert d == "confirm" and dbg["reason"] == "l2_confirm_secondary_override", (age, dbg)


def test_a_book_that_never_saw_the_back_half_cannot_override(confirm_on):
    """The depth feed stopped 25 s ago; this tape's back half began 19.5 s ago. Six
    snapshots sit inside the window, rising — but none was taken while the buying stopped
    carrying, so the book cannot speak for it. Named `not_current`, and the receipt still
    shows what the book said (`book_would_agree`), so the anchor — not the book — is
    visibly what decided."""
    db = _TapeAndBookDB(_fading_rows(step_s=1.0), _rising_book(25.0))
    d, dbg = _l2_entry_confirm("ABCD", db=db, settings=settings, l2_as_of=_AS_OF)
    assert dbg["n_snaps"] == 6
    assert dbg["snapshot_age_s"] == pytest.approx(25.0)
    assert dbg["book_unreadable_why"] == "not_current"
    assert dbg["book_would_agree"] is True
    assert dbg["depth_imbal_pctile"] == pytest.approx(1.0), "raw book value is reported"
    assert dbg["depth_rising"] is False, "…but the leg that can release is the readable one"
    assert d == "defer" and dbg["reason"] == "l2_confirm_buying_not_carrying"


def test_the_book_is_read_over_the_same_prints_at_the_same_instant(confirm_on):
    """The reader's window is the age of the OLDEST decided print (0.5 s + 39 prints x
    1 s = 39.5 s), anchored at the tape's own decision instant — not 30 s of wall clock."""
    db = _TapeAndBookDB(_fading_rows(step_s=1.0), _rising_book(2.0))
    _d, dbg = _l2_entry_confirm("ABCD", db=db, settings=settings, l2_as_of=_AS_OF)
    assert db.depth_params, db.depth_params
    p = db.depth_params[-1]
    assert float(p["w"]) == pytest.approx(39.5, abs=1e-6)
    assert p["as_of"] == _AS_OF
    assert int(p["k"]) == 6
    assert dbg["book_window_s"] == pytest.approx(39.5, abs=1e-3)
    assert dbg["book_k"] == 6
    # a FAST tape (40 prints in 3.9 s) gets a 4.1-s book window: the two snapshots it
    # holds cannot be ranked, so the book is `thin` — never a 30-s book of other prints.
    fast = _TapeAndBookDB(_fading_rows(step_s=0.1, newest_age_s=0.2), _rising_book(1.0))
    _d2, dbg2 = _l2_entry_confirm("ABCD", db=fast, settings=settings, l2_as_of=_AS_OF)
    assert float(fast.depth_params[-1]["w"]) == pytest.approx(4.1, abs=1e-6)
    assert dbg2["n_snaps"] == 2 and dbg2["book_unreadable_why"] == "thin"


@pytest.mark.parametrize("case", ["thrust", "defer", "no_tape", "tape_error", "stale"])
def test_every_path_that_reaches_the_read_reports_its_wall_time(confirm_on, monkeypatch, case):
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.pipeline.read_ladder_distribution",
        lambda *a, **k: None,
    )
    kw = {}
    if case == "thrust":
        db = _FakeDB(_tape([(10.00, 400, False), (10.01, 400, False),
                            (10.02, 600, True), (10.03, 600, True)]))
    elif case == "defer":
        db = _FakeDB(_tape([(10.00, 600, True), (10.01, 600, True),
                            (10.02, 400, False), (10.01, 400, False)]))
    elif case == "no_tape":
        db = _FakeDB([])
    elif case == "tape_error":
        db = _RaisingDB(_timeout_exc())
    else:  # stale: prints that finished 20 h before the decision instant
        as_of = datetime(2026, 9, 10, 14, 20, 0)
        t0 = (as_of - datetime(1970, 1, 1)).total_seconds() - 20 * 3600.0
        rows = [(10.0 + i * 0.001, 100.0, 9.99, 10.0, t0 + i * 0.05) for i in range(40)]

        class _Rows:
            def execute(self, *_a, **_k):
                class _R:
                    def fetchall(self_inner):
                        return rows
                return _R()

        db = _Rows()
        kw = {"l2_as_of": as_of}
    decision, dbg = _l2_entry_confirm("ABCD", db=db, settings=settings, **kw)
    assert isinstance(dbg.get("tape_read_ms"), float), dbg
    assert dbg["tape_read_ms"] >= 0.0
    if case == "stale":
        assert dbg["reason"] == "l2_confirm_tape_stale"
        assert dbg["fallback"] == "fail_open_confirm"


# ── (9) FAIL-OPEN: stale book -> confirm ─────────────────────────────────────────




# ══ THE PREDICATE AFTER THE OUTCOMES WERE MEASURED ══════════════════════════════
# Threshold-free discrimination over 39 readable entries / 23 symbol-days
# (2026-09-08), AUC per leg and clustered per symbol-day:
#
#     buy_share_delta       0.717 / 0.671    median win +0.0926, loss -0.0737
#     prints_since_high     0.667 / 0.671    median win  190.5,  loss   64.0
#     high_print_position   0.636 / 0.605    median win 0.8230,  loss 0.4529
#     signed_tape_accel     0.490 / 0.592    NO outcome information
#     tick_rate             0.434 / 0.487    NO outcome information
#
# Three consequences, encoded below:
#   * buy_share_delta gates — best discrimination, and its sign is the one the
#     design assumed;
#   * the "spent move" refusal is GONE. The median winner sat at 0.823, above the
#     0.75 line it refused at, so it was refusing the median winner. Inside a
#     fifteen-second window "the high is behind us" is a pullback that has been
#     holding, not a spent burst — the operator's own method, and the tape agrees;
#   * signed_tape_accel and tick_rate gate nothing. They were the whole of the
#     original predicate.

def _tape(prices_sizes_sides, t0=1.0, step=3.0):
    """(price, size, is_lift) -> rows the parser reads."""
    out = []
    for i, (px, sz, lift) in enumerate(prices_sizes_sides):
        bid, ask = (px - 0.01, px) if lift else (px, px + 0.01)
        out.append(_tick(px, sz, bid, ask, t0 + i * step))
    return out


def test_carrying_confirms(confirm_on, monkeypatch):
    """Back half more buy-dominated than the front, counted in prints."""
    rows = _tape([(10.00, 400, False), (10.01, 400, False),
                  (10.02, 600, True), (10.03, 600, True)])
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.pipeline.read_ladder_distribution",
        lambda *a, **k: None)
    decision, dbg = _l2_entry_confirm("ABCD", db=_FakeDB(rows), settings=settings)
    assert dbg["buy_share_delta"] > 0
    assert decision == "confirm"
    assert dbg["reason"] == "l2_confirm_tape_thrust"


def test_fading_defers_with_no_book_at_all(confirm_on, monkeypatch):
    """THE decisive case: it must decide without depth, because the replay corpus
    holds zero depth rows and 309 of 348 live entries had none either."""
    rows = _tape([(10.00, 600, True), (10.01, 600, True),
                  (10.02, 400, False), (10.01, 400, False)])
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.pipeline.read_ladder_distribution",
        lambda *a, **k: None)
    decision, dbg = _l2_entry_confirm("ABCD", db=_FakeDB(rows), settings=settings)
    assert dbg["book_readable"] is False
    assert dbg["buy_share_delta"] < 0
    assert decision == "defer"
    assert dbg["reason"] == "l2_confirm_buying_not_carrying"


def test_the_high_being_behind_us_no_longer_refuses(confirm_on, monkeypatch):
    """THE REVERSAL. Median winner sat at high_print_position 0.823. A tape whose
    high is far behind but whose buying is carrying must now CONFIRM — that is the
    pullback the operator buys, and the gate used to refuse it."""
    rows = _tape([(10.05, 400, False), (10.00, 400, False),
                  (10.01, 700, True), (10.02, 700, True)])
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.pipeline.read_ladder_distribution",
        lambda *a, **k: None)
    decision, dbg = _l2_entry_confirm("ABCD", db=_FakeDB(rows), settings=settings)
    assert dbg["high_print_position"] >= 0.75      # would have been "spent"
    assert decision == "confirm"
    assert dbg["reason"] == "l2_confirm_tape_thrust"


def test_an_accumulating_book_overrides_a_fading_tape(confirm_on, monkeypatch):
    """The override survives where it belongs: buying under a fading tape into
    demonstrably accumulating depth is the reclaim this lane should take."""
    rows = _tape([(10.00, 600, True), (10.01, 600, True),
                  (10.02, 400, False), (10.01, 400, False)])
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.pipeline.read_ladder_distribution",
        lambda *a, **k: _ladder(ofi=0.9, micro=2.0, pctile=0.9, age=2.0))
    decision, dbg = _l2_entry_confirm("ABCD", db=_FakeDB(rows), settings=settings)
    assert decision == "confirm"
    assert dbg["reason"] == "l2_confirm_secondary_override"


def test_a_stale_book_cannot_override_because_it_cannot_be_trusted(
        confirm_on, monkeypatch):
    """The invariant the old stale-book test really protected: an unreadable book
    supplies no second opinion. It also supplies no refusal — the refusal here comes
    from the tape, which is fresh."""
    rows = _tape([(10.00, 600, True), (10.01, 600, True),
                  (10.02, 400, False), (10.01, 400, False)])
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.pipeline.read_ladder_distribution",
        lambda *a, **k: _ladder(ofi=0.9, micro=2.0, pctile=0.9, age=9999.0))
    decision, dbg = _l2_entry_confirm("ABCD", db=_FakeDB(rows), settings=settings)
    assert dbg["book_readable"] is False
    assert decision == "defer"
    assert dbg["reason"] == "l2_confirm_buying_not_carrying"


def test_the_two_dead_features_no_longer_decide_anything(confirm_on, monkeypatch):
    """AUC 0.490 and 0.434 — they know nothing about the outcome. A strongly
    negative accel must not refuse a carrying tape, and must not be needed to
    refuse a fading one."""
    carrying = _tape([(10.00, 100, False), (10.01, 100, False),
                      (10.02, 900, True), (10.03, 900, True)])
    decision, dbg = _l2_entry_confirm("ABCD", db=_FakeDB(carrying), settings=settings)
    assert dbg["signed_tape_accel"] > 0 or decision == "confirm"
    assert decision == "confirm"


def test_too_little_tape_to_halve_still_confirms(confirm_on, monkeypatch):
    """Fail-open contract, unchanged: an unreadable share is a missing input."""
    rows = _tape([(10.00, 400, True), (10.01, 400, True), (10.02, 400, True)])
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.pipeline.read_ladder_distribution",
        lambda *a, **k: None)
    decision, dbg = _l2_entry_confirm("ABCD", db=_FakeDB(rows), settings=settings)
    assert decision == "confirm"
