"""Decide whether a candidate distilled model becomes the new tier-1.

Mirrors app.services.trading.pattern_regime_promotion_service.evaluate_promotion
shape:
  - never auto-promote on a regression
  - hard rule: zero failures on the golden 10-task subset
  - soft rule: candidate_pass >= incumbent_pass AND latency within 20%

Decision is one of: 'promote' | 'reject' | 'shadow' (mirror trading shadow mode).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class PromotionDecision:
    decision: str           # 'promote' | 'reject' | 'shadow'
    reason: str
    incumbent_pass: float
    candidate_pass: float


def ensemble_code_promotion_check(
    *,
    incumbent_pass: float,
    candidate_pass: float,
    incumbent_p50_ms: int,
    candidate_p50_ms: int,
    golden_failures: int,
    shadow_required_cycles: int = 2,
    cycles_in_shadow: int = 0,
) -> PromotionDecision:
    if golden_failures > 0:
        return PromotionDecision(
            decision="reject",
            reason=f"golden_failures={golden_failures} (zero tolerance)",
            incumbent_pass=incumbent_pass,
            candidate_pass=candidate_pass,
        )

    if candidate_pass < incumbent_pass:
        return PromotionDecision(
            decision="reject",
            reason=f"pass_regression candidate={candidate_pass:.3f} < incumbent={incumbent_pass:.3f}",
            incumbent_pass=incumbent_pass,
            candidate_pass=candidate_pass,
        )

    latency_budget = max(int(incumbent_p50_ms * 1.2), incumbent_p50_ms + 250)
    if candidate_p50_ms > latency_budget:
        return PromotionDecision(
            decision="reject",
            reason=f"latency_regression candidate={candidate_p50_ms}ms > budget={latency_budget}ms",
            incumbent_pass=incumbent_pass,
            candidate_pass=candidate_pass,
        )

    if cycles_in_shadow < shadow_required_cycles:
        return PromotionDecision(
            decision="shadow",
            reason=f"shadow {cycles_in_shadow + 1}/{shadow_required_cycles}",
            incumbent_pass=incumbent_pass,
            candidate_pass=candidate_pass,
        )

    return PromotionDecision(
        decision="promote",
        reason="all_gates_passed",
        incumbent_pass=incumbent_pass,
        candidate_pass=candidate_pass,
    )
