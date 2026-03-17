"""DevOps Engineer Agent — CI/CD, infrastructure, deployment, and dependency health.

Self-evolving: analyzes deployment configs, evaluates infrastructure patterns,
researches latest DevOps trends, and monitors dependency health.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict

from sqlalchemy.orm import Session

from ..base import AgentBase
from ...llm_caller import call_llm

logger = logging.getLogger(__name__)

_DEVOPS_ROLE_PROMPT = (
    "You are Chili's DevOps Engineer agent — a self-evolving AI expert in CI/CD pipelines, "
    "container orchestration, infrastructure-as-code, monitoring, observability, and deployment "
    "strategies. You analyze Dockerfiles, CI configs, deployment scripts, and infrastructure "
    "definitions. You detect outdated dependencies, version conflicts, missing health checks, "
    "and scaling gaps. You stay current with the latest DevOps tools (GitHub Actions, Docker, "
    "Kubernetes, Terraform, Pulumi) and proactively recommend infrastructure improvements, "
    "automation opportunities, and reliability enhancements."
)


class DevOpsEngineerAgent(AgentBase):
    name = "devops"
    label = "DevOps"
    icon = "\U0001F680"
    role_prompt = _DEVOPS_ROLE_PROMPT
    active = True

    def run_cycle(self, db: Session, user_id: int) -> Dict[str, Any]:
        steps_done = 0
        report: Dict[str, Any] = {}

        msgs = self._process_messages(db, user_id)
        report["messages_processed"] = msgs
        steps_done += 1

        snapshot = self._analyze_infra(db)
        report["infra_files"] = snapshot.get("infra_files", 0)
        steps_done += 1

        patterns = self._evaluate_patterns(snapshot)
        report["pattern_issues"] = patterns.get("issues_found", 0)
        steps_done += 1

        dep_health = self._analyze_dependencies(db, snapshot)
        report["dep_issues"] = dep_health.get("issues", 0)
        steps_done += 1

        research_count = self._research_trends(db, user_id)
        report["research_done"] = research_count
        steps_done += 1

        findings = self._generate_recommendations(db, user_id, snapshot, patterns, dep_health)
        report["new_findings"] = findings
        steps_done += 1

        old_conf = self._get_confidence(db, user_id)
        new_conf = self._update_state(db, user_id, snapshot, report)
        report["confidence"] = new_conf
        steps_done += 1

        self._publish_summary(db, user_id, report)
        steps_done += 1

        if abs(new_conf - old_conf) > 0.01:
            self.evolve(db, user_id, "devops_expertise",
                        f"Cycle: {snapshot.get('infra_files', 0)} infra files, {dep_health.get('issues', 0)} dep issues",
                        old_conf, new_conf, trigger="learning_cycle")

        return {"steps": steps_done, **report}

    def _process_messages(self, db: Session, user_id: int) -> int:
        messages = self.get_messages(db, user_id)
        for msg in messages:
            self.acknowledge_message(db, msg)
        return len(messages)

    def _analyze_infra(self, db: Session) -> Dict[str, Any]:
        try:
            from ....models.code_brain import CodeHotspot
            hotspots = db.query(CodeHotspot).order_by(CodeHotspot.combined_score.desc()).limit(50).all()
            infra_keywords = ("docker", "dockerfile", "compose", "ci", "cd", "deploy", "terraform",
                              "pulumi", "k8s", "kubernetes", "helm", "github/workflows", ".github",
                              "nginx", "makefile", "procfile", "requirements", "package.json",
                              "pyproject", "setup.py", "setup.cfg")
            infra_files = [
                {"file": h.file_path, "score": round(h.combined_score, 3)}
                for h in hotspots
                if any(kw in h.file_path.lower() for kw in infra_keywords)
            ]
            return {"infra_files": len(infra_files), "files": infra_files[:10]}
        except Exception as e:
            logger.warning("[devops] Infra analysis failed: %s", e)
            return {"infra_files": 0, "files": []}

    def _evaluate_patterns(self, snapshot: dict) -> Dict[str, Any]:
        files = snapshot.get("files", [])
        if not files:
            return {"issues_found": 0}
        files_text = "\n".join(f"- {f['file']}" for f in files[:10])
        prompt = (
            "You are a DevOps engineer reviewing infrastructure files.\n\n"
            f"Infrastructure files found:\n{files_text}\n\n"
            "Identify 2-4 potential issues (missing health checks, no multi-stage Docker builds, "
            "missing CI caching, hardcoded secrets, no rollback strategy, missing monitoring).\n\n"
            "Return ONLY valid JSON:\n"
            "{\"issues\": [{\"file\": \"...\", \"issue\": \"...\", \"severity\": \"info|warn|critical\"}]}\n"
        )
        reply = call_llm(messages=[{"role": "user", "content": prompt}], max_tokens=500, trace_id="devops-patterns")
        if not reply:
            return {"issues_found": 0}
        try:
            data = json.loads(reply[reply.find("{"):reply.rfind("}") + 1])
            return {"issues_found": len(data.get("issues", [])), "issues": data.get("issues", [])}
        except Exception:
            return {"issues_found": 0}

    def _analyze_dependencies(self, db: Session, snapshot: dict) -> Dict[str, Any]:
        try:
            from ....models.code_brain import CodeDependency
            from sqlalchemy import func
            total = db.query(func.count(CodeDependency.id)).scalar() or 0
            return {"total_deps": total, "issues": 0}
        except Exception:
            return {"total_deps": 0, "issues": 0}

    def _research_trends(self, db: Session, user_id: int) -> int:
        topics = [
            "DevOps best practices CI/CD pipeline optimization 2026",
            "container security and Dockerfile best practices 2026",
        ]
        results = self.research(db, user_id, topics[:2], trace_id="devops-research")
        return len(results)

    def _generate_recommendations(self, db: Session, user_id: int, snapshot: dict,
                                  patterns: dict, dep_health: dict) -> int:
        issues = patterns.get("issues", [])
        if not issues and not snapshot.get("files"):
            return 0
        context = "Infrastructure issues:\n" + "\n".join(
            f"- [{i.get('severity', 'info')}] {i.get('file', '?')}: {i.get('issue', '')}"
            for i in issues[:5]
        ) if issues else "No specific issues detected."

        prompt = (
            "You are a senior DevOps engineer. Generate 2-3 actionable recommendations.\n\n"
            f"{context}\n\n"
            "Findings can be: ci_cd, containerization, monitoring, dependency_health, "
            "deployment_strategy, infrastructure.\n"
            "Return ONLY valid JSON:\n"
            "{\"findings\": [{\"category\": \"...\", \"title\": \"...\", "
            "\"description\": \"...\", \"severity\": \"info|warn|critical\"}]}\n"
        )
        reply = call_llm(messages=[{"role": "user", "content": prompt}], max_tokens=500, trace_id="devops-findings")
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
            self.publish_finding(db, user_id, category=f.get("category", "devops"),
                                title=title, description=f.get("description", ""),
                                severity=f.get("severity", "info"))
            added += 1
        return added

    def _get_confidence(self, db: Session, user_id: int) -> float:
        state = self.get_state(db, user_id)
        return state.confidence if state else 0.0

    def _update_state(self, db: Session, user_id: int, snapshot: dict, report: dict) -> float:
        files = snapshot.get("infra_files", 0)
        issues = report.get("pattern_issues", 0)
        coverage = min(1.0, files / 8)
        penalty = min(0.2, issues * 0.04)
        confidence = round(0.4 * coverage + 0.4 - penalty + 0.1, 3)
        confidence = max(0.0, min(1.0, confidence))
        new_state = {"infra_files": files, "pattern_issues": issues,
                     "dep_issues": report.get("dep_issues", 0),
                     "last_cycle": datetime.utcnow().isoformat()}
        self.save_state(db, user_id, new_state, confidence)
        return confidence

    def _publish_summary(self, db: Session, user_id: int, report: dict) -> None:
        summary = {"type": "cycle_complete", "infra_files": report.get("infra_files", 0),
                   "confidence": report.get("confidence", 0)}
        for target in ["project_manager", "architect"]:
            self.send_message(db, user_id, target, "cycle_summary", summary)

    def get_chat_context(self, db: Session, user_id: int) -> str:
        parts = [f"[Project Brain — {self.label}]", self.role_prompt]
        state = self.get_state(db, user_id)
        if state:
            parts.append(f"Confidence: {state.confidence:.0%}")
        findings = self.get_findings(db, user_id, limit=3)
        if findings:
            parts.append("Recent DevOps findings:")
            for f in findings:
                parts.append(f"  [{f.severity}] {f.title}: {f.description[:80]}")
        return "\n".join(parts)
