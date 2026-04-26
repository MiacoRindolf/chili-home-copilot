"""Incremental user memory extraction and retrieval.

Extracts personal facts from each conversation turn and stores them
in the UserMemory table. Provides memory context for LLM prompt injection.
"""
import json
from datetime import datetime

from sqlalchemy.orm import Session

from .models import UserMemory, ChatMessage
from . import openai_client
from .logger import log_info

VALID_CATEGORIES = frozenset({
    "interest", "preference", "habit", "event", "person",
    "dietary", "work", "health", "memory", "schedule", "goal",
})

SKIP_ACTION_TYPES = frozenset({
    "list_chores", "list_chores_pending", "mark_chore_done",
    "add_chore", "add_birthday", "list_birthdays",
    "crisis_support", "llm_offline", "guest_blocked",
    "pair_device",
})

EXTRACTION_PROMPT = (
    "From this conversation exchange, extract any personal facts about the user. "
    "Focus on things worth remembering long-term: interests, preferences, habits, "
    "life events, people they mention, dietary info, work/school, health, goals, "
    "schedule patterns, or anything personally meaningful.\n\n"
    "Categories: interest, preference, habit, event, person, dietary, work, health, "
    "memory, schedule, goal\n\n"
    "Rules:\n"
    "- Only extract facts clearly stated or strongly implied by the USER (not the assistant)\n"
    "- Each fact should be a concise, standalone statement (e.g., 'Enjoys hiking on weekends')\n"
    "- Do NOT extract greetings, chore requests, or system commands\n"
    "- Do NOT repeat facts that are just the assistant echoing back\n\n"
    "Return ONLY a JSON array: [{\"category\": \"...\", \"content\": \"...\"}]\n"
    "Return [] if no personal facts are present.\n\n"
)


def _should_extract(action_type: str | None, user_message: str) -> bool:
    """Return False for messages that are unlikely to contain personal facts."""
    if action_type and action_type in SKIP_ACTION_TYPES:
        return False
    msg = user_message.strip().lower()
    if len(msg) < 8:
        return False
    return True


def extract_facts(
    user_message: str,
    assistant_reply: str,
    user_id: int,
    db: Session,
    action_type: str | None = None,
    source_message_id: int | None = None,
    trace_id: str = "memory",
) -> list[dict]:
    """Extract personal facts from a conversation turn and store them.

    Returns list of newly stored facts (may be empty).
    """
    if not openai_client.is_configured():
        return []

    if not _should_extract(action_type, user_message):
        return []

    exchange = f"USER: {user_message}\nASSISTANT: {assistant_reply}"
    prompt = EXTRACTION_PROMPT + exchange

    try:
        try:
            from .services.context_brain.llm_gateway import gateway_chat
            result = gateway_chat(
                messages=[{"role": "user", "content": prompt}],
                purpose='memory_extract',
                system_prompt="You are a fact extraction assistant. Return only valid JSON arrays.",
                trace_id=trace_id,
            )
        except Exception:
            result = openai_client.chat(
                messages=[{"role": "user", "content": prompt}],
                system_prompt="You are a fact extraction assistant. Return only valid JSON arrays.",
                trace_id=trace_id,
            )
    except Exception as e:
        log_info(trace_id, f"memory_extraction_error={e}")
        return []

    if not result.get("reply"):
        return []

    facts = _parse_facts(result["reply"], trace_id)
    if not facts:
        return []

    stored = []
    for fact in facts:
        category = fact.get("category", "").lower().strip()
        content = fact.get("content", "").strip()
        if not content or category not in VALID_CATEGORIES:
            continue

        if _is_duplicate(user_id, content, db):
            log_info(trace_id, f"memory_duplicate_skipped content={content!r}")
            continue

        mem = UserMemory(
            user_id=user_id,
            category=category,
            content=content,
            source_message_id=source_message_id,
        )
        db.add(mem)
        stored.append({"category": category, "content": content})

    if stored:
        db.commit()
        log_info(trace_id, f"memory_stored user_id={user_id} count={len(stored)}")

    return stored


def _parse_facts(text: str, trace_id: str) -> list[dict]:
    """Parse LLM response into a list of fact dicts."""
    text = text.strip()
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1:
        return []
    try:
        parsed = json.loads(text[start:end + 1])
        if isinstance(parsed, list):
            return parsed
    except (json.JSONDecodeError, ValueError) as e:
        log_info(trace_id, f"memory_parse_error={e}")
    return []


def _is_duplicate(user_id: int, content: str, db: Session) -> bool:
    """Check if an identical (case-insensitive) fact already exists for this user."""
    normalized = content.lower().strip()
    existing = (
        db.query(UserMemory)
        .filter(
            UserMemory.user_id == user_id,
            UserMemory.superseded == False,
        )
        .all()
    )
    for mem in existing:
        if mem.content.lower().strip() == normalized:
            return True
    return False


def get_memory_context(user_id: int, db: Session, limit: int = 30) -> str | None:
    """Return recent memories as a prompt-ready string for LLM injection.

    Returns None if no memories exist.
    """
    memories = (
        db.query(UserMemory)
        .filter(
            UserMemory.user_id == user_id,
            UserMemory.superseded == False,
        )
        .order_by(UserMemory.created_at.desc())
        .limit(limit)
        .all()
    )

    if not memories:
        return None

    lines = []
    for m in reversed(memories):
        lines.append(f"- [{m.category}] {m.content}")

    return "Things I remember about this person:\n" + "\n".join(lines)


def get_memories_paginated(
    user_id: int, db: Session, page: int = 1, per_page: int = 20,
) -> dict:
    """Return paginated memories for the profile page."""
    base = db.query(UserMemory).filter(
        UserMemory.user_id == user_id,
        UserMemory.superseded == False,
    )
    total = base.count()
    memories = (
        base.order_by(UserMemory.created_at.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )

    return {
        "memories": [
            {
                "id": m.id,
                "category": m.category,
                "content": m.content,
                "created_at": m.created_at.strftime("%Y-%m-%d %H:%M") if m.created_at else "",
            }
            for m in memories
        ],
        "total": total,
        "page": page,
        "pages": max(1, (total + per_page - 1) // per_page),
    }


def get_interest_breakdown(user_id: int, db: Session) -> list[dict]:
    """Return category counts for the interest tracking chart."""
    from sqlalchemy import func
    rows = (
        db.query(UserMemory.category, func.count(UserMemory.id))
        .filter(
            UserMemory.user_id == user_id,
            UserMemory.superseded == False,
        )
        .group_by(UserMemory.category)
        .order_by(func.count(UserMemory.id).desc())
        .all()
    )
    return [{"category": cat, "count": cnt} for cat, cnt in rows]


def delete_memory(memory_id: int, user_id: int, db: Session) -> bool:
    """Delete a specific memory (only if it belongs to the user)."""
    mem = db.query(UserMemory).filter(
        UserMemory.id == memory_id,
        UserMemory.user_id == user_id,
    ).first()
    if not mem:
        return False
    db.delete(mem)
    db.commit()
    return True
