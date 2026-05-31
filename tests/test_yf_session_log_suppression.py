import logging

from app.services import yf_session


def test_quiet_yfinance_provider_logs_restores_levels(monkeypatch):
    monkeypatch.delenv("CHILI_YFINANCE_SUPPRESS_PROVIDER_ERRORS", raising=False)
    yflog = logging.getLogger("yfinance")
    old = yflog.level
    try:
        yflog.setLevel(logging.ERROR)
        with yf_session._quiet_yfinance_provider_logs():
            assert yflog.level == logging.CRITICAL
        assert yflog.level == logging.ERROR
    finally:
        yflog.setLevel(old)


def test_quiet_yfinance_provider_logs_can_be_disabled(monkeypatch):
    monkeypatch.setenv("CHILI_YFINANCE_SUPPRESS_PROVIDER_ERRORS", "0")
    yflog = logging.getLogger("yfinance")
    old = yflog.level
    try:
        yflog.setLevel(logging.ERROR)
        with yf_session._quiet_yfinance_provider_logs():
            assert yflog.level == logging.ERROR
    finally:
        yflog.setLevel(old)
