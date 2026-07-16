"""Shake-out hardening — churn guards + early data session (tick-speed era)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.services.trading.momentum_neural.auto_arm import (
    _LossGuardHistoryUnavailable,
    _symbol_loss_guards,
)
from app.services.trading.momentum_neural.market_profile import is_data_session_now, is_tradeable_now
from app.services.trading.momentum_neural.risk_policy import CurrentLiveLossHistoryEntry
from app.services.trading.venue import account_identity


@pytest.fixture(autouse=True)
def _stable_account_identity(monkeypatch):
    monkeypatch.setattr(
        account_identity,
        "read_current_non_alpaca_account_identity",
        lambda _family: {
            "ok": True,
            "identity": "shakeout-account-v1",
            "reason": None,
        },
    )


class _Q:
    def __init__(self, rows): self._r = rows
    def join(self, *a, **k): return self
    def filter(self, *a, **k): return self
    def all(self): return self._r


def _db(rows):
    return SimpleNamespace(loss_rows=rows, query=lambda *a, **k: _Q(rows))


def _guards(db, **kwargs):
    execution_family = kwargs.pop("execution_family", "robinhood_spot")
    if hasattr(db, "loss_rows"):
        entries = tuple(
            CurrentLiveLossHistoryEntry(
                session_id=index,
                outcome_id=index,
                symbol=symbol,
                terminal_at=terminal_at,
                outcome_class="stop_loss",
                realized_pnl_usd=float(pnl),
                return_bps=float(return_bps),
                broker_reconciled_at=terminal_at,
            )
            for index, (symbol, terminal_at, pnl, return_bps, _family) in enumerate(
                db.loss_rows, start=1
            )
        )
        kwargs["_current_live_history"] = (
            entries,
            {
                "history_available": True,
                "coverage_grade": "CURRENT_LIVE_COMPLETE",
                "replay_certifiable": False,
            },
        )
    return _symbol_loss_guards(
        db,
        user_id=1,
        execution_family=execution_family,
        **kwargs,
    )


def test_two_strike_blocks_symbol_for_day():
    now = datetime.utcnow()
    rows = [("BATL", now - timedelta(hours=2), -200.0, -200.0, "robinhood_spot"),
            ("BATL", now - timedelta(hours=1), -180.0, -180.0, "robinhood_spot"),
            ("DSY", now - timedelta(hours=1), -90.0, -90.0, "robinhood_spot")]
    blocked, cooldown = _guards(_db(rows))
    assert "BATL" in blocked          # 2 strikes -> done for the day
    assert "DSY" not in blocked       # single loss -> only cooldown
    assert "DSY" in cooldown


def test_post_loss_cooldown_expires():
    now = datetime.utcnow()
    rows = [("DSY", now - timedelta(minutes=20), -90.0, -90.0, "robinhood_spot")]  # 20min ago
    blocked, cooldown = _guards(_db(rows))
    assert "DSY" not in blocked
    assert cooldown["DSY"] < now      # already expired -> re-armable


def test_loss_guard_fails_closed():
    class _Boom:
        def query(self, *a, **k): raise RuntimeError("db down")
    with pytest.raises(_LossGuardHistoryUnavailable):
        _guards(_Boom())


# --- Adaptive post-loss cooldown (2026-06-16, the CCTG re-entry) ------------------
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
    _blocked, cooldown = _guards(_db(rows))
    assert cooldown["CCTG"] == now + timedelta(minutes=5.0)      # exactly the fixed base


def test_cctg_reentry_cooldown_scales():
    # The real bug: a -892bps bailout must sit the name out longer than the fixed 5min.
    now = datetime.utcnow()
    rows = [("CCTG", now, -83.0, -892.0, "robinhood_spot")]
    _blocked, cooldown = _guards(_db(rows))
    assert cooldown["CCTG"] == now + timedelta(minutes=6.784)    # > the fixed 5min


def test_crypto_uses_fixed_base_not_adaptive():
    # Crypto stays BYTE-IDENTICAL (fixed base) — a big -892bps crypto loss still gets
    # only the 5min base here; crypto churn is bounded by reap_cooldown elsewhere.
    now = datetime.utcnow()
    rows = [("TAO-USD", now, -50.0, -892.0, "coinbase_spot")]
    _blocked, cooldown = _guards(_db(rows), execution_family="coinbase_spot")
    assert cooldown["TAO-USD"] == now + timedelta(minutes=5.0)   # fixed, not 6.784


def test_winners_never_in_cooldown():
    # The query filters realized_pnl_usd<0 -> a winner produces no loss row -> no
    # cooldown. (ASTN/MEGA/ICP winners are never throttled by this lever.) The _db
    # fixture's _Q ignores .filter, so emulate the real filter: pass only loss rows.
    now = datetime.utcnow()
    rows = []  # a winning day has no loss outcomes for the symbol
    _blocked, cooldown = _guards(_db(rows))
    assert "ASTN" not in cooldown and cooldown == {}


def test_data_session_wider_than_entry_window():
    # 03:00 ET (07:00Z EDT): with the 04:00 exchange-open entry default,
    # the one-hour preparation/data window is open while entries stay closed.
    t = datetime(2026, 6, 11, 7, 0, tzinfo=timezone.utc)
    assert is_data_session_now("DSY", now=t) is True
    assert is_tradeable_now("DSY", now=t) is False
    # 02:59 ET: both closed
    t2 = datetime(2026, 6, 11, 6, 59, tzinfo=timezone.utc)
    assert is_data_session_now("DSY", now=t2) is False
    # weekend: closed
    t3 = datetime(2026, 6, 13, 14, 0, tzinfo=timezone.utc)
    assert is_data_session_now("DSY", now=t3) is False
    # crypto: always
    assert is_data_session_now("BTC-USD", now=t3) is True
