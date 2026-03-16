"""AgentBase: shared infrastructure for all Project Brain agents.

Every agent inherits from AgentBase and gets: web research, finding storage,
inter-agent messaging, evolution logging, and metrics out of the box.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session
from sqlalchemy import func

from ...models.project_brain import (
    AgentEvolution, AgentFinding, AgentGoal, AgentMessage,
    AgentResearch, ProjectAgentState,
)

logger = logging.getLogger(__name__)


class AgentBase:
    name: str = ""
    label: str = ""
    icon: str = ""
    role_prompt: str = ""
    active: bool = False

    def run_cycle(self, db: Session, user_id: int) -> Dict[str, Any]:
        """Override in subclass: agent-specific learning cycle."""
        return {"steps": 0}

    # ── State management ──────────────────────────────────────────────

    def get_state(self, db: Session, user_id: int) -> Optional[ProjectAgentState]:
        return (
            db.query(ProjectAgentState)
            .filter(ProjectAgentState.agent_name == self.name, ProjectAgentState.user_id == user_id)
            .first()
        )

    def save_state(self, db: Session, user_id: int, state: dict, confidence: float = 0.0) -> ProjectAgentState:
        row = self.get_state(db, user_id)
        if row:
            row.state_json = json.dumps(state, ensure_ascii=False)
            row.confidence = confidence
            row.last_cycle_at = datetime.utcnow()
            row.updated_at = datetime.utcnow()
        else:
            row = ProjectAgentState(
                agent_name=self.name,
                user_id=user_id,
                state_json=json.dumps(state, ensure_ascii=False),
                confidence=confidence,
                last_cycle_at=datetime.utcnow(),
            )
            db.add(row)
        db.commit()
        db.refresh(row)
        return row

    # ── Findings ──────────────────────────────────────────────────────

    def publish_finding(
        self, db: Session, user_id: int,
        category: str, title: str, description: str,
        severity: str = "info", evidence: Optional[dict] = None,
    ) -> AgentFinding:
        finding = AgentFinding(
            agent_name=self.name,
            user_id=user_id,
            category=category,
            title=title,
            description=description,
            severity=severity,
            evidence_json=json.dumps(evidence, ensure_ascii=False) if evidence else None,
        )
        db.add(finding)
        db.commit()
        db.refresh(finding)
        self._broadcast_finding(db, user_id, finding)
        return finding

    def get_findings(self, db: Session, user_id: int, limit: int = 20) -> List[AgentFinding]:
        return (
            db.query(AgentFinding)
            .filter(AgentFinding.agent_name == self.name, AgentFinding.user_id == user_id)
            .order_by(AgentFinding.created_at.desc())
            .limit(limit)
            .all()
        )

    # ── Research ──────────────────────────────────────────────────────

    def research(self, db: Session, user_id: int, topics: List[str], trace_id: str = "agent") -> List[AgentResearch]:
        from .web_research import research_topics
        return research_topics(db, self.name, user_id, topics, trace_id=trace_id)

    def get_research(self, db: Session, user_id: int, limit: int = 20) -> List[AgentResearch]:
        return (
            db.query(AgentResearch)
            .filter(AgentResearch.agent_name == self.name, AgentResearch.user_id == user_id, AgentResearch.stale.is_(False))
            .order_by(AgentResearch.searched_at.desc())
            .limit(limit)
            .all()
        )

    # ── Goals ─────────────────────────────────────────────────────────

    def add_goal(self, db: Session, user_id: int, description: str, goal_type: str = "learn") -> AgentGoal:
        goal = AgentGoal(
            agent_name=self.name,
            user_id=user_id,
            description=description,
            goal_type=goal_type,
        )
        db.add(goal)
        db.commit()
        db.refresh(goal)
        return goal

    def get_goals(self, db: Session, user_id: int, active_only: bool = True) -> List[AgentGoal]:
        q = db.query(AgentGoal).filter(AgentGoal.agent_name == self.name, AgentGoal.user_id == user_id)
        if active_only:
            q = q.filter(AgentGoal.status == "active")
        return q.order_by(AgentGoal.created_at.desc()).all()

    def complete_goal(self, db: Session, goal: AgentGoal) -> None:
        goal.status = "completed"
        goal.completed_at = datetime.utcnow()
        db.commit()

    # ── Evolution ─────────────────────────────────────────────────────

    def evolve(
        self, db: Session, user_id: int,
        dimension: str, description: str,
        confidence_before: float, confidence_after: float,
        trigger: str = "cycle",
    ) -> AgentEvolution:
        ev = AgentEvolution(
            agent_name=self.name,
            user_id=user_id,
            dimension=dimension,
            description=description,
            confidence_before=confidence_before,
            confidence_after=confidence_after,
            trigger=trigger,
        )
        db.add(ev)
        db.commit()
        return ev

    def get_evolution(self, db: Session, user_id: int, limit: int = 20) -> List[AgentEvolution]:
        return (
            db.query(AgentEvolution)
            .filter(AgentEvolution.agent_name == self.name, AgentEvolution.user_id == user_id)
            .order_by(AgentEvolution.created_at.desc())
            .limit(limit)
            .all()
        )

    # ── Inter-agent messaging ─────────────────────────────────────────

    def send_message(self, db: Session, user_id: int, to_agent: str, message_type: str, content: dict) -> AgentMessage:
        msg = AgentMessage(
            from_agent=self.name,
            to_agent=to_agent,
            user_id=user_id,
            message_type=message_type,
            content_json=json.dumps(content, ensure_ascii=False),
        )
        db.add(msg)
        db.commit()
        return msg

    def get_messages(self, db: Session, user_id: int, unread_only: bool = True) -> List[AgentMessage]:
        q = db.query(AgentMessage).filter(
            AgentMessage.to_agent == self.name,
            AgentMessage.user_id == user_id,
        )
        if unread_only:
            q = q.filter(AgentMessage.acknowledged.is_(False))
        return q.order_by(AgentMessage.created_at.desc()).all()

    def acknowledge_message(self, db: Session, msg: AgentMessage) -> None:
        msg.acknowledged = True
        db.commit()

    def _broadcast_finding(self, db: Session, user_id: int, finding: AgentFinding) -> None:
        """Notify all other registered agents about a new finding."""
        from .registry import AGENT_REGISTRY
        content = {"finding_id": finding.id, "title": finding.title, "category": finding.category, "severity": finding.severity}
        for agent_name in AGENT_REGISTRY:
            if agent_name != self.name:
                self.send_message(db, user_id, agent_name, "finding", content)

    # ── Metrics ───────────────────────────────────────────────────────

    def get_metrics(self, db: Session, user_id: int) -> Dict[str, Any]:
        state = self.get_state(db, user_id)
        finding_count = db.query(func.count(AgentFinding.id)).filter(
            AgentFinding.agent_name == self.name, AgentFinding.user_id == user_id,
        ).scalar() or 0
        research_count = db.query(func.count(AgentResearch.id)).filter(
            AgentResearch.agent_name == self.name, AgentResearch.user_id == user_id,
        ).scalar() or 0
        goal_count = db.query(func.count(AgentGoal.id)).filter(
            AgentGoal.agent_name == self.name, AgentGoal.user_id == user_id, AgentGoal.status == "active",
        ).scalar() or 0
        evolution_count = db.query(func.count(AgentEvolution.id)).filter(
            AgentEvolution.agent_name == self.name, AgentEvolution.user_id == user_id,
        ).scalar() or 0
        unread_msgs = db.query(func.count(AgentMessage.id)).filter(
            AgentMessage.to_agent == self.name, AgentMessage.user_id == user_id, AgentMessage.acknowledged.is_(False),
        ).scalar() or 0

        return {
            "agent": self.name,
            "label": self.label,
            "icon": self.icon,
            "active": self.active,
            "confidence": state.confidence if state else 0.0,
            "last_cycle": state.last_cycle_at.isoformat() if state and state.last_cycle_at else None,
            "finding_count": finding_count,
            "research_count": research_count,
            "active_goals": goal_count,
            "evolution_count": evolution_count,
            "unread_messages": unread_msgs,
        }

    # ── Chat context ──────────────────────────────────────────────────

    def get_chat_context(self, db: Session, user_id: int) -> str:
        """Return agent-specific context for the LLM system prompt."""
        state = self.get_state(db, user_id)
        parts = [f"[Project Brain — {self.label}]", self.role_prompt]
        if state and state.state_json:
            try:
                s = json.loads(state.state_json)
                if isinstance(s, dict):
                    for k, v in list(s.items())[:5]:
                        parts.append(f"- {k}: {v}")
            except Exception:
                pass
        findings = self.get_findings(db, user_id, limit=3)
        if findings:
            parts.append("Recent findings:")
            for f in findings:
                parts.append(f"  [{f.severity}] {f.title}: {f.description[:100]}")
        return "\n".join(parts)
