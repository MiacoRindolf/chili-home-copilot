from app.models.trading import PaperTrade
import pytest

from app.services.trading.paper_trading import (
    _paper_dynamic_monitor_cooldown_minutes,
    _paper_dynamic_monitor_decision,
    _paper_dynamic_near_stop_exit,
    _paper_loosened_target_level,
    _paper_mark_pnl_pct,
    _paper_tightened_stop_level,
)


class _FakeDb:
    def get(self, model, ident):
        return None


@pytest.mark.parametrize("bad_minutes", [True, float("nan"), float("inf"), "bad"])
def test_paper_dynamic_monitor_cooldown_defaults_bad_setting(
    bad_minutes,
    monkeypatch,
):
    monkeypatch.setattr(
        "app.services.trading.paper_trading.settings",
        type(
            "S",
            (),
            {
                "chili_autotrader_paper_dynamic_monitor_cooldown_minutes": bad_minutes,
            },
        )(),
    )

    assert _paper_dynamic_monitor_cooldown_minutes() == 5


@pytest.mark.parametrize(
    ("raw_minutes", "expected"),
    [(-1, 0), (0, 0), (2.9, 2), ("3", 3)],
)
def test_paper_dynamic_monitor_cooldown_clamps_nonnegative_int(
    raw_minutes,
    expected,
    monkeypatch,
):
    monkeypatch.setattr(
        "app.services.trading.paper_trading.settings",
        type(
            "S",
            (),
            {
                "chili_autotrader_paper_dynamic_monitor_cooldown_minutes": raw_minutes,
            },
        )(),
    )

    assert _paper_dynamic_monitor_cooldown_minutes() == expected


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


@pytest.mark.parametrize("bad_stop", [True, float("nan"), float("inf"), "bad", 0, -1])
def test_paper_dynamic_near_stop_rejects_bad_stop(bad_stop):
    pt = PaperTrade(
        id=125,
        ticker="TEST",
        direction="long",
        stop_price=bad_stop,
        signal_json={"auto_trader_v1": True},
    )

    assert _paper_dynamic_near_stop_exit(pt, price=9.6) is None


@pytest.mark.parametrize("bad_price", [True, float("nan"), float("inf"), "bad", 0, -1])
def test_paper_dynamic_near_stop_rejects_bad_price(bad_price):
    pt = PaperTrade(
        id=126,
        ticker="TEST",
        direction="long",
        stop_price=9.5,
        signal_json={"auto_trader_v1": True},
    )

    assert _paper_dynamic_near_stop_exit(pt, price=bad_price) is None


def test_paper_mark_pnl_pct_is_direction_aware_for_shorts():
    long_pt = PaperTrade(
        ticker="TEST",
        direction="long",
        entry_price=10.0,
        signal_json={"auto_trader_v1": True},
    )
    short_pt = PaperTrade(
        ticker="TEST",
        direction="short",
        entry_price=10.0,
        signal_json={"auto_trader_v1": True},
    )

    assert _paper_mark_pnl_pct(long_pt, 9.0) == pytest.approx(-10.0)
    assert _paper_mark_pnl_pct(short_pt, 9.0) == pytest.approx(10.0)


@pytest.mark.parametrize("bad_entry", [True, float("nan"), float("inf"), "bad", 0, -1])
def test_paper_mark_pnl_pct_rejects_bad_entry(bad_entry):
    pt = PaperTrade(
        ticker="TEST",
        direction="long",
        entry_price=bad_entry,
        signal_json={"auto_trader_v1": True},
    )

    assert _paper_mark_pnl_pct(pt, 9.0) is None


@pytest.mark.parametrize("bad_price", [True, float("nan"), float("inf"), "bad", 0, -1])
def test_paper_mark_pnl_pct_rejects_bad_price(bad_price):
    pt = PaperTrade(
        ticker="TEST",
        direction="long",
        entry_price=10.0,
        signal_json={"auto_trader_v1": True},
    )

    assert _paper_mark_pnl_pct(pt, bad_price) is None


@pytest.mark.parametrize("bad_level", [True, float("nan"), float("inf"), "bad", 0, -1])
def test_paper_tightened_stop_level_rejects_bad_new_stop(bad_level):
    assert (
        _paper_tightened_stop_level(
            current_stop=None,
            new_stop=bad_level,
            price=10.0,
            direction="long",
        )
        is None
    )


def test_paper_tightened_stop_level_respects_long_and_short_geometry():
    assert _paper_tightened_stop_level(
        current_stop=9.0,
        new_stop=9.5,
        price=10.0,
        direction="long",
    ) == pytest.approx(9.5)
    assert _paper_tightened_stop_level(
        current_stop=11.0,
        new_stop=10.5,
        price=10.0,
        direction="short",
    ) == pytest.approx(10.5)
    assert _paper_tightened_stop_level(
        current_stop=None,
        new_stop=10.5,
        price=10.0,
        direction="long",
    ) is None
    assert _paper_tightened_stop_level(
        current_stop=None,
        new_stop=9.5,
        price=10.0,
        direction="short",
    ) is None


@pytest.mark.parametrize("bad_level", [True, float("nan"), float("inf"), "bad", 0, -1])
def test_paper_loosened_target_level_rejects_bad_new_target(bad_level):
    assert (
        _paper_loosened_target_level(
            current_target=None,
            new_target=bad_level,
            price=10.0,
            direction="long",
        )
        is None
    )


def test_paper_loosened_target_level_respects_long_and_short_geometry():
    assert _paper_loosened_target_level(
        current_target=11.0,
        new_target=12.0,
        price=10.0,
        direction="long",
    ) == pytest.approx(12.0)
    assert _paper_loosened_target_level(
        current_target=9.0,
        new_target=8.0,
        price=10.0,
        direction="short",
    ) == pytest.approx(8.0)
    assert _paper_loosened_target_level(
        current_target=None,
        new_target=9.5,
        price=10.0,
        direction="long",
    ) is None
    assert _paper_loosened_target_level(
        current_target=None,
        new_target=10.5,
        price=10.0,
        direction="short",
    ) is None


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
