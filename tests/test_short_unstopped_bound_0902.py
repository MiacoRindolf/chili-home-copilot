"""aggregate_open_risk_usd: an UNSTOPPED SHORT is BOUNDED, not a raise.

PR #1285 charged an unstopped LONG its full notional but kept the fail-closed
``position_risk_fields_invalid`` raise for an unstopped SHORT (alpaca_short /
side_long False). That raise is the same account-wide landmine class: one such
row fails EVERY Alpaca submit (the 806x long case on 09-01/09-02). The short
lane is quarantined today (``alpaca_short_execution_not_certified``), so the
shape is low-frequency — an alpaca_spot row carrying a short marker after an
operator repair — but the blast radius is the whole account.

THE BOUND (one documented base): notional x
``chili_momentum_short_unstopped_notional_multiple`` (default 2.0 = a +200%
adverse move, the price triples). Admitted ONLY for a row whose direction is
provably short on every marker ``_le_side_long`` reads. Unreadable qty/basis
and contradictory/unreadable direction still raise.

Runnable: pytest tests/test_short_unstopped_bound_0902.py -v
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.services.trading.momentum_neural import risk_evaluator as RE


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *_a, **_k):
        return self

    def all(self):
        return list(self._rows)


class _FakeDb:
    def __init__(self, rows):
        self._rows = rows

    def query(self, *_a, **_k):
        return _FakeQuery(self._rows)


def _sess(sid, *, state="live_entered", le, family="alpaca_spot", symbol="CANF"):
    return SimpleNamespace(
        id=sid, symbol=symbol, state=state, execution_family=family,
        risk_snapshot_json={"alpaca_account_scope": "alpaca:paper",
                            "momentum_live_execution": le},
        user_id=1,
    )


@pytest.fixture(autouse=True)
def _no_claims(monkeypatch):
    monkeypatch.setattr(RE, "_alpaca_unresolved_entry_claims", lambda db: [])


def _agg(db):
    return RE.aggregate_open_risk_usd(db, user_id=1, execution_family="alpaca_spot")


_UNSTOPPED = {"quantity": 177, "avg_entry_price": 9.91, "stop_price": None}


# ── 1. the bound ─────────────────────────────────────────────────────────────


def test_unstopped_alpaca_short_family_is_notional_times_multiple():
    db = _FakeDb([_sess(1, family="alpaca_short", le={"position": dict(_UNSTOPPED)})])
    total, rows = _agg(db)
    assert total == pytest.approx(177 * 9.91 * 2.0)
    assert rows == [{"symbol": "CANF", "session_id": 1, "execution_family": "alpaca_short",
                     "at_risk_usd": pytest.approx(3508.14),
                     "note": "stop_unknown_short_notional_multiple_bound",
                     "notional_multiple": 2.0}]


@pytest.mark.parametrize("le_extra,pos_extra", [
    ({"side_long": False}, {}),
    ({}, {"side_long": False}),
    ({}, {"side": "short"}),
    ({}, {"side": "sell"}),
    ({"side": "short"}, {}),
    ({}, {"position_intent": "sell_to_open"}),
    ({}, {"intent": "buy_to_close"}),
    ({"position_intent": "sell_to_open"}, {}),
    ({"side_long": False}, {"side": "short", "intent": "buy_to_close"}),
])
def test_unstopped_short_shaped_alpaca_spot_row_is_bounded(le_extra, pos_extra):
    """Every short marker `_le_side_long` honours, alone or consistently combined."""
    pos = {**_UNSTOPPED, **pos_extra}
    db = _FakeDb([_sess(1, le={**le_extra, "position": pos})])
    total, rows = _agg(db)
    assert total == pytest.approx(3508.14)
    assert rows[0]["note"] == "stop_unknown_short_notional_multiple_bound"


def test_the_bound_never_blocks_a_sibling_by_raising():
    """The landmine shape: one unstopped short + a healthy stopped long."""
    short = _sess(1, family="alpaca_short", le={"position": dict(_UNSTOPPED)})
    long_ = _sess(2, le={"side_long": True, "position": {"quantity": 100, "avg_entry_price": 5.0, "stop_price": 4.8}})
    total, rows = _agg(_FakeDb([short, long_]))
    assert total == pytest.approx(3508.14 + 20.0)
    assert {r["session_id"] for r in rows} == {1, 2}


# ── 2. what still raises ─────────────────────────────────────────────────────


@pytest.mark.parametrize("le_extra,pos_extra", [
    ({"side_long": True}, {"side": "short"}),        # contradictory
    ({"side_long": False}, {"side": "long"}),        # contradictory
    ({}, {"side": "sideways"}),                       # unreadable
    ({"side_long": False}, {"intent": "buy_to_open"}),  # contradictory intent
    ({}, {"side_long": "no"}),                        # non-bool flag
])
def test_contradictory_or_unreadable_direction_still_raises(le_extra, pos_extra):
    pos = {**_UNSTOPPED, **pos_extra}
    db = _FakeDb([_sess(1, le={**le_extra, "position": pos})])
    with pytest.raises(RuntimeError, match="position_risk_fields_invalid"):
        _agg(db)


def test_alpaca_short_family_with_a_long_marker_still_raises():
    db = _FakeDb([_sess(1, family="alpaca_short", le={"side_long": True, "position": dict(_UNSTOPPED)})])
    with pytest.raises(RuntimeError, match="position_risk_fields_invalid"):
        _agg(db)


@pytest.mark.parametrize("field", ["quantity", "avg_entry_price"])
@pytest.mark.parametrize("bad", [None, "bad", "nan", "inf", True, 0, -1])
def test_unreadable_qty_or_basis_on_a_short_still_raises(field, bad):
    pos = {**_UNSTOPPED, field: bad}
    db = _FakeDb([_sess(1, family="alpaca_short", le={"position": pos})])
    with pytest.raises(RuntimeError, match="position_risk_fields_invalid"):
        _agg(db)


def test_a_stopped_short_keeps_the_stop_minus_entry_geometry():
    pos = {"quantity": 100, "avg_entry_price": 10.0, "stop_price": 10.5}
    total, rows = _agg(_FakeDb([_sess(1, family="alpaca_short", le={"position": pos})]))
    assert total == pytest.approx(50.0)
    assert "note" not in rows[0]


def test_unstopped_long_bound_is_unchanged():
    total, rows = _agg(_FakeDb([_sess(1, le={"side_long": True, "position": dict(_UNSTOPPED)})]))
    assert total == pytest.approx(1754.07)
    assert rows[0]["note"] == "stop_unknown_full_notional_bound"
    assert "notional_multiple" not in rows[0]


# ── 3. the multiple: one documented base, never looser than a doubling ───────


def test_multiple_reads_settings_and_falls_back_to_default(monkeypatch):
    monkeypatch.setattr(RE, "settings", SimpleNamespace(chili_momentum_short_unstopped_notional_multiple=3.5))
    assert RE._short_unstopped_notional_multiple() == 3.5
    total, rows = _agg(_FakeDb([_sess(1, family="alpaca_short", le={"position": dict(_UNSTOPPED)})]))
    assert total == pytest.approx(177 * 9.91 * 3.5) and rows[0]["notional_multiple"] == 3.5
    for bad in (0.5, 0, -1, "nan", "inf", "x", None):
        monkeypatch.setattr(RE, "settings", SimpleNamespace(chili_momentum_short_unstopped_notional_multiple=bad))
        assert RE._short_unstopped_notional_multiple() == 2.0, bad
    monkeypatch.setattr(RE, "settings", SimpleNamespace())
    assert RE._short_unstopped_notional_multiple() == 2.0


def test_settings_default_and_floor():
    assert Settings().chili_momentum_short_unstopped_notional_multiple == 2.0
    with pytest.raises(Exception):
        Settings(chili_momentum_short_unstopped_notional_multiple=0.5)


# ── 4. guards ────────────────────────────────────────────────────────────────


def test_certified_short_helper_vocabulary():
    assert RE._alpaca_position_certified_short(SimpleNamespace(execution_family="alpaca_short"), {}, {}) is True
    assert RE._alpaca_position_certified_short(SimpleNamespace(execution_family="alpaca_spot"), {}, {}) is False, \
        "no marker at all is NOT a short (it is the long-by-default row)"
    assert RE._alpaca_position_certified_short(SimpleNamespace(execution_family="alpaca_spot"), {"side_long": False}, {}) is True
    assert RE._alpaca_position_certified_short(SimpleNamespace(execution_family="alpaca_spot"), "bad", {}) is False


def test_aggregate_source_keeps_the_raise_and_both_bounds():
    src = inspect.getsource(RE.aggregate_open_risk_usd)
    assert src.count('"position_risk_fields_invalid"') == 2, "qty/basis + direction raises"
    assert "stop_unknown_full_notional_bound" in src
    assert "stop_unknown_short_notional_multiple_bound" in src
    assert "_alpaca_position_certified_short(" in src
    assert "_short_unstopped_notional_multiple()" in src


def test_short_bound_row_is_a_note_row_not_a_displacement_target():
    src = inspect.getsource(RE._enqueue_risk_envelope_displacement)
    assert 'not r.get("note")' in src
    row = {"symbol": "A", "session_id": 1, "at_risk_usd": 3508.14,
           "note": "stop_unknown_short_notional_multiple_bound", "notional_multiple": 2.0}
    assert not [r for r in [row] if not r.get("note")]
