from __future__ import annotations

import json
from datetime import datetime
from typing import Iterable, Optional

from sqlalchemy.orm import Session

from ...logger import log_info
from ...models import (
    ChatMessage,
    ReasoningAnticipation,
    ReasoningInterest,
    ReasoningUserModel,
)


def _current_user_model(db: Session, user_id: int) -> Optional[ReasoningUserModel]:
    return (
        db.query(ReasoningUserModel)
        .filter(ReasoningUserModel.user_id == user_id, ReasoningUserModel.active.is_(True))
        .order_by(ReasoningUserModel.created_at.desc())
        .first()
    )


def generate_anticipations(db: Session, user_id: int, trace_id: str = "reasoning") -> list[ReasoningAnticipation]:
    """Analyze user model + interests + recent activity to predict likely needs."""
    model = _current_user_model(db, user_id)
    if not model:
        return []

    interests: Iterable[ReasoningInterest] = (
        db.query(ReasoningInterest)
        .filter(ReasoningInterest.user_id == user_id, ReasoningInterest.active.is_(True))
        .order_by(ReasoningInterest.weight.desc())
        .limit(30)
        .all()
    )
    recent_msgs: Iterable[ChatMessage] = (
        db.query(ChatMessage)
        .filter(ChatMessage.convo_key == f"user:{user_id}")
        .order_by(ChatMessage.id.desc())
        .limit(40)
        .all()
    )

    interests_text = ", ".join(i.topic for i in interests)
    convo_text = "\n".join(f"{m.role.upper()}: {m.content}" for m in reversed(list(recent_msgs)))

    try:
        comm_prefs = json.loads(model.communication_prefs or "{}")
    except Exception:
        comm_prefs = {}

    prompt = (
        "You are Chili's anticipatory Reasoning Brain.\n\n"
        "Based on this user model, infer a small set of concrete anticipations:\n\n"
        f"Decision style: {model.decision_style or 'unknown'}\n"
        f"Risk tolerance: {model.risk_tolerance or 'unknown'}\n"
        f"Communication prefs JSON: {json.dumps(comm_prefs)}\n"
        f"Active goals JSON: {model.active_goals or '[]'}\n"
        f"Knowledge gaps JSON: {model.knowledge_gaps or '[]'}\n\n"
        f"Top interests: {interests_text or '(none)'}\n\n"
        "Recent conversation:\n"
        f"{convo_text or '(none)'}\n\n"
        "Return ONLY valid JSON with this structure:\n"
        "[\n"
        '  {\n'
        '    "description": "what Chili should prepare or offer soon",\n'
        '    "domain": "trading|code|general|life|other",\n'
        '    "confidence": 0.0,\n'
        '    "context": {"why": "short explanation"}\n'
        "  }\n"
        "]\n"
    )

    from ... import openai_client

    if not openai_client.is_configured():
        return []

    try:
        from ..context_brain.llm_gateway import gateway_chat
        result = gateway_chat(
            messages=[{"role": "user", "content": prompt}],
            purpose='reasoning_anticipate',
            system_prompt="You are a disciplined anticipatory assistant. Return only valid JSON array.",
            trace_id=trace_id,
        )
    except Exception:
        result = openai_client.chat(
            messages=[{"role": "user", "content": prompt}],
            system_prompt="You are a disciplined anticipatory assistant. Return only valid JSON array.",
            trace_id=trace_id,
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
    except Exception as e:  # pragma: no cover
        log_info(trace_id, f"reasoning_anticipations_parse_error={e}")
        return []

    anticipations: list[ReasoningAnticipation] = []
    now = datetime.utcnow()
    for item in data or []:
        desc = (item.get("description") or "").strip()
        if not desc:
            continue
        domain = (item.get("domain") or "general").strip()
        conf = float(item.get("confidence") or 0.5)
        ctx = item.get("context") or {}
        row = ReasoningAnticipation(
            user_id=user_id,
            description=desc,
            domain=domain,
            context=json.dumps(ctx, ensure_ascii=False),
            confidence=conf,
            created_at=now,
            acted_on=False,
            dismissed=False,
        )
        db.add(row)
        anticipations.append(row)

    db.commit()
    log_info(trace_id, f"reasoning_anticipations_saved user_id={user_id} count={len(anticipations)}")
    return anticipations

