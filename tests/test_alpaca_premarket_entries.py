"""Alpaca-paper PREMARKET entry window (2026-08-17 Ross live study).

Ebidensiya: ang BUONG kita ni Ross ngayong araw (+$63k sa IPST) ay premarket,
habang ang lane ay may TATLONG naka-stack na hard-coded RTH-only gate — kaya ang
premarket conversion ay istrukturang zero. Ang change: isang flag
(`chili_momentum_alpaca_premarket_entries_enabled`, default ON) na nagbubukas ng
premarket window sa (1) `_strict_alpaca_rth_entry_window` at (2) ang instruction
certification carve-out (literal `extended_hours=True` + `time_in_force="day"`
HABANG premarket pa ngayon). Ang afterhours/overnight ay nananatiling blocked;
pagsapit ng 09:30 ang carve-out ay sarado (session != premarket) kaya walang
silent crossover ng premarket-shaped orders papasok sa RTH.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from app.config import settings
import app.services.trading.momentum_neural.live_runner as lr


def _sess():
    return SimpleNamespace(
        execution_family="alpaca_spot",
        symbol="IPST",
        risk_snapshot_json={lr.KEY_LIVE_EXEC: {"effective_max_hold_seconds": 900.0}},
    )


def _clock(*, is_open: bool):
    return (
        {
            "ok": True,
            "paper": True,
            "is_open": is_open,
            "_broker_now": None,
            "_next_close": None,
            "_seconds_to_close": 20_000.0,
            "timestamp": "t",
            "next_close": "c",
        },
        {"evidence": True},
    )


def _window(session: str, *, is_open: bool):
    with patch.object(lr, "_strict_alpaca_clock_truth", return_value=_clock(is_open=is_open)), \
         patch("app.services.trading.momentum_neural.market_profile.market_session_now",
               return_value=session):
        return lr._strict_alpaca_rth_entry_window(adapter=object(), sess=_sess())


def test_premarket_window_open_when_flag_on(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_alpaca_premarket_entries_enabled", True)
    ok, ev = _window("premarket", is_open=False)
    assert ok is True
    assert ev["local_market_session"] == "premarket"


def test_premarket_window_closed_when_flag_off(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_alpaca_premarket_entries_enabled", False)
    ok, ev = _window("premarket", is_open=False)
    assert ok is False
    assert ev["reason"] == "alpaca_new_entries_rth_only"


def test_afterhours_stays_blocked(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_alpaca_premarket_entries_enabled", True)
    ok, ev = _window("afterhours", is_open=False)
    assert ok is False
    assert ev["reason"] == "alpaca_new_entries_rth_only"


def test_regular_open_unchanged(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_alpaca_premarket_entries_enabled", True)
    ok, _ = _window("regular", is_open=True)
    assert ok is True


def test_regular_but_broker_closed_still_blocked(monkeypatch):
    """Halloween case: kalendaryo regular pero sabi ng broker sarado — blocked."""
    monkeypatch.setattr(settings, "chili_momentum_alpaca_premarket_entries_enabled", True)
    ok, ev = _window("regular", is_open=False)
    assert ok is False
    assert ev["reason"] == "alpaca_new_entries_rth_only"


# ---- instruction certification carve-out ----

def _kind(*, ext, tif, premarket_now: bool, flag: bool, monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_alpaca_premarket_entries_enabled", flag)
    kwargs = {
        "side": "buy",
        "position_intent": "buy_to_open",
        "time_in_force": tif,
        "extended_hours": ext,
    }
    with patch.object(lr, "_alpaca_session_is_premarket_now", return_value=premarket_now):
        return lr._alpaca_place_instruction_kind(_sess(), kwargs)


def test_premarket_extended_day_entry_certified(monkeypatch):
    assert _kind(ext=True, tif="day", premarket_now=True, flag=True,
                 monkeypatch=monkeypatch) == "entry"


def test_extended_with_gfd_rejected(monkeypatch):
    """Ang adapter ay nangangailangan ng LITERAL na 'day' sa extended — gfd = invalid."""
    assert _kind(ext=True, tif="gfd", premarket_now=True, flag=True,
                 monkeypatch=monkeypatch) == "invalid_entry_extended_hours"


def test_extended_after_0930_rejected(monkeypatch):
    """Walang silent crossover: premarket-shaped order na sumubok mag-submit sa RTH."""
    assert _kind(ext=True, tif="day", premarket_now=False, flag=True,
                 monkeypatch=monkeypatch) == "invalid_entry_extended_hours"


def test_extended_rejected_when_flag_off(monkeypatch):
    assert _kind(ext=True, tif="day", premarket_now=True, flag=False,
                 monkeypatch=monkeypatch) == "invalid_entry_extended_hours"


def test_regular_entry_shape_unchanged(monkeypatch):
    assert _kind(ext=False, tif="gfd", premarket_now=False, flag=True,
                 monkeypatch=monkeypatch) == "entry"


def test_missing_extended_still_invalid(monkeypatch):
    assert _kind(ext=None, tif="day", premarket_now=True, flag=True,
                 monkeypatch=monkeypatch) == "invalid_entry_extended_hours"
