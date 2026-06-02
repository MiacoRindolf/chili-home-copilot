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


def _dash_with_trading(open_positions, closes_fmt):
    return {"trading": {"open_positions": open_positions, "closes_fmt": closes_fmt}}


def test_lists_extracts_positions_and_closes():
    dash = _dash_with_trading(
        open_positions=[{"ticker": "AAPL", "side": "long"}, {"ticker": "BTC", "side": ""}],
        closes_fmt=[{"ticker": "TSLA", "pattern": "breakout", "pnl_fmt": "+$10.00", "pnl_up": True},
                    {"ticker": "NVDA", "pattern": None, "pnl_fmt": "-$5.00", "pnl_up": False}],
    )
    with patch("app.services.dashboard_summary.build_dashboard", return_value=dash):
        out = dl._lists(object(), 1)
    assert out["positions"] == [{"ticker": "AAPL", "side": "long"}, {"ticker": "BTC", "side": ""}]
    assert out["closes"][0] == {"ticker": "TSLA", "pattern": "breakout", "pnl_fmt": "+$10.00", "pnl_up": True}
    # missing pattern degrades to em dash; pnl_up coerced to bool
    assert out["closes"][1]["pattern"] == "—" and out["closes"][1]["pnl_up"] is False


def test_lists_caps_positions_at_six_and_closes_at_five():
    dash = _dash_with_trading(
        open_positions=[{"ticker": "T%d" % i, "side": "long"} for i in range(10)],
        closes_fmt=[{"ticker": "C%d" % i, "pattern": "p", "pnl_fmt": "+$1.00", "pnl_up": True} for i in range(10)],
    )
    with patch("app.services.dashboard_summary.build_dashboard", return_value=dash):
        out = dl._lists(object(), 1)
    assert len(out["positions"]) == 6 and len(out["closes"]) == 5


def test_lists_defensive_when_dashboard_fails():
    with patch("app.services.dashboard_summary.build_dashboard", side_effect=RuntimeError("boom")):
        out = dl._lists(object(), 1)
    assert out == {"positions": [], "closes": []}


def test_lists_skips_non_dict_rows():
    dash = _dash_with_trading(open_positions=[{"ticker": "AAPL", "side": "long"}, "junk", None],
                              closes_fmt=["bad", {"ticker": "TSLA", "pnl_fmt": "+$1", "pnl_up": True}])
    with patch("app.services.dashboard_summary.build_dashboard", return_value=dash):
        out = dl._lists(object(), 1)
    assert out["positions"] == [{"ticker": "AAPL", "side": "long"}]
    assert len(out["closes"]) == 1 and out["closes"][0]["ticker"] == "TSLA"


class _FakeQuery:
    def __init__(self, value):
        self._value = value

    def filter(self, *a, **k):
        return self

    def scalar(self):
        return self._value


class _FakeDB:
    def __init__(self, value):
        self._value = value

    def query(self, *a, **k):
        return _FakeQuery(self._value)


def test_last_activity_returns_iso_for_naive_utc():
    from datetime import datetime
    db = _FakeDB(datetime(2026, 6, 2, 13, 30, 0))
    out = dl._last_activity(db, 1)
    # naive is treated as UTC and emitted with a trailing Z
    assert out == "2026-06-02T13:30:00Z"


def test_last_activity_normalizes_aware_to_utc_z():
    from datetime import datetime, timezone, timedelta
    # +02:00 → 11:30Z
    db = _FakeDB(datetime(2026, 6, 2, 13, 30, 0, tzinfo=timezone(timedelta(hours=2))))
    out = dl._last_activity(db, 1)
    assert out == "2026-06-02T11:30:00Z"


def test_last_activity_none_for_guest():
    # guest never touches the DB
    assert dl._last_activity(_FakeDB(object()), None) is None


def test_last_activity_none_when_no_trades():
    assert dl._last_activity(_FakeDB(None), 1) is None


def test_last_activity_none_on_failure():
    class _BoomDB:
        def query(self, *a, **k):
            raise RuntimeError("boom")

    assert dl._last_activity(_BoomDB(), 1) is None


def test_build_live_includes_last_trade_iso():
    with patch.object(dl, "_numbers", return_value={
            "net_pnl_fmt": "$0.00", "net_pnl_up": True, "win_rate_fmt": "—",
            "open_positions": 0, "closes_today": 0, "top_patterns": 0}), \
         patch.object(dl, "_lists", return_value={"positions": [], "closes": []}), \
         patch.object(dl, "_kill_switch", return_value={"ok": True, "active": False, "reason": None}), \
         patch.object(dl, "_breaker", return_value={"ok": True, "tripped": False, "reason": None}), \
         patch.object(dl, "_market", return_value={"ok": True, "equities_open": True, "crypto_open": True}), \
         patch.object(dl, "_last_activity", return_value="2026-06-02T13:30:00Z"):
        out = dl.build_live(object(), 1)
    assert out["last_trade_iso"] == "2026-06-02T13:30:00Z"


def test_build_live_includes_lists():
    with patch.object(dl, "_numbers", return_value={
            "net_pnl_fmt": "$0.00", "net_pnl_up": True, "win_rate_fmt": "—",
            "open_positions": 0, "closes_today": 0, "top_patterns": 0}), \
         patch.object(dl, "_lists", return_value={"positions": [{"ticker": "AAPL", "side": "long"}], "closes": []}), \
         patch.object(dl, "_kill_switch", return_value={"ok": True, "active": False, "reason": None}), \
         patch.object(dl, "_breaker", return_value={"ok": True, "tripped": False, "reason": None}), \
         patch.object(dl, "_market", return_value={"ok": True, "equities_open": True, "crypto_open": True}):
        out = dl.build_live(object(), 1)
    assert out["positions"] == [{"ticker": "AAPL", "side": "long"}]
    assert out["closes"] == []
