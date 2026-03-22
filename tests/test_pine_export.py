"""Unit tests for Pine Script export (no DB fixture)."""
from __future__ import annotations

from app.services.trading.pine_export import rules_json_to_pine


def test_pine_export_rsi_threshold_strategy_default():
    s, warnings = rules_json_to_pine(
        pattern_id=1,
        name="RSI test",
        description=None,
        timeframe="1d",
        rules_json='{"conditions":[{"indicator":"rsi_14","op":">","value":65}]}',
    )
    assert "ta.rsi(close, 14)" in s
    assert "(v_rsi14 > 65)" in s
    assert "longSignal" in s
    assert "strategy(" in s
    assert "strategy.entry" in s
    assert "longEntry" in s
    assert "longExit" in s
    assert "indicator(" not in s
    assert isinstance(warnings, list)
    assert any("Strategy Tester" in w for w in warnings)


def test_pine_export_price_above_ema_stack():
    s, _w = rules_json_to_pine(
        pattern_id=2,
        name="EMA stack",
        description=None,
        timeframe="1d",
        rules_json="""{"conditions":[
            {"indicator":"price","op":">","ref":"ema_20"},
            {"indicator":"price","op":">","ref":"ema_50"}
        ]}""",
    )
    assert "ta.ema(close, 20)" in s
    assert "ta.ema(close, 50)" in s
    assert "(close > v_ema20)" in s
    assert "(close > v_ema50)" in s
    assert "strategy.entry" in s


def test_pine_export_unmapped_indicator():
    s, warnings = rules_json_to_pine(
        pattern_id=3,
        name="Custom",
        description=None,
        timeframe="1d",
        rules_json='{"conditions":[{"indicator":"bb_squeeze","op":"==","value":true}]}',
    )
    assert "UNMAPPED" in s or "false" in s
    assert any("bb_squeeze" in w or "not mapped" in w.lower() for w in warnings)


def test_pine_export_indicator_kind():
    s, warnings = rules_json_to_pine(
        pattern_id=4,
        name="RSI ind",
        description=None,
        timeframe="1d",
        rules_json='{"conditions":[{"indicator":"rsi_14","op":">","value":65}]}',
        kind="indicator",
    )
    assert "indicator(" in s
    assert "alertcondition" in s
    assert "plotshape" in s
    assert "strategy.entry" not in s
    assert any("indicator export" in w.lower() for w in warnings)
