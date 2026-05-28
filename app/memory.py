"""Incremental user memory extraction and retrieval.

Extracts personal facts from each conversation turn and stores them
in the UserMemory table. Provides memory context for LLM prompt injection.
"""
import json
import re
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

MEMORY_SIGNAL_RE = re.compile(
    r"\b("
    r"i|i'm|im|i've|ive|i'd|i'll|ill|my|mine|myself|"
    r"we|we're|we've|our|ours|"
    r"favorite|favourite|prefer|preference|like|love|enjoy|hate|"
    r"allerg(?:y|ic|ies)|diet|vegetarian|vegan|gluten|"
    r"work|job|career|school|class|study|goal|habit|routine|"
    r"birthday|anniversary|family|mom|mother|dad|father|"
    r"wife|husband|partner|son|daughter|friend|personal|"
    r"usually|always|often|every"
    r")\b",
    re.IGNORECASE,
)

_CLAUSE_SPLIT_RE = re.compile(
    r"[.;!\n]+|\s+(?:and|also|but)\s+(?=(?:i\b|i[' ]?m\b|im\b|i[' ]?ve\b|ive\b|my\b|every\b|each\b))",
    re.IGNORECASE,
)
_INTEREST_RE = re.compile(
    r"^(?:i\s+)?(?:really\s+|very\s+)?(?:love|like|enjoy|am into|i[' ]?m into|im into)\s+(.+)$",
    re.IGNORECASE,
)
_FAVORITE_RE = re.compile(
    r"^my\s+favorite\s+([\w -]{2,40})\s+is\s+(.+)$",
    re.IGNORECASE,
)
_DIETARY_RE = re.compile(
    r"^(?:i(?:'m| am)|im)\s+(vegetarian|vegan|gluten[- ]free|dairy[- ]free|pescatarian|keto|kosher|halal)\b",
    re.IGNORECASE,
)
_ALLERGY_RE = re.compile(
    r"^(?:i(?:'m| am)|im)\s+allergic\s+to\s+(.+)$",
    re.IGNORECASE,
)
_WORK_RE = re.compile(
    r"^(?:i\s+)?(?:work\s+as|work\s+in|am\s+working\s+as|i[' ]?m\s+working\s+as|im\s+working\s+as)\s+(.+)$",
    re.IGNORECASE,
)
_GOAL_RE = re.compile(
    r"^(?:i\s+)?(?:want\s+to|plan\s+to|hope\s+to|my\s+goal\s+is\s+to|goal\s+is\s+to)\s+(.+)$",
    re.IGNORECASE,
)
_PREFERENCE_RE = re.compile(
    r"^(?:i\s+)?(?:prefer|would\s+rather|like\s+it\s+when)\s+(.+)$",
    re.IGNORECASE,
)
_DISLIKE_RE = re.compile(
    r"^(?:i\s+)?(?:hate|dislike|do\s+not\s+like|don't\s+like)\s+(.+)$",
    re.IGNORECASE,
)
_HABIT_RE = re.compile(
    r"^(?:i\s+)?(?P<freq>usually|always|often)\s+(?P<habit>.+)$",
    re.IGNORECASE,
)
_SCHEDULE_RE = re.compile(
    r"^(?:every|each)\s+(?P<when>[\w -]{3,40})\s+i\s+(?P<event>.+)$",
    re.IGNORECASE,
)
_SCHEDULE_IS_RE = re.compile(
    r"^my\s+(?:work\s+)?schedule\s+is\s+(.+)$",
    re.IGNORECASE,
)
_BIRTHDAY_RE = re.compile(
    r"^my\s+(birthday|anniversary)\s+is\s+(.+)$",
    re.IGNORECASE,
)
_PERSON_RE = re.compile(
    r"^my\s+(wife|husband|partner|mom|mother|dad|father|son|daughter|friend)\s+is\s+(.+)$",
    re.IGNORECASE,
)
_DUPLICATE_PREFIX_RE = re.compile(r"^(?:likes|loves|enjoys)\s+", re.IGNORECASE)
_VAGUE_OBJECTS = {"it", "this", "that", "things", "stuff", "them"}

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
    return bool(MEMORY_SIGNAL_RE.search(msg))


def _clean_mechanical_value(value: str) -> str:
    text = re.sub(r"\s+", " ", (value or "").strip(" .;!?,\"'"))
    text = re.sub(r"\s+(?:a lot|so much|very much|too)$", "", text, flags=re.IGNORECASE)
    return text.strip(" .;!?,\"'")


def _title_fact(value: str) -> str:
    value = _clean_mechanical_value(value)
    if not value:
        return ""
    return value[0].upper() + value[1:]


def _mechanical_fact_for_clause(clause: str) -> dict | None:
    text = _clean_mechanical_value(clause)
    if not text:
        return None

    match = _DIETARY_RE.match(text)
    if match:
        diet = match.group(1).replace("-", " ").title()
        return {"category": "dietary", "content": diet}

    match = _ALLERGY_RE.match(text)
    if match:
        item = _title_fact(match.group(1))
        if item:
            return {"category": "health", "content": f"Allergic to {item.lower()}"}
        return None

    match = _WORK_RE.match(text)
    if match:
        role = _clean_mechanical_value(match.group(1))
        if role and role.casefold() not in _VAGUE_OBJECTS:
            return {"category": "work", "content": f"Works as {role}"}
        return None

    match = _GOAL_RE.match(text)
    if match:
        goal = _clean_mechanical_value(match.group(1))
        if goal and goal.casefold() not in _VAGUE_OBJECTS:
            return {"category": "goal", "content": f"Goal: {goal}"}
        return None

    match = _PREFERENCE_RE.match(text)
    if match:
        pref = _clean_mechanical_value(match.group(1))
        if pref and pref.casefold() not in _VAGUE_OBJECTS:
            return {"category": "preference", "content": f"Prefers {pref}"}
        return None

    match = _DISLIKE_RE.match(text)
    if match:
        pref = _clean_mechanical_value(match.group(1))
        if pref and pref.casefold() not in _VAGUE_OBJECTS:
            return {"category": "preference", "content": f"Dislikes {pref}"}
        return None

    match = _HABIT_RE.match(text)
    if match:
        freq = match.group("freq").title()
        habit = _clean_mechanical_value(match.group("habit"))
        if habit and habit.casefold() not in _VAGUE_OBJECTS:
            return {"category": "habit", "content": f"{freq} {habit}"}
        return None

    match = _SCHEDULE_RE.match(text)
    if match:
        when = _clean_mechanical_value(match.group("when")).lower()
        event = _clean_mechanical_value(match.group("event"))
        if event and event.casefold() not in _VAGUE_OBJECTS:
            return {"category": "schedule", "content": f"Every {when}: {event}"}
        return None

    match = _SCHEDULE_IS_RE.match(text)
    if match:
        schedule = _clean_mechanical_value(match.group(1))
        if schedule and schedule.casefold() not in _VAGUE_OBJECTS:
            return {"category": "schedule", "content": f"Schedule: {schedule}"}
        return None

    match = _BIRTHDAY_RE.match(text)
    if match:
        label = match.group(1).title()
        value = _clean_mechanical_value(match.group(2))
        if value and value.casefold() not in _VAGUE_OBJECTS:
            return {"category": "event", "content": f"{label}: {value}"}
        return None

    match = _PERSON_RE.match(text)
    if match:
        relation = match.group(1).title()
        name = _clean_mechanical_value(match.group(2))
        if name and name.casefold() not in _VAGUE_OBJECTS:
            return {"category": "person", "content": f"{relation} is {name}"}
        return None

    match = _FAVORITE_RE.match(text)
    if match:
        kind = _clean_mechanical_value(match.group(1)).lower()
        thing = _clean_mechanical_value(match.group(2))
        if thing and thing.casefold() not in _VAGUE_OBJECTS:
            category = "interest" if any(word in kind for word in ("hobby", "activity", "sport", "food")) else "preference"
            return {"category": category, "content": f"Favorite {kind}: {thing}"}
        return None

    match = _INTEREST_RE.match(text)
    if match:
        thing = _clean_mechanical_value(match.group(1))
        if thing and thing.casefold() not in _VAGUE_OBJECTS:
            return {"category": "interest", "content": f"Likes {thing}"}

    return None


def _extract_mechanical_facts(user_message: str) -> tuple[list[dict], bool]:
    """Extract simple explicit facts without LLM help.

    The boolean is True only when every memory-bearing clause was handled,
    letting the caller skip the LLM for clearly mechanical cases.
    """
    clauses = [
        _clean_mechanical_value(part)
        for part in _CLAUSE_SPLIT_RE.split(user_message or "")
        if _clean_mechanical_value(part)
    ]
    facts: list[dict] = []
    unmatched = 0
    seen: set[tuple[str, str]] = set()

    for clause in clauses:
        fact = _mechanical_fact_for_clause(clause)
        if not fact:
            unmatched += 1
            continue
        key = (str(fact.get("category") or ""), str(fact.get("content") or "").casefold())
        if key not in seen:
            facts.append(fact)
            seen.add(key)

    return facts, bool(facts) and unmatched == 0


def _store_facts(
    *,
    user_id: int,
    facts: list[dict],
    db: Session,
    source_message_id: int | None,
    trace_id: str,
) -> list[dict]:
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


def _memory_duplicate_key(content: str) -> str:
    normalized = _clean_mechanical_value(content).casefold()
    normalized = _DUPLICATE_PREFIX_RE.sub("", normalized).strip()
    return normalized


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
    if not _should_extract(action_type, user_message):
        return []

    mechanical_facts, mechanical_complete = _extract_mechanical_facts(user_message)
    if mechanical_complete:
        return _store_facts(
            user_id=user_id,
            facts=mechanical_facts,
            db=db,
            source_message_id=source_message_id,
            trace_id=trace_id,
        )

    if not openai_client.is_configured():
        if mechanical_facts:
            return _store_facts(
                user_id=user_id,
                facts=mechanical_facts,
                db=db,
                source_message_id=source_message_id,
                trace_id=trace_id,
            )
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
        except Exception as e:
            log_info(trace_id, f"memory_gateway_error={e}")
            if mechanical_facts:
                return _store_facts(
                    user_id=user_id,
                    facts=mechanical_facts,
                    db=db,
                    source_message_id=source_message_id,
                    trace_id=trace_id,
                )
            return []
    except Exception as e:
        log_info(trace_id, f"memory_extraction_error={e}")
        if mechanical_facts:
            return _store_facts(
                user_id=user_id,
                facts=mechanical_facts,
                db=db,
                source_message_id=source_message_id,
                trace_id=trace_id,
            )
        return []

    if not result.get("reply"):
        if mechanical_facts:
            return _store_facts(
                user_id=user_id,
                facts=mechanical_facts,
                db=db,
                source_message_id=source_message_id,
                trace_id=trace_id,
            )
        return []

    facts = _parse_facts(result["reply"], trace_id)
    if not facts:
        if mechanical_facts:
            return _store_facts(
                user_id=user_id,
                facts=mechanical_facts,
                db=db,
                source_message_id=source_message_id,
                trace_id=trace_id,
            )
        return []

    return _store_facts(
        user_id=user_id,
        facts=facts,
        db=db,
        source_message_id=source_message_id,
        trace_id=trace_id,
    )


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
    normalized = _memory_duplicate_key(content)
    existing = (
        db.query(UserMemory)
        .filter(
            UserMemory.user_id == user_id,
            UserMemory.superseded == False,
        )
        .all()
    )
    for mem in existing:
        if _memory_duplicate_key(mem.content) == normalized:
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
