"""Housemate personality profiling.

Consolidates UserMemory facts into a summary HousemateProfile,
and provides profile context for injection into LLM prompts.
"""
import json
from datetime import datetime
from sqlalchemy.orm import Session

from .models import HousemateProfile, ChatMessage, UserMemory
from . import openai_client
from .logger import log_info

EXTRACTION_THRESHOLD = 20  # re-consolidate after this many new messages

CONSOLIDATION_PROMPT = (
    "Given these personal facts about a housemate, create a concise personality summary.\n"
    "Return ONLY valid JSON with these exact keys:\n"
    "{\n"
    '  "interests": ["list", "of", "interests"],\n'
    '  "dietary": "any dietary preferences or restrictions, or empty string",\n'
    '  "tone": "their preferred communication style: casual, formal, friendly, brief, etc.",\n'
    '  "notes": "any other observations about this person (habits, preferences, schedule patterns)"\n'
    "}\n\n"
    "Merge related items, resolve contradictions (prefer newer facts), and be concise.\n"
    "If there isn't enough information for a field, use an empty string or empty list.\n\n"
    "Facts:\n"
)


def should_update(user_id: int, db: Session) -> bool:
    """Return True if this user has enough new messages since last extraction."""
    if not openai_client.is_configured():
        return False

    profile = db.query(HousemateProfile).filter(
        HousemateProfile.user_id == user_id
    ).first()

    current_count = db.query(ChatMessage).filter(
        ChatMessage.convo_key == f"user:{user_id}",
        ChatMessage.role == "user",
    ).count()

    if profile is None:
        return current_count >= EXTRACTION_THRESHOLD

    messages_since = current_count - (profile.message_count_at_extraction or 0)
    return messages_since >= EXTRACTION_THRESHOLD


def extract_profile(user_id: int, db: Session, trace_id: str = "personality") -> dict | None:
    """Consolidate UserMemory facts into a summary HousemateProfile.

    If memories exist, uses LLM to synthesize them into the profile fields.
    Falls back to direct message analysis if no memories are available.
    Returns the extracted profile dict or None on failure.
    """
    memories = (
        db.query(UserMemory)
        .filter(UserMemory.user_id == user_id, UserMemory.superseded == False)
        .order_by(UserMemory.created_at.asc())
        .all()
    )

    if memories:
        facts_text = "\n".join(
            f"- [{m.category}] {m.content}" for m in memories
        )
        prompt = CONSOLIDATION_PROMPT + facts_text
    else:
        messages = (
            db.query(ChatMessage)
            .filter(ChatMessage.convo_key == f"user:{user_id}")
            .filter(ChatMessage.content != "")
            .order_by(ChatMessage.id.desc())
            .limit(40)
            .all()
        )
        messages = list(reversed(messages))
        if not messages:
            return None
        conversation_text = "\n".join(
            f"{m.role.upper()}: {m.content}" for m in messages
        )
        prompt = (
            "Analyze these conversation messages from a housemate and extract a personality profile.\n"
            "Return ONLY valid JSON with these exact keys:\n"
            '{"interests": [...], "dietary": "...", "tone": "...", "notes": "..."}\n\n'
            "Messages:\n" + conversation_text
        )

    try:
        from .services.context_brain.llm_gateway import gateway_chat
        result = gateway_chat(
            messages=[{"role": "user", "content": prompt}],
            purpose='personality_apply',
            system_prompt="You are a personality analysis assistant. Return only valid JSON.",
            trace_id=trace_id,
        )
    except Exception:
        result = openai_client.chat(
            messages=[{"role": "user", "content": prompt}],
            system_prompt="You are a personality analysis assistant. Return only valid JSON.",
            trace_id=trace_id,
        )

    if not result.get("reply"):
        return None

    try:
        text = result["reply"]
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1:
            text = text[start:end + 1]
        profile_data = json.loads(text)
    except (json.JSONDecodeError, ValueError) as e:
        log_info(trace_id, f"personality_parse_error={e}")
        return None

    user_msg_count = db.query(ChatMessage).filter(
        ChatMessage.convo_key == f"user:{user_id}",
        ChatMessage.role == "user",
    ).count()

    existing = db.query(HousemateProfile).filter(
        HousemateProfile.user_id == user_id
    ).first()

    interests = profile_data.get("interests", [])
    if isinstance(interests, list):
        interests = json.dumps(interests)
    else:
        interests = json.dumps([])

    if existing:
        existing.interests = interests
        existing.dietary = profile_data.get("dietary", "") or ""
        existing.tone = profile_data.get("tone", "") or ""
        existing.notes = profile_data.get("notes", "") or ""
        existing.last_extracted_at = datetime.utcnow()
        existing.message_count_at_extraction = user_msg_count
    else:
        db.add(HousemateProfile(
            user_id=user_id,
            interests=interests,
            dietary=profile_data.get("dietary", "") or "",
            tone=profile_data.get("tone", "") or "",
            notes=profile_data.get("notes", "") or "",
            last_extracted_at=datetime.utcnow(),
            message_count_at_extraction=user_msg_count,
        ))

    db.commit()
    log_info(trace_id, f"personality_consolidated user_id={user_id} memories={len(memories)} interests={interests}")
    return profile_data


def get_profile_context(user_id: int, db: Session) -> str | None:
    """Return a prompt-ready string describing this housemate's personality.

    Returns None if no profile exists.
    """
    profile = db.query(HousemateProfile).filter(
        HousemateProfile.user_id == user_id
    ).first()

    if not profile:
        return None

    parts = []
    try:
        interests = json.loads(profile.interests) if profile.interests else []
    except json.JSONDecodeError:
        interests = []

    if interests:
        parts.append(f"Interests: {', '.join(interests)}")
    if profile.dietary:
        parts.append(f"Dietary: {profile.dietary}")
    if profile.tone:
        parts.append(f"Preferred tone: {profile.tone}")
    if profile.notes:
        parts.append(f"Notes: {profile.notes}")

    if not parts:
        return None

    return "Housemate personality profile:\n" + "\n".join(parts)
