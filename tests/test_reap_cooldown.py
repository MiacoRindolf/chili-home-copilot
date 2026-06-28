"""Post-reap cooldown (§7A): a crypto name reaped pre-entry (watched the full
window without firing) sits out chili_momentum_reap_cooldown_sec before it can
re-arm — so RENDER/WLD (looped arm->reap 88x/56x/24h) stop hogging the single
live slot and a different fresh mover gets watched. Crypto-only; equity untouched.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.config import settings
from app.services.trading.momentum_neural.auto_arm import (
    _reap_cooldown_active,
    _REAP_COOLDOWN,
    _write_reap_cooldown,
)


@pytest.fixture(autouse=True)
def _clear_cooldown():
    _REAP_COOLDOWN.clear()
    yield
    _REAP_COOLDOWN.clear()


def test_inactive_when_never_reaped():
    assert _reap_cooldown_active("NEVER-USD", datetime.utcnow()) is False


def test_active_right_after_reap():
    now = datetime.utcnow()
    _REAP_COOLDOWN["RENDER-USD"] = now
    assert _reap_cooldown_active("RENDER-USD", now + timedelta(seconds=10)) is True


def test_expires_after_window(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_reap_cooldown_sec", 120.0)
    now = datetime.utcnow()
    _REAP_COOLDOWN["WLD-USD"] = now
    assert _reap_cooldown_active("WLD-USD", now + timedelta(seconds=119)) is True
    assert _reap_cooldown_active("WLD-USD", now + timedelta(seconds=121)) is False


def test_kill_switch_zero_disables(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_reap_cooldown_sec", 0.0)
    now = datetime.utcnow()
    _REAP_COOLDOWN["SEI-USD"] = now
    assert _reap_cooldown_active("SEI-USD", now + timedelta(seconds=1)) is False


def test_reap_writes_cooldown_regardless_of_asset_class(monkeypatch):
    """A pre-entry reap damps the reaped symbol REGARDLESS of asset class. The cooldown
    was generalized off the old '-USD'-only gate (2026-06-17, _write_reap_cooldown) so
    equities — the rank-displacement motivating case (e.g. UTSI) — are damped too, not
    just crypto. So a reap writes an active cooldown for both an equity and a crypto name."""
    monkeypatch.setattr(settings, "chili_momentum_reap_cooldown_sec", 120.0)
    now = datetime.utcnow()
    for sym in ("UTSI", "RENDER-USD"):  # equity + crypto
        _write_reap_cooldown(sym, now)
        assert _reap_cooldown_active(sym, now + timedelta(seconds=10)) is True, sym
