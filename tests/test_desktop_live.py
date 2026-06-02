"""Unit tests for the live desktop-cockpit view-model (DB-free; accessors patched).

Focus: the defensive wrapping — a failing safety/market read degrades that
section to ok=False with neutral defaults rather than raising.
"""
from unittest.mock import patch

from app.services import desktop_live as dl


def test_build_live_composes_sections():
    with patch.object(dl, "_numbers", return_value={
            "net_pnl_fmt": "+$1.00", "net_pnl_up": True, "win_rate_fmt": "50%",
            "open_positions": 2, "closes_today": 3, "top_patterns": 4}), \
         patch.object(dl, "_kill_switch", return_value={"ok": True, "active": False, "reason": None}), \
         patch.object(dl, "_breaker", return_value={"ok": True, "tripped": False, "reason": None}), \
         patch.object(dl, "_market", return_value={"ok": True, "equities_open": True, "crypto_open": True}):
        out = dl.build_live(object(), 1)
    assert out["ok"] is True
    assert out["open_positions"] == 2 and out["net_pnl_fmt"] == "+$1.00"
    assert out["kill_switch"]["active"] is False
    assert out["market"]["equities_open"] is True


def test_kill_switch_healthy_and_defensive():
    import app.services.trading.governance as gov
    with patch.object(gov, "get_kill_switch_status", return_value={"active": True, "reason": "halt"}):
        assert dl._kill_switch() == {"ok": True, "active": True, "reason": "halt"}
    with patch.object(gov, "get_kill_switch_status", side_effect=RuntimeError("boom")):
        assert dl._kill_switch() == {"ok": False, "active": False, "reason": None}


def test_breaker_healthy_and_defensive():
    import app.services.trading.portfolio_risk as pr
    with patch.object(pr, "get_breaker_status", return_value={"tripped": True, "reason": "5d dd"}):
        assert dl._breaker() == {"ok": True, "tripped": True, "reason": "5d dd"}
    with patch.object(pr, "get_breaker_status", side_effect=RuntimeError("boom")):
        assert dl._breaker() == {"ok": False, "tripped": False, "reason": None}


def test_market_healthy_and_defensive():
    import app.services.trading.momentum_neural.market_profile as mp
    with patch.object(mp, "market_open_now", return_value=False):
        out = dl._market()
        assert out["ok"] is True and out["equities_open"] is False and out["crypto_open"] is True
    with patch.object(mp, "market_open_now", side_effect=RuntimeError("boom")):
        out = dl._market()
        assert out["ok"] is False and out["equities_open"] is None


def test_numbers_defensive_when_dashboard_fails():
    with patch("app.services.dashboard_summary.build_dashboard", side_effect=RuntimeError("boom")):
        out = dl._numbers(object(), 1)
    assert out["open_positions"] == 0 and out["net_pnl_fmt"] == "$0.00"
