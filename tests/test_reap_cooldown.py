"""Post-reap cooldown (§7A): a crypto name reaped pre-entry (watched the full
window without firing) sits out chili_momentum_reap_cooldown_sec before it can
re-arm — so RENDER/WLD (looped arm->reap 88x/56x/24h) stop hogging the single
live slot and a different fresh mover gets watched. Crypto-only; equity untouched.
"""

from __future__ import annotations

import io
from datetime import datetime, timedelta

import pytest

from app.config import settings
from app.services.trading.momentum_neural.auto_arm import _reap_cooldown_active, _REAP_COOLDOWN


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


def test_record_and_skip_are_crypto_gated():
    """The reap RECORD and the eligible-loop SKIP must both be -USD-gated so equity
    arming stays byte-identical (the cooldown is the crypto churn fix only)."""
    src = io.open(
        "app/services/trading/momentum_neural/auto_arm.py", encoding="utf-8"
    ).read()
    # record side: inside the reap loop, the write to _REAP_COOLDOWN is guarded by .endswith("-USD")
    rec = src[src.index("reaped += 1"):src.index("reaped += 1") + 700]
    assert "_REAP_COOLDOWN[_rs] = now" in rec
    assert '.endswith("-USD")' in rec
    # skip side: the eligible-loop skip is -USD-gated
    skip_anchor = src.index("reap_cooldown_skipped\"] += 1")
    skip = src[skip_anchor - 200:skip_anchor]
    assert '_sym_u.endswith("-USD")' in skip
    assert "_reap_cooldown_active" in skip
