"""Regression tests for the 2026-06-22 ENTRY-REJECT cooldown (conversion fix).

When the broker REFUSES a live entry (place_equity_order isError — a leveraged/inverse
ETF tripping EQUITY_SUITABILITY like RKLZ/CORD, or a name untradable in the current
session), auto-arm must sit that name out so the lane stops looping
arm->break->reject->reap on a name the rail won't fill, freeing the slot for a fillable
mover. ADAPTIVE (learns from real rejections, no hardcoded leveraged-ETF list),
SELF-HEALING (TTL), fail-open (no record -> arm normally).
"""
from __future__ import annotations

from datetime import datetime, timedelta

from app.config import settings
from app.services.trading.momentum_neural.auto_arm import (
    _ENTRY_REJECT_COOLDOWN,
    _entry_reject_cooldown_active,
    _write_entry_reject_cooldown,
)

_T0 = datetime(2026, 6, 22, 14, 0, 0)


def test_recorded_symbol_is_cooled_down_within_ttl_then_expires(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_entry_reject_cooldown_sec", 900.0)
    _ENTRY_REJECT_COOLDOWN.clear()
    _write_entry_reject_cooldown("RKLZ", _T0)
    assert _entry_reject_cooldown_active("RKLZ", _T0 + timedelta(seconds=60)) is True
    assert _entry_reject_cooldown_active("RKLZ", _T0 + timedelta(seconds=899)) is True
    # SELF-HEALING: past the TTL the name can re-arm (handles a transient halt).
    assert _entry_reject_cooldown_active("RKLZ", _T0 + timedelta(seconds=901)) is False


def test_fail_open_for_unrecorded_symbol(monkeypatch):
    # No rejection recorded -> never blocked (the cooldown is an optimization, not a gate).
    monkeypatch.setattr(settings, "chili_momentum_entry_reject_cooldown_sec", 900.0)
    _ENTRY_REJECT_COOLDOWN.clear()
    assert _entry_reject_cooldown_active("AAPL", _T0) is False


def test_zero_seconds_disables_the_cooldown(monkeypatch):
    # Instant kill-switch: 0 => never cool anything down.
    _ENTRY_REJECT_COOLDOWN.clear()
    _write_entry_reject_cooldown("RKLZ", _T0)
    monkeypatch.setattr(settings, "chili_momentum_entry_reject_cooldown_sec", 0.0)
    assert _entry_reject_cooldown_active("RKLZ", _T0 + timedelta(seconds=1)) is False


def test_write_ignores_empty_symbol():
    _ENTRY_REJECT_COOLDOWN.clear()
    _write_entry_reject_cooldown("", _T0)
    assert "" not in _ENTRY_REJECT_COOLDOWN


def test_reap_and_entry_reject_cooldowns_are_independent(monkeypatch):
    # The two cooldowns are separate stores with separate TTLs — a reap must not
    # imply a broker rejection, nor vice-versa.
    monkeypatch.setattr(settings, "chili_momentum_entry_reject_cooldown_sec", 900.0)
    _ENTRY_REJECT_COOLDOWN.clear()
    _write_entry_reject_cooldown("CORD", _T0)
    assert _entry_reject_cooldown_active("CORD", _T0 + timedelta(seconds=120)) is True
    assert _entry_reject_cooldown_active("OTHER", _T0 + timedelta(seconds=120)) is False
