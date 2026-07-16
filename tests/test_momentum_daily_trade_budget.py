from __future__ import annotations

from app.config import settings
from app.services.trading.momentum_neural import risk_policy


def _ross_tick_context(**overrides):
    ctx = {
        "mode": "live",
        "asset_class": "stock",
        "entry_trigger_reason": "tick_first_pullback_scalp",
        "micro_frame_used": "tick",
        "setup_coverage": "structural_a_setup",
        "ross_universe_ok": True,
        "ross_entry_shape_ok": True,
        "stale_pre_submit": False,
    }
    ctx.update(overrides)
    return ctx


def test_daily_trade_budget_allows_proven_ross_tick_overflow(monkeypatch) -> None:
    monkeypatch.setattr(settings, "chili_momentum_daily_trade_count_budget_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_daily_trade_count_base", 5)
    monkeypatch.setattr(settings, "chili_momentum_daily_trade_count_max_multiple", 2.0)
    monkeypatch.setattr(risk_policy, "_count_real_entries_today", lambda *a, **k: 5)
    monkeypatch.setattr(risk_policy, "_recent_realized_r", lambda *a, **k: [-1, -1, 1, 1, -1])

    allowed, meta = risk_policy.daily_trade_count_budget_decision(
        object(),
        execution_family="robinhood_agentic_mcp",
        open_entry_count=0,
        entry_context=_ross_tick_context(),
    )

    assert allowed is True
    assert meta["reason"] == "ross_a_plus_budget_overflow_allowed"
    assert meta["ceiling"] == 5
    assert meta["effective_ceiling"] == 10
    assert meta["ross_a_plus_overflow_allowed"] is True
    assert meta["ross_a_plus_overflow_reason"] == "ross_a_plus_budget_overflow_allowed"


def test_daily_trade_budget_still_blocks_generic_after_cap(monkeypatch) -> None:
    monkeypatch.setattr(settings, "chili_momentum_daily_trade_count_budget_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_daily_trade_count_base", 5)
    monkeypatch.setattr(settings, "chili_momentum_daily_trade_count_max_multiple", 2.0)
    monkeypatch.setattr(risk_policy, "_count_real_entries_today", lambda *a, **k: 5)
    monkeypatch.setattr(risk_policy, "_recent_realized_r", lambda *a, **k: [-1, -1, 1, 1, -1])

    allowed, meta = risk_policy.daily_trade_count_budget_decision(
        object(),
        execution_family="robinhood_agentic_mcp",
        open_entry_count=0,
        entry_context=_ross_tick_context(
            micro_frame_used="5m",
            ross_universe_ok=False,
        ),
    )

    assert allowed is False
    assert meta["reason"] == "daily_trade_count_budget_reached"
    assert meta["effective_ceiling"] == 5
    assert meta["ross_a_plus_overflow_allowed"] is False
    assert meta["ross_a_plus_overflow_reason"] == "ross_universe_not_proven"


def test_daily_trade_budget_blocks_stale_ross_overflow(monkeypatch) -> None:
    monkeypatch.setattr(settings, "chili_momentum_daily_trade_count_budget_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_daily_trade_count_base", 5)
    monkeypatch.setattr(settings, "chili_momentum_daily_trade_count_max_multiple", 2.0)
    monkeypatch.setattr(risk_policy, "_count_real_entries_today", lambda *a, **k: 5)
    monkeypatch.setattr(risk_policy, "_recent_realized_r", lambda *a, **k: [-1, -1, 1, 1, -1])

    allowed, meta = risk_policy.daily_trade_count_budget_decision(
        object(),
        execution_family="robinhood_agentic_mcp",
        open_entry_count=0,
        entry_context=_ross_tick_context(stale_pre_submit=True),
    )

    assert allowed is False
    assert meta["reason"] == "daily_trade_count_budget_reached"
    assert meta["ross_a_plus_overflow_allowed"] is False
    assert meta["ross_a_plus_overflow_reason"] == "stale_pre_submit"


def test_daily_trade_budget_blocks_even_ross_tick_after_overflow_ceiling(monkeypatch) -> None:
    monkeypatch.setattr(settings, "chili_momentum_daily_trade_count_budget_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_daily_trade_count_base", 5)
    monkeypatch.setattr(settings, "chili_momentum_daily_trade_count_max_multiple", 2.0)
    monkeypatch.setattr(risk_policy, "_count_real_entries_today", lambda *a, **k: 10)
    monkeypatch.setattr(risk_policy, "_recent_realized_r", lambda *a, **k: [-1, -1, 1, 1, -1])

    allowed, meta = risk_policy.daily_trade_count_budget_decision(
        object(),
        execution_family="robinhood_agentic_mcp",
        open_entry_count=1,
        entry_context=_ross_tick_context(),
    )

    assert allowed is False
    assert meta["reason"] == "daily_trade_count_budget_reached"
    assert meta["entered_today"] == 10
    assert meta["used"] == 11
    assert meta["overflow_ceiling"] == 10
    assert meta["ross_a_plus_overflow_allowed"] is False
    assert meta["ross_a_plus_overflow_reason"] == "overflow_ceiling_reached"
