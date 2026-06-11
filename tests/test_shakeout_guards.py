"""Shake-out hardening — churn guards + early data session (tick-speed era)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.services.trading.momentum_neural.auto_arm import _symbol_loss_guards
from app.services.trading.momentum_neural.market_profile import is_data_session_now, is_tradeable_now


class _Q:
    def __init__(self, rows): self._r = rows
    def filter(self, *a, **k): return self
    def all(self): return self._r


def _db(rows):
    return SimpleNamespace(query=lambda *a, **k: _Q(rows))


def test_two_strike_blocks_symbol_for_day():
    now = datetime.utcnow()
    rows = [("BATL", now - timedelta(hours=2), -200.0), ("BATL", now - timedelta(hours=1), -180.0),
            ("DSY", now - timedelta(hours=1), -90.0)]
    blocked, cooldown = _symbol_loss_guards(_db(rows))
    assert "BATL" in blocked          # 2 strikes -> done for the day
    assert "DSY" not in blocked       # single loss -> only cooldown
    assert "DSY" in cooldown


def test_post_loss_cooldown_expires():
    now = datetime.utcnow()
    rows = [("DSY", now - timedelta(minutes=20), -90.0)]  # loss 20min ago, cooldown 5min
    blocked, cooldown = _symbol_loss_guards(_db(rows))
    assert "DSY" not in blocked
    assert cooldown["DSY"] < now      # already expired -> re-armable


def test_loss_guard_fails_open():
    class _Boom:
        def query(self, *a, **k): raise RuntimeError("db down")
    blocked, cooldown = _symbol_loss_guards(_Boom())
    assert blocked == set() and cooldown == {}


def test_data_session_wider_than_entry_window():
    # 05:00 ET (09:00Z EDT): data session OPEN, entries CLOSED — the prep window
    t = datetime(2026, 6, 11, 9, 0, tzinfo=timezone.utc)
    assert is_data_session_now("DSY", now=t) is True
    assert is_tradeable_now("DSY", now=t) is False
    # 03:00 ET: both closed
    t2 = datetime(2026, 6, 11, 7, 0, tzinfo=timezone.utc)
    assert is_data_session_now("DSY", now=t2) is False
    # weekend: closed
    t3 = datetime(2026, 6, 13, 14, 0, tzinfo=timezone.utc)
    assert is_data_session_now("DSY", now=t3) is False
    # crypto: always
    assert is_data_session_now("BTC-USD", now=t3) is True
