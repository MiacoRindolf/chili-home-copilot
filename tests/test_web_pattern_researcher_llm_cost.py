from app.services.trading import web_pattern_researcher as researcher


def test_extract_patterns_skips_irrelevant_content_without_llm(monkeypatch):
    def fail_call_llm(*_args, **_kwargs):
        raise AssertionError("irrelevant content should not spend LLM tokens")

    monkeypatch.setattr(researcher, "call_llm", fail_call_llm)

    result = researcher._extract_patterns_from_content(
        "Cookie policy. Contact us. This page explains account settings and billing.",
        existing_names=set(),
    )

    assert result == []


def test_extract_patterns_uses_mechanical_parser_without_llm(monkeypatch):
    def fail_call_llm(*_args, **_kwargs):
        raise AssertionError("explicit mechanical patterns should not spend LLM tokens")

    monkeypatch.setattr(researcher, "call_llm", fail_call_llm)

    result = researcher._extract_patterns_from_content(
        (
            "A simple momentum breakout can be coded as RSI > 55 and price "
            "above EMA 20 with relative volume >= 1.5."
        ),
        existing_names=set(),
    )

    assert len(result) == 1
    assert result[0]["source"] == "mechanical"
    assert {"indicator": "rsi_14", "op": ">", "value": 55.0} in result[0]["conditions"]
    assert {"indicator": "price", "op": ">", "ref": "ema_20"} in result[0]["conditions"]
    assert {"indicator": "rel_vol", "op": ">=", "value": 1.5} in result[0]["conditions"]


def test_extract_patterns_uses_close_alias_mechanically_without_llm(monkeypatch):
    def fail_call_llm(*_args, **_kwargs):
        raise AssertionError("close alias pattern should not spend LLM tokens")

    monkeypatch.setattr(researcher, "call_llm", fail_call_llm)

    result = researcher._extract_patterns_from_content(
        "A clean breakout is close above EMA20 with RSI > 55 and relative volume >= 1.5.",
        existing_names=set(),
    )

    assert len(result) == 1
    assert result[0]["source"] == "mechanical"
    assert {"indicator": "price", "op": ">", "ref": "ema_20"} in result[0]["conditions"]


def test_extract_patterns_uses_rising_volume_mechanically_without_llm(monkeypatch):
    def fail_call_llm(*_args, **_kwargs):
        raise AssertionError("rising volume pattern should not spend LLM tokens")

    monkeypatch.setattr(researcher, "call_llm", fail_call_llm)

    result = researcher._extract_patterns_from_content(
        "A trend breakout is close above EMA20 with RSI > 55 and rising volume.",
        existing_names=set(),
    )

    assert len(result) == 1
    assert result[0]["source"] == "mechanical"
    assert {"indicator": "rel_vol", "op": ">=", "value": 1.5} in result[0]["conditions"]


def test_extract_patterns_uses_reversed_ma_notation_without_llm(monkeypatch):
    def fail_call_llm(*_args, **_kwargs):
        raise AssertionError("reversed MA notation should be mechanical")

    monkeypatch.setattr(researcher, "call_llm", fail_call_llm)

    result = researcher._extract_patterns_from_content(
        "A trend setup is 50 EMA crossing above 200 EMA with volume breakout.",
        existing_names=set(),
    )

    assert len(result) == 1
    assert {"indicator": "ema_50", "op": ">", "ref": "ema_200"} in result[0]["conditions"]
    assert {"indicator": "rel_vol", "op": ">=", "value": 1.5} in result[0]["conditions"]


def test_extract_patterns_uses_vcp_setup_without_llm(monkeypatch):
    def fail_call_llm(*_args, **_kwargs):
        raise AssertionError("VCP setup should be mechanical")

    monkeypatch.setattr(researcher, "call_llm", fail_call_llm)

    result = researcher._extract_patterns_from_content(
        "A compression setup uses VCP setup and RSI > 55.",
        existing_names=set(),
    )

    assert len(result) == 1
    assert result[0]["source"] == "mechanical"
    assert {"indicator": "vcp_count", "op": ">=", "value": 2.0} in result[0]["conditions"]
    assert {"indicator": "rsi_14", "op": ">", "value": 55.0} in result[0]["conditions"]


def test_extract_patterns_uses_volatility_contraction_count_without_llm(monkeypatch):
    def fail_call_llm(*_args, **_kwargs):
        raise AssertionError("volatility contraction count should be mechanical")

    monkeypatch.setattr(researcher, "call_llm", fail_call_llm)

    result = researcher._extract_patterns_from_content(
        "A compression setup uses volatility contraction pattern with 3 contractions and volume spike.",
        existing_names=set(),
    )

    assert len(result) == 1
    assert result[0]["source"] == "mechanical"
    assert {"indicator": "vcp_count", "op": ">=", "value": 3.0} in result[0]["conditions"]
    assert {"indicator": "rel_vol", "op": ">=", "value": 1.5} in result[0]["conditions"]


def test_extract_patterns_uses_numeric_resistance_retests_without_llm(monkeypatch):
    def fail_call_llm(*_args, **_kwargs):
        raise AssertionError("resistance retest count should be mechanical")

    monkeypatch.setattr(researcher, "call_llm", fail_call_llm)

    result = researcher._extract_patterns_from_content(
        "A breakout setup uses 3 resistance retests and RSI > 55.",
        existing_names=set(),
    )

    assert len(result) == 1
    assert result[0]["source"] == "mechanical"
    assert {"indicator": "resistance_retests", "op": ">=", "value": 3.0} in result[0]["conditions"]
    assert {"indicator": "rsi_14", "op": ">", "value": 55.0} in result[0]["conditions"]


def test_extract_patterns_uses_multiple_resistance_retests_without_llm(monkeypatch):
    def fail_call_llm(*_args, **_kwargs):
        raise AssertionError("multiple resistance retests should be mechanical")

    monkeypatch.setattr(researcher, "call_llm", fail_call_llm)

    result = researcher._extract_patterns_from_content(
        "A breakout setup uses multiple resistance retests and volume spike.",
        existing_names=set(),
    )

    assert len(result) == 1
    assert result[0]["source"] == "mechanical"
    assert {"indicator": "resistance_retests", "op": ">=", "value": 2.0} in result[0]["conditions"]
    assert {"indicator": "rel_vol", "op": ">=", "value": 1.5} in result[0]["conditions"]


def test_extract_patterns_uses_third_test_of_resistance_without_llm(monkeypatch):
    def fail_call_llm(*_args, **_kwargs):
        raise AssertionError("ordinal resistance test should be mechanical")

    monkeypatch.setattr(researcher, "call_llm", fail_call_llm)

    result = researcher._extract_patterns_from_content(
        "A breakout setup uses third test of resistance and RSI > 55.",
        existing_names=set(),
    )

    assert len(result) == 1
    assert result[0]["source"] == "mechanical"
    assert {"indicator": "resistance_retests", "op": ">=", "value": 3.0} in result[0]["conditions"]
    assert {"indicator": "rsi_14", "op": ">", "value": 55.0} in result[0]["conditions"]


def test_extract_patterns_uses_resistance_distance_without_llm(monkeypatch):
    def fail_call_llm(*_args, **_kwargs):
        raise AssertionError("resistance distance should be mechanical")

    monkeypatch.setattr(researcher, "call_llm", fail_call_llm)

    result = researcher._extract_patterns_from_content(
        "A breakout setup uses price within 2% of resistance and RSI > 55.",
        existing_names=set(),
    )

    assert len(result) == 1
    assert result[0]["source"] == "mechanical"
    assert {"indicator": "dist_to_resistance_pct", "op": "<=", "value": 2.0} in result[0]["conditions"]
    assert {"indicator": "rsi_14", "op": ">", "value": 55.0} in result[0]["conditions"]


def test_extract_patterns_uses_near_resistance_without_llm(monkeypatch):
    def fail_call_llm(*_args, **_kwargs):
        raise AssertionError("near resistance should be mechanical")

    monkeypatch.setattr(researcher, "call_llm", fail_call_llm)

    result = researcher._extract_patterns_from_content(
        "A breakout setup waits for price near resistance with volume spike.",
        existing_names=set(),
    )

    assert len(result) == 1
    assert result[0]["source"] == "mechanical"
    assert {"indicator": "dist_to_resistance_pct", "op": "<=", "value": 2.0} in result[0]["conditions"]
    assert {"indicator": "rel_vol", "op": ">=", "value": 1.5} in result[0]["conditions"]


def test_extract_patterns_uses_percent_below_resistance_without_llm(monkeypatch):
    def fail_call_llm(*_args, **_kwargs):
        raise AssertionError("percent below resistance should be mechanical")

    monkeypatch.setattr(researcher, "call_llm", fail_call_llm)

    result = researcher._extract_patterns_from_content(
        "A breakout setup uses close less than three percent below resistance and RSI > 55.",
        existing_names=set(),
    )

    assert len(result) == 1
    assert result[0]["source"] == "mechanical"
    assert {"indicator": "dist_to_resistance_pct", "op": "<=", "value": 3.0} in result[0]["conditions"]
    assert {"indicator": "rsi_14", "op": ">", "value": 55.0} in result[0]["conditions"]


def test_extract_patterns_uses_low_adx_without_llm(monkeypatch):
    def fail_call_llm(*_args, **_kwargs):
        raise AssertionError("low ADX squeeze should be mechanical")

    monkeypatch.setattr(researcher, "call_llm", fail_call_llm)

    result = researcher._extract_patterns_from_content(
        "A squeeze setup uses Bollinger Band squeeze with low ADX and RSI between 40 and 65.",
        existing_names=set(),
    )

    assert len(result) == 1
    assert result[0]["source"] == "mechanical"
    assert {"indicator": "bb_squeeze", "op": "==", "value": True} in result[0]["conditions"]
    assert {"indicator": "adx", "op": "<", "value": 20.0} in result[0]["conditions"]
    assert {"indicator": "rsi_14", "op": "between", "value": [40.0, 65.0]} in result[0]["conditions"]


def test_extract_patterns_uses_adx_trend_strength_without_llm(monkeypatch):
    def fail_call_llm(*_args, **_kwargs):
        raise AssertionError("ADX trend strength should be mechanical")

    monkeypatch.setattr(researcher, "call_llm", fail_call_llm)

    result = researcher._extract_patterns_from_content(
        "A breakout setup uses ADX trend strength confirmation with price above EMA20.",
        existing_names=set(),
    )

    assert len(result) == 1
    assert result[0]["source"] == "mechanical"
    assert {"indicator": "adx", "op": ">=", "value": 20.0} in result[0]["conditions"]
    assert {"indicator": "price", "op": ">", "ref": "ema_20"} in result[0]["conditions"]


def test_extract_patterns_uses_volume_multiple_without_llm(monkeypatch):
    def fail_call_llm(*_args, **_kwargs):
        raise AssertionError("volume multiple should be mechanical")

    monkeypatch.setattr(researcher, "call_llm", fail_call_llm)

    result = researcher._extract_patterns_from_content(
        "A breakout setup uses 2x volume and RSI > 55.",
        existing_names=set(),
    )

    assert len(result) == 1
    assert result[0]["source"] == "mechanical"
    assert {"indicator": "rel_vol", "op": ">=", "value": 2.0} in result[0]["conditions"]
    assert {"indicator": "rsi_14", "op": ">", "value": 55.0} in result[0]["conditions"]


def test_extract_patterns_uses_relative_volume_multiple_without_llm(monkeypatch):
    def fail_call_llm(*_args, **_kwargs):
        raise AssertionError("relative volume multiple should be mechanical")

    monkeypatch.setattr(researcher, "call_llm", fail_call_llm)

    result = researcher._extract_patterns_from_content(
        "A breakout setup uses relative volume above 2.5x with price above EMA20.",
        existing_names=set(),
    )

    assert len(result) == 1
    assert result[0]["source"] == "mechanical"
    assert {"indicator": "rel_vol", "op": ">=", "value": 2.5} in result[0]["conditions"]
    assert {"indicator": "price", "op": ">", "ref": "ema_20"} in result[0]["conditions"]


def test_extract_patterns_uses_volume_surge_without_llm(monkeypatch):
    def fail_call_llm(*_args, **_kwargs):
        raise AssertionError("volume surge should be mechanical")

    monkeypatch.setattr(researcher, "call_llm", fail_call_llm)

    result = researcher._extract_patterns_from_content(
        "A breakout setup uses volume surge and RSI > 55.",
        existing_names=set(),
    )

    assert len(result) == 1
    assert result[0]["source"] == "mechanical"
    assert {"indicator": "rel_vol", "op": ">=", "value": 1.5} in result[0]["conditions"]
    assert {"indicator": "rsi_14", "op": ">", "value": 55.0} in result[0]["conditions"]


def test_extract_patterns_uses_macd_plus_without_llm(monkeypatch):
    def fail_call_llm(*_args, **_kwargs):
        raise AssertionError("MACD+ shorthand should be mechanical")

    monkeypatch.setattr(researcher, "call_llm", fail_call_llm)

    result = researcher._extract_patterns_from_content(
        "A breakout setup uses MACD+ with RSI > 55.",
        existing_names=set(),
    )

    assert len(result) == 1
    assert result[0]["source"] == "mechanical"
    assert {"indicator": "macd_hist", "op": ">", "value": 0.0} in result[0]["conditions"]
    assert {"indicator": "rsi_14", "op": ">", "value": 55.0} in result[0]["conditions"]


def test_extract_patterns_uses_bullish_macd_without_llm(monkeypatch):
    def fail_call_llm(*_args, **_kwargs):
        raise AssertionError("bullish MACD should be mechanical")

    monkeypatch.setattr(researcher, "call_llm", fail_call_llm)

    result = researcher._extract_patterns_from_content(
        "A momentum setup uses bullish MACD and relative volume above 2x.",
        existing_names=set(),
    )

    assert len(result) == 1
    assert result[0]["source"] == "mechanical"
    assert {"indicator": "macd_hist", "op": ">", "value": 0.0} in result[0]["conditions"]
    assert {"indicator": "rel_vol", "op": ">=", "value": 2.0} in result[0]["conditions"]


def test_extract_patterns_uses_rsi_oversold_without_llm(monkeypatch):
    def fail_call_llm(*_args, **_kwargs):
        raise AssertionError("RSI oversold should be mechanical")

    monkeypatch.setattr(researcher, "call_llm", fail_call_llm)

    result = researcher._extract_patterns_from_content(
        "A reversal setup uses RSI oversold with MACD+.",
        existing_names=set(),
    )

    assert len(result) == 1
    assert result[0]["source"] == "mechanical"
    assert {"indicator": "rsi_14", "op": "<", "value": 30.0} in result[0]["conditions"]
    assert {"indicator": "macd_hist", "op": ">", "value": 0.0} in result[0]["conditions"]


def test_extract_patterns_uses_rsi_overbought_without_llm(monkeypatch):
    def fail_call_llm(*_args, **_kwargs):
        raise AssertionError("RSI overbought should be mechanical")

    monkeypatch.setattr(researcher, "call_llm", fail_call_llm)

    result = researcher._extract_patterns_from_content(
        "A fade setup uses RSI overbought and bearish MACD.",
        existing_names=set(),
    )

    assert len(result) == 1
    assert result[0]["source"] == "mechanical"
    assert {"indicator": "rsi_14", "op": ">", "value": 70.0} in result[0]["conditions"]
    assert {"indicator": "macd_hist", "op": "<", "value": 0.0} in result[0]["conditions"]


def test_extract_patterns_uses_rsi_neutral_without_llm(monkeypatch):
    def fail_call_llm(*_args, **_kwargs):
        raise AssertionError("RSI neutral should be mechanical")

    monkeypatch.setattr(researcher, "call_llm", fail_call_llm)

    result = researcher._extract_patterns_from_content(
        "A breakout setup uses RSI neutral with ADX trend strength confirmation.",
        existing_names=set(),
    )

    assert len(result) == 1
    assert result[0]["source"] == "mechanical"
    assert {"indicator": "rsi_14", "op": "between", "value": [40.0, 65.0]} in result[0]["conditions"]
    assert {"indicator": "adx", "op": ">=", "value": 20.0} in result[0]["conditions"]


def test_extract_patterns_uses_bullish_ema_stack_without_llm(monkeypatch):
    def fail_call_llm(*_args, **_kwargs):
        raise AssertionError("bullish EMA stack should be mechanical")

    monkeypatch.setattr(researcher, "call_llm", fail_call_llm)

    result = researcher._extract_patterns_from_content(
        "A continuation setup uses EMA stacking bullish with RSI neutral.",
        existing_names=set(),
    )

    assert len(result) == 1
    assert result[0]["source"] == "mechanical"
    assert {"indicator": "price", "op": ">", "ref": "ema_20"} in result[0]["conditions"]
    assert {"indicator": "ema_20", "op": ">", "ref": "ema_50"} in result[0]["conditions"]
    assert {"indicator": "ema_50", "op": ">", "ref": "ema_100"} in result[0]["conditions"]
    assert {"indicator": "rsi_14", "op": "between", "value": [40.0, 65.0]} in result[0]["conditions"]


def test_extract_patterns_uses_squeeze_firing_without_llm(monkeypatch):
    def fail_call_llm(*_args, **_kwargs):
        raise AssertionError("squeeze firing should be mechanical")

    monkeypatch.setattr(researcher, "call_llm", fail_call_llm)

    result = researcher._extract_patterns_from_content(
        "A breakout setup uses squeeze firing with RSI neutral.",
        existing_names=set(),
    )

    assert len(result) == 1
    assert result[0]["source"] == "mechanical"
    assert {"indicator": "bb_squeeze", "op": "==", "value": True} in result[0]["conditions"]
    assert {"indicator": "rsi_14", "op": "between", "value": [40.0, 65.0]} in result[0]["conditions"]


def test_extract_patterns_uses_vwap_hold_without_llm(monkeypatch):
    def fail_call_llm(*_args, **_kwargs):
        raise AssertionError("VWAP hold should be mechanical")

    monkeypatch.setattr(researcher, "call_llm", fail_call_llm)

    result = researcher._extract_patterns_from_content(
        "A continuation setup uses pullback holds VWAP with relative volume above 2x.",
        existing_names=set(),
    )

    assert len(result) == 1
    assert result[0]["source"] == "mechanical"
    assert {"indicator": "vwap_reclaim", "op": "==", "value": True} in result[0]["conditions"]
    assert {"indicator": "rel_vol", "op": ">=", "value": 2.0} in result[0]["conditions"]


def test_extract_patterns_uses_ema_support_without_llm(monkeypatch):
    def fail_call_llm(*_args, **_kwargs):
        raise AssertionError("EMA support should be mechanical")

    monkeypatch.setattr(researcher, "call_llm", fail_call_llm)

    result = researcher._extract_patterns_from_content(
        "A continuation setup uses 20 EMA support with volume surge.",
        existing_names=set(),
    )

    assert len(result) == 1
    assert result[0]["source"] == "mechanical"
    assert {"indicator": "price", "op": ">", "ref": "ema_20"} in result[0]["conditions"]
    assert {"indicator": "rel_vol", "op": ">=", "value": 1.5} in result[0]["conditions"]


def test_extract_patterns_uses_tape_speed_shorthand_without_llm(monkeypatch):
    def fail_call_llm(*_args, **_kwargs):
        raise AssertionError("tape-speed shorthand should be mechanical")

    monkeypatch.setattr(researcher, "call_llm", fail_call_llm)

    result = researcher._extract_patterns_from_content(
        "A tape-speed setup uses volume burst with RSI rising and MACD histogram expanding.",
        existing_names=set(),
    )

    assert len(result) == 1
    assert result[0]["source"] == "mechanical"
    assert {"indicator": "rel_vol", "op": ">=", "value": 1.5} in result[0]["conditions"]
    assert {"indicator": "rsi_14", "op": ">", "value": 55.0} in result[0]["conditions"]
    assert {"indicator": "macd_hist", "op": ">", "value": 0.0} in result[0]["conditions"]


def test_extract_patterns_uses_tight_range_coil_without_llm(monkeypatch):
    def fail_call_llm(*_args, **_kwargs):
        raise AssertionError("tight range coil should be mechanical")

    monkeypatch.setattr(researcher, "call_llm", fail_call_llm)

    result = researcher._extract_patterns_from_content(
        "A consolidation setup uses tight range coiling with RSI neutral.",
        existing_names=set(),
    )

    assert len(result) == 1
    assert result[0]["source"] == "mechanical"
    assert {"indicator": "narrow_range", "op": "any_of", "value": ["NR4", "NR7"]} in result[0]["conditions"]
    assert {"indicator": "rsi_14", "op": "between", "value": [40.0, 65.0]} in result[0]["conditions"]


def test_extract_patterns_uses_vcp_plus_count_without_llm(monkeypatch):
    def fail_call_llm(*_args, **_kwargs):
        raise AssertionError("VCP 3+ should be mechanical")

    monkeypatch.setattr(researcher, "call_llm", fail_call_llm)

    result = researcher._extract_patterns_from_content(
        "A compression setup uses VCP 3+ with RSI neutral.",
        existing_names=set(),
    )

    assert len(result) == 1
    assert result[0]["source"] == "mechanical"
    assert {"indicator": "vcp_count", "op": ">=", "value": 3.0} in result[0]["conditions"]
    assert {"indicator": "rsi_14", "op": "between", "value": [40.0, 65.0]} in result[0]["conditions"]


def test_pattern_extract_prompt_keeps_variable_content_last():
    prompt = researcher._build_pattern_extract_prompt(
        "RSI breakout above 55 with price over EMA 20.",
        existing_names={"Known Setup"},
    )

    assert prompt.index("## Available Indicators") < prompt.index("## Variable Inputs")
    assert prompt.index("Already Known Patterns") < prompt.index("Web Content:")
    assert prompt.endswith("RSI breakout above 55 with price over EMA 20.")


def test_extract_patterns_uses_cache_friendly_prompt_and_system(monkeypatch):
    captured: dict[str, object] = {}

    def fake_call_llm(**kwargs):
        captured.update(kwargs)
        return (
            '[{"name":"RSI EMA Breakout","description":"Momentum breakout",'
            '"asset_class":"all","conditions":['
            '{"indicator":"rsi_14","op":">","value":55},'
            '{"indicator":"price","op":">","ref":"ema_20"}],'
            '"score_boost":1.0,"min_base_score":4.0}]'
        )

    monkeypatch.setattr(researcher, "call_llm", fake_call_llm)

    result = researcher._extract_patterns_from_content(
        "The setup is an RSI breakout with improving breadth and catalyst confirmation.",
        existing_names=set(),
    )

    assert result[0]["name"] == "RSI EMA Breakout"
    assert captured["purpose"] == "pattern_research_extract"
    assert captured["system_prompt"] == researcher._PATTERN_EXTRACT_SYSTEM_PROMPT
    prompt = captured["messages"][0]["content"]
    assert prompt.index("## Available Indicators") < prompt.index("Web Content:")
