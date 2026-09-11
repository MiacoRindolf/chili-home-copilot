"""L2 entry CONFIRMER (DEFER-only) — docs/DESIGN/L2_PRIMARY_SIGNAL.md.

The confirmer runs at the live entry seam AFTER the chart trigger fires AND AFTER both
existing vetoes (_l2_entry_veto + _entry_flow_veto) pass — a veto ALWAYS wins. It reads
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
        FAILED tape read (l2_confirm_tape_error, why/error/where), and any other
        exception (l2_confirm_error + WARNING) are three different receipts — before,
        all three were `l2_confirm_no_data`. `l2_confirm_no_data` now means only
        "db None / blank symbol". Every one of them still CONFIRMS, with
        `fallback=fail_open_confirm` on the receipt.
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


def test_a_feature_computation_that_raises_names_where(confirm_on, monkeypatch):
    """The rows came back; computing the features raised. Still an error, and the
    receipt says it was the features, not the query."""
    rows = _tape([(10.00, 600, True), (10.01, 600, True),
                  (10.02, 400, False), (10.01, 400, False)])

    def _boom(*_a, **_k):
        raise ZeroDivisionError("bad window")

    monkeypatch.setattr(entry_gates, "_signed_tape_features", _boom)
    decision, dbg = _l2_entry_confirm("ABCD", db=_FakeDB(rows), settings=settings)
    assert decision == "confirm"
    assert dbg["reason"] == "l2_confirm_tape_error"
    assert dbg["why"] == "error"
    assert dbg["error"] == "ZeroDivisionError"
    assert dbg["where"] == "features"


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
    assert dbg["fallback"] == "fail_open_confirm"
    assert "tape_read_ms" in dbg, "the read completed before the bug"
    warned = [r for r in caplog.records
              if r.levelno == logging.WARNING and r.getMessage().startswith("[entry_gates]")]
    assert warned, "an exception in the confirmer must leave a [entry_gates] WARNING"
    assert "ValueError" in warned[0].getMessage()


def test_a_book_that_raises_is_named_and_the_tape_still_decides(confirm_on, monkeypatch):
    """The book is secondary. A RAISED book read is recorded as `book_error`, supplies
    no override, and the fresh tape decides exactly as with no book at all."""
    rows = _tape([(10.00, 600, True), (10.01, 600, True),
                  (10.02, 400, False), (10.01, 400, False)])

    def _raise(*_a, **_k):
        raise RuntimeError("depth bridge table missing")

    monkeypatch.setattr(
        "app.services.trading.momentum_neural.pipeline.read_ladder_distribution", _raise)
    decision, dbg = _l2_entry_confirm("ABCD", db=_FakeDB(rows), settings=settings)
    assert dbg["book_error"] == "RuntimeError"
    assert dbg["book_readable"] is False
    assert dbg["buy_share_delta"] < 0
    assert decision == "defer" and dbg["reason"] == "l2_confirm_buying_not_carrying"
    assert "fallback" not in dbg, "a tape-decided defer is a decision, not a fallback"


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
