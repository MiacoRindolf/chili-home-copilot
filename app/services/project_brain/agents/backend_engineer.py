"""Backend Engineer Agent — analyzes backend code, patterns, and best practices.

Self-evolving: researches latest backend trends, evaluates project code via
Code Brain data, generates implementation recommendations, and coordinates
with PO requirements and Architect findings.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict, List

from sqlalchemy.orm import Session
from sqlalchemy import func

from ..base import AgentBase
from ....models.project_brain import AgentFinding, ProjectAgentState
from ...llm_caller import call_llm

logger = logging.getLogger(__name__)

_BE_ROLE_PROMPT = (
    "You are Chili's Backend Engineer agent — a self-evolving AI that deeply understands "
    "backend architecture, API design, database patterns, server performance, and service "
    "reliability. You analyze backend code for anti-patterns, missing error handling, "
    "inconsistent API shapes, and security gaps. You stay current with the latest backend "
    "frameworks, ORMs, caching strategies, and microservice patterns. You proactively "
    "recommend refactors, performance improvements, and modern patterns specific to the "
    "project's tech stack."
)


class BackendEngineerAgent(AgentBase):
    name = "backend"
    label = "Backend"
    icon = "\u2699"
    role_prompt = _BE_ROLE_PROMPT
    active = True

    def run_cycle(self, db: Session, user_id: int) -> Dict[str, Any]:
        steps_done = 0
        report: Dict[str, Any] = {}

        msgs = self._process_messages(db, user_id)
        report["messages_processed"] = msgs
        steps_done += 1

        snapshot = self._analyze_backend_code(db)
        report["files_analyzed"] = snapshot.get("backend_files", 0)
        steps_done += 1

        patterns = self._evaluate_patterns(db, snapshot)
        report["pattern_issues"] = patterns.get("issues_found", 0)
        steps_done += 1

        research_count = self._research_trends(db, user_id, snapshot)
        report["research_done"] = research_count
        steps_done += 1

        recommendations = self._generate_recommendations(db, user_id, snapshot, patterns)
        report["new_findings"] = recommendations
        steps_done += 1

        self._cross_reference_requirements(db, user_id, snapshot)
        steps_done += 1

        old_conf = self._get_confidence(db, user_id)
        new_conf = self._update_state(db, user_id, snapshot, report)
        report["confidence"] = new_conf
        steps_done += 1

        self._publish_summary(db, user_id, report)
        steps_done += 1

        if abs(new_conf - old_conf) > 0.01:
            self.evolve(db, user_id, "backend_expertise",
                        f"Cycle: {snapshot.get('backend_files', 0)} files, {patterns.get('issues_found', 0)} issues",
                        old_conf, new_conf, trigger="learning_cycle")

        return {"steps": steps_done, **report}

    def _process_messages(self, db: Session, user_id: int) -> int:
        messages = self.get_messages(db, user_id)
        for msg in messages:
            self.acknowledge_message(db, msg)
        return len(messages)

    def _analyze_backend_code(self, db: Session) -> Dict[str, Any]:
        try:
            from ....models.code_brain import CodeRepo, CodeDependency, CodeHotspot
            repos = db.query(CodeRepo).filter(CodeRepo.active.is_(True)).all()
            total_deps = db.query(func.count(CodeDependency.id)).scalar() or 0
            hotspots = (
                db.query(CodeHotspot)
                .filter(CodeHotspot.file_path.like("%.py"))
                .order_by(CodeHotspot.combined_score.desc())
                .limit(15)
                .all()
            )
            backend_hotspots = [
                {"file": h.file_path, "score": round(h.combined_score, 3), "commits": h.commit_count}
                for h in hotspots
                if any(kw in h.file_path.lower() for kw in
                       ["service", "route", "model", "api", "handler", "middleware", "db", "schema"])
            ]
            return {
                "repo_count": len(repos),
                "total_deps": total_deps,
                "backend_files": len(backend_hotspots),
                "backend_hotspots": backend_hotspots[:10],
            }
        except Exception as e:
            logger.warning("[backend] Code analysis failed: %s", e)
            return {"repo_count": 0, "total_deps": 0, "backend_files": 0, "backend_hotspots": []}

    def _evaluate_patterns(self, db: Session, snapshot: dict) -> Dict[str, Any]:
        hotspots = snapshot.get("backend_hotspots", [])
        if not hotspots:
            return {"issues_found": 0}

        files_text = "\n".join(
            f"- {h['file']} (complexity={h['score']}, commits={h['commits']})"
            for h in hotspots[:10]
        )
        prompt = (
            "You are a senior backend engineer reviewing a project's backend code hotspots.\n\n"
            f"High-churn/complexity backend files:\n{files_text}\n\n"
            "Based on these file names and complexity scores, identify 2-5 potential pattern issues "
            "(anti-patterns, missing concerns, coupling risks, naming inconsistencies).\n\n"
            "Return ONLY valid JSON:\n"
            "{\"issues\": [{\"file\": \"...\", \"issue\": \"...\", \"severity\": \"info|warn|critical\"}]}\n"
        )
        reply = call_llm(messages=[{"role": "user", "content": prompt}], max_tokens=500, trace_id="be-patterns")
        if not reply:
            return {"issues_found": 0}
        try:
            data = json.loads(reply[reply.find("{"):reply.rfind("}") + 1])
            return {"issues_found": len(data.get("issues", [])), "issues": data.get("issues", [])}
        except Exception:
            return {"issues_found": 0}

    def _research_trends(self, db: Session, user_id: int, snapshot: dict) -> int:
        state = self.get_state(db, user_id)
        tech = "Python FastAPI"
        if state and state.state_json:
            try:
                s = json.loads(state.state_json)
                tech = s.get("tech_stack", tech)
            except Exception:
                pass
        topics = [
            f"{tech} best practices and performance optimization 2026",
            "backend API design patterns and error handling best practices 2026",
        ]
        results = self.research(db, user_id, topics[:2], trace_id="be-research")
        return len(results)

    def _generate_recommendations(self, db: Session, user_id: int, snapshot: dict, patterns: dict) -> int:
        issues = patterns.get("issues", [])
        hotspots = snapshot.get("backend_hotspots", [])
        if not issues and not hotspots:
            return 0

        context = ""
        if issues:
            context += "Pattern issues found:\n" + "\n".join(
                f"- [{i.get('severity', 'info')}] {i.get('file', '?')}: {i.get('issue', '')}"
                for i in issues[:5]
            ) + "\n\n"
        if hotspots:
            context += "High complexity files:\n" + "\n".join(
                f"- {h['file']} (score={h['score']})"
                for h in hotspots[:5]
            ) + "\n"

        prompt = (
            "You are a senior backend engineer. Generate 2-4 actionable recommendations.\n\n"
            f"{context}\n"
            "Findings can be: refactoring_opportunity, performance_issue, api_pattern, "
            "error_handling, database_pattern, security_concern.\n"
            "Return ONLY valid JSON:\n"
            "{\"findings\": [{\"category\": \"...\", \"title\": \"...\", "
            "\"description\": \"...\", \"severity\": \"info|warn|critical\"}]}\n"
        )
        reply = call_llm(messages=[{"role": "user", "content": prompt}], max_tokens=600, trace_id="be-findings")
        if not reply:
            return 0
        try:
            data = json.loads(reply[reply.find("{"):reply.rfind("}") + 1])
            findings_data = data.get("findings", [])
        except Exception:
            return 0

        added = 0
        for f in findings_data:
            title = f.get("title", "").strip()
            if not title:
                continue
            self.publish_finding(db, user_id, category=f.get("category", "backend"),
                                title=title, description=f.get("description", ""),
                                severity=f.get("severity", "info"))
            added += 1
        return added

    def _cross_reference_requirements(self, db: Session, user_id: int, snapshot: dict) -> None:
        from ....models.project_brain import PORequirement
        reqs = (
            db.query(PORequirement)
            .filter(PORequirement.user_id == user_id, PORequirement.status.in_(["ready", "in_planner"]))
            .limit(5)
            .all()
        )
        if not reqs:
            return
        for req in reqs:
            self.send_message(db, user_id, "project_manager", "backend_review", {
                "requirement_id": req.id,
                "title": req.title,
                "backend_ready": True,
            })

    def _get_confidence(self, db: Session, user_id: int) -> float:
        state = self.get_state(db, user_id)
        return state.confidence if state else 0.0

    def _update_state(self, db: Session, user_id: int, snapshot: dict, report: dict) -> float:
        files = snapshot.get("backend_files", 0)
        issues = report.get("pattern_issues", 0)
        research = report.get("research_done", 0)

        data_coverage = min(1.0, files / 20)
        issue_penalty = min(0.2, issues * 0.03)
        research_bonus = min(0.2, research * 0.1)
        confidence = round(0.4 * data_coverage + 0.3 - issue_penalty + research_bonus + 0.1, 3)
        confidence = max(0.0, min(1.0, confidence))

        new_state = {
            "backend_files": files,
            "total_deps": snapshot.get("total_deps", 0),
            "pattern_issues": issues,
            "research_done": research,
            "last_cycle": datetime.utcnow().isoformat(),
        }
        self.save_state(db, user_id, new_state, confidence)
        return confidence

    def _publish_summary(self, db: Session, user_id: int, report: dict) -> None:
        summary = {
            "type": "cycle_complete",
            "files_analyzed": report.get("files_analyzed", 0),
            "pattern_issues": report.get("pattern_issues", 0),
            "confidence": report.get("confidence", 0),
        }
        for target in ["project_manager", "architect", "qa"]:
            self.send_message(db, user_id, target, "cycle_summary", summary)

    def get_chat_context(self, db: Session, user_id: int) -> str:
        parts = [f"[Project Brain — {self.label}]", self.role_prompt]
        state = self.get_state(db, user_id)
        if state and state.state_json:
            try:
                s = json.loads(state.state_json)
                parts.append(f"Backend files tracked: {s.get('backend_files', 0)}, pattern issues: {s.get('pattern_issues', 0)}")
            except Exception:
                pass
            parts.append(f"Confidence: {state.confidence:.0%}")
        findings = self.get_findings(db, user_id, limit=3)
        if findings:
            parts.append("Recent backend findings:")
            for f in findings:
                parts.append(f"  [{f.severity}] {f.title}: {f.description[:100]}")
        return "\n".join(parts)
