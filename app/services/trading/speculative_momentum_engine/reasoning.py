"""Structured non-promotion reasons (codes + human text)."""

from __future__ import annotations

from typing import Any

from .clusters import ClusterResolution
from .schema import ReasonCode, REASON_TEXT, ClusterId


def build_non_promotion(
    *,
    scores: dict[str, Any],
    cluster: ClusterResolution,
) -> tuple[list[str], list[str], dict[str, Any]]:
    """Returns (codes, lines, structured non_promotion dict)."""
    codes: list[str] = []
    details: list[dict[str, str]] = []

    def add(code: str) -> None:
        if code in codes:
            return
        codes.append(code)
        text = REASON_TEXT.get(code, code)
        details.append({"code": code, "text": text})

    add(ReasonCode.not_pattern_imminent_engine.value)

    if scores.get("repeatability_confidence", 1) < 0.45:
        add(ReasonCode.low_repeatability_signature.value)

    if scores.get("extension_risk", 0) >= 0.55 or cluster.cluster_id in (
        ClusterId.blow_off_risk.value,
        ClusterId.too_extended.value,
    ):
        add(ReasonCode.excessive_extension_or_blowoff.value)

    if scores.get("blowoff_risk", 0) >= 0.55:
        add(ReasonCode.high_blowoff_risk.value)

    if scores.get("execution_risk", 0) >= 0.45:
        add(ReasonCode.execution_slippage_risk.value)

    if cluster.cluster_id == ClusterId.event_driven_spike.value:
        add(ReasonCode.event_or_flow_driven.value)

    if scores.get("structural_confirmation", 1) < 0.42:
        add(ReasonCode.weak_structural_confirmation.value)

    if scores.get("liquidity_quality", 1) < 0.35:
        add(ReasonCode.poor_liquidity_proxy.value)

    lines = [d["text"] for d in details]
    structured = {"codes": list(codes), "details": details}
    return codes, lines, structured


def operator_hint(cluster_id: str, scores: dict[str, Any], vwap_pullback_score: float) -> str:
    if cluster_id == ClusterId.blow_off_risk.value:
        return "Avoid chase — blow-off / exhaustion profile."
    if cluster_id == ClusterId.too_extended.value:
        return "Too extended for sane core entry — watch only."
    if cluster_id == ClusterId.first_pullback_candidate.value and scores.get("extension_risk", 1) < 0.65:
        return "First pullback candidate — still speculative; verify tape."
    if cluster_id == ClusterId.execution_risk_high.value:
        return "Execution risk high — size down or pass."
    if cluster_id == ClusterId.watch_only.value:
        return "Watch only — weak cluster alignment."
    return "Speculative — verify liquidity/spread live."
