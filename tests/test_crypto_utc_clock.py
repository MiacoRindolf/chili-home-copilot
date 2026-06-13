"""Crypto entry-window clock (2026-06-13 crypto-live plan, A5).

The clock analysis found 0/21 earned in 21:00–05:00 UTC; bursts concentrate in
05:00–10:00 and 12:00–21:00 UTC. These pin the window boundaries.
"""

from datetime import datetime, timezone

from app.services.trading.momentum_neural.market_profile import crypto_session_active_now


def _utc(h, m=0):
    return datetime(2026, 6, 13, h, m, tzinfo=timezone.utc)


def test_morning_window_active():
    assert crypto_session_active_now(_utc(5, 0)) is True
    assert crypto_session_active_now(_utc(7, 30)) is True
    assert crypto_session_active_now(_utc(9, 59)) is True


def test_us_overlap_window_active():
    assert crypto_session_active_now(_utc(12, 0)) is True
    assert crypto_session_active_now(_utc(16, 0)) is True
    assert crypto_session_active_now(_utc(20, 59)) is True


def test_dead_band_quiet():
    assert crypto_session_active_now(_utc(21, 0)) is False   # the 0/21 band opens
    assert crypto_session_active_now(_utc(0, 0)) is False
    assert crypto_session_active_now(_utc(4, 59)) is False


def test_midday_gap_quiet():
    # 10:00–12:00 UTC is between the two active windows.
    assert crypto_session_active_now(_utc(10, 30)) is False
    assert crypto_session_active_now(_utc(11, 59)) is False


def test_naive_datetime_treated_as_utc():
    naive = datetime(2026, 6, 13, 7, 0)  # no tzinfo
    assert crypto_session_active_now(naive) is True
