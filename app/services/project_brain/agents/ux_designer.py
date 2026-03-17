"""UX Designer Agent — evaluates user experience through heuristics and accessibility.

Self-evolving: applies Nielsen's 10 heuristics, WCAG accessibility guidelines,
and Gestalt principles to analyze the project's UI. Researches modern UX trends
and publishes findings to Frontend and PM agents.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict

from sqlalchemy.orm import Session

from ..base import AgentBase
from ....models.project_brain import AgentFinding, ProjectAgentState
from ...llm_caller import call_llm

logger = logging.getLogger(__name__)

_NIELSEN_HEURISTICS = [
    "Visibility of system status",
    "Match between system and the real world",
    "User control and freedom",
    "Consistency and standards",
    "Error prevention",
    "Recognition rather than recall",
    "Flexibility and efficiency of use",
    "Aesthetic and minimalist design",
    "Help users recognize, diagnose, and recover from errors",
    "Help and documentation",
]

_UX_ROLE_PROMPT = (
    "You are Chili's UX Designer agent — a self-evolving AI with deep expertise in "
    "user experience design. You evaluate interfaces against Nielsen's 10 usability "
    "heuristics, WCAG 2.2 accessibility guidelines, and Gestalt principles. You analyze "
    "navigation flows, form design, feedback mechanisms, error states, loading states, "
    "and information architecture. You research the latest UX trends (micro-interactions, "
    "progressive disclosure, mobile-first, dark patterns to avoid) and proactively "
    "recommend improvements that reduce user friction and increase task completion."
)


class UXDesignerAgent(AgentBase):
    name = "ux"
    label = "UX"
    icon = "\U0001F441"
    role_prompt = _UX_ROLE_PROMPT
    active = True

    def run_cycle(self, db: Session, user_id: int) -> Dict[str, Any]:
        steps_done = 0
        report: Dict[str, Any] = {}

        msgs = self._process_messages(db, user_id)
        report["messages_processed"] = msgs
        steps_done += 1

        snapshot = self._analyze_ui_templates(db)
        report["templates_analyzed"] = snapshot.get("template_count", 0)
        steps_done += 1

        heuristic_results = self._evaluate_heuristics(db, user_id, snapshot)
        report["heuristic_violations"] = heuristic_results.get("violations", 0)
        steps_done += 1

        a11y_results = self._audit_accessibility(db, user_id, snapshot)
        report["accessibility_issues"] = a11y_results.get("issues", 0)
        steps_done += 1

        research_count = self._research_trends(db, user_id)
        report["research_done"] = research_count
        steps_done += 1

        findings = self._generate_ux_findings(db, user_id, heuristic_results, a11y_results)
        report["new_findings"] = findings
        steps_done += 1

        old_conf = self._get_confidence(db, user_id)
        new_conf = self._update_state(db, user_id, snapshot, report, heuristic_results)
        report["confidence"] = new_conf
        steps_done += 1

        self._publish_summary(db, user_id, report)
        steps_done += 1

        if abs(new_conf - old_conf) > 0.01:
            self.evolve(db, user_id, "ux_expertise",
                        f"Cycle: {heuristic_results.get('violations', 0)} heuristic violations, "
                        f"{a11y_results.get('issues', 0)} a11y issues",
                        old_conf, new_conf, trigger="learning_cycle")

        return {"steps": steps_done, **report}

    def _process_messages(self, db: Session, user_id: int) -> int:
        messages = self.get_messages(db, user_id)
        for msg in messages:
            self.acknowledge_message(db, msg)
        return len(messages)

    def _analyze_ui_templates(self, db: Session) -> Dict[str, Any]:
        try:
            from ....models.code_brain import CodeHotspot
            hotspots = (
                db.query(CodeHotspot)
                .order_by(CodeHotspot.combined_score.desc())
                .limit(50)
                .all()
            )
            ui_extensions = (".html", ".jsx", ".tsx", ".vue", ".svelte")
            templates = [
                {"file": h.file_path, "score": round(h.combined_score, 3), "commits": h.commit_count}
                for h in hotspots
                if any(h.file_path.lower().endswith(ext) for ext in ui_extensions)
                or "template" in h.file_path.lower()
            ]
            return {"template_count": len(templates), "templates": templates[:10]}
        except Exception as e:
            logger.warning("[ux] Template analysis failed: %s", e)
            return {"template_count": 0, "templates": []}

    def _evaluate_heuristics(self, db: Session, user_id: int, snapshot: dict) -> Dict[str, Any]:
        templates = snapshot.get("templates", [])
        if not templates:
            return {"violations": 0, "results": []}

        files_text = "\n".join(f"- {t['file']} (complexity={t['score']})" for t in templates[:8])
        heuristics_text = "\n".join(f"{i+1}. {h}" for i, h in enumerate(_NIELSEN_HEURISTICS))

        prompt = (
            "You are a UX expert evaluating an application against Nielsen's 10 usability heuristics.\n\n"
            f"UI template files:\n{files_text}\n\n"
            f"Heuristics:\n{heuristics_text}\n\n"
            "Based on the file names and complexity, evaluate which heuristics are likely violated. "
            "For each violation, provide the heuristic number, a brief description, and severity.\n\n"
            "Return ONLY valid JSON:\n"
            "{\"violations\": [{\"heuristic\": 1, \"name\": \"...\", \"issue\": \"...\", "
            "\"severity\": \"info|warn|critical\"}]}\n"
        )
        reply = call_llm(messages=[{"role": "user", "content": prompt}], max_tokens=600, trace_id="ux-heuristics")
        if not reply:
            return {"violations": 0, "results": []}
        try:
            data = json.loads(reply[reply.find("{"):reply.rfind("}") + 1])
            violations = data.get("violations", [])
            return {"violations": len(violations), "results": violations}
        except Exception:
            return {"violations": 0, "results": []}

    def _audit_accessibility(self, db: Session, user_id: int, snapshot: dict) -> Dict[str, Any]:
        templates = snapshot.get("templates", [])
        if not templates:
            return {"issues": 0, "results": []}

        files_text = "\n".join(f"- {t['file']}" for t in templates[:8])
        prompt = (
            "You are a WCAG 2.2 accessibility expert auditing a web application.\n\n"
            f"UI files:\n{files_text}\n\n"
            "Identify the top 3-5 likely accessibility issues based on common patterns "
            "(missing ARIA labels, color contrast, keyboard navigation, focus management, "
            "screen reader support, form labels, alt text).\n\n"
            "Return ONLY valid JSON:\n"
            "{\"issues\": [{\"wcag\": \"...\", \"issue\": \"...\", \"severity\": \"info|warn|critical\", "
            "\"fix\": \"...\"}]}\n"
        )
        reply = call_llm(messages=[{"role": "user", "content": prompt}], max_tokens=500, trace_id="ux-a11y")
        if not reply:
            return {"issues": 0, "results": []}
        try:
            data = json.loads(reply[reply.find("{"):reply.rfind("}") + 1])
            issues = data.get("issues", [])
            return {"issues": len(issues), "results": issues}
        except Exception:
            return {"issues": 0, "results": []}

    def _research_trends(self, db: Session, user_id: int) -> int:
        topics = [
            "UX design best practices 2026 micro-interactions progressive disclosure",
            "web accessibility WCAG 2.2 latest guidance and tools 2026",
        ]
        results = self.research(db, user_id, topics[:2], trace_id="ux-research")
        return len(results)

    def _generate_ux_findings(self, db: Session, user_id: int,
                              heuristic_results: dict, a11y_results: dict) -> int:
        violations = heuristic_results.get("results", [])
        a11y_issues = a11y_results.get("results", [])
        added = 0

        for v in violations[:3]:
            title = f"Heuristic #{v.get('heuristic', '?')}: {v.get('name', 'Unknown')}"
            self.publish_finding(db, user_id, category="heuristic_violation",
                                title=title, description=v.get("issue", ""),
                                severity=v.get("severity", "warn"))
            added += 1

        for a in a11y_issues[:3]:
            title = f"A11y: {a.get('wcag', 'WCAG')}"
            self.publish_finding(db, user_id, category="accessibility",
                                title=title, description=f"{a.get('issue', '')} — Fix: {a.get('fix', '')}",
                                severity=a.get("severity", "warn"))
            added += 1

        return added

    def _get_confidence(self, db: Session, user_id: int) -> float:
        state = self.get_state(db, user_id)
        return state.confidence if state else 0.0

    def _update_state(self, db: Session, user_id: int, snapshot: dict, report: dict,
                      heuristic_results: dict) -> float:
        templates = snapshot.get("template_count", 0)
        violations = report.get("heuristic_violations", 0)
        a11y = report.get("accessibility_issues", 0)

        coverage = min(1.0, templates / 10)
        violation_penalty = min(0.3, violations * 0.04)
        a11y_penalty = min(0.2, a11y * 0.04)
        confidence = round(0.4 * coverage + 0.4 - violation_penalty - a11y_penalty + 0.1, 3)
        confidence = max(0.0, min(1.0, confidence))

        heuristics_evaluated = list({v.get("heuristic", 0) for v in heuristic_results.get("results", [])})
        new_state = {
            "templates_analyzed": templates,
            "heuristic_violations": violations,
            "accessibility_issues": a11y,
            "heuristics_evaluated": heuristics_evaluated,
            "last_cycle": datetime.utcnow().isoformat(),
        }
        self.save_state(db, user_id, new_state, confidence)
        return confidence

    def _publish_summary(self, db: Session, user_id: int, report: dict) -> None:
        summary = {"type": "cycle_complete",
                   "heuristic_violations": report.get("heuristic_violations", 0),
                   "accessibility_issues": report.get("accessibility_issues", 0),
                   "confidence": report.get("confidence", 0)}
        for target in ["frontend", "project_manager", "qa"]:
            self.send_message(db, user_id, target, "cycle_summary", summary)

    def get_chat_context(self, db: Session, user_id: int) -> str:
        parts = [f"[Project Brain — {self.label}]", self.role_prompt]
        state = self.get_state(db, user_id)
        if state and state.state_json:
            try:
                s = json.loads(state.state_json)
                parts.append(f"Templates: {s.get('templates_analyzed', 0)}, "
                             f"heuristic violations: {s.get('heuristic_violations', 0)}, "
                             f"a11y issues: {s.get('accessibility_issues', 0)}")
            except Exception:
                pass
            parts.append(f"Confidence: {state.confidence:.0%}")
        findings = self.get_findings(db, user_id, limit=3)
        if findings:
            parts.append("Recent UX findings:")
            for f in findings:
                parts.append(f"  [{f.severity}] {f.title}: {f.description[:80]}")
        return "\n".join(parts)
