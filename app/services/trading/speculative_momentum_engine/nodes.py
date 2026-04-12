"""Per-node activation evaluators (scores align with mesh node IDs)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import features as feat
from .schema import (
    NODE_EVENT_IMPULSE,
    NODE_EXHAUSTION,
    NODE_EXECUTION_RISK,
    NODE_EXTENSION_RISK,
    NODE_SQUEEZE_PRESSURE,
    NODE_VOLUME_EXPANSION,
    NODE_VWAP_PULLBACK,
)


@dataclass(frozen=True)
class NodeActivation:
    node_id: str
    score: float
    evidence: str

    def to_dict(self) -> dict[str, Any]:
        return {"node_id": self.node_id, "score": round(self.score, 4), "evidence": self.evidence}


# Execution risk is never truly zero for speculative momentum plays — there is
# always base slippage/liquidity uncertainty even with neutral scanner signals.
EXECUTION_RISK_BASELINE = 0.25


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


def eval_volume_expansion(f: feat.SignalFeatures) -> NodeActivation:
    s = 0.0
    bits: list[str] = []
    if f.vol_ratio is not None:
        if f.vol_ratio >= 4.0:
            s = max(s, 0.95)
            bits.append(f"volume_ratio≈{f.vol_ratio:.1f}")
        elif f.vol_ratio >= 2.5:
            s = max(s, 0.75)
            bits.append(f"volume_ratio≈{f.vol_ratio:.1f}")
        elif f.vol_ratio >= 1.5:
            s = max(s, 0.45)
            bits.append(f"volume_ratio≈{f.vol_ratio:.1f}")
    if feat.volume_re().search(f.blob):
        s = max(s, 0.55)
        bits.append("lexical_volume_spike")
    return NodeActivation(
        NODE_VOLUME_EXPANSION,
        _clamp(s),
        "; ".join(bits) or "no strong volume expansion signal",
    )


def eval_squeeze_pressure(f: feat.SignalFeatures) -> NodeActivation:
    if not feat.squeeze_re().search(f.blob):
        return NodeActivation(NODE_SQUEEZE_PRESSURE, 0.0, "no squeeze/halt lexicon")
    return NodeActivation(NODE_SQUEEZE_PRESSURE, 0.88, "squeeze/halt/resume language in scanner text")


def eval_event_impulse(f: feat.SignalFeatures) -> NodeActivation:
    if not feat.event_re().search(f.blob):
        return NodeActivation(NODE_EVENT_IMPULSE, 0.0, "no event/catalyst lexicon")
    return NodeActivation(NODE_EVENT_IMPULSE, 0.82, "event/catalyst language in scanner text")


def eval_extension_risk(f: feat.SignalFeatures) -> NodeActivation:
    s = 0.0
    bits: list[str] = []
    if feat.extension_re().search(f.blob):
        s = 0.72
        bits.append("extension/blow-off lexicon")
    # Scanner heat is a weak prior only; lexical/structural extension evidence stays primary.
    scanner_prior = 0.0
    if f.scanner_score >= 8.5:
        scanner_prior = 0.12
        bits.append(f"scanner_score_prior={f.scanner_score:.1f}")
    elif f.scanner_score >= 8.0:
        scanner_prior = 0.06
        bits.append(f"scanner_score_prior={f.scanner_score:.1f}")
    s = _clamp(s + scanner_prior)
    return NodeActivation(
        NODE_EXTENSION_RISK,
        s,
        "; ".join(bits) or "no extension signal",
    )


def eval_execution_risk(f: feat.SignalFeatures) -> NodeActivation:
    s = EXECUTION_RISK_BASELINE
    bits: list[str] = []
    if f.risk_level == "high":
        s += 0.4
        bits.append("scanner_risk_high")
    if f.vol_ratio is not None and f.vol_ratio >= 4.0:
        s += 0.25
        bits.append("extreme_volume_ratio")
    elif f.vol_ratio is not None and f.vol_ratio >= 3.0:
        s += 0.12
        bits.append("high_volume_ratio")
    return NodeActivation(
        NODE_EXECUTION_RISK,
        _clamp(s),
        "; ".join(bits) or "baseline_execution_uncertainty",
    )


def eval_vwap_pullback(f: feat.SignalFeatures) -> NodeActivation:
    if not feat.vwap_pullback_re().search(f.blob):
        return NodeActivation(NODE_VWAP_PULLBACK, 0.0, "no vwap/pullback lexicon")
    return NodeActivation(NODE_VWAP_PULLBACK, 0.68, "vwap/pullback/reclaim language")


def eval_exhaustion(f: feat.SignalFeatures) -> NodeActivation:
    if not feat.exhaustion_re().search(f.blob):
        return NodeActivation(NODE_EXHAUSTION, 0.0, "no exhaustion/failed-continuation lexicon")
    return NodeActivation(NODE_EXHAUSTION, 0.7, "exhaustion/rejection language")


def evaluate_all_nodes(f: feat.SignalFeatures) -> list[NodeActivation]:
    return [
        eval_volume_expansion(f),
        eval_squeeze_pressure(f),
        eval_event_impulse(f),
        eval_extension_risk(f),
        eval_execution_risk(f),
        eval_vwap_pullback(f),
        eval_exhaustion(f),
    ]
