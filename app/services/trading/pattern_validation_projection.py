"""Typed read/write helpers for ``ScanPattern.oos_validation_json`` contracts."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


_CONTRACT_KEYS = (
    "edge_evidence",
    "selection_bias",
    "parameter_stability",
    "live_drift",
    "live_drift_v2",
    "execution_robustness",
    "execution_robustness_v2",
    "allocation_state",
)


def _clone_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


@dataclass(frozen=True)
class PatternValidationProjection:
    edge_evidence: dict[str, Any] = field(default_factory=dict)
    selection_bias: dict[str, Any] = field(default_factory=dict)
    parameter_stability: dict[str, Any] = field(default_factory=dict)
    live_drift: dict[str, Any] = field(default_factory=dict)
    live_drift_v2: dict[str, Any] = field(default_factory=dict)
    execution_robustness: dict[str, Any] = field(default_factory=dict)
    execution_robustness_v2: dict[str, Any] = field(default_factory=dict)
    allocation_state: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: Any) -> "PatternValidationProjection":
        raw = _clone_dict(payload)
        return cls(**{key: _clone_dict(raw.get(key)) for key in _CONTRACT_KEYS})

    def to_payload(self) -> dict[str, Any]:
        return {key: _clone_dict(value) for key, value in asdict(self).items() if isinstance(value, dict) and value}


def read_pattern_validation_projection(pattern_or_payload: Any) -> PatternValidationProjection:
    if hasattr(pattern_or_payload, "oos_validation_json"):
        return PatternValidationProjection.from_payload(getattr(pattern_or_payload, "oos_validation_json", None))
    return PatternValidationProjection.from_payload(pattern_or_payload)


def read_validation_contract(pattern_or_payload: Any, key: str) -> dict[str, Any]:
    if key not in _CONTRACT_KEYS:
        return {}
    return _clone_dict(getattr(read_pattern_validation_projection(pattern_or_payload), key, {}))


def write_validation_contract(pattern: Any, key: str, contract: dict[str, Any] | None) -> dict[str, Any]:
    if key not in _CONTRACT_KEYS:
        raise KeyError(f"unknown validation contract '{key}'")
    payload = _clone_dict(getattr(pattern, "oos_validation_json", None))
    if isinstance(contract, dict) and contract:
        payload[key] = dict(contract)
    else:
        payload.pop(key, None)
    pattern.oos_validation_json = payload
    return payload


def merge_validation_contracts(pattern: Any, contracts: dict[str, dict[str, Any] | None]) -> dict[str, Any]:
    payload = _clone_dict(getattr(pattern, "oos_validation_json", None))
    for key, contract in contracts.items():
        if key not in _CONTRACT_KEYS:
            continue
        if isinstance(contract, dict) and contract:
            payload[key] = dict(contract)
        else:
            payload.pop(key, None)
    pattern.oos_validation_json = payload
    return payload

