from __future__ import annotations

import json

from app.services.trading import signal_explainability


def test_top_importance_contribs_uses_bounded_heap_selection(monkeypatch) -> None:
    contribs = [
        {"indicator": "low", "importance": 0.1},
        {"indicator": "high", "importance": 0.9},
    ]
    calls: list[tuple[int, list[dict]]] = []

    def fake_nlargest(limit: int, items: list[dict], *, key):
        calls.append((limit, items))
        return [max(items, key=key)]

    monkeypatch.setattr(signal_explainability.heapq, "nlargest", fake_nlargest)

    assert signal_explainability._top_importance_contribs(contribs, limit=1) == [
        {"indicator": "high", "importance": 0.9}
    ]
    assert calls == [(1, contribs)]


def test_top_importance_contribs_keeps_descending_importance() -> None:
    contribs = [
        {"indicator": "mid", "importance": 0.5},
        {"indicator": "low", "importance": 0.1},
        {"indicator": "high", "importance": 0.9},
    ]

    assert signal_explainability._top_importance_contribs(contribs, limit=2) == [
        {"indicator": "high", "importance": 0.9},
        {"indicator": "mid", "importance": 0.5},
    ]


def test_top_importance_contribs_handles_empty_and_nonpositive_limits() -> None:
    assert signal_explainability._top_importance_contribs([], limit=10) == []
    assert signal_explainability._top_importance_contribs(
        [{"indicator": "a", "importance": 0.1}],
        limit=0,
    ) == []


def test_conditions_from_rules_json_caches_repeated_rule_strings(monkeypatch) -> None:
    signal_explainability._conditions_from_rules_json.cache_clear()
    raw = json.dumps({"conditions": [{"indicator": "rsi", "op": ">", "value": 55}]})
    real_loads = json.loads
    calls = 0

    def counting_loads(value, *args, **kwargs):
        nonlocal calls
        calls += 1
        return real_loads(value, *args, **kwargs)

    monkeypatch.setattr(signal_explainability.json, "loads", counting_loads)

    assert signal_explainability._conditions_from_rules(raw) == (
        {"indicator": "rsi", "op": ">", "value": 55},
    )
    assert signal_explainability._conditions_from_rules(raw) == (
        {"indicator": "rsi", "op": ">", "value": 55},
    )

    assert calls == 1
    info = signal_explainability._conditions_from_rules_json.cache_info()
    assert info.hits == 1
    assert info.maxsize == 2048


def test_conditions_from_rules_json_cache_is_bounded() -> None:
    signal_explainability._conditions_from_rules_json.cache_clear()

    for i in range(2050):
        signal_explainability._conditions_from_rules_json(
            json.dumps({"conditions": [{"indicator": f"ind_{i}"}]})
        )

    info = signal_explainability._conditions_from_rules_json.cache_info()
    assert info.maxsize == 2048
    assert info.currsize == 2048


def test_conditions_from_rules_native_dict_bypasses_json(monkeypatch) -> None:
    def fail_loads(*_args, **_kwargs):
        raise AssertionError("native rules dict should not be parsed as JSON")

    monkeypatch.setattr(signal_explainability.json, "loads", fail_loads)

    assert signal_explainability._conditions_from_rules(
        {"conditions": [{"indicator": "macd", "op": ">", "value": 0}]}
    ) == ({"indicator": "macd", "op": ">", "value": 0},)
