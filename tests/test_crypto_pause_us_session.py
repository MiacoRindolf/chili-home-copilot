"""Crypto stands down during the US equity session (operator 2026-06-12).

SpaceX morning: crypto arms were consuming live slots during the premarket
equity tape. The gate is time-aware — crypto resumes automatically at the
16:00 ET close, no manual flag to flip back.
"""

from datetime import datetime, timezone

from app.services.trading.momentum_neural import auto_arm as aa
from app.services.trading.momentum_neural.market_profile import market_session_now


def _et(hour, minute=0, weekday_date="2026-06-12"):
    # 2026-06-12 is a Friday; ET = UTC-4 in June.
    return datetime.fromisoformat(f"{weekday_date}T{hour:02d}:{minute:02d}:00+00:00")


def test_equity_session_classifier_anchors():
    # 08:00 ET (12:00 UTC) on a weekday = premarket; 20:30 ET = closed/afterhours.
    assert market_session_now("SPY", now=_et(12, 0)) == "premarket"
    assert market_session_now("SPY", now=_et(15, 0)) == "regular"   # 11:00 ET
    assert market_session_now("SPY", now=_et(21, 0)) in ("afterhours", "closed")  # 17:00 ET


def test_crypto_paused_during_premarket_and_rth(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "chili_momentum_crypto_pause_during_us_session", True)
    import app.services.trading.momentum_neural.market_profile as mp

    monkeypatch.setattr(mp, "market_session_now", lambda s, now=None: "premarket")
    assert aa._crypto_paused_us_session() is True
    monkeypatch.setattr(mp, "market_session_now", lambda s, now=None: "regular")
    assert aa._crypto_paused_us_session() is True


def test_crypto_resumes_after_close(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "chili_momentum_crypto_pause_during_us_session", True)
    import app.services.trading.momentum_neural.market_profile as mp

    for sess in ("afterhours", "closed"):
        monkeypatch.setattr(mp, "market_session_now", lambda s, now=None, _v=sess: _v)
        assert aa._crypto_paused_us_session() is False


def test_gate_disabled_by_setting(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "chili_momentum_crypto_pause_during_us_session", False)
    assert aa._crypto_paused_us_session() is False
