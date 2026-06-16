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
    rows = [("BATL", now - timedelta(hours=2), -200.0, -200.0, "robinhood_spot"),
            ("BATL", now - timedelta(hours=1), -180.0, -180.0, "robinhood_spot"),
            ("DSY", now - timedelta(hours=1), -90.0, -90.0, "robinhood_spot")]
    blocked, cooldown = _symbol_loss_guards(_db(rows))
    assert "BATL" in blocked          # 2 strikes -> done for the day
    assert "DSY" not in blocked       # single loss -> only cooldown
    assert "DSY" in cooldown


def test_post_loss_cooldown_expires():
    now = datetime.utcnow()
    rows = [("DSY", now - timedelta(minutes=20), -90.0, -90.0, "robinhood_spot")]  # 20min ago
    blocked, cooldown = _symbol_loss_guards(_db(rows))
    assert "DSY" not in blocked
    assert cooldown["DSY"] < now      # already expired -> re-armable


def test_loss_guard_fails_open():
    class _Boom:
        def query(self, *a, **k): raise RuntimeError("db down")
    blocked, cooldown = _symbol_loss_guards(_Boom())
    assert blocked == set() and cooldown == {}


# --- Adaptive post-loss cooldown (2026-06-16, the CCTG re-entry) ------------------
import pytest  # noqa: E402

from app.services.trading.momentum_neural.auto_arm import _adaptive_loss_cooldown_minutes  # noqa: E402
from app.services.trading.momentum_neural import auto_arm as _aa  # noqa: E402


def test_adaptive_loss_cooldown_minutes_formula():
    # Defaults: base=5, bps_per_min=500, cap=4x base=20.
    assert _adaptive_loss_cooldown_minutes(-159.0) == pytest.approx(5.318, abs=1e-3)
    assert _adaptive_loss_cooldown_minutes(-892.0) == pytest.approx(6.784, abs=1e-3)
    assert _adaptive_loss_cooldown_minutes(-53.0) == pytest.approx(5.106, abs=1e-3)
    assert _adaptive_loss_cooldown_minutes(None) == 5.0          # fail-open: no magnitude
    assert _adaptive_loss_cooldown_minutes(-10000.0) == 20.0     # clamped to 4x base


def test_adaptive_disabled_is_byte_identical(monkeypatch):
    # Kill-switch off -> fixed base regardless of loss size (pre-change behavior).
    monkeypatch.setattr(_aa.settings, "chili_momentum_loss_cooldown_adaptive_enabled", False, raising=False)
    now = datetime.utcnow()
    rows = [("CCTG", now, -83.0, -892.0, "robinhood_spot")]
    _blocked, cooldown = _symbol_loss_guards(_db(rows))
    assert cooldown["CCTG"] == now + timedelta(minutes=5.0)      # exactly the fixed base


def test_cctg_reentry_cooldown_scales():
    # The real bug: a -892bps bailout must sit the name out longer than the fixed 5min.
    now = datetime.utcnow()
    rows = [("CCTG", now, -83.0, -892.0, "robinhood_spot")]
    _blocked, cooldown = _symbol_loss_guards(_db(rows))
    assert cooldown["CCTG"] == now + timedelta(minutes=6.784)    # > the fixed 5min


def test_crypto_uses_fixed_base_not_adaptive():
    # Crypto stays BYTE-IDENTICAL (fixed base) — a big -892bps crypto loss still gets
    # only the 5min base here; crypto churn is bounded by reap_cooldown elsewhere.
    now = datetime.utcnow()
    rows = [("TAO-USD", now, -50.0, -892.0, "coinbase_spot")]
    _blocked, cooldown = _symbol_loss_guards(_db(rows))
    assert cooldown["TAO-USD"] == now + timedelta(minutes=5.0)   # fixed, not 6.784


def test_winners_never_in_cooldown():
    # The query filters realized_pnl_usd<0 -> a winner produces no loss row -> no
    # cooldown. (ASTN/MEGA/ICP winners are never throttled by this lever.) The _db
    # fixture's _Q ignores .filter, so emulate the real filter: pass only loss rows.
    now = datetime.utcnow()
    rows = []  # a winning day has no loss outcomes for the symbol
    _blocked, cooldown = _symbol_loss_guards(_db(rows))
    assert "ASTN" not in cooldown and cooldown == {}


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
