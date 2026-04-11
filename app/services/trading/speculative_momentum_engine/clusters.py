"""Derive speculative cluster from node activations (ordered decision table)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .nodes import NodeActivation
from .schema import (
    NODE_EVENT_IMPULSE,
    NODE_EXECUTION_RISK,
    NODE_EXHAUSTION,
    NODE_EXTENSION_RISK,
    NODE_SQUEEZE_PRESSURE,
    NODE_VOLUME_EXPANSION,
    NODE_VWAP_PULLBACK,
    ClusterId,
)


def _by_id(acts: list[NodeActivation]) -> dict[str, float]:
    return {a.node_id: a.score for a in acts}


@dataclass(frozen=True)
class ClusterResolution:
    cluster_id: str
    rationale: str

    def to_dict(self) -> dict[str, Any]:
        return {"cluster_id": self.cluster_id, "rationale": self.rationale}


def resolve_cluster(acts: list[NodeActivation], *, scanner_score: float) -> ClusterResolution:
    """Priority-ordered rules; first match wins."""
    m = _by_id(acts)
    ext = m.get(NODE_EXTENSION_RISK, 0.0)
    exe = m.get(NODE_EXECUTION_RISK, 0.0)
    sq = m.get(NODE_SQUEEZE_PRESSURE, 0.0)
    ev = m.get(NODE_EVENT_IMPULSE, 0.0)
    vol = m.get(NODE_VOLUME_EXPANSION, 0.0)
    vwap = m.get(NODE_VWAP_PULLBACK, 0.0)
    exh = m.get(NODE_EXHAUSTION, 0.0)

    # Blow-off: strong extension + high score or exhaustion co-fired
    if ext >= 0.72 and scanner_score >= 8.0:
        return ClusterResolution(
            ClusterId.blow_off_risk.value,
            "extension_risk_high_with_elevated_scanner_score",
        )
    if ext >= 0.6 and exh >= 0.5:
        return ClusterResolution(
            ClusterId.blow_off_risk.value,
            "extension_with_exhaustion_language",
        )

    # Severe execution / liquidity stress must not hide behind squeeze/event labels.
    if exe >= 0.85 and (sq >= 0.45 or ev >= 0.45):
        return ClusterResolution(
            ClusterId.execution_risk_high.value,
            "severe_execution_stress_over_thematic_label",
        )

    if ext >= 0.55:
        return ClusterResolution(ClusterId.too_extended.value, "extension_risk_dominant")

    # First pullback: structure cue without dominant extension
    if vwap >= 0.45 and ext < 0.5:
        return ClusterResolution(
            ClusterId.first_pullback_candidate.value,
            "vwap_pullback_signal_without_strong_extension",
        )

    if sq >= 0.45:
        return ClusterResolution(ClusterId.speculative_squeeze.value, "squeeze_halt_pressure")

    if ev >= 0.45:
        return ClusterResolution(ClusterId.event_driven_spike.value, "event_flow_impulse")

    if exe >= 0.65:
        return ClusterResolution(ClusterId.execution_risk_high.value, "execution_liquidity_stress")

    if vol >= 0.5 and scanner_score >= 7.5:
        return ClusterResolution(
            ClusterId.structured_momentum.value,
            "volume_expansion_with_strong_scanner_score",
        )

    return ClusterResolution(ClusterId.watch_only.value, "no_dominant_cluster_signal")
