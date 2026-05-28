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
        "The setup is an RSI breakout with price above EMA 20 and rising volume.",
        existing_names=set(),
    )

    assert result[0]["name"] == "RSI EMA Breakout"
    assert captured["purpose"] == "pattern_research_extract"
    assert captured["system_prompt"] == researcher._PATTERN_EXTRACT_SYSTEM_PROMPT
    prompt = captured["messages"][0]["content"]
    assert prompt.index("## Available Indicators") < prompt.index("Web Content:")
