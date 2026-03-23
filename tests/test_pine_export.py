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
    assert "chili_atrMult" in s
    assert "ta.sma(ta.tr, 14)" in s
    assert "ta.pivotlow" in s
    assert "chiliUseBos" in s
    assert "strategy.close" in s
    assert "indicator(" not in s
    assert isinstance(warnings, list)
    assert any("Pine strategy" in w for w in warnings)


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
    # longSignal must be one physical line (Pine rejects newline + leading `and`)
    long_sig_lines = [ln for ln in s.splitlines() if ln.strip().startswith("longSignal =")]
    assert len(long_sig_lines) == 1


def test_pine_export_multicondition_longsignal_single_line():
    s, _w = rules_json_to_pine(
        pattern_id=99,
        name="Multi",
        description=None,
        timeframe="1d",
        rules_json="""{"conditions":[
            {"indicator":"rsi_14","op":">","value":30},
            {"indicator":"price","op":">","ref":"ema_20"},
            {"indicator":"price","op":">","ref":"ema_50"}
        ]}""",
    )
    line = [ln for ln in s.splitlines() if ln.strip().startswith("longSignal =")][0]
    rhs = line.split("=", 1)[1].strip()
    assert " and " in rhs
    assert "\n" not in rhs


def test_pine_export_resistance_retests_proxy():
    s, warnings = rules_json_to_pine(
        pattern_id=5,
        name="Retest",
        description=None,
        timeframe="1d",
        rules_json=(
            '{"conditions":['
            '{"indicator":"rsi_14","op":">","value":50},'
            '{"indicator":"  resistance_retests  ","op":">=","value":3,'
            '"params":{"tolerance_pct":1.5,"lookback":20}}'
            "]}"
        ),
    )
    assert "f_chili_resistance_retests" in s
    assert "windowBars = lookback + 1" in s
    assert "v_rr_L20_t150" in s
    assert ">=" in s
    assert "false  // UNMAPPED" not in s.split("longSignal = ")[1].split("\n")[0]
    assert any("proxy" in w.lower() or "resistance_retests" in w.lower() for w in warnings)


def test_pine_export_header_includes_insight_id():
    s, _w = rules_json_to_pine(
        pattern_id=7,
        name="X",
        description=None,
        timeframe="1d",
        rules_json='{"conditions":[{"indicator":"rsi_14","op":">","value":50}]}',
        trading_insight_id=42,
    )
    assert "scan_pattern_id=7" in s
    assert "pine_export_rev=4" in s
    assert "TradingInsight id=42" in s


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


def test_pine_export_strategy_use_bos_false():
    s, _w = rules_json_to_pine(
        pattern_id=8,
        name="No BOS",
        description=None,
        timeframe="1d",
        rules_json='{"conditions":[{"indicator":"rsi_14","op":">","value":50}]}',
        exit_config_json='{"use_bos": false}',
    )
    assert "chiliUseBos = false" in s
    assert "bosHit = chiliUseBos and" in s


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
