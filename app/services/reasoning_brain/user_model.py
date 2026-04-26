from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from ... import memory as memory_module
from ... import personality as personality_module
from ... import openai_client
from ...logger import log_info
from ...models import (
    ChatMessage,
    LearningEvent,
    ReasoningUserModel,
)


def _collect_signals(user_id: int, db: Session) -> dict:
    """Gather raw signals from memories, personality, chat, and trading brain."""
    memories = memory_module.get_memory_context(user_id, db) or ""
    personality_ctx = personality_module.get_profile_context(user_id, db) or ""

    recent_msgs = (
        db.query(ChatMessage)
        .filter(ChatMessage.convo_key == f"user:{user_id}")
        .order_by(ChatMessage.id.desc())
        .limit(40)
        .all()
    )
    messages_text = "\n".join(
        f"{m.role.upper()}: {m.content}" for m in reversed(recent_msgs)
    )

    # Trading learning events as a proxy for behaviour/risk.
    trading_events = (
        db.query(LearningEvent)
        .order_by(LearningEvent.created_at.desc())
        .limit(50)
        .all()
    )
    trading_text = "\n".join(
        f"- [{e.event_type}] {e.description}" for e in trading_events
    )

    return {
        "memories": memories,
        "personality": personality_ctx,
        "recent_messages": messages_text,
        "trading_events": trading_text,
    }


def _call_llm(signals: dict, trace_id: str) -> Optional[dict]:
    if not openai_client.is_configured():
        return None

    prompt = (
        "You are Chili's Reasoning Brain. Synthesize a structured user reasoning model.\n\n"
        "Use these inputs:\n\n"
        "Memories:\n"
        f"{signals.get('memories') or '(none)'}\n\n"
        "Personality profile:\n"
        f"{signals.get('personality') or '(none)'}\n\n"
        "Recent chat messages:\n"
        f"{signals.get('recent_messages') or '(none)'}\n\n"
        "Trading learning events (behavioural hints):\n"
        f"{signals.get('trading_events') or '(none)'}\n\n"
        "Return ONLY valid JSON with this exact structure:\n"
        "{\n"
        '  "decision_style": "conservative|exploratory|balanced|unsure",\n'
        '  "risk_tolerance": "low|medium|high|unsure",\n'
        '  "communication_prefs": {\n'
        '     "detail_level": "brief|normal|deep",\n'
        '     "tone": "casual|formal|friendly|direct|mixed",\n'
        '     "examples": ["short example of how they like replies"]\n'
        "  },\n"
        '  "active_goals": [\n'
        '     {"area": "trading|coding|life|other", "goal": "text", "horizon": "short|medium|long"}\n'
        "  ],\n"
        '  "knowledge_gaps": [\n'
        '     {"topic": "text", "description": "what they seem unsure about"}\n'
        "  ],\n"
        '  "source_memory_count": 0\n'
        "}\n\n"
        "Be concise but specific. Infer carefully; if unsure, use 'unsure' or empty arrays.\n"
    )

    try:
        from ..context_brain.llm_gateway import gateway_chat
        result = gateway_chat(
            messages=[{"role": "user", "content": prompt}],
            purpose='reasoning_user_model',
            system_prompt="You are a precise user modelling engine. Return only valid JSON.",
            trace_id=trace_id,
        )
    except Exception:
        result = openai_client.chat(
            messages=[{"role": "user", "content": prompt}],
            system_prompt="You are a precise user modelling engine. Return only valid JSON.",
            trace_id=trace_id,
        )
    if not result.get("reply"):
        return None

    text = result["reply"].strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        return json.loads(text[start : end + 1])
    except Exception as e:  # pragma: no cover - defensive
        log_info(trace_id, f"reasoning_user_model_parse_error={e}")
        return None


def synthesize_user_model(db: Session, user_id: int, trace_id: str = "reasoning") -> Optional[ReasoningUserModel]:
    """Create and persist a new ReasoningUserModel snapshot for this user."""
    signals = _collect_signals(user_id, db)
    data = _call_llm(signals, trace_id)
    if not data:
        return None

    # Mark existing models inactive
    db.query(ReasoningUserModel).filter(
        ReasoningUserModel.user_id == user_id,
        ReasoningUserModel.active.is_(True),
    ).update({"active": False})

    model = ReasoningUserModel(
        user_id=user_id,
        decision_style=data.get("decision_style") or None,
        risk_tolerance=data.get("risk_tolerance") or None,
        communication_prefs=json.dumps(data.get("communication_prefs") or {}) or None,
        active_goals=json.dumps(data.get("active_goals") or []) or None,
        knowledge_gaps=json.dumps(data.get("knowledge_gaps") or []) or None,
        source_memory_count=int(data.get("source_memory_count") or 0),
        created_at=datetime.utcnow(),
        active=True,
    )
    db.add(model)
    db.commit()
    db.refresh(model)

    log_info(
        trace_id,
        f"reasoning_user_model_updated user_id={user_id} "
        f"decision_style={model.decision_style!r} risk_tolerance={model.risk_tolerance!r}",
    )
    return model

