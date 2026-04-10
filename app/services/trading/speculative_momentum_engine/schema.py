"""Stable IDs for the speculative momentum engine and neural-mesh subgraph."""

from __future__ import annotations

from enum import Enum
from typing import Any

ENGINE_ID = "speculative_momentum"
ENGINE_CORE_REPEATABLE_EDGE = "core_repeatable_edge"
ENGINE_VERSION = 1
METHODOLOGY_KEY = "graph_native_heuristic_v1"

# Must match migration 092 `brain_graph_nodes.id` values.
HUB_NODE_ID = "nm_speculative_momentum_hub"

NODE_VOLUME_EXPANSION = "nm_sm_volume_expansion"
NODE_SQUEEZE_PRESSURE = "nm_sm_squeeze_pressure"
NODE_EVENT_IMPULSE = "nm_sm_event_impulse"
NODE_EXTENSION_RISK = "nm_sm_extension_risk"
NODE_EXECUTION_RISK = "nm_sm_execution_risk"
NODE_VWAP_PULLBACK = "nm_sm_vwap_pullback"
NODE_EXHAUSTION = "nm_sm_exhaustion"

SPECULATIVE_FEATURE_NODE_IDS: frozenset[str] = frozenset(
    {
        NODE_VOLUME_EXPANSION,
        NODE_SQUEEZE_PRESSURE,
        NODE_EVENT_IMPULSE,
        NODE_EXTENSION_RISK,
        NODE_EXECUTION_RISK,
        NODE_VWAP_PULLBACK,
        NODE_EXHAUSTION,
    }
)

SPECULATIVE_GRAPH_NODE_IDS: frozenset[str] = frozenset({HUB_NODE_ID, *SPECULATIVE_FEATURE_NODE_IDS})

# Registry for docs / UI labels (layer matches migration).
NODE_REGISTRY: list[dict[str, Any]] = [
    {
        "id": HUB_NODE_ID,
        "layer": 4,
        "node_type": "speculative_momentum_hub",
        "label": "Speculative momentum hub",
        "is_observer": False,
    },
    {
        "id": NODE_VOLUME_EXPANSION,
        "layer": 5,
        "node_type": "speculative_signal",
        "label": "Abnormal volume expansion",
        "is_observer": True,
    },
    {
        "id": NODE_SQUEEZE_PRESSURE,
        "layer": 5,
        "node_type": "speculative_signal",
        "label": "Squeeze / halt pressure",
        "is_observer": True,
    },
    {
        "id": NODE_EVENT_IMPULSE,
        "layer": 5,
        "node_type": "speculative_signal",
        "label": "Event / flow impulse",
        "is_observer": True,
    },
    {
        "id": NODE_EXTENSION_RISK,
        "layer": 5,
        "node_type": "speculative_signal",
        "label": "Extension / blow-off risk",
        "is_observer": True,
    },
    {
        "id": NODE_EXECUTION_RISK,
        "layer": 5,
        "node_type": "speculative_signal",
        "label": "Execution / liquidity stress",
        "is_observer": True,
    },
    {
        "id": NODE_VWAP_PULLBACK,
        "layer": 5,
        "node_type": "speculative_signal",
        "label": "VWAP / pullback structure",
        "is_observer": True,
    },
    {
        "id": NODE_EXHAUSTION,
        "layer": 5,
        "node_type": "speculative_signal",
        "label": "Exhaustion / failed continuation",
        "is_observer": True,
    },
]


class ClusterId(str, Enum):
    blow_off_risk = "blow_off_risk"
    too_extended = "too_extended"
    first_pullback_candidate = "first_pullback_candidate"
    speculative_squeeze = "speculative_squeeze"
    event_driven_spike = "event_driven_spike"
    execution_risk_high = "execution_risk_high"
    structured_momentum = "structured_momentum"
    watch_only = "watch_only"


CLUSTER_LABELS: dict[str, str] = {
    ClusterId.blow_off_risk.value: "Blow-Off Risk",
    ClusterId.too_extended.value: "Too Extended",
    ClusterId.first_pullback_candidate.value: "First Pullback Candidate",
    ClusterId.speculative_squeeze.value: "Speculative Squeeze",
    ClusterId.event_driven_spike.value: "Event-Driven Spike",
    ClusterId.execution_risk_high.value: "Execution Risk High",
    ClusterId.structured_momentum.value: "Structured Momentum",
    ClusterId.watch_only.value: "Watch Only",
}


class ReasonCode(str, Enum):
    not_pattern_imminent_engine = "not_pattern_imminent_engine"
    low_repeatability_signature = "low_repeatability_signature"
    excessive_extension_or_blowoff = "excessive_extension_or_blowoff"
    execution_slippage_risk = "execution_slippage_risk"
    event_or_flow_driven = "event_or_flow_driven"
    weak_structural_confirmation = "weak_structural_confirmation"
    high_blowoff_risk = "high_blowoff_risk"
    poor_liquidity_proxy = "poor_liquidity_proxy"


REASON_TEXT: dict[str, str] = {
    ReasonCode.not_pattern_imminent_engine.value: (
        "Core Tier A/B requires a promoted/live ScanPattern evaluated through the imminent "
        "repeatable-edge engine — this row is scanner/context only."
    ),
    ReasonCode.low_repeatability_signature.value: (
        "Low repeatability signature: explosive scanner narratives rarely match the core "
        "backtested pattern library."
    ),
    ReasonCode.excessive_extension_or_blowoff.value: (
        "Extension or blow-off profile suggests late-stage chase risk relative to the core entry model."
    ),
    ReasonCode.execution_slippage_risk.value: (
        "Execution/slippage risk is elevated — stops may not fill where modeled."
    ),
    ReasonCode.event_or_flow_driven.value: (
        "Event- or flow-driven impulse — outside the core structural repeatability thesis."
    ),
    ReasonCode.weak_structural_confirmation.value: (
        "Weak structural confirmation versus the core pattern-imminent bar."
    ),
    ReasonCode.high_blowoff_risk.value: (
        "Blow-off / exhaustion signals dominate — not a core-quality entry."
    ),
    ReasonCode.poor_liquidity_proxy.value: (
        "Liquidity/flow stress proxy is poor (volume spike + high risk flag)."
    ),
}
