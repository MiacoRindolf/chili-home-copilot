from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Iterable, Optional

from sqlalchemy.orm import Session

from ...logger import log_info
from ...models import (
    ChatMessage,
    ReasoningAnticipation,
    ReasoningInterest,
    ReasoningUserModel,
)

_VALID_DOMAINS = {"trading", "code", "general", "life", "other"}


def _current_user_model(db: Session, user_id: int) -> Optional[ReasoningUserModel]:
    return (
        db.query(ReasoningUserModel)
        .filter(ReasoningUserModel.user_id == user_id, ReasoningUserModel.active.is_(True))
        .order_by(ReasoningUserModel.created_at.desc())
        .first()
    )


def _json_list(raw: str | None) -> list[Any]:
    try:
        data = json.loads(raw or "[]")
    except Exception:
        return []
    return data if isinstance(data, list) else []


def _goal_text(goal: Any) -> str:
    if isinstance(goal, dict):
        return str(goal.get("goal") or goal.get("description") or goal.get("topic") or "").strip()
    return str(goal or "").strip()


def _goal_domain(goal: Any, goal_text: str) -> str:
    area = str(goal.get("area") or "").strip().lower() if isinstance(goal, dict) else ""
    lower = f"{area} {goal_text}".lower()
    if "trading" in lower or "trade" in lower or "portfolio" in lower or "option" in lower or "crypto" in lower:
        return "trading"
    if "coding" in lower or "code" in lower or "software" in lower or "project" in lower or "repo" in lower:
        return "code"
    if "life" in lower or "routine" in lower or "health" in lower or "home" in lower:
        return "life"
    if area in _VALID_DOMAINS:
        return area
    return "general"


def _mechanical_anticipation_items(model: ReasoningUserModel, *, max_items: int = 3) -> list[dict]:
    """Turn explicit active goals into anticipations without an LLM pass."""
    items: list[dict] = []
    seen: set[str] = set()
    for goal in _json_list(getattr(model, "active_goals", None)):
        goal_text = _goal_text(goal)
        if not goal_text:
            continue
        domain = _goal_domain(goal, goal_text)
        key = f"{domain}:{goal_text.casefold()}"
        if key in seen:
            continue
        seen.add(key)

        if domain == "trading":
            description = f"Prepare a concise trading risk check for {goal_text}."
        elif domain == "code":
            description = f"Keep recent project context ready for {goal_text}."
        elif domain == "life":
            description = f"Watch for a practical routine support angle around {goal_text}."
        else:
            description = f"Watch for chances to help with {goal_text}."

        items.append({
            "description": description,
            "domain": domain,
            "confidence": 0.72,
            "context": {
                "why": "derived_from_active_goal",
                "goal": goal_text,
                "horizon": goal.get("horizon") if isinstance(goal, dict) else None,
            },
        })
        if len(items) >= max_items:
            break
    return items


def _existing_pending_descriptions(db: Session, user_id: int) -> set[str]:
    try:
        rows = (
            db.query(ReasoningAnticipation)
            .filter(
                ReasoningAnticipation.user_id == user_id,
                ReasoningAnticipation.dismissed.is_(False),
                ReasoningAnticipation.acted_on.is_(False),
            )
            .all()
        )
    except Exception:
        return set()
    return {
        str(getattr(row, "description", "") or "").strip().casefold()
        for row in rows
        if str(getattr(row, "description", "") or "").strip()
    }


def _store_anticipations(
    db: Session,
    *,
    user_id: int,
    items: list[dict],
    trace_id: str,
    source: str,
) -> list[ReasoningAnticipation]:
    anticipations: list[ReasoningAnticipation] = []
    existing = _existing_pending_descriptions(db, user_id)
    seen = set(existing)
    now = datetime.utcnow()
    for item in items or []:
        desc = (item.get("description") or "").strip()
        if not desc:
            continue
        key = desc.casefold()
        if key in seen:
            continue
        seen.add(key)
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

    if anticipations:
        db.commit()
        log_info(trace_id, f"reasoning_anticipations_saved source={source} user_id={user_id} count={len(anticipations)}")
    return anticipations


def generate_anticipations(db: Session, user_id: int, trace_id: str = "reasoning") -> list[ReasoningAnticipation]:
    """Analyze user model + interests + recent activity to predict likely needs."""
    model = _current_user_model(db, user_id)
    if not model:
        return []

    mechanical_items = _mechanical_anticipation_items(model)
    if mechanical_items:
        return _store_anticipations(
            db,
            user_id=user_id,
            items=mechanical_items,
            trace_id=trace_id,
            source="mechanical",
        )

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
    except Exception as e:
        log_info(trace_id, f"reasoning_anticipate_gateway_error={e}")
        return []
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

    return _store_anticipations(
        db,
        user_id=user_id,
        items=data or [],
        trace_id=trace_id,
        source="gateway",
    )

