"""Reasoning Brain learning cycle orchestrator.

Synthesizes user model, rebuilds interest graph, runs web research,
and generates anticipations. Designed to run periodically in the
background via the trading scheduler.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session
from sqlalchemy import func

from ...config import settings
from ...models import (
    ReasoningAnticipation,
    ReasoningConfidenceSnapshot,
    ReasoningEvent,
    ReasoningHypothesis,
    ReasoningInterest,
    ReasoningLearningGoal,
    ReasoningResearch,
    ReasoningUserModel,
)
from . import user_model as user_model_mod
from . import interest_graph as interest_graph_mod
from . import web_researcher as web_researcher_mod
from . import anticipator as anticipator_mod
from . import evolution as evolution_mod

logger = logging.getLogger(__name__)

_reasoning_status: Dict[str, Any] = {
    "running": False,
    "last_run": None,
    "phase": "idle",
    "steps_completed": 0,
    "total_steps": 4,
    "current_step": "",
    "started_at": None,
    "error": None,
}


def _log_event(db: Session, event_type: str, description: str, user_id: Optional[int] = None) -> None:
    ev = ReasoningEvent(
        user_id=user_id,
        event_type=event_type,
        description=description,
        created_at=datetime.utcnow(),
    )
    db.add(ev)
    try:
        db.commit()
    except Exception:
        db.rollback()


def get_reasoning_status() -> Dict[str, Any]:
    status = dict(_reasoning_status)
    if status.get("running") and status.get("started_at"):
        try:
            started = datetime.fromisoformat(status["started_at"])
            status["elapsed_seconds"] = int((datetime.utcnow() - started).total_seconds())
        except Exception:
            pass
    return status


def run_reasoning_cycle(db: Session, user_id: int, trace_id: str = "reasoning") -> Dict[str, Any]:
    """Full Reasoning Brain cycle for a single user."""
    if not settings.reasoning_enabled:
        return {"ok": False, "reason": "Reasoning Brain disabled"}

    if _reasoning_status["running"]:
        return {"ok": False, "reason": "Reasoning cycle already in progress"}

    _reasoning_status["running"] = True
    _reasoning_status["phase"] = "starting"
    _reasoning_status["steps_completed"] = 0
    _reasoning_status["total_steps"] = 8
    _reasoning_status["started_at"] = datetime.utcnow().isoformat()
    _reasoning_status["current_step"] = ""
    _reasoning_status["error"] = None

    start = time.time()
    report: Dict[str, Any] = {}

    try:
        # Step 1: synthesize user model
        _reasoning_status["phase"] = "user_model"
        _reasoning_status["current_step"] = "Synthesizing user reasoning model"
        model = user_model_mod.synthesize_user_model(db, user_id, trace_id=trace_id)
        _reasoning_status["steps_completed"] = 1
        if not model:
            raise RuntimeError("Failed to synthesize user model")
        _log_event(db, "user_model", "Updated reasoning user model", user_id)

        # Step 2: rebuild interest graph
        _reasoning_status["phase"] = "interests"
        _reasoning_status["current_step"] = "Rebuilding interest graph"
        interest_graph_mod.rebuild_interest_graph(db, user_id)
        _reasoning_status["steps_completed"] = 2
        _log_event(db, "interests", "Rebuilt interest graph", user_id)

        # Step 3: web research
        _reasoning_status["phase"] = "web_research"
        _reasoning_status["current_step"] = "Refreshing web research"
        web_researcher_mod.refresh_research_for_top_interests(db, user_id, trace_id=trace_id)
        _reasoning_status["steps_completed"] = 3
        _log_event(db, "web_research", "Refreshed web research for top interests", user_id)

        # Step 4: snapshot confidence
        _reasoning_status["phase"] = "confidence_snapshot"
        _reasoning_status["current_step"] = "Snapshotting confidence by dimension"
        evolution_mod.snapshot_confidence(db, user_id)
        _reasoning_status["steps_completed"] = 4
        _log_event(db, "confidence", "Captured confidence snapshots", user_id)

        # Step 5: generate hypotheses
        _reasoning_status["phase"] = "hypotheses"
        _reasoning_status["current_step"] = "Generating hypotheses about user behaviour"
        hyps = evolution_mod.generate_hypotheses(db, user_id)
        _reasoning_status["steps_completed"] = 5
        _log_event(db, "hypotheses", f"Generated {len(hyps)} hypotheses", user_id)

        # Step 6: test hypotheses
        _reasoning_status["phase"] = "hypothesis_testing"
        _reasoning_status["current_step"] = "Testing hypotheses against recent evidence"
        evolution_mod.test_hypotheses(db, user_id)
        _reasoning_status["steps_completed"] = 6
        _log_event(db, "hypotheses_tested", "Tested reasoning hypotheses", user_id)

        # Step 7: generate learning goals from gaps and drift
        _reasoning_status["phase"] = "goals"
        _reasoning_status["current_step"] = "Generating new learning goals"
        evolution_mod.generate_learning_goals(db, user_id)
        _reasoning_status["steps_completed"] = 7
        _log_event(db, "goals", "Generated or refreshed learning goals", user_id)

        # Step 8: anticipations
        _reasoning_status["phase"] = "anticipations"
        _reasoning_status["current_step"] = "Generating anticipations"
        anticipations = anticipator_mod.generate_anticipations(db, user_id, trace_id=trace_id)
        _reasoning_status["steps_completed"] = 8
        _log_event(
            db,
            "anticipations",
            f"Generated {len(anticipations)} anticipations",
            user_id,
        )

        elapsed = round(time.time() - start, 1)
        _reasoning_status["phase"] = "idle"
        _reasoning_status["last_run"] = datetime.utcnow().isoformat()
        _reasoning_status["current_step"] = ""

        report.update(
            {
                "ok": True,
                "elapsed_seconds": elapsed,
                "anticipations": len(anticipations),
            }
        )
        logger.info(
            "[reasoning] Reasoning cycle completed user_id=%s in %ss anticipations=%s",
            user_id,
            elapsed,
            len(anticipations),
        )
    except Exception as e:  # pragma: no cover
        logger.exception("[reasoning] Reasoning cycle error: %s", e)
        _reasoning_status["error"] = str(e)
        _reasoning_status["phase"] = "idle"
        _reasoning_status["current_step"] = ""
        _reasoning_status["last_run"] = datetime.utcnow().isoformat()
        report["ok"] = False
        report["error"] = str(e)
        _log_event(db, "error", f"Reasoning cycle error: {e}", user_id)
    finally:
        _reasoning_status["running"] = False

    return report


def get_reasoning_metrics(db: Session, user_id: int) -> Dict[str, Any]:
    """Aggregate metrics for Reasoning Brain dashboard."""
    status = get_reasoning_status()
    user_model = (
        db.query(ReasoningUserModel)
        .filter(ReasoningUserModel.user_id == user_id, ReasoningUserModel.active.is_(True))
        .order_by(ReasoningUserModel.created_at.desc())
        .first()
    )
    interest_count = (
        db.query(func.count(ReasoningInterest.id))
        .filter(ReasoningInterest.user_id == user_id)
        .scalar()
        or 0
    )
    research_count = (
        db.query(func.count(ReasoningResearch.id))
        .filter(ReasoningResearch.user_id == user_id)
        .scalar()
        or 0
    )
    anticipations_count = (
        db.query(func.count(ReasoningAnticipation.id))
        .filter(ReasoningAnticipation.user_id == user_id, ReasoningAnticipation.dismissed.is_(False))
        .scalar()
        or 0
    )
    hypothesis_count = (
        db.query(func.count(ReasoningHypothesis.id))
        .filter(ReasoningHypothesis.user_id == user_id)
        .scalar()
        or 0
    )
    active_goals_count = (
        db.query(func.count(ReasoningLearningGoal.id))
        .filter(
            ReasoningLearningGoal.user_id == user_id,
            ReasoningLearningGoal.status.in_(["pending", "active"]),
        )
        .scalar()
        or 0
    )
    completed_goals_count = (
        db.query(func.count(ReasoningLearningGoal.id))
        .filter(
            ReasoningLearningGoal.user_id == user_id,
            ReasoningLearningGoal.status == "completed",
        )
        .scalar()
        or 0
    )
    model_versions = (
        db.query(func.count(ReasoningUserModel.id))
        .filter(ReasoningUserModel.user_id == user_id)
        .scalar()
        or 0
    )

    return {
        "status": status,
        "has_model": bool(user_model),
        "user_model": {
            "decision_style": user_model.decision_style if user_model else None,
            "risk_tolerance": user_model.risk_tolerance if user_model else None,
            "created_at": user_model.created_at.isoformat() if user_model and user_model.created_at else None,
        },
        "interest_count": interest_count,
        "research_count": research_count,
        "anticipation_count": anticipations_count,
        "hypothesis_count": hypothesis_count,
        "active_goals_count": active_goals_count,
        "completed_goals_count": completed_goals_count,
        "model_versions": model_versions,
    }


def get_reasoning_chat_context(db: Session, user_id: int) -> str:
    """Short natural-language description of the user's style, goals, and anticipations."""
    user_model = (
        db.query(ReasoningUserModel)
        .filter(ReasoningUserModel.user_id == user_id, ReasoningUserModel.active.is_(True))
        .order_by(ReasoningUserModel.created_at.desc())
        .first()
    )
    if not user_model:
        return ""

    import json as _json

    parts: list[str] = []
    parts.append("User reasoning + preference snapshot:")
    if user_model.decision_style:
        parts.append(f"- Decision style: {user_model.decision_style}")
    if user_model.risk_tolerance:
        parts.append(f"- Risk tolerance: {user_model.risk_tolerance}")
    try:
        comm = _json.loads(user_model.communication_prefs or "{}")
    except Exception:
        comm = {}
    detail = comm.get("detail_level")
    tone = comm.get("tone")
    if detail or tone:
        line = "- Communication: "
        if detail:
            line += f"prefers {detail} details"
        if tone:
            if detail:
                line += ", "
            line += f"tone {tone}"
        parts.append(line)

    try:
        goals = _json.loads(user_model.active_goals or "[]")
    except Exception:
        goals = []
    if goals:
        parts.append("- Active goals:")
        for g in goals[:5]:
            parts.append(f"  • [{g.get('area','?')}] {g.get('goal','')} ({g.get('horizon','?')} term)")

    anticipations = (
        db.query(ReasoningAnticipation)
        .filter(ReasoningAnticipation.user_id == user_id, ReasoningAnticipation.dismissed.is_(False))
        .order_by(ReasoningAnticipation.created_at.desc())
        .limit(5)
        .all()
    )
    if anticipations:
        parts.append("Near-term anticipations Chili has about what the user may need:")
        for a in anticipations:
            parts.append(f"  • [{a.domain or 'general'} | {a.confidence:.2f}] {a.description}")

    return "\n".join(parts)

