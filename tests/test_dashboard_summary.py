"""Unit tests for the workspace dashboard builder (DB-free; query helpers patched)."""
from unittest.mock import patch

from app.services import dashboard_summary as ds


_TRADING = {
    "net_pnl": 340.12, "win_rate": 0.35,
    "closes": [{"ticker": "ACHC", "pnl": 86.0, "pattern": "537 Reclaim", "reason": "target"},
               {"ticker": "EKSO", "pnl": -5.4, "pattern": "585 Wedge", "reason": "stop"}],
    "open_positions": [{"ticker": "NVDA", "side": "long"}],
    "top_patterns": [{"id": "537 Reclaim", "pnl": 86.0, "trades": 1, "payoff": 29.6}],
}


def test_no_user_yields_empty_but_valid():
    d = ds.build_dashboard(object(), None)
    assert d["kpis"][0]["val"] == "$0.00"
    assert d["trading"]["closes_fmt"] == []
    assert d["research"] == []
    assert d["has_any"] is False


def test_builds_from_trading_summary():
    with patch("app.services.trading_summary.build_trading_summary", return_value=_TRADING), \
         patch.object(ds, "_research", return_value=[{"topic": "NVDA", "summary": "x", "source": "reuters.com"}]):
        d = ds.build_dashboard(object(), 1)
    # KPIs
    assert d["kpis"][0]["val"] == "+$340.12" and d["kpis"][0]["cls"] == "ws-up"
    assert d["kpis"][1]["val"] == "35%"
    assert d["kpis"][2]["val"] == "1"   # open positions
    # closes formatted with sign + up/down
    closes = d["trading"]["closes_fmt"]
    assert closes[0]["pnl_fmt"] == "+$86.00" and closes[0]["pnl_up"] is True
    assert closes[1]["pnl_fmt"] == "-$5.40" and closes[1]["pnl_up"] is False
    # top patterns carry payoff ratio
    assert d["trading"]["top_patterns"][0]["payoff"] == "29.60:1"
    assert d["research"][0]["topic"] == "NVDA"
    assert d["has_any"] is True


def test_negative_net_pnl_marks_down():
    with patch("app.services.trading_summary.build_trading_summary",
               return_value={"net_pnl": -50.0, "closes": [], "open_positions": [], "top_patterns": []}), \
         patch.object(ds, "_research", return_value=[]):
        d = ds.build_dashboard(object(), 1)
    assert d["kpis"][0]["val"] == "-$50.00" and d["kpis"][0]["cls"] == "ws-down"


def test_trading_failure_degrades_gracefully():
    with patch("app.services.trading_summary.build_trading_summary", side_effect=Exception("boom")), \
         patch.object(ds, "_research", return_value=[]):
        d = ds.build_dashboard(object(), 1)
    assert d["trading"]["closes_fmt"] == []   # no crash
    assert d["has_any"] is False
