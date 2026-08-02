"""L8b — pending-refire cooldown (pure clock helper, walang I/O).

Churn bound para sa zero-band demote loop: pagkatapos ng late_window demote,
nilalaktawan ang trigger ladder nang ~20s (ang sched band ay minuto-scale
magbago). Fail-OPEN ang lahat ng sira/missing — hindi kailanman naka-strand
ang pangalan sa cooldown dahil sa bug.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.services.trading.momentum_neural.entry_gates import refire_cooldown_active

_NOW = datetime(2026, 6, 30, 21, 30, tzinfo=timezone.utc)


def test_active_habang_hindi_pa_lampas():
    until = (_NOW + timedelta(seconds=15)).isoformat()
    assert refire_cooldown_active(now=_NOW, until_iso=until, enabled=True) is True


def test_expired_pagkalampas():
    until = (_NOW - timedelta(seconds=1)).isoformat()
    assert refire_cooldown_active(now=_NOW, until_iso=until, enabled=True) is False


def test_flag_off_ay_fail_open():
    until = (_NOW + timedelta(seconds=60)).isoformat()
    assert refire_cooldown_active(now=_NOW, until_iso=until, enabled=False) is False


def test_missing_o_sirang_marker_ay_fail_open():
    for bad in (None, "", "hindi-iso", 12345, {"x": 1}):
        assert refire_cooldown_active(now=_NOW, until_iso=bad, enabled=True) is False, bad


def test_naive_iso_ay_itinuturing_na_utc():
    until = (_NOW + timedelta(seconds=15)).replace(tzinfo=None).isoformat()
    assert refire_cooldown_active(now=_NOW, until_iso=until, enabled=True) is True
