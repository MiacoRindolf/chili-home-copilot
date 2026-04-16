"""Shared self-graduating intelligence lifecycle for all mesh decision nodes.

Stages:
  bootstrap  -> teacher LLM decides; mechanical rules shadow & learn
  shadow     -> mechanical rules decide; teacher validates a sample
  graduated  -> mechanical rules alone; periodic drift checks
  demoted    -> accuracy drop → back to bootstrap

This module provides the lifecycle math and stage transitions. Each node
stores its graduation state in ``BrainNodeState.local_state["graduation"]``.
"""
from __future__ import annotations

import logging
import random
from typing import Any, Optional

_log = logging.getLogger(__name__)

# ── Default thresholds (nodes may override) ────────────────────────

DEFAULT_THRESHOLDS = {
    "bootstrap_min_samples": 15,
    "shadow_min_samples": 40,
    "graduation_agreement_min": 0.82,
    "graduation_benefit_min": 0.60,
    "regression_benefit_floor": 0.45,
    "regression_window": 12,
    "shadow_llm_sample_rate": 0.20,
    "graduated_llm_sample_rate": 0.05,
}


def empty_graduation_state() -> dict[str, Any]:
    return {
        "stage": "bootstrap",
        "sample_count": 0,
        "agreement_count": 0,
        "benefit_count": 0,
        "llm_calls_total": 0,
        "mechanical_correct": 0,
        "last_retrained": None,
        "rolling_benefit": None,
    }


def compute_stage(
    grad: dict[str, Any],
    thresholds: Optional[dict[str, Any]] = None,
) -> str:
    """Compute the graduation stage from accumulated statistics."""
    t = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    samples = grad.get("sample_count", 0)

    if samples < t["bootstrap_min_samples"]:
        return "bootstrap"

    agreement_rate = grad.get("agreement_count", 0) / max(samples, 1)
    benefit_rate = grad.get("benefit_count", 0) / max(samples, 1)
    current = grad.get("stage", "bootstrap")

    if current == "graduated":
        rolling = grad.get("rolling_benefit", benefit_rate)
        if rolling < t["regression_benefit_floor"]:
            return "demoted"
        return "graduated"

    if samples >= t["shadow_min_samples"]:
        if agreement_rate >= t["graduation_agreement_min"] and benefit_rate >= t["graduation_benefit_min"]:
            return "graduated"
        if benefit_rate < 0.40:
            return "demoted"

    if samples >= t["bootstrap_min_samples"]:
        return "shadow"

    return "bootstrap"


def should_call_teacher(stage: str, thresholds: Optional[dict[str, Any]] = None) -> bool:
    """Determine whether to call the teacher LLM this cycle."""
    t = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    if stage == "bootstrap":
        return True
    if stage == "shadow":
        return random.random() < t["shadow_llm_sample_rate"]
    if stage == "graduated":
        return random.random() < t["graduated_llm_sample_rate"]
    return True  # demoted -> always teacher


def update_stats(
    grad: dict[str, Any],
    mechanical_decision: dict[str, Any],
    teacher_decision: dict[str, Any],
    *,
    action_key: str = "action",
    urgency_key: str = "urgency",
    confidence_key: str = "confidence",
) -> None:
    """Compare mechanical vs teacher, update agreement/benefit counters."""
    grad["sample_count"] = grad.get("sample_count", 0) + 1
    grad["llm_calls_total"] = grad.get("llm_calls_total", 0) + 1

    mech_action = mechanical_decision.get(action_key, "hold")
    teach_action = teacher_decision.get(action_key, "hold")
    mech_urgency = mechanical_decision.get(urgency_key, "none")
    teach_urgency = teacher_decision.get(urgency_key, "none")

    if mech_action == teach_action or mech_urgency == teach_urgency:
        grad["agreement_count"] = grad.get("agreement_count", 0) + 1

    mech_conf = float(mechanical_decision.get(confidence_key, 0.5))
    teach_conf = float(teacher_decision.get(confidence_key, 0.5))
    if mech_conf >= teach_conf * 0.85:
        grad["benefit_count"] = grad.get("benefit_count", 0) + 1
        grad["mechanical_correct"] = grad.get("mechanical_correct", 0) + 1


def graduation_summary(grad: dict[str, Any]) -> dict[str, Any]:
    """Return a human-readable summary of graduation state."""
    samples = grad.get("sample_count", 0)
    return {
        "stage": grad.get("stage", "bootstrap"),
        "samples": samples,
        "agreement_rate": round(grad.get("agreement_count", 0) / max(samples, 1), 3),
        "benefit_rate": round(grad.get("benefit_count", 0) / max(samples, 1), 3),
        "llm_calls_total": grad.get("llm_calls_total", 0),
        "mechanical_correct": grad.get("mechanical_correct", 0),
    }
