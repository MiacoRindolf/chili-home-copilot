"""Product Owner Agent — the first autonomous Project Brain agent.

Drives project understanding through user questions, requirement synthesis,
tech research, and recommendation generation. 8-step learning cycle.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict, List

from sqlalchemy.orm import Session
from sqlalchemy import func

from ..base import AgentBase
from ....models.project_brain import (
    AgentFinding, AgentGoal, POQuestion, PORequirement, ProjectAgentState,
)
from ...llm_caller import call_llm

logger = logging.getLogger(__name__)

_PO_ROLE_PROMPT = (
    "You are Chili's Product Owner agent — a self-evolving AI that deeply understands "
    "the user's project, goals, target users, and priorities. You gather requirements "
    "through smart questions, research modern technologies, synthesize user stories, "
    "and flag risks or opportunities. You stay up-to-date with industry trends and "
    "proactively advise on product direction."
)

_QUESTION_CATEGORIES = [
    "vision", "features", "priorities", "tech_stack",
    "users", "success_criteria", "constraints", "domain",
]


class ProductOwnerAgent(AgentBase):
    name = "product_owner"
    label = "Product Owner"
    icon = "\U0001F451"  # crown
    role_prompt = _PO_ROLE_PROMPT
    active = True

    # ── 8-Step Learning Cycle ─────────────────────────────────────────

    def run_cycle(self, db: Session, user_id: int) -> Dict[str, Any]:
        steps_done = 0
        report: Dict[str, Any] = {}

        # 1. Review context
        context = self._review_context(db, user_id)
        steps_done += 1

        # 2. Identify gaps
        gaps = self._identify_gaps(db, user_id, context)
        report["gaps"] = len(gaps)
        steps_done += 1

        # 3. Generate questions
        new_qs = self._generate_questions(db, user_id, gaps, context)
        report["new_questions"] = new_qs
        steps_done += 1

        # 4. Research tech
        research_count = self._research_tech(db, user_id, context)
        report["research_done"] = research_count
        steps_done += 1

        # 5. Synthesize requirements
        new_reqs = self._synthesize_requirements(db, user_id)
        report["new_requirements"] = new_reqs
        steps_done += 1

        # 6. Update state
        old_conf = self._get_confidence(db, user_id)
        new_conf = self._update_state(db, user_id, context, report)
        report["confidence"] = new_conf
        steps_done += 1

        # 7. Generate recommendations
        findings = self._generate_recommendations(db, user_id, context)
        report["new_findings"] = findings
        steps_done += 1

        # 8. Publish to bus
        self._publish_to_bus(db, user_id, report)
        steps_done += 1

        if abs(new_conf - old_conf) > 0.01:
            self.evolve(db, user_id, "overall_understanding",
                        f"Cycle completed: {report}",
                        old_conf, new_conf, trigger="learning_cycle")

        return {"steps": steps_done, **report}

    # ── Step implementations ──────────────────────────────────────────

    def _review_context(self, db: Session, user_id: int) -> Dict[str, Any]:
        """Step 1: Gather all existing knowledge about the project."""
        state = self.get_state(db, user_id)
        existing_state = {}
        if state and state.state_json:
            try:
                existing_state = json.loads(state.state_json)
            except Exception:
                pass

        answered_qs = (
            db.query(POQuestion)
            .filter(POQuestion.user_id == user_id, POQuestion.status == "answered")
            .order_by(POQuestion.answered_at.desc())
            .limit(20)
            .all()
        )
        requirements = (
            db.query(PORequirement)
            .filter(PORequirement.user_id == user_id)
            .order_by(PORequirement.created_at.desc())
            .limit(20)
            .all()
        )
        pending_qs = (
            db.query(POQuestion)
            .filter(POQuestion.user_id == user_id, POQuestion.status == "pending")
            .count()
        )

        return {
            "state": existing_state,
            "answered_questions": [
                {"q": q.question, "a": q.answer, "category": q.category}
                for q in answered_qs
            ],
            "requirements_count": len(requirements),
            "pending_questions": pending_qs,
            "requirements": [
                {"title": r.title, "priority": r.priority, "status": r.status}
                for r in requirements
            ],
        }

    def _identify_gaps(self, db: Session, user_id: int, context: dict) -> List[str]:
        """Step 2: Use LLM to determine what's still unknown."""
        answered = context.get("answered_questions", [])
        state = context.get("state", {})

        answered_text = "\n".join(
            f"- [{qa['category']}] Q: {qa['q']} A: {qa['a']}"
            for qa in answered[-10:]
        ) or "No questions answered yet."

        state_text = json.dumps(state, indent=2)[:1500] if state else "No existing state."

        prompt = (
            "You are the Product Owner AI for a software project. "
            "Based on the following known information, identify the TOP 5 knowledge gaps "
            "that need to be filled to make the project successful.\n\n"
            f"Existing project understanding:\n{state_text}\n\n"
            f"Already answered:\n{answered_text}\n\n"
            "Return ONLY valid JSON: {\"gaps\": [\"gap1\", \"gap2\", ...]}\n"
        )

        reply = call_llm(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=400,
            trace_id="po-gaps",
        )
        if not reply:
            return ["Project vision", "Target users", "Key features"]

        try:
            start = reply.find("{")
            end = reply.rfind("}")
            data = json.loads(reply[start: end + 1])
            return data.get("gaps", [])[:5]
        except Exception:
            return ["Project vision", "Target users", "Key features"]

    def _generate_questions(self, db: Session, user_id: int, gaps: List[str], context: dict) -> int:
        """Step 3: Create prioritized questions from the identified gaps."""
        pending = context.get("pending_questions", 0)
        if pending >= 5:
            return 0

        existing = {
            q.question for q in
            db.query(POQuestion.question)
            .filter(POQuestion.user_id == user_id)
            .all()
        }

        gap_text = "\n".join(f"- {g}" for g in gaps)
        prompt = (
            "You are a Product Owner gathering requirements for a software project. "
            "Generate 3 specific, actionable questions to fill these knowledge gaps.\n\n"
            f"Gaps:\n{gap_text}\n\n"
            "Each question should help clarify the project direction. "
            "Categorize each as one of: vision, features, priorities, tech_stack, users, "
            "success_criteria, constraints, domain.\n\n"
            "Return ONLY valid JSON:\n"
            "{\"questions\": [{\"question\": \"...\", \"category\": \"...\", "
            "\"context\": \"why this matters\", \"priority\": 1-10}]}\n"
        )

        reply = call_llm(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=600,
            trace_id="po-questions",
        )
        if not reply:
            return 0

        try:
            start = reply.find("{")
            end = reply.rfind("}")
            data = json.loads(reply[start: end + 1])
            questions = data.get("questions", [])
        except Exception:
            return 0

        added = 0
        for q_data in questions:
            q_text = q_data.get("question", "").strip()
            if not q_text or q_text in existing:
                continue
            cat = q_data.get("category", "general")
            if cat not in _QUESTION_CATEGORIES:
                cat = "general"
            db.add(POQuestion(
                user_id=user_id,
                question=q_text,
                context=q_data.get("context", ""),
                category=cat,
                priority=min(10, max(1, int(q_data.get("priority", 5)))),
            ))
            existing.add(q_text)
            added += 1

        if added:
            db.commit()
        return added

    def _research_tech(self, db: Session, user_id: int, context: dict) -> int:
        """Step 4: Web research for modern tech relevant to the project."""
        from ...config import settings
        state = context.get("state", {})
        tech_stack = state.get("tech_stack", "")
        domain = state.get("domain", "software project")

        topics = [
            f"latest {domain} best practices 2025 2026",
        ]
        if tech_stack:
            topics.append(f"{tech_stack} latest updates and improvements 2026")

        max_searches = getattr(settings, "project_brain_max_web_searches", 5)
        results = self.research(
            db, user_id,
            topics[:max_searches],
            trace_id="po-research",
        )
        return len(results)

    def _synthesize_requirements(self, db: Session, user_id: int) -> int:
        """Step 5: Convert answered questions into structured requirements."""
        unprocessed = (
            db.query(POQuestion)
            .filter(
                POQuestion.user_id == user_id,
                POQuestion.status == "answered",
            )
            .all()
        )

        already_sourced: set[int] = set()
        for req in db.query(PORequirement).filter(PORequirement.user_id == user_id).all():
            if req.source_questions_json:
                try:
                    ids = json.loads(req.source_questions_json)
                    already_sourced.update(ids)
                except Exception:
                    pass

        new_questions = [q for q in unprocessed if q.id not in already_sourced]
        if not new_questions:
            return 0

        qa_text = "\n".join(
            f"- [{q.category}] Q: {q.question} A: {q.answer}"
            for q in new_questions[:5]
        )
        prompt = (
            "You are a Product Owner synthesizing user stories from Q&A sessions.\n\n"
            f"New Q&A:\n{qa_text}\n\n"
            "Extract 1-3 structured requirements / user stories. Each should have:\n"
            "- title (short)\n"
            "- description (1-2 sentences)\n"
            "- priority: critical/high/medium/low\n"
            "- acceptance_criteria (comma-separated testable conditions)\n\n"
            "Return ONLY valid JSON:\n"
            "{\"requirements\": [{\"title\": \"...\", \"description\": \"...\", "
            "\"priority\": \"...\", \"acceptance_criteria\": \"...\"}]}\n"
        )

        reply = call_llm(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=600,
            trace_id="po-synthesize",
        )
        if not reply:
            return 0

        try:
            start = reply.find("{")
            end = reply.rfind("}")
            data = json.loads(reply[start: end + 1])
            reqs = data.get("requirements", [])
        except Exception:
            return 0

        q_ids = [q.id for q in new_questions[:5]]
        added = 0
        for r in reqs:
            title = r.get("title", "").strip()
            if not title:
                continue
            db.add(PORequirement(
                user_id=user_id,
                title=title,
                description=r.get("description", ""),
                priority=r.get("priority", "medium"),
                acceptance_criteria=r.get("acceptance_criteria", ""),
                source_questions_json=json.dumps(q_ids),
            ))
            added += 1

        if added:
            db.commit()
        return added

    def _get_confidence(self, db: Session, user_id: int) -> float:
        state = self.get_state(db, user_id)
        return state.confidence if state else 0.0

    def _update_state(self, db: Session, user_id: int, context: dict, report: dict) -> float:
        """Step 6: Recalculate overall project understanding and save state."""
        answered_count = len(context.get("answered_questions", []))
        reqs_count = context.get("requirements_count", 0) + report.get("new_requirements", 0)

        categories_covered = set()
        for qa in context.get("answered_questions", []):
            categories_covered.add(qa.get("category", ""))

        coverage = len(categories_covered) / max(len(_QUESTION_CATEGORIES), 1)
        depth = min(1.0, answered_count / 20)
        confidence = round(0.4 * coverage + 0.4 * depth + 0.2 * min(1.0, reqs_count / 10), 3)

        new_state = {
            **(context.get("state", {})),
            "answered_questions_total": answered_count,
            "requirements_total": reqs_count,
            "categories_covered": list(categories_covered),
            "coverage": coverage,
            "depth": depth,
            "last_cycle": datetime.utcnow().isoformat(),
        }

        self.save_state(db, user_id, new_state, confidence)
        return confidence

    def _generate_recommendations(self, db: Session, user_id: int, context: dict) -> int:
        """Step 7: Create findings (tech suggestions, risk flags, priority adjustments)."""
        state = context.get("state", {})
        reqs = context.get("requirements", [])

        if not reqs and not state:
            return 0

        state_text = json.dumps(state, indent=2)[:1000]
        reqs_text = "\n".join(
            f"- [{r['priority']}] {r['title']} ({r['status']})"
            for r in reqs[:10]
        ) or "No requirements yet."

        prompt = (
            "You are a Product Owner reviewing a project. Generate 1-3 actionable findings.\n\n"
            f"Project state:\n{state_text}\n\n"
            f"Requirements:\n{reqs_text}\n\n"
            "Findings can be: tech_suggestion, risk_flag, priority_adjustment, opportunity.\n"
            "Return ONLY valid JSON:\n"
            "{\"findings\": [{\"category\": \"...\", \"title\": \"...\", "
            "\"description\": \"...\", \"severity\": \"info|warn|critical\"}]}\n"
        )

        reply = call_llm(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=500,
            trace_id="po-recommendations",
        )
        if not reply:
            return 0

        try:
            start = reply.find("{")
            end = reply.rfind("}")
            data = json.loads(reply[start: end + 1])
            findings_data = data.get("findings", [])
        except Exception:
            return 0

        added = 0
        for f in findings_data:
            title = f.get("title", "").strip()
            if not title:
                continue
            self.publish_finding(
                db, user_id,
                category=f.get("category", "general"),
                title=title,
                description=f.get("description", ""),
                severity=f.get("severity", "info"),
            )
            added += 1

        return added

    def _publish_to_bus(self, db: Session, user_id: int, report: dict) -> None:
        """Step 8: Send summary to other agents (PM, Architect)."""
        summary = {
            "type": "cycle_complete",
            "new_questions": report.get("new_questions", 0),
            "new_requirements": report.get("new_requirements", 0),
            "confidence": report.get("confidence", 0.0),
        }
        for target in ["project_manager", "architect"]:
            self.send_message(db, user_id, target, "cycle_summary", summary)

    # ── PO-specific API methods ───────────────────────────────────────

    def get_next_question(self, db: Session, user_id: int) -> POQuestion | None:
        return (
            db.query(POQuestion)
            .filter(POQuestion.user_id == user_id, POQuestion.status == "pending")
            .order_by(POQuestion.priority.desc(), POQuestion.asked_at.asc())
            .first()
        )

    def get_questions(self, db: Session, user_id: int, status: str | None = None, limit: int = 20) -> list[POQuestion]:
        q = db.query(POQuestion).filter(POQuestion.user_id == user_id)
        if status:
            q = q.filter(POQuestion.status == status)
        return q.order_by(POQuestion.priority.desc(), POQuestion.asked_at.desc()).limit(limit).all()

    def answer_question(self, db: Session, question_id: int, answer: str) -> POQuestion | None:
        q = db.query(POQuestion).get(question_id)
        if not q:
            return None
        q.status = "answered"
        q.answer = answer
        q.answered_at = datetime.utcnow()
        db.commit()
        db.refresh(q)
        return q

    def skip_question(self, db: Session, question_id: int) -> POQuestion | None:
        q = db.query(POQuestion).get(question_id)
        if not q:
            return None
        q.status = "skipped"
        db.commit()
        return q

    def get_requirements(self, db: Session, user_id: int, limit: int = 30) -> list[PORequirement]:
        return (
            db.query(PORequirement)
            .filter(PORequirement.user_id == user_id)
            .order_by(
                func.case(
                    (PORequirement.priority == "critical", 1),
                    (PORequirement.priority == "high", 2),
                    (PORequirement.priority == "medium", 3),
                    else_=4,
                ),
                PORequirement.created_at.desc(),
            )
            .limit(limit)
            .all()
        )

    def push_requirement_to_planner(self, db: Session, user_id: int, requirement_id: int) -> dict:
        """Create a planner task from a PO requirement."""
        req = db.query(PORequirement).get(requirement_id)
        if not req:
            return {"ok": False, "error": "Requirement not found"}

        try:
            from ...services import planner_service
            task = planner_service.create_task(
                db,
                user_id=user_id,
                title=req.title,
                description=f"{req.description}\n\nAcceptance Criteria:\n{req.acceptance_criteria or 'TBD'}",
                priority=req.priority,
            )
            req.status = "in_planner"
            db.commit()
            return {"ok": True, "task_id": task.id if task else None}
        except Exception as e:
            logger.warning("[po] push_to_planner failed: %s", e)
            return {"ok": False, "error": str(e)}

    def get_metrics(self, db: Session, user_id: int) -> Dict[str, Any]:
        base = super().get_metrics(db, user_id)
        pending_qs = db.query(func.count(POQuestion.id)).filter(
            POQuestion.user_id == user_id, POQuestion.status == "pending",
        ).scalar() or 0
        answered_qs = db.query(func.count(POQuestion.id)).filter(
            POQuestion.user_id == user_id, POQuestion.status == "answered",
        ).scalar() or 0
        total_reqs = db.query(func.count(PORequirement.id)).filter(
            PORequirement.user_id == user_id,
        ).scalar() or 0
        ready_reqs = db.query(func.count(PORequirement.id)).filter(
            PORequirement.user_id == user_id, PORequirement.status == "ready",
        ).scalar() or 0

        base.update({
            "pending_questions": pending_qs,
            "answered_questions": answered_qs,
            "total_requirements": total_reqs,
            "ready_requirements": ready_reqs,
        })
        return base
