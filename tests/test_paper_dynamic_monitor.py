from app.models.trading import PaperTrade
from app.services.trading.paper_trading import _paper_dynamic_monitor_decision


class _FakeDb:
    def get(self, model, ident):
        return None


def test_autotrader_paper_dynamic_monitor_exits_near_stop(monkeypatch):
    monkeypatch.setattr(
        "app.services.trading.paper_trading.settings",
        type(
            "S",
            (),
            {
                "chili_autotrader_paper_dynamic_monitor_enabled": True,
                "chili_autotrader_paper_dynamic_monitor_cooldown_minutes": 5,
            },
        )(),
    )
    pt = PaperTrade(
        id=123,
        user_id=1,
        ticker="TEST",
        direction="long",
        entry_price=10.0,
        stop_price=9.5,
        target_price=12.0,
        quantity=1.0,
        status="open",
        signal_json={"auto_trader_v1": True, "_paper_meta": {}},
    )

    decision = _paper_dynamic_monitor_decision(
        _FakeDb(),
        pt,
        price=9.6,
        quote_source="test",
    )

    assert decision is not None
    assert decision["action"] == "exit_now"
    assert decision["decision_source"] == "plan_levels"
    dyn = pt.signal_json["_paper_meta"]["dynamic_monitor"]
    assert dyn["last_action"] == "exit_now"
    assert dyn["history"][-1]["reason"] == "plan_levels_near_stop"


def test_non_autotrader_paper_trade_skips_dynamic_monitor(monkeypatch):
    monkeypatch.setattr(
        "app.services.trading.paper_trading.settings",
        type("S", (), {"chili_autotrader_paper_dynamic_monitor_enabled": True})(),
    )
    pt = PaperTrade(
        id=124,
        user_id=1,
        ticker="TEST",
        direction="long",
        entry_price=10.0,
        stop_price=9.5,
        target_price=12.0,
        quantity=1.0,
        status="open",
        signal_json={"_paper_meta": {}},
    )

    decision = _paper_dynamic_monitor_decision(
        _FakeDb(),
        pt,
        price=9.6,
        quote_source="test",
    )

    assert decision is None
    assert pt.signal_json == {"_paper_meta": {}}
