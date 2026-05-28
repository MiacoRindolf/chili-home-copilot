from __future__ import annotations

import json
import re
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

_MEMORY_LINE_RE = re.compile(r"^\s*-\s*\[(?P<category>[^\]]+)\]\s*(?P<content>.+?)\s*$")
_GOAL_PREFIX_RE = re.compile(r"^(?:goal\s*:\s*|wants?\s+to\s+|plans?\s+to\s+)", re.IGNORECASE)


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


def _memory_rows(memories: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for line in (memories or "").splitlines():
        match = _MEMORY_LINE_RE.match(line)
        if match:
            rows.append({
                "category": match.group("category").strip().lower(),
                "content": match.group("content").strip(),
            })
    return rows


def _goal_area(text: str) -> str:
    lower = text.lower()
    if any(word in lower for word in ("trade", "trading", "option", "stock", "crypto", "portfolio")):
        return "trading"
    if any(word in lower for word in ("code", "coding", "app", "software", "python", "project", "repo")):
        return "coding"
    if any(word in lower for word in ("health", "family", "home", "routine", "sleep", "fitness")):
        return "life"
    return "other"


def _goal_horizon(text: str) -> str:
    lower = text.lower()
    if any(word in lower for word in ("today", "tomorrow", "this week", "soon", "next few days")):
        return "short"
    if any(word in lower for word in ("year", "long term", "long-term", "eventually")):
        return "long"
    return "medium"


def _mechanical_user_model(signals: dict) -> Optional[dict]:
    """Build a conservative model from explicit memories/profile facts.

    This avoids paying an LLM to restate structured facts CHILI already
    extracted. Ambiguous dimensions stay "unsure" instead of being invented.
    """
    memories = str(signals.get("memories") or "")
    personality = str(signals.get("personality") or "")
    recent = str(signals.get("recent_messages") or "")
    trading = str(signals.get("trading_events") or "")
    rows = _memory_rows(memories)
    if not rows and not personality.strip():
        return None

    combined = " ".join([memories, personality, recent, trading]).lower()

    if any(word in combined for word in ("aggressive", "high risk", "big upside", "risk-on")):
        risk_tolerance = "high"
    elif any(word in combined for word in ("conservative", "careful", "cautious", "safe", "avoid risk", "risk off", "risk-off")):
        risk_tolerance = "low"
    elif "trading" in combined or "portfolio" in combined or "option" in combined:
        risk_tolerance = "medium"
    else:
        risk_tolerance = "unsure"

    if any(word in combined for word in ("experiment", "explore", "try things", "prototype")):
        decision_style = "exploratory"
    elif any(word in combined for word in ("conservative", "careful", "cautious", "safe")):
        decision_style = "conservative"
    elif any(word in combined for word in ("compare", "plan", "prioritize", "tradeoff", "trade-off")):
        decision_style = "balanced"
    else:
        decision_style = "unsure"

    detail_level = "normal"
    if any(word in combined for word in ("brief", "concise", "short version", "quick version")):
        detail_level = "brief"
    elif any(word in combined for word in ("deep dive", "deeper context", "detailed", "explain why")):
        detail_level = "deep"

    tone = "mixed"
    for candidate in ("casual", "formal", "friendly", "direct"):
        if candidate in combined:
            tone = candidate
            break

    examples = [
        row["content"]
        for row in rows
        if row["category"] == "preference" and any(word in row["content"].lower() for word in ("reply", "tone", "brief", "detail"))
    ][:2]

    active_goals = []
    seen_goals: set[str] = set()
    for row in rows:
        if row["category"] != "goal":
            continue
        goal_text = _GOAL_PREFIX_RE.sub("", row["content"]).strip(" .")
        if not goal_text:
            continue
        key = goal_text.casefold()
        if key in seen_goals:
            continue
        seen_goals.add(key)
        active_goals.append({
            "area": _goal_area(goal_text),
            "goal": goal_text,
            "horizon": _goal_horizon(goal_text),
        })

    has_specific_signal = bool(
        active_goals
        or examples
        or risk_tolerance != "unsure"
        or decision_style != "unsure"
        or detail_level != "normal"
        or tone != "mixed"
        or len(rows) >= 2
    )
    if not has_specific_signal:
        return None

    return {
        "decision_style": decision_style,
        "risk_tolerance": risk_tolerance,
        "communication_prefs": {
            "detail_level": detail_level,
            "tone": tone,
            "examples": examples,
        },
        "active_goals": active_goals,
        "knowledge_gaps": [],
        "source_memory_count": len(rows),
        "_mechanical": True,
    }


def _call_llm(signals: dict, trace_id: str) -> Optional[dict]:
    mechanical = _mechanical_user_model(signals)
    if mechanical:
        log_info(
            trace_id,
            "reasoning_user_model_mechanical "
            f"memories={mechanical.get('source_memory_count', 0)} "
            f"goals={len(mechanical.get('active_goals') or [])}",
        )
        return mechanical

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
    except Exception as e:
        log_info(trace_id, f"reasoning_user_model_gateway_error={e}")
        return None
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

