"""Extended-hours session model for the momentum equity lane.

Ross trades the pre-market gap-and-go (he streams 7:00am ET), so the lane must be
tradeable across premarket → regular → afterhours, not just RTH. These tests pin the
session boundaries + the config-driven tunables + the crypto/weekend behaviour.
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import app.services.trading.momentum_neural.market_profile as mp

_ET = ZoneInfo("America/New_York")


def _at(y, mo, d, hh, mm):
    """A tz-aware UTC instant for the given ET wall-clock time."""
    return datetime(y, mo, d, hh, mm, tzinfo=_ET).astimezone(timezone.utc)


# A known weekday (Tue 2026-06-09) and weekend (Sat 2026-06-13).
def test_regular_session_is_regular_and_open():
    t = _at(2026, 6, 9, 10, 0)  # 10:00 ET
    assert mp.market_session_now("PAVS", now=t) == "regular"
    assert mp.market_open_now("PAVS", now=t) is True
    assert mp.is_tradeable_now("PAVS", now=t) is True


def test_premarket_is_tradeable_but_not_regular_open():
    t = _at(2026, 6, 9, 7, 30)  # 7:30 ET — Ross is live, pre-market
    assert mp.market_session_now("PAVS", now=t) == "premarket"
    assert mp.market_open_now("PAVS", now=t) is False   # regular session label stays honest
    assert mp.is_tradeable_now("PAVS", now=t) is True   # but the lane CAN trade it


def test_before_premarket_start_is_closed():
    t = _at(2026, 6, 9, 6, 0)  # 6:00 ET — before the 7:00 default pre-market start
    assert mp.market_session_now("PAVS", now=t) == "closed"
    assert mp.is_tradeable_now("PAVS", now=t) is False


def test_afterhours_is_tradeable():
    t = _at(2026, 6, 9, 17, 0)  # 5:00pm ET
    assert mp.market_session_now("PAVS", now=t) == "afterhours"
    assert mp.market_open_now("PAVS", now=t) is False
    assert mp.is_tradeable_now("PAVS", now=t) is True


def test_after_afterhours_end_is_closed():
    t = _at(2026, 6, 9, 21, 0)  # 9:00pm ET — past the 20:00 default close
    assert mp.market_session_now("PAVS", now=t) == "closed"
    assert mp.is_tradeable_now("PAVS", now=t) is False


def test_weekend_is_closed_for_equities():
    t = _at(2026, 6, 13, 10, 0)  # Saturday 10:00 ET
    assert mp.market_session_now("PAVS", now=t) == "closed"
    assert mp.is_tradeable_now("PAVS", now=t) is False


def test_crypto_is_always_tradeable():
    for t in (_at(2026, 6, 13, 3, 0), _at(2026, 6, 9, 23, 0)):  # weekend + late night
        assert mp.market_session_now("BTC-USD", now=t) == "regular"
        assert mp.is_tradeable_now("BTC-USD", now=t) is True


def test_premarket_start_config_collapses_premarket(monkeypatch):
    """Set pre-market start to 09:30 → pre-market window disappears (the window IS the
    control; no separate on/off flag)."""
    monkeypatch.setattr(mp.settings, "chili_momentum_premarket_start_et", "09:30", raising=False)
    t = _at(2026, 6, 9, 7, 30)  # 7:30 ET would be pre-market by default
    assert mp.market_session_now("PAVS", now=t) == "closed"
    assert mp.is_tradeable_now("PAVS", now=t) is False


def test_malformed_config_falls_back_safe(monkeypatch):
    monkeypatch.setattr(mp.settings, "chili_momentum_premarket_start_et", "not-a-time", raising=False)
    t = _at(2026, 6, 9, 7, 30)  # falls back to the 07:00 default → still pre-market
    assert mp.market_session_now("PAVS", now=t) == "premarket"
