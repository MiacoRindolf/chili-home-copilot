from __future__ import annotations

import json
from types import SimpleNamespace

from app.services.trading import pattern_ml, signal_explainability


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


class _FakeQuery:
    def __init__(self, row: object) -> None:
        self.row = row
        self.filter_calls = 0

    def filter(self, *args: object) -> "_FakeQuery":
        self.filter_calls += 1
        return self

    def first(self) -> object:
        return self.row


class _FakeSession:
    def __init__(self, row: object) -> None:
        self.row = row
        self.query_args: list[tuple[object, ...]] = []
        self.last_query: _FakeQuery | None = None

    def query(self, *args: object) -> _FakeQuery:
        self.query_args.append(args)
        self.last_query = _FakeQuery(self.row)
        return self.last_query


class _NotReadyLearner:
    def is_ready(self) -> bool:
        return False


def test_explain_signal_reads_pattern_name_and_rules_columns_only(monkeypatch) -> None:
    monkeypatch.setattr(pattern_ml, "get_meta_learner", lambda: _NotReadyLearner())
    rules = {
        "conditions": [
            {"indicator": "rsi", "op": ">", "value": 50},
            {"indicator": "adx", "op": ">=", "value": 25},
        ]
    }
    db = _FakeSession(("Momentum break", rules))

    out = signal_explainability.explain_signal(
        db,
        "AAPL",
        scan_pattern_id=42,
        indicator_values={"rsi": 62, "adx": 30},
    )

    assert out["pattern_name"] == "Momentum break"
    assert out["method"] == "rule_based"
    assert [c["indicator"] for c in out["contributions"]] == ["rsi", "adx"]
    assert all(c["passed"] is True for c in out["contributions"])
    assert tuple(getattr(arg, "key", None) for arg in db.query_args[0]) == ("name", "rules_json")
    assert db.last_query is not None
    assert db.last_query.filter_calls == 1


def test_pattern_explain_values_handles_object_tuple_and_empty_rows() -> None:
    assert signal_explainability._pattern_explain_values(("Pattern", {"conditions": []})) == (
        "Pattern",
        {"conditions": []},
    )
    assert signal_explainability._pattern_explain_values(
        SimpleNamespace(name="Obj", rules_json={"conditions": []})
    ) == ("Obj", {"conditions": []})
    assert signal_explainability._pattern_explain_values(()) == (None, None)
