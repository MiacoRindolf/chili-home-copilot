"""Project Manager Agent — organizes work, tracks velocity, manages the Planner.

Receives requirements from the PO agent via the message bus, breaks them into
actionable tasks, maintains project timelines, and tracks velocity/burndown.
8-step learning cycle.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime
from typing import Any, Dict, List

from sqlalchemy.orm import Session
from sqlalchemy import func

from ..base import AgentBase
from ....models.project_brain import (
    AgentFinding, AgentGoal, AgentMessage, PORequirement, ProjectAgentState,
)
from ...llm_caller import call_llm

logger = logging.getLogger(__name__)

_PM_ROLE_PROMPT = (
    "You are Chili's Project Manager agent — a self-evolving AI that organizes work, "
    "tracks progress, manages timelines, and ensures the project moves forward smoothly. "
    "You receive requirements from the Product Owner, break them into actionable tasks, "
    "assign priorities, identify blockers, and provide velocity/burndown insights. "
    "You stay updated on modern project management practices and proactively flag risks."
)


class ProjectManagerAgent(AgentBase):
    name = "project_manager"
    label = "PM"
    icon = "\U0001F4CB"  # clipboard
    role_prompt = _PM_ROLE_PROMPT
    active = True

    # ── 8-Step Learning Cycle ─────────────────────────────────────────

    def run_cycle(self, db: Session, user_id: int) -> Dict[str, Any]:
        steps_done = 0
        report: Dict[str, Any] = {}

        # 1. Read messages from other agents (especially PO)
        new_reqs = self._process_incoming_messages(db, user_id)
        report["new_requirements_received"] = new_reqs
        steps_done += 1

        # 2. Review current project state from Planner
        planner_state = self._review_planner_state(db, user_id)
        report["projects"] = planner_state.get("project_count", 0)
        report["total_tasks"] = planner_state.get("total_tasks", 0)
        steps_done += 1

        # 3. Analyze velocity and identify blockers
        velocity = self._analyze_velocity(db, user_id, planner_state)
        report["velocity"] = velocity
        steps_done += 1

        # 4. Identify unplanned requirements
        unplanned = self._find_unplanned_requirements(db, user_id)
        report["unplanned_requirements"] = unplanned
        steps_done += 1

        # 5. Generate task breakdown for unplanned requirements
        tasks_created = self._breakdown_requirements(db, user_id, unplanned, planner_state)
        report["tasks_created"] = tasks_created
        steps_done += 1

        # 6. Research project management best practices
        research_count = self._research_practices(db, user_id, planner_state)
        report["research_done"] = research_count
        steps_done += 1

        # 7. Generate findings (risk flags, bottlenecks, recommendations)
        old_conf = self._get_confidence(db, user_id)
        findings = self._generate_findings(db, user_id, planner_state, velocity)
        report["new_findings"] = findings
        steps_done += 1

        # 8. Update state and evolve
        new_conf = self._update_state(db, user_id, planner_state, report)
        report["confidence"] = new_conf
        steps_done += 1

        if abs(new_conf - old_conf) > 0.01:
            self.evolve(db, user_id, "project_management",
                        f"Cycle completed: {tasks_created} tasks created, velocity={velocity}",
                        old_conf, new_conf, trigger="learning_cycle")

        summary = {
            "type": "cycle_complete",
            "tasks_created": tasks_created,
            "health": velocity.get("health", "unknown"),
            "completion_pct": velocity.get("completion_pct", 0),
            "confidence": new_conf,
        }
        for target in ["product_owner", "architect"]:
            self.send_message(db, user_id, target, "cycle_summary", summary)

        return {"steps": steps_done, **report}

    # ── Step implementations ──────────────────────────────────────────

    def _process_incoming_messages(self, db: Session, user_id: int) -> int:
        """Step 1: Read and acknowledge messages from other agents.

        Tracks new requirements signaled by PO cycle summaries so the
        breakdown step can pick them up.
        """
        messages = self.get_messages(db, user_id)
        count = 0
        for msg in messages:
            if msg.message_type == "finding" and msg.from_agent == "product_owner":
                logger.info("[pm] Received PO finding: %s", msg.content_json[:200] if msg.content_json else "")
            elif msg.message_type == "cycle_summary" and msg.from_agent == "product_owner":
                logger.info("[pm] PO cycle complete — new_reqs=%s", msg.content_json[:200] if msg.content_json else "")
            self.acknowledge_message(db, msg)
            count += 1
        return count

    def _review_planner_state(self, db: Session, user_id: int) -> Dict[str, Any]:
        """Step 2: Pull current project state from the Planner module."""
        try:
            from ...planner_service import list_projects, get_user_project_summary
            projects = list_projects(db, user_id)
            summary_text = get_user_project_summary(db, user_id)
        except Exception as e:
            logger.warning("[pm] Could not read planner: %s", e)
            projects = []
            summary_text = ""

        total_tasks = 0
        done_tasks = 0
        in_progress = 0
        blocked = 0
        overdue = 0
        today = date.today()

        for p in projects:
            for t in self._get_project_tasks(db, user_id, p.get("id")):
                total_tasks += 1
                status = t.get("status", "todo")
                if status == "done":
                    done_tasks += 1
                elif status == "in_progress":
                    in_progress += 1
                elif status == "blocked":
                    blocked += 1
                end = t.get("end_date")
                if end and status != "done":
                    try:
                        if date.fromisoformat(end) < today:
                            overdue += 1
                    except Exception:
                        pass

        return {
            "project_count": len(projects),
            "projects": projects,
            "total_tasks": total_tasks,
            "done_tasks": done_tasks,
            "in_progress": in_progress,
            "blocked": blocked,
            "overdue": overdue,
            "summary_text": summary_text,
        }

    def _get_project_tasks(self, db: Session, user_id: int, project_id: int | None) -> list:
        if not project_id:
            return []
        try:
            from ...planner_service import list_tasks
            return list_tasks(db, project_id, user_id)
        except Exception:
            return []

    def _analyze_velocity(self, db: Session, user_id: int, planner_state: dict) -> Dict[str, Any]:
        """Step 3: Calculate velocity metrics from planner data."""
        total = planner_state.get("total_tasks", 0)
        done = planner_state.get("done_tasks", 0)
        in_prog = planner_state.get("in_progress", 0)
        blocked = planner_state.get("blocked", 0)
        overdue = planner_state.get("overdue", 0)

        completion_pct = round((done / total * 100) if total > 0 else 0, 1)
        health = "healthy"
        if overdue > 0 or blocked > 2:
            health = "at_risk"
        if overdue > 3 or blocked > 5:
            health = "critical"

        return {
            "completion_pct": completion_pct,
            "done": done,
            "in_progress": in_prog,
            "blocked": blocked,
            "overdue": overdue,
            "health": health,
        }

    def _find_unplanned_requirements(self, db: Session, user_id: int) -> List[dict]:
        """Step 4: Find PO requirements that haven't been pushed to the Planner yet."""
        reqs = (
            db.query(PORequirement)
            .filter(
                PORequirement.user_id == user_id,
                PORequirement.status.in_(["draft", "refined", "ready"]),
            )
            .order_by(
                func.case(
                    (PORequirement.priority == "critical", 1),
                    (PORequirement.priority == "high", 2),
                    (PORequirement.priority == "medium", 3),
                    else_=4,
                )
            )
            .limit(10)
            .all()
        )
        return [
            {
                "id": r.id,
                "title": r.title,
                "description": r.description,
                "priority": r.priority,
                "acceptance_criteria": r.acceptance_criteria,
            }
            for r in reqs
        ]

    def _breakdown_requirements(
        self, db: Session, user_id: int,
        unplanned: List[dict], planner_state: dict,
    ) -> int:
        """Step 5: Use LLM to break requirements into Planner tasks."""
        if not unplanned:
            return 0

        projects = planner_state.get("projects", [])
        project_id = projects[0]["id"] if projects else None

        if not project_id:
            project_id = self._ensure_project(db, user_id)
            if not project_id:
                return 0

        reqs_text = "\n".join(
            f"- [{r['priority']}] {r['title']}: {r['description']} "
            f"(AC: {r.get('acceptance_criteria', 'TBD')})"
            for r in unplanned[:5]
        )
        prompt = (
            "You are a Project Manager breaking requirements into actionable tasks.\n\n"
            f"Requirements:\n{reqs_text}\n\n"
            "For each requirement, create 1-3 concrete tasks. Each task must have:\n"
            "- title (short, actionable)\n"
            "- description (1-2 sentences, what to do)\n"
            "- priority: critical/high/medium/low\n\n"
            "Return ONLY valid JSON:\n"
            "{\"tasks\": [{\"req_title\": \"...\", \"title\": \"...\", "
            "\"description\": \"...\", \"priority\": \"...\"}]}\n"
        )

        reply = call_llm(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=800,
            trace_id="pm-breakdown",
        )
        if not reply:
            return 0

        try:
            start = reply.find("{")
            end = reply.rfind("}")
            data = json.loads(reply[start: end + 1])
            tasks = data.get("tasks", [])
        except Exception:
            return 0

        created = 0
        for t_data in tasks:
            title = t_data.get("title", "").strip()
            if not title:
                continue
            try:
                from ...planner_service import create_task
                result = create_task(
                    db, project_id, user_id,
                    title=title,
                    description=t_data.get("description", ""),
                    priority=t_data.get("priority", "medium"),
                )
                if result:
                    created += 1
            except Exception as e:
                logger.warning("[pm] create_task failed: %s", e)

        for r in unplanned[:5]:
            req = db.query(PORequirement).get(r["id"])
            if req and req.status in ("draft", "refined", "ready"):
                req.status = "in_planner"
                db.commit()

        return created

    def _ensure_project(self, db: Session, user_id: int) -> int | None:
        """Create a default project if none exists."""
        try:
            from ...planner_service import create_project
            result = create_project(db, user_id, "Chili Project", description="Auto-created by PM agent")
            return result.get("id") if result else None
        except Exception as e:
            logger.warning("[pm] create_project failed: %s", e)
            return None

    def _research_practices(self, db: Session, user_id: int, planner_state: dict) -> int:
        """Step 6: Research project management best practices."""
        from ...config import settings
        health = "at_risk"
        for p in (planner_state.get("projects") or []):
            pass  # could derive domain from project names

        topics = [
            "agile project management best practices for small teams 2026",
        ]
        blocked = planner_state.get("blocked", 0)
        if blocked > 0:
            topics.append("how to resolve blocked tasks in software projects")

        max_searches = getattr(settings, "project_brain_max_web_searches", 5)
        results = self.research(db, user_id, topics[:max_searches], trace_id="pm-research")
        return len(results)

    def _get_confidence(self, db: Session, user_id: int) -> float:
        state = self.get_state(db, user_id)
        return state.confidence if state else 0.0

    def _generate_findings(
        self, db: Session, user_id: int,
        planner_state: dict, velocity: dict,
    ) -> int:
        """Step 7: Generate risk flags, bottleneck warnings, and recommendations."""
        summary = planner_state.get("summary_text", "")
        vel_text = json.dumps(velocity, indent=2)

        if not summary and not velocity.get("done", 0):
            return 0

        prompt = (
            "You are a PM agent reviewing project health. Generate 1-3 actionable findings.\n\n"
            f"Project summary:\n{summary[:1500]}\n\n"
            f"Velocity metrics:\n{vel_text}\n\n"
            "Findings can be: risk_flag, bottleneck, priority_adjustment, process_improvement.\n"
            "Return ONLY valid JSON:\n"
            "{\"findings\": [{\"category\": \"...\", \"title\": \"...\", "
            "\"description\": \"...\", \"severity\": \"info|warn|critical\"}]}\n"
        )

        reply = call_llm(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=500,
            trace_id="pm-findings",
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

    def _update_state(self, db: Session, user_id: int, planner_state: dict, report: dict) -> float:
        """Step 8: Recalculate confidence and save state."""
        total = planner_state.get("total_tasks", 0)
        done = planner_state.get("done_tasks", 0)
        blocked = planner_state.get("blocked", 0)
        overdue = planner_state.get("overdue", 0)

        task_coverage = min(1.0, total / 20) if total > 0 else 0.0
        completion = (done / total) if total > 0 else 0.0
        health_penalty = min(0.3, (blocked + overdue) * 0.05)
        confidence = round(0.3 * task_coverage + 0.5 * completion - health_penalty + 0.2, 3)
        confidence = max(0.0, min(1.0, confidence))

        new_state = {
            "total_tasks": total,
            "done_tasks": done,
            "blocked": blocked,
            "overdue": overdue,
            "project_count": planner_state.get("project_count", 0),
            "tasks_created_this_cycle": report.get("tasks_created", 0),
            "health": report.get("velocity", {}).get("health", "unknown"),
            "last_cycle": datetime.utcnow().isoformat(),
        }

        self.save_state(db, user_id, new_state, confidence)
        return confidence

    # ── PM-specific API methods ───────────────────────────────────────

    def get_velocity(self, db: Session, user_id: int) -> Dict[str, Any]:
        """Return current velocity metrics for the dashboard."""
        planner_state = self._review_planner_state(db, user_id)
        return self._analyze_velocity(db, user_id, planner_state)

    def get_project_health(self, db: Session, user_id: int) -> Dict[str, Any]:
        """Return a comprehensive project health report."""
        planner_state = self._review_planner_state(db, user_id)
        velocity = self._analyze_velocity(db, user_id, planner_state)
        return {
            **planner_state,
            "velocity": velocity,
        }

    def get_task_breakdown(self, db: Session, user_id: int) -> Dict[str, Any]:
        """Return task status breakdown by project."""
        try:
            from ...planner_service import list_projects
            projects = list_projects(db, user_id)
        except Exception:
            projects = []

        breakdown = []
        for p in projects:
            tasks = self._get_project_tasks(db, user_id, p.get("id"))
            by_status = {}
            by_priority = {}
            for t in tasks:
                s = t.get("status", "todo")
                by_status[s] = by_status.get(s, 0) + 1
                pr = t.get("priority", "medium")
                by_priority[pr] = by_priority.get(pr, 0) + 1
            breakdown.append({
                "project_id": p.get("id"),
                "project_name": p.get("name", ""),
                "total": len(tasks),
                "by_status": by_status,
                "by_priority": by_priority,
            })
        return {"projects": breakdown}

    def get_chat_context(self, db: Session, user_id: int) -> str:
        """Richer PM context: velocity, health, blockers, overdue tasks."""
        parts = [f"[Project Brain — {self.label}]", self.role_prompt]
        try:
            velocity = self.get_velocity(db, user_id)
            parts.append(
                f"Project health: {velocity.get('health', 'unknown')} — "
                f"{velocity.get('completion_pct', 0)}% complete, "
                f"{velocity.get('done', 0)} done, {velocity.get('in_progress', 0)} in progress, "
                f"{velocity.get('blocked', 0)} blocked, {velocity.get('overdue', 0)} overdue"
            )
        except Exception:
            pass

        state = self.get_state(db, user_id)
        if state:
            parts.append(f"PM confidence: {state.confidence:.0%}")

        findings = self.get_findings(db, user_id, limit=3)
        if findings:
            parts.append("Recent PM findings:")
            for f in findings:
                parts.append(f"  [{f.severity}] {f.title}: {f.description[:100]}")

        return "\n".join(parts)

    def get_metrics(self, db: Session, user_id: int) -> Dict[str, Any]:
        base = super().get_metrics(db, user_id)
        try:
            velocity = self.get_velocity(db, user_id)
            base.update({
                "completion_pct": velocity.get("completion_pct", 0),
                "done_tasks": velocity.get("done", 0),
                "in_progress_tasks": velocity.get("in_progress", 0),
                "blocked_tasks": velocity.get("blocked", 0),
                "overdue_tasks": velocity.get("overdue", 0),
                "health": velocity.get("health", "unknown"),
            })
        except Exception:
            pass
        return base
