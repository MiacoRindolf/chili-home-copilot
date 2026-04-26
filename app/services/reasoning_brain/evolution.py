from __future__ import annotations

import json
from datetime import datetime
from typing import Dict, List

from sqlalchemy.orm import Session

from ... import openai_client
from ...logger import log_info
from ...models import (
    ReasoningConfidenceSnapshot,
    ReasoningHypothesis,
    ReasoningLearningGoal,
    ReasoningUserModel,
)


def snapshot_confidence(db: Session, user_id: int) -> None:
    """Snapshot confidence-like dimensions from the current user model."""
    um = (
        db.query(ReasoningUserModel)
        .filter(ReasoningUserModel.user_id == user_id, ReasoningUserModel.active.is_(True))
        .order_by(ReasoningUserModel.created_at.desc())
        .first()
    )
    if not um:
        return

    dims: Dict[str, float] = {}
    # Heuristic: map categorical fields to numeric confidences for visualization.
    if um.decision_style:
        dims["decision_style"] = 0.7
    if um.risk_tolerance:
        dims["risk_tolerance"] = 0.7
    try:
        comm = json.loads(um.communication_prefs or "{}")
    except Exception:
        comm = {}
    if comm:
        dims["communication_prefs"] = 0.7

    now = datetime.utcnow()
    for dim, val in dims.items():
        snap = ReasoningConfidenceSnapshot(
            user_id=user_id,
            dimension=dim,
            confidence_value=val,
            snapshot_date=now,
        )
        db.add(snap)
    db.commit()


def detect_model_drift(db: Session, user_id: int) -> Dict[str, str]:
    """Compare last two user models and report changed dimensions."""
    models: List[ReasoningUserModel] = (
        db.query(ReasoningUserModel)
        .filter(ReasoningUserModel.user_id == user_id)
        .order_by(ReasoningUserModel.created_at.desc())
        .limit(2)
        .all()
    )
    if len(models) < 2:
        return {}
    newest, prev = models[0], models[1]
    drift: Dict[str, str] = {}
    if newest.decision_style != prev.decision_style:
        drift["decision_style"] = f"{prev.decision_style!r} -> {newest.decision_style!r}"
    if newest.risk_tolerance != prev.risk_tolerance:
        drift["risk_tolerance"] = f"{prev.risk_tolerance!r} -> {newest.risk_tolerance!r}"
    if newest.communication_prefs != prev.communication_prefs:
        drift["communication_prefs"] = "communication preferences changed"
    return drift


def generate_hypotheses(db: Session, user_id: int) -> List[ReasoningHypothesis]:
    """Use LLM to generate testable hypotheses about the user."""
    if not openai_client.is_configured():
        return []

    um = (
        db.query(ReasoningUserModel)
        .filter(ReasoningUserModel.user_id == user_id, ReasoningUserModel.active.is_(True))
        .order_by(ReasoningUserModel.created_at.desc())
        .first()
    )
    if not um:
        return []

    try:
        goals = json.loads(um.active_goals or "[]")
        gaps = json.loads(um.knowledge_gaps or "[]")
    except Exception:
        goals, gaps = [], []

    prompt = (
        "You are Chili's Reasoning Brain. Propose 3-5 concrete, testable hypotheses "
        "about this user that Chili should validate over time.\n\n"
        f"Decision style: {um.decision_style or 'unknown'}\n"
        f"Risk tolerance: {um.risk_tolerance or 'unknown'}\n"
        f"Active goals JSON: {json.dumps(goals)}\n"
        f"Knowledge gaps JSON: {json.dumps(gaps)}\n\n"
        "Return ONLY valid JSON array like:\n"
        '[{"claim": "...", "domain": "trading|code|general|life|other"}]\n'
    )

    try:
        from ..context_brain.llm_gateway import gateway_chat
        result = gateway_chat(
            messages=[{"role": "user", "content": prompt}],
            purpose='reasoning_evolve',
            system_prompt="You are a disciplined hypothesis generator. Return only JSON.",
            trace_id="reasoning_hypotheses",
        )
    except Exception:
        result = openai_client.chat(
            messages=[{"role": "user", "content": prompt}],
            system_prompt="You are a disciplined hypothesis generator. Return only JSON.",
            trace_id="reasoning_hypotheses",
        )
    if not result.get("reply"):
        return []

    text = result["reply"].strip()
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1:
        return []
    try:
        data = json.loads(text[start : end + 1])
    except Exception as e:
        log_info("reasoning_hypotheses", f"parse_error={e}")
        return []

    out: List[ReasoningHypothesis] = []
    for item in data or []:
        claim = (item.get("claim") or "").strip()
        if not claim:
            continue
        domain = (item.get("domain") or "general").strip()
        hyp = ReasoningHypothesis(
            user_id=user_id,
            claim=claim,
            domain=domain,
            confidence=0.5,
            evidence_for=0,
            evidence_against=0,
            created_at=datetime.utcnow(),
            active=True,
        )
        db.add(hyp)
        out.append(hyp)
    db.commit()
    return out


def test_hypotheses(db: Session, user_id: int) -> None:
    """Very lightweight hypothesis testing placeholder.

    For now we treat recent model changes as weak evidence for/against.
    """
    drift = detect_model_drift(db, user_id)
    if not drift:
        return

    active_hyps: List[ReasoningHypothesis] = (
        db.query(ReasoningHypothesis)
        .filter(ReasoningHypothesis.user_id == user_id, ReasoningHypothesis.active.is_(True))
        .limit(20)
        .all()
    )
    if not active_hyps:
        return

    for hyp in active_hyps:
        # Heuristic: if drift mentions a dimension referenced in the claim, mark as needing more evidence
        text = hyp.claim.lower()
        touched = any(dim in text for dim in drift.keys())
        if touched:
            hyp.evidence_for += 1
        else:
            hyp.evidence_against += 1
        total = hyp.evidence_for + hyp.evidence_against
        if total > 0:
            hyp.confidence = max(0.1, min(0.95, hyp.evidence_for / total))
        hyp.tested_at = datetime.utcnow()
        if total >= 5 and hyp.confidence < 0.3:
            hyp.active = False
        db.add(hyp)
    db.commit()


def generate_learning_goals(db: Session, user_id: int) -> None:
    """Create or refresh ReasoningLearningGoal rows from gaps, hypotheses, and drift."""
    um = (
        db.query(ReasoningUserModel)
        .filter(ReasoningUserModel.user_id == user_id, ReasoningUserModel.active.is_(True))
        .order_by(ReasoningUserModel.created_at.desc())
        .first()
    )
    if not um:
        return

    try:
        gaps = json.loads(um.knowledge_gaps or "[]")
    except Exception:
        gaps = []

    existing_dims = {
        g.dimension
        for g in db.query(ReasoningLearningGoal).filter(
            ReasoningLearningGoal.user_id == user_id,
            ReasoningLearningGoal.status.in_(["pending", "active"]),
        )
    }

    # From knowledge gaps
    for gap in gaps:
        topic = gap.get("topic") or "general"
        if topic in existing_dims:
            continue
        desc = gap.get("description") or f"Learn more about {topic}"
        goal = ReasoningLearningGoal(
            user_id=user_id,
            dimension=topic,
            description=desc,
            status="pending",
            created_at=datetime.utcnow(),
        )
        db.add(goal)

    db.commit()

