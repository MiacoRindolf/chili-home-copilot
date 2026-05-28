import json
from types import SimpleNamespace

from app.routers.trading_sub import patterns
from app.routers.trading_sub.patterns import (
    _SuggestPatternBody,
    _mechanical_pattern_suggestion,
    api_suggest_pattern,
)
from app.services.trading.mechanical_pattern_parser import mechanical_pattern_suggestion


def test_pattern_endpoint_uses_shared_mechanical_parser():
    assert patterns._mechanical_pattern_suggestion is mechanical_pattern_suggestion


def test_mechanical_pattern_suggestion_parses_numeric_and_ref_conditions():
    parsed = _mechanical_pattern_suggestion(
        "RSI > 55 and price above EMA 20 with relative volume >= 1.5"
    )

    assert parsed is not None
    assert parsed["source"] == "mechanical"
    assert {"indicator": "rsi_14", "op": ">", "value": 55.0} in parsed["conditions"]
    assert {"indicator": "price", "op": ">", "ref": "ema_20"} in parsed["conditions"]
    assert {"indicator": "rel_vol", "op": ">=", "value": 1.5} in parsed["conditions"]


def test_mechanical_pattern_suggestion_parses_boolean_and_between_conditions():
    parsed = _mechanical_pattern_suggestion(
        "BB squeeze and ADX below 20 and RSI between 40 and 65"
    )

    assert parsed is not None
    assert {"indicator": "bb_squeeze", "op": "==", "value": True} in parsed["conditions"]
    assert {"indicator": "adx", "op": "<", "value": 20.0} in parsed["conditions"]
    assert {
        "indicator": "rsi_14",
        "op": "between",
        "value": [40.0, 65.0],
    } in parsed["conditions"]


def test_mechanical_pattern_suggestion_parses_vwap_and_narrow_range():
    parsed = _mechanical_pattern_suggestion("price above VWAP and NR7")

    assert parsed is not None
    assert {"indicator": "vwap_reclaim", "op": "==", "value": True} in parsed["conditions"]
    assert {"indicator": "narrow_range", "op": "==", "value": "NR7"} in parsed["conditions"]


def test_mechanical_pattern_suggestion_requires_two_conditions():
    assert _mechanical_pattern_suggestion("breakout after good news") is None
    assert _mechanical_pattern_suggestion("RSI above 55") is None


def test_pattern_suggest_endpoint_uses_mechanical_path_without_llm(monkeypatch):
    created: dict[str, object] = {}

    class FakeQuery:
        def filter(self, *_args, **_kwargs):
            return self

        def first(self):
            return None

    class FakeDb:
        def query(self, *_args, **_kwargs):
            return FakeQuery()

        def add(self, _obj):
            return None

        def commit(self):
            return None

        def refresh(self, obj):
            obj.id = 101

    def fake_create_pattern(_db, data):
        created["data"] = data
        return SimpleNamespace(
            id=7,
            name=data["name"],
            description=data["description"],
            rules_json=data["rules_json"],
            score_boost=data["score_boost"],
        )

    def fail_llm_call(*_args, **_kwargs):
        raise AssertionError("mechanical suggestions must not call the LLM")

    monkeypatch.setattr("app.services.trading.public_api.create_pattern", fake_create_pattern)
    monkeypatch.setattr("app.services.llm_caller.call_llm", fail_llm_call)
    monkeypatch.setattr(patterns, "get_identity_ctx", lambda *_args, **_kwargs: {"user_id": "u1"})

    response = api_suggest_pattern(
        _SuggestPatternBody(description="RSI > 55 and price above EMA 20"),
        request=SimpleNamespace(),
        db=FakeDb(),
    )
    payload = json.loads(response.body)
    rules = json.loads(created["data"]["rules_json"])

    assert payload["ok"] is True
    assert payload["suggestion_source"] == "mechanical"
    assert {"indicator": "rsi_14", "op": ">", "value": 55.0} in rules["conditions"]
    assert {"indicator": "price", "op": ">", "ref": "ema_20"} in rules["conditions"]
