"""Speculative scoring plane (orthogonal to core-edge composite)."""

from __future__ import annotations

from typing import Any

from .clusters import ClusterResolution
from .features import SignalFeatures
from .nodes import NodeActivation
from .schema import (
    NODE_EVENT_IMPULSE,
    NODE_EXHAUSTION,
    NODE_EXECUTION_RISK,
    NODE_EXTENSION_RISK,
    NODE_SQUEEZE_PRESSURE,
    NODE_VOLUME_EXPANSION,
    NODE_VWAP_PULLBACK,
    ClusterId,
)


def _m(acts: list[NodeActivation]) -> dict[str, float]:
    return {a.node_id: a.score for a in acts}


def build_scoring_plane(
    f: SignalFeatures,
    acts: list[NodeActivation],
    cluster: ClusterResolution,
) -> dict[str, Any]:
    m = _m(acts)
    base_momo = min(1.0, max(0.0, f.scanner_score / 10.0))
    lexical_boost = 0.08 * m.get(NODE_SQUEEZE_PRESSURE, 0.0)
    lexical_boost += 0.07 * m.get(NODE_VOLUME_EXPANSION, 0.0)
    lexical_boost += 0.06 * m.get(NODE_EVENT_IMPULSE, 0.0)
    speculative_momentum_score = round(min(1.0, base_momo * 0.52 + lexical_boost), 4)

    extension_risk = round(min(1.0, 0.2 + 0.85 * m.get(NODE_EXTENSION_RISK, 0.0)), 4)
    execution_risk = round(min(1.0, m.get(NODE_EXECUTION_RISK, 0.0)), 4)
    blowoff_risk = round(
        min(
            1.0,
            0.35 * m.get(NODE_EXTENSION_RISK, 0.0)
            + 0.4 * m.get(NODE_EXHAUSTION, 0.0)
            + (0.15 if cluster.cluster_id == ClusterId.blow_off_risk.value else 0.0),
        ),
        4,
    )

    structural_confirmation = round(
        min(
            1.0,
            0.28
            + 0.22 * m.get(NODE_VWAP_PULLBACK, 0.0)
            + 0.18 * m.get(NODE_VOLUME_EXPANSION, 0.0),
        ),
        4,
    )

    # Proxy: tighter liquidity when execution risk high and vol spike
    liq = 0.55
    if execution_risk >= 0.55:
        liq -= 0.25
    if m.get(NODE_VOLUME_EXPANSION, 0.0) >= 0.85:
        liq -= 0.12
    liquidity_quality = round(max(0.0, min(1.0, liq)), 4)

    # Continuation vs exhaustion tension
    continuation_quality = round(
        max(0.0, min(1.0, 0.5 + 0.35 * speculative_momentum_score - 0.45 * m.get(NODE_EXHAUSTION, 0.0))),
        4,
    )

    repeatability_confidence = round(0.22 + 0.08 * min(1.0, f.scanner_score / 10.0), 4)

    core_edge_score = round(max(0.0, (1.0 - speculative_momentum_score) * structural_confirmation), 4)

    return {
        "speculative_momentum_score": speculative_momentum_score,
        "core_edge_score": core_edge_score,
        "extension_risk": extension_risk,
        "execution_risk": execution_risk,
        "blowoff_risk": blowoff_risk,
        "repeatability_confidence": repeatability_confidence,
        "structural_confirmation": structural_confirmation,
        "spread_liquidity_quality": None,
        "liquidity_quality": liquidity_quality,
        "continuation_quality": continuation_quality,
    }
