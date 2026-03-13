from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from ... import memory as memory_module
from ... import openai_client
from ...config import settings
from ...logger import log_info
from ...models import ReasoningLearningGoal, ReasoningUserModel

_pending_openers: dict[int, dict] = {}


def _ensure_goal(db: Session, user_id: int) -> Optional[ReasoningLearningGoal]:
    goal = (
        db.query(ReasoningLearningGoal)
        .filter(
            ReasoningLearningGoal.user_id == user_id,
            ReasoningLearningGoal.status.in_(["pending", "active"]),
        )
        .order_by(ReasoningLearningGoal.created_at.asc())
        .first()
    )
    if goal:
        if goal.status == "pending":
            goal.status = "active"
            db.add(goal)
            db.commit()
        return goal
    # No existing goals; fall back to a generic \"get to know you\" goal.
    goal = ReasoningLearningGoal(
        user_id=user_id,
        dimension="general_personality",
        description="Understand the user's general preferences and priorities.",
        status="active",
        created_at=datetime.utcnow(),
    )
    db.add(goal)
    db.commit()
    return goal


def pick_learning_goal(db: Session, user_id: int) -> Optional[ReasoningLearningGoal]:
    if not settings.reasoning_insight_chat_enabled:
        return None
    return _ensure_goal(db, user_id)


def generate_opening_message(db: Session, user_id: int, goal: ReasoningLearningGoal) -> Optional[dict]:
    """Use LLM to craft a subtle, engaging opener for Insight Chat."""
    if not openai_client.is_configured():
        return None

    um = (
        db.query(ReasoningUserModel)
        .filter(ReasoningUserModel.user_id == user_id, ReasoningUserModel.active.is_(True))
        .order_by(ReasoningUserModel.created_at.desc())
        .first()
    )
    profile_bits = []
    if um:
        profile_bits.append(f"decision_style={um.decision_style or 'unknown'}")
        profile_bits.append(f"risk_tolerance={um.risk_tolerance or 'unknown'}")
        try:
            comm = json.loads(um.communication_prefs or "{}")
        except Exception:
            comm = {}
        if comm:
            profile_bits.append(f"communication_prefs={json.dumps(comm)}")
    profile_summary = ", ".join(profile_bits) if profile_bits else "(no profile yet)"

    prompt = (
        "You are Chili, an AI housemate. You want to better understand the user, but you must NEVER say that explicitly.\n"
        "You are starting a casual conversation in a dedicated \"Reasoning\" page.\n\n"
        f"Current high-level profile: {profile_summary}\n"
        f"Current learning goal dimension: {goal.dimension}\n"
        f"Goal description: {goal.description}\n\n"
        "Write a single opening message that:\n"
        "- Feels natural and friendly\n"
        "- Is 1-3 sentences\n"
        "- Steers the conversation toward that goal dimension\n"
        "- Does NOT mention \"data\", \"learning\", \"collecting info\", or anything meta about being an AI\n\n"
        "Return ONLY the message text, no JSON, no quotes."
    )

    result = openai_client.chat(
        messages=[{"role": "user", "content": prompt}],
        system_prompt="You are Chili's conversational brain. Return only the chat message.",
        trace_id="reasoning_insight_opening",
    )
    msg = (result.get("reply") or "").strip()
    if not msg:
        return None

    data = {"message": msg, "goal_id": goal.id, "goal_description": goal.description}
    _pending_openers[user_id] = data
    return data


def get_pending_opener(db: Session, user_id: int) -> Optional[dict]:
    """Return a pending opener, generating one if needed."""
    if not settings.reasoning_insight_chat_enabled:
        return None
    if user_id in _pending_openers:
        return _pending_openers[user_id]
    goal = pick_learning_goal(db, user_id)
    if not goal:
        return None
    return generate_opening_message(db, user_id, goal)


def process_insight_reply(
    db: Session,
    user_id: int,
    user_message: str,
    goal_id: int,
    trace_id: str = "reasoning_insight_reply",
) -> dict:
    """Process a user's reply in the Insight Chat: extract facts, update goal."""
    goal = (
        db.query(ReasoningLearningGoal)
        .filter(
            ReasoningLearningGoal.id == goal_id,
            ReasoningLearningGoal.user_id == user_id,
        )
        .first()
    )
    if not goal:
        return {"ok": False, "reason": "goal_not_found"}

    # Very lightweight fact extraction via memory module.
    from ...db import SessionLocal

    # Use a short-lived session for extraction to avoid interfering with caller transaction.
    s = SessionLocal()
    new_facts = []
    try:
        new_facts = memory_module.extract_facts(
            user_message=user_message,
            assistant_reply="",
            user_id=user_id,
            db=s,
            action_type=None,
            source_message_id=None,
            trace_id=trace_id,
        )
    except Exception as e:
        log_info(trace_id, f"insight_reply_memory_error={e}")
    finally:
        s.close()

    goal.evidence_count += max(1, len(new_facts))
    # Simple completion heuristic.
    if goal.evidence_count >= 5 and goal.status != "completed":
        goal.status = "completed"
        goal.completed_at = datetime.utcnow()
    db.add(goal)
    db.commit()

    _pending_openers.pop(user_id, None)
    return {"ok": True, "stored_facts": new_facts, "goal": {"id": goal.id, "status": goal.status, "evidence_count": goal.evidence_count}}

