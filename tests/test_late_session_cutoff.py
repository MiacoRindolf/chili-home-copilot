"""Ross gap #13: late-session new-entry cutoff for the EQUITY lane (videos 13/23/26/39).
Ross stops taking NEW setups after late morning (his afternoon metrics are negative). The
cutoff blocks NEW equity arming past the cutoff ET time; crypto (24/7) is exempt; open
positions are unaffected. Pure-logic tests on the extracted decision helper.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.services.trading.momentum_neural.auto_arm import (
    _LATE_SESSION_CUTOFF_ET,
    _late_session_cutoff_active,
)

_ET = ZoneInfo("America/New_York")


def _et(h, m):
    return datetime(2026, 6, 15, h, m, tzinfo=_ET)


def test_crypto_lane_never_cut_off():
    # crypto is 24/7 -> the guard is a no-op regardless of the clock
    assert _late_session_cutoff_active(_et(15, 0), crypto_only=True) is False
    assert _late_session_cutoff_active(_et(23, 30), crypto_only=True) is False


def test_equity_blocked_after_cutoff():
    assert _late_session_cutoff_active(_et(11, 45), crypto_only=False) is True
    assert _late_session_cutoff_active(_et(14, 0), crypto_only=False) is True


def test_equity_allowed_before_cutoff():
    assert _late_session_cutoff_active(_et(9, 45), crypto_only=False) is False   # RTH morning
    assert _late_session_cutoff_active(_et(7, 0), crypto_only=False) is False    # premarket survives


def test_exactly_at_cutoff_is_blocked():
    h, m = _LATE_SESSION_CUTOFF_ET
    assert _late_session_cutoff_active(_et(h, m), crypto_only=False) is True
    # one minute before is allowed
    assert _late_session_cutoff_active(_et(h, m - 1), crypto_only=False) is False
