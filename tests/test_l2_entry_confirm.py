"""Phase-1 L2 entry CONFIRMER (DEFER-only) — docs/DESIGN/L2_PRIMARY_SIGNAL.md.

The confirmer runs at the live entry seam AFTER the chart trigger fires AND AFTER both
existing vetoes (_l2_entry_veto + _entry_flow_veto) pass — a veto ALWAYS wins. It is
TAPE-PRIMARY: a CONFIRM needs the executed tape to actively confirm thrust
(signed_tape_accel>0 AND tick_rate>=self-relative floor); OFI/micro + a rising depth-
imbalance percentile are secondary agreement confirmers. CONSERVATIVE-ACTIVE: it DEFERs
ONLY on CLEAR no-confirmation (signed_tape_accel<=0 AND OFI<0).

These tests pin the two pure pieces (no real DB — a tiny fake `db` returns canned rows /
a canned LadderRead via a monkeypatched read_ladder_distribution):

  TAPE HELPER (_signed_tape_features):
    (1) rising aggressor-signed buy tape -> signed_tape_accel > 0.
    (2) dead/negative tape -> signed_tape_accel <= 0.
    (3) too-few / empty ticks -> None (=> caller fails open).

  _l2_entry_confirm:
    (4) DISABLED (flag False) -> ("confirm", reason=l2_confirm_disabled) before any I/O.
    (5) rising tape -> confirm (reason l2_confirm_tape_thrust).
    (6) dead/negative tape + net-selling OFI + no secondary -> DEFER (l2_confirm_defer_no_tape).
    (7) dead tape but a secondary buy-side confirmer disagrees -> confirm (override).
    (8) FAIL-OPEN: empty tape -> confirm (l2_confirm_no_data).
    (9) FAIL-OPEN: stale book (snapshot_age too old) -> confirm (l2_confirm_no_data).
    (10) mixed (flat tape, OFI not negative) -> confirm (conservative-active, no over-defer).
"""

from __future__ import annotations

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
    The confirmer's book read is monkeypatched separately, so this only feeds the tape."""

    def __init__(self, rows):
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
    out = _signed_tape_features(rows, window_s=15.0)
    assert out is not None
    assert out["signed_tape_accel"] > 0.0
    assert out["n_ticks"] == 6
    assert out["tick_rate"] >= 0.0


def test_tape_helper_dead_negative_tape_nonpositive_accel():
    # back half is bid-hitting sells; no back-half buy volume -> accel <= 0.
    rows = [
        _tick(10.01, 400, 10.00, 10.01, 1.0),   # front: buy
        _tick(10.02, 500, 10.01, 10.02, 4.0),   # front: buy
        _tick(10.00, 300, 10.00, 10.01, 9.0),   # back: px<=bid -> sell
        _tick(9.99, 400, 9.99, 10.00, 12.0),    # back: sell
        _tick(9.98, 500, 9.98, 9.99, 15.0),     # back: sell
    ]
    out = _signed_tape_features(rows, window_s=15.0)
    assert out is not None
    assert out["signed_tape_accel"] <= 0.0


def test_tape_helper_too_few_ticks_returns_none():
    assert _signed_tape_features([], window_s=15.0) is None
    assert _signed_tape_features([_tick(10.0, 100, 9.99, 10.0, 1.0)], window_s=15.0) is None


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

def test_dead_tape_selling_ofi_defers(confirm_on, monkeypatch):
    rows = [
        _tick(10.01, 400, 10.00, 10.01, 1.0),   # front buy
        _tick(10.02, 500, 10.01, 10.02, 4.0),   # front buy
        _tick(10.00, 300, 10.00, 10.01, 9.0),   # back sell
        _tick(9.99, 400, 9.99, 10.00, 12.0),    # back sell
        _tick(9.98, 500, 9.98, 9.99, 15.0),     # back sell
    ]
    db = _FakeDB(rows)
    # OFI negative, micro<0, depth pctile low (ask-heavy) -> no secondary buy-side disagreement.
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.pipeline.read_ladder_distribution",
        lambda *a, **k: _ladder(ofi=-0.7, micro=-2.0, pctile=0.1, age=2.0),
    )
    decision, dbg = _l2_entry_confirm("ABCD", db=db, settings=settings)
    assert decision == "defer"
    assert dbg["reason"] == "l2_confirm_defer_no_tape"


# ── (7) dead tape but secondary buy-side confirmer disagrees -> CONFIRM (override) ──

def test_dead_tape_but_secondary_buyside_overrides_to_confirm(confirm_on, monkeypatch):
    rows = [
        _tick(10.01, 400, 10.00, 10.01, 1.0),
        _tick(10.02, 500, 10.01, 10.02, 4.0),
        _tick(10.00, 300, 10.00, 10.01, 9.0),
        _tick(9.99, 400, 9.99, 10.00, 12.0),
        _tick(9.98, 500, 9.98, 9.99, 15.0),
    ]
    db = _FakeDB(rows)
    # accel<=0 but OFI negative — yet micro_edge>0 (book leans buy at the touch) AND depth
    # pctile rising (>=0.5): a secondary confirmer disagrees with the bearish read -> confirm.
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.pipeline.read_ladder_distribution",
        lambda *a, **k: _ladder(ofi=-0.1, micro=1.5, pctile=0.8, age=2.0),
    )
    decision, dbg = _l2_entry_confirm("ABCD", db=db, settings=settings)
    assert decision == "confirm"
    assert dbg["reason"] == "l2_confirm_secondary_override"


# ── (8) FAIL-OPEN: empty tape -> confirm ─────────────────────────────────────────

def test_empty_tape_fails_open_to_confirm(confirm_on, monkeypatch):
    db = _FakeDB([])  # no ticks -> tape helper None -> fail-open
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.pipeline.read_ladder_distribution",
        lambda *a, **k: _ladder(ofi=-0.9, micro=-3.0, pctile=0.05, age=2.0),
    )
    decision, dbg = _l2_entry_confirm("ABCD", db=db, settings=settings)
    assert decision == "confirm"
    assert dbg["reason"] == "l2_confirm_no_data"


# ── (9) FAIL-OPEN: stale book -> confirm ─────────────────────────────────────────

def test_stale_book_fails_open_to_confirm(confirm_on, monkeypatch):
    # dead/negative tape that WOULD defer, but the book is stale -> fail-open BEFORE deferring.
    rows = [
        _tick(10.01, 400, 10.00, 10.01, 1.0),
        _tick(10.02, 500, 10.01, 10.02, 4.0),
        _tick(10.00, 300, 10.00, 10.01, 9.0),
        _tick(9.99, 400, 9.99, 10.00, 12.0),
        _tick(9.98, 500, 9.98, 9.99, 15.0),
    ]
    db = _FakeDB(rows)
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.pipeline.read_ladder_distribution",
        lambda *a, **k: _ladder(ofi=-0.9, micro=-3.0, pctile=0.05, age=9999.0),
    )
    decision, dbg = _l2_entry_confirm("ABCD", db=db, settings=settings)
    assert decision == "confirm"
    assert dbg["reason"] == "l2_confirm_no_data"


# ── (10) mixed (flat tape, OFI not negative) -> confirm (no over-defer) ──────────

def test_mixed_flat_tape_positive_ofi_confirms(confirm_on, monkeypatch):
    # back half flat/no-buy (accel<=0) but OFI is NOT negative -> not the clear-no-confirm
    # condition -> conservative-active confirm.
    rows = [
        _tick(10.01, 400, 10.00, 10.01, 1.0),
        _tick(10.02, 500, 10.01, 10.02, 4.0),
        _tick(10.02, 100, 10.01, 10.02, 9.0),   # back: tick-rule zero/flat
        _tick(10.02, 100, 10.01, 10.02, 12.0),
        _tick(10.02, 100, 10.01, 10.02, 15.0),
    ]
    db = _FakeDB(rows)
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.pipeline.read_ladder_distribution",
        lambda *a, **k: _ladder(ofi=0.05, micro=0.0, pctile=0.4, age=2.0),
    )
    decision, dbg = _l2_entry_confirm("ABCD", db=db, settings=settings)
    assert decision == "confirm"
    # either pass_mixed or tape_thrust depending on the exact accel sign; must NOT defer.
    assert dbg["reason"] != "l2_confirm_defer_no_tape"
