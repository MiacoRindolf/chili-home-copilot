"""Frontend Engineer Agent — analyzes frontend code, UI patterns, and modern practices.

Self-evolving: researches latest frontend trends, evaluates templates and
component patterns via Code Brain, generates recommendations, and coordinates
with the UX agent and PO requirements.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict

from sqlalchemy.orm import Session
from sqlalchemy import func

from ..base import AgentBase
from ....models.project_brain import AgentFinding, ProjectAgentState
from ...llm_caller import call_llm

logger = logging.getLogger(__name__)

_FE_ROLE_PROMPT = (
    "You are Chili's Frontend Engineer agent — a self-evolving AI that deeply understands "
    "frontend architecture, component design, CSS systems, JavaScript patterns, accessibility, "
    "and performance optimization. You analyze frontend code for anti-patterns, inconsistent "
    "styling, poor component structure, and accessibility gaps. You stay current with the "
    "latest frontend frameworks (React, Vue, Svelte, HTMX), CSS methodologies, build tools, "
    "and web platform APIs. You proactively recommend modern patterns, bundle optimizations, "
    "and responsive design improvements."
)


class FrontendEngineerAgent(AgentBase):
    name = "frontend"
    label = "Frontend"
    icon = "\U0001F3A8"
    role_prompt = _FE_ROLE_PROMPT
    active = True

    def run_cycle(self, db: Session, user_id: int) -> Dict[str, Any]:
        steps_done = 0
        report: Dict[str, Any] = {}

        msgs = self._process_messages(db, user_id)
        report["messages_processed"] = msgs
        steps_done += 1

        snapshot = self._analyze_frontend_code(db)
        report["files_analyzed"] = snapshot.get("frontend_files", 0)
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

        self._cross_reference_ux(db, user_id)
        steps_done += 1

        old_conf = self._get_confidence(db, user_id)
        new_conf = self._update_state(db, user_id, snapshot, report)
        report["confidence"] = new_conf
        steps_done += 1

        self._publish_summary(db, user_id, report)
        steps_done += 1

        if abs(new_conf - old_conf) > 0.01:
            self.evolve(db, user_id, "frontend_expertise",
                        f"Cycle: {snapshot.get('frontend_files', 0)} files, {patterns.get('issues_found', 0)} issues",
                        old_conf, new_conf, trigger="learning_cycle")

        return {"steps": steps_done, **report}

    def _process_messages(self, db: Session, user_id: int) -> int:
        messages = self.get_messages(db, user_id)
        for msg in messages:
            self.acknowledge_message(db, msg)
        return len(messages)

    def _analyze_frontend_code(self, db: Session) -> Dict[str, Any]:
        try:
            from ....models.code_brain import CodeRepo, CodeHotspot
            repos = db.query(CodeRepo).filter(CodeRepo.active.is_(True)).all()
            hotspots = (
                db.query(CodeHotspot)
                .order_by(CodeHotspot.combined_score.desc())
                .limit(30)
                .all()
            )
            fe_extensions = (".html", ".css", ".js", ".jsx", ".tsx", ".ts", ".vue", ".svelte", ".scss", ".less")
            frontend_hotspots = [
                {"file": h.file_path, "score": round(h.combined_score, 3), "commits": h.commit_count}
                for h in hotspots
                if any(h.file_path.lower().endswith(ext) for ext in fe_extensions)
                or any(kw in h.file_path.lower() for kw in ["template", "component", "static", "public", "assets"])
            ]
            return {
                "repo_count": len(repos),
                "frontend_files": len(frontend_hotspots),
                "frontend_hotspots": frontend_hotspots[:10],
            }
        except Exception as e:
            logger.warning("[frontend] Code analysis failed: %s", e)
            return {"repo_count": 0, "frontend_files": 0, "frontend_hotspots": []}

    def _evaluate_patterns(self, db: Session, snapshot: dict) -> Dict[str, Any]:
        hotspots = snapshot.get("frontend_hotspots", [])
        if not hotspots:
            return {"issues_found": 0}

        files_text = "\n".join(
            f"- {h['file']} (complexity={h['score']}, commits={h['commits']})"
            for h in hotspots[:10]
        )
        prompt = (
            "You are a senior frontend engineer reviewing a project's frontend code hotspots.\n\n"
            f"High-churn/complexity frontend files:\n{files_text}\n\n"
            "Identify 2-5 potential issues (component bloat, CSS inconsistencies, accessibility gaps, "
            "bundle size concerns, state management issues, missing responsiveness).\n\n"
            "Return ONLY valid JSON:\n"
            "{\"issues\": [{\"file\": \"...\", \"issue\": \"...\", \"severity\": \"info|warn|critical\"}]}\n"
        )
        reply = call_llm(messages=[{"role": "user", "content": prompt}], max_tokens=500, trace_id="fe-patterns")
        if not reply:
            return {"issues_found": 0}
        try:
            data = json.loads(reply[reply.find("{"):reply.rfind("}") + 1])
            return {"issues_found": len(data.get("issues", [])), "issues": data.get("issues", [])}
        except Exception:
            return {"issues_found": 0}

    def _research_trends(self, db: Session, user_id: int, snapshot: dict) -> int:
        topics = [
            "modern frontend development best practices 2026 performance accessibility",
            "CSS architecture and component design patterns 2026",
        ]
        results = self.research(db, user_id, topics[:2], trace_id="fe-research")
        return len(results)

    def _generate_recommendations(self, db: Session, user_id: int, snapshot: dict, patterns: dict) -> int:
        issues = patterns.get("issues", [])
        hotspots = snapshot.get("frontend_hotspots", [])
        if not issues and not hotspots:
            return 0

        context = ""
        if issues:
            context += "Pattern issues:\n" + "\n".join(
                f"- [{i.get('severity', 'info')}] {i.get('file', '?')}: {i.get('issue', '')}"
                for i in issues[:5]
            ) + "\n\n"
        if hotspots:
            context += "Complex frontend files:\n" + "\n".join(
                f"- {h['file']} (score={h['score']})" for h in hotspots[:5]
            ) + "\n"

        prompt = (
            "You are a senior frontend engineer. Generate 2-4 actionable recommendations.\n\n"
            f"{context}\n"
            "Findings can be: component_refactor, css_architecture, accessibility, "
            "performance, responsive_design, state_management.\n"
            "Return ONLY valid JSON:\n"
            "{\"findings\": [{\"category\": \"...\", \"title\": \"...\", "
            "\"description\": \"...\", \"severity\": \"info|warn|critical\"}]}\n"
        )
        reply = call_llm(messages=[{"role": "user", "content": prompt}], max_tokens=600, trace_id="fe-findings")
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
            self.publish_finding(db, user_id, category=f.get("category", "frontend"),
                                title=title, description=f.get("description", ""),
                                severity=f.get("severity", "info"))
            added += 1
        return added

    def _cross_reference_ux(self, db: Session, user_id: int) -> None:
        findings = self.get_findings(db, user_id, limit=3)
        for f in findings:
            if f.category in ("accessibility", "responsive_design", "component_refactor"):
                self.send_message(db, user_id, "ux", "frontend_finding", {
                    "finding_id": f.id, "title": f.title, "category": f.category,
                })

    def _get_confidence(self, db: Session, user_id: int) -> float:
        state = self.get_state(db, user_id)
        return state.confidence if state else 0.0

    def _update_state(self, db: Session, user_id: int, snapshot: dict, report: dict) -> float:
        files = snapshot.get("frontend_files", 0)
        issues = report.get("pattern_issues", 0)
        research = report.get("research_done", 0)

        data_coverage = min(1.0, files / 15)
        issue_penalty = min(0.2, issues * 0.03)
        research_bonus = min(0.2, research * 0.1)
        confidence = round(0.4 * data_coverage + 0.3 - issue_penalty + research_bonus + 0.1, 3)
        confidence = max(0.0, min(1.0, confidence))

        new_state = {
            "frontend_files": files,
            "pattern_issues": issues,
            "research_done": research,
            "last_cycle": datetime.utcnow().isoformat(),
        }
        self.save_state(db, user_id, new_state, confidence)
        return confidence

    def _publish_summary(self, db: Session, user_id: int, report: dict) -> None:
        summary = {"type": "cycle_complete", "files_analyzed": report.get("files_analyzed", 0),
                   "pattern_issues": report.get("pattern_issues", 0), "confidence": report.get("confidence", 0)}
        for target in ["project_manager", "ux", "qa"]:
            self.send_message(db, user_id, target, "cycle_summary", summary)

    def get_chat_context(self, db: Session, user_id: int) -> str:
        parts = [f"[Project Brain — {self.label}]", self.role_prompt]
        state = self.get_state(db, user_id)
        if state and state.state_json:
            try:
                s = json.loads(state.state_json)
                parts.append(f"Frontend files: {s.get('frontend_files', 0)}, issues: {s.get('pattern_issues', 0)}")
            except Exception:
                pass
            parts.append(f"Confidence: {state.confidence:.0%}")
        findings = self.get_findings(db, user_id, limit=3)
        if findings:
            parts.append("Recent frontend findings:")
            for f in findings:
                parts.append(f"  [{f.severity}] {f.title}: {f.description[:100]}")
        return "\n".join(parts)
