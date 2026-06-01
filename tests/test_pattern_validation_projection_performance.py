from __future__ import annotations

from app.services.trading.pattern_validation_projection import (
    PatternValidationProjection,
    _CONTRACT_KEYS,
    read_validation_contract,
)


def test_projection_to_payload_iterates_contract_keys_without_asdict(monkeypatch) -> None:
    import dataclasses

    def fail_asdict(*_args, **_kwargs):
        raise AssertionError("to_payload should avoid recursive dataclass asdict copies")

    monkeypatch.setattr(dataclasses, "asdict", fail_asdict)

    projection = PatternValidationProjection.from_payload(
        {
            "edge_evidence": {"score": 1, "nested": {"kept": True}},
            "parameter_stability": {"tier": "plateau"},
            "ignored": {"value": "not exported"},
        }
    )

    payload = projection.to_payload()

    assert list(payload) == ["edge_evidence", "parameter_stability"]
    assert payload["edge_evidence"] == {"score": 1, "nested": {"kept": True}}
    assert payload["parameter_stability"] == {"tier": "plateau"}
    assert "ignored" not in payload


def test_projection_to_payload_clones_top_level_contract_dicts() -> None:
    projection = PatternValidationProjection.from_payload(
        {"edge_evidence": {"score": 1}, "selection_bias": {}}
    )

    payload = projection.to_payload()
    payload["edge_evidence"]["score"] = 2

    assert projection.edge_evidence["score"] == 1
    assert "selection_bias" not in payload
    assert set(payload).issubset(set(_CONTRACT_KEYS))


def test_read_validation_contract_clones_only_requested_contract(monkeypatch) -> None:
    calls: list[object] = []

    def fail_from_payload(cls, payload):
        calls.append(payload)
        raise AssertionError("single-contract reads should not build a full projection")

    monkeypatch.setattr(
        PatternValidationProjection,
        "from_payload",
        classmethod(fail_from_payload),
    )

    payload = {
        "edge_evidence": {"score": 1},
        "parameter_stability": {"tier": "plateau"},
    }
    contract = read_validation_contract(payload, "edge_evidence")
    contract["score"] = 2

    assert calls == []
    assert payload["edge_evidence"]["score"] == 1
    assert read_validation_contract(payload, "unknown") == {}
