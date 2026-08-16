"""L14 — post-bailout maker re-entry (2026-08-16, L2a churn autopsy).

Ang ebidensya sa likod ng lever (canon v4, fill-derived):
  * churn class ~ -450 ng -492 na natirang net: TVRD 23 churn cycles -156.61
    (20/29 exits = bailout), VTAK 10 = -158.04 (15/18 bailout), CWD 6 = -123.36;
  * TUNAY na cadence sa SIM time: TVRD median re-entry gap 3s / hold 1s;
  * time-spacing = winner-killer (101-cycle sukat: panalo median gap 4s,
    71% <20s vs talo 55%) — kaya HALAGA per attempt ang binabawasan (post sa
    bid) at hindi ang bilang ng attempts.
"""
from __future__ import annotations

from datetime import datetime

from app.services.trading.momentum_neural.risk_policy import (
    bailout_maker_reentry_decision,
)

NOW = datetime(2026, 7, 7, 13, 45, 30)


def _decide(**kw):
    base = dict(
        enabled=True,
        last_exit_reason="bailout",
        last_exit_return_bps=-9.0,
        last_exit_at_utc=datetime(2026, 7, 7, 13, 45, 27).isoformat(),
        now_utc=NOW,
        window_seconds=90.0,
    )
    base.update(kw)
    return bailout_maker_reentry_decision(**base)


def test_tvrd_shape_rapid_losing_bailout_goes_maker():
    """Ang sinukat na TVRD churn shape (gap 3s, talo): maker."""
    ok, reason = _decide()
    assert ok is True and reason == "maker_reentry"


def test_profit_bailout_stays_marketable():
    ok, reason = _decide(last_exit_return_bps=4.0)
    assert ok is False and reason == "not_a_loss"


def test_breakeven_bailout_stays_marketable():
    """rb=0 (ang VTAK cycle-8 breakeven): hindi talo, malayang mag-marketable."""
    ok, reason = _decide(last_exit_return_bps=0.0)
    assert ok is False and reason == "not_a_loss"


def test_non_bailout_exit_stays_marketable():
    ok, reason = _decide(last_exit_reason="trail_stop")
    assert ok is False and reason == "not_bailout_exit"


def test_outside_window_stays_marketable():
    ok, reason = _decide(
        last_exit_at_utc=datetime(2026, 7, 7, 13, 43, 0).isoformat()
    )
    assert ok is False and reason == "outside_window"


def test_flag_off_byte_identical():
    ok, reason = _decide(enabled=False)
    assert ok is False and reason == "flag_off"


def test_missing_timestamp_fails_toward_legacy():
    ok, reason = _decide(last_exit_at_utc=None)
    assert ok is False and reason == "no_exit_timestamp"


def test_garbage_timestamp_fails_toward_legacy():
    ok, reason = _decide(last_exit_at_utc="hindi-petsa")
    assert ok is False and reason == "bad_timestamp"


def test_zero_window_is_off():
    ok, reason = _decide(window_seconds=0.0)
    assert ok is False and reason == "bad_timestamp"


def test_negative_age_clock_skew_fails_toward_legacy():
    """Exit timestamp sa HINAHARAP (clock skew): huwag mag-maker."""
    ok, reason = _decide(
        last_exit_at_utc=datetime(2026, 7, 7, 13, 46, 0).isoformat()
    )
    assert ok is False and reason == "bad_timestamp"


def test_tz_aware_timestamp_accepted():
    ok, reason = _decide(last_exit_at_utc="2026-07-07T13:45:27+00:00")
    assert ok is True and reason == "maker_reentry"
