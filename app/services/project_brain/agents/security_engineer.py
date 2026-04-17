"""Security Engineer Agent — vulnerability scanning, code security, and threat research.

Self-evolving: scans dependencies for CVEs, reviews code for security patterns,
audits API endpoints for auth/CORS/rate-limiting, and researches emerging threats.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict

from sqlalchemy.orm import Session
from sqlalchemy import func

from ..base import AgentBase
from ...llm_caller import call_llm

logger = logging.getLogger(__name__)

_SEC_ROLE_PROMPT = (
    "You are Chili's Security Engineer agent — a self-evolving AI expert in application "
    "security. You scan dependencies for known CVEs, review authentication and authorization "
    "flows, detect SQL injection and XSS patterns, identify secrets in code, audit API "
    "endpoints for proper middleware (auth, rate limiting, CORS), and evaluate encryption "
    "practices. You stay current with the OWASP Top 10, latest CVE advisories, and "
    "framework-specific security guidance. You proactively flag risks with severity scoring "
    "and recommend remediations."
)


class SecurityEngineerAgent(AgentBase):
    name = "security"
    label = "Security"
    icon = "\U0001F512"
    role_prompt = _SEC_ROLE_PROMPT
    active = True

    def run_cycle(self, db: Session, user_id: int) -> Dict[str, Any]:
        steps_done = 0
        report: Dict[str, Any] = {}

        msgs = self._process_messages(db, user_id)
        report["messages_processed"] = msgs
        steps_done += 1

        dep_scan = self._scan_dependencies(db, user_id)
        report["dep_vulnerabilities"] = dep_scan.get("vulnerabilities", 0)
        steps_done += 1

        code_review = self._review_security_patterns(db, user_id)
        report["security_issues"] = code_review.get("issues", 0)
        steps_done += 1

        api_audit = self._audit_api_security(db, user_id)
        report["api_issues"] = api_audit.get("issues", 0)
        steps_done += 1

        research_count = self._research_threats(db, user_id)
        report["research_done"] = research_count
        steps_done += 1

        risk_score = self._calculate_risk_score(report)
        report["risk_score"] = risk_score
        steps_done += 1

        findings = self._publish_security_findings(db, user_id, dep_scan, code_review, api_audit)
        report["new_findings"] = findings
        steps_done += 1

        old_conf = self._get_confidence(db, user_id)
        new_conf = self._update_state(db, user_id, report)
        report["confidence"] = new_conf
        steps_done += 1

        self._publish_summary(db, user_id, report)

        if abs(new_conf - old_conf) > 0.01:
            self.evolve(db, user_id, "security_expertise",
                        f"Cycle: {dep_scan.get('vulnerabilities', 0)} dep vulns, "
                        f"{code_review.get('issues', 0)} code issues, risk={risk_score}",
                        old_conf, new_conf, trigger="learning_cycle")

        return {"steps": steps_done, **report}

    def _process_messages(self, db: Session, user_id: int) -> int:
        messages = self.get_messages(db, user_id)
        for msg in messages:
            self.acknowledge_message(db, msg)
        return len(messages)

    def _scan_dependencies(self, db: Session, user_id: int) -> Dict[str, Any]:
        try:
            from ....models.code_brain import CodeHotspot
            dep_files = (
                db.query(CodeHotspot)
                .filter(CodeHotspot.file_path.in_(["requirements.txt", "package.json", "Pipfile", "pyproject.toml"]))
                .all()
            )
            if not dep_files:
                return {"vulnerabilities": 0}

            files_text = ", ".join(f.file_path for f in dep_files)
            prompt = (
                "You are a security engineer scanning dependency files for vulnerabilities.\n\n"
                f"Dependency files found: {files_text}\n\n"
                "List 2-4 common vulnerability patterns in Python/JS projects "
                "(outdated packages, known CVE-prone packages, missing pinning).\n\n"
                "Return ONLY valid JSON:\n"
                "{\"vulnerabilities\": [{\"package\": \"...\", \"issue\": \"...\", "
                "\"severity\": \"low|medium|high|critical\"}]}\n"
            )
            reply = call_llm(messages=[{"role": "user", "content": prompt}], max_tokens=400, trace_id="sec-deps", cacheable=True)
            if not reply:
                return {"vulnerabilities": 0}
            data = json.loads(reply[reply.find("{"):reply.rfind("}") + 1])
            vulns = data.get("vulnerabilities", [])
            return {"vulnerabilities": len(vulns), "details": vulns}
        except Exception:
            return {"vulnerabilities": 0}

    def _review_security_patterns(self, db: Session, user_id: int) -> Dict[str, Any]:
        try:
            from ....models.code_brain import CodeHotspot
            hotspots = (
                db.query(CodeHotspot)
                .order_by(CodeHotspot.combined_score.desc())
                .limit(20)
                .all()
            )
            security_relevant = [
                h.file_path for h in hotspots
                if any(kw in h.file_path.lower() for kw in
                       ["auth", "login", "session", "token", "password", "encrypt", "secret",
                        "middleware", "permission", "route", "api", "handler"])
            ]
        except Exception:
            security_relevant = []

        if not security_relevant:
            return {"issues": 0}

        files_text = "\n".join(f"- {f}" for f in security_relevant[:10])
        prompt = (
            "You are a security engineer reviewing code files for security concerns.\n\n"
            f"Security-relevant files:\n{files_text}\n\n"
            "Identify 2-4 potential security issues (weak auth, missing input validation, "
            "hardcoded secrets, SQL injection risk, XSS patterns, insecure session handling).\n\n"
            "Return ONLY valid JSON:\n"
            "{\"issues\": [{\"file\": \"...\", \"issue\": \"...\", \"severity\": \"low|medium|high|critical\"}]}\n"
        )
        reply = call_llm(messages=[{"role": "user", "content": prompt}], max_tokens=500, trace_id="sec-code", cacheable=True)
        if not reply:
            return {"issues": 0}
        try:
            data = json.loads(reply[reply.find("{"):reply.rfind("}") + 1])
            issues = data.get("issues", [])
            return {"issues": len(issues), "details": issues}
        except Exception:
            return {"issues": 0}

    def _audit_api_security(self, db: Session, user_id: int) -> Dict[str, Any]:
        try:
            from ....models.code_brain import CodeHotspot
            route_files = (
                db.query(CodeHotspot)
                .filter(CodeHotspot.file_path.like("%route%"))
                .limit(10)
                .all()
            )
            if not route_files:
                return {"issues": 0}
            files_text = "\n".join(f"- {f.file_path}" for f in route_files)
            prompt = (
                "You are a security engineer auditing API route files.\n\n"
                f"Route files:\n{files_text}\n\n"
                "Check for: missing auth middleware, open CORS, no rate limiting, "
                "missing input validation, overly permissive responses.\n\n"
                "Return ONLY valid JSON:\n"
                "{\"issues\": [{\"file\": \"...\", \"issue\": \"...\", \"severity\": \"low|medium|high|critical\"}]}\n"
            )
            reply = call_llm(messages=[{"role": "user", "content": prompt}], max_tokens=400, trace_id="sec-api", cacheable=True)
            if not reply:
                return {"issues": 0}
            data = json.loads(reply[reply.find("{"):reply.rfind("}") + 1])
            return {"issues": len(data.get("issues", [])), "details": data.get("issues", [])}
        except Exception:
            return {"issues": 0}

    def _research_threats(self, db: Session, user_id: int) -> int:
        topics = [
            "OWASP Top 10 2025 2026 web application security threats",
            "Python FastAPI security best practices and CVE advisories 2026",
        ]
        results = self.research(db, user_id, topics[:2], trace_id="sec-research")
        return len(results)

    def _calculate_risk_score(self, report: dict) -> float:
        vulns = report.get("dep_vulnerabilities", 0)
        code_issues = report.get("security_issues", 0)
        api_issues = report.get("api_issues", 0)
        total = vulns + code_issues + api_issues
        if total == 0:
            return 0.0
        return round(min(10.0, total * 1.5), 1)

    def _publish_security_findings(self, db: Session, user_id: int,
                                   dep_scan: dict, code_review: dict, api_audit: dict) -> int:
        added = 0
        for source, category in [(dep_scan, "dependency_vulnerability"),
                                 (code_review, "code_security"), (api_audit, "api_security")]:
            for item in source.get("details", [])[:2]:
                title = item.get("issue", item.get("package", "Security issue"))[:200]
                self.publish_finding(db, user_id, category=category, title=title,
                                     description=json.dumps(item, ensure_ascii=False)[:500],
                                     severity=item.get("severity", "medium"))
                added += 1
        return added

    def _get_confidence(self, db: Session, user_id: int) -> float:
        state = self.get_state(db, user_id)
        return state.confidence if state else 0.0

    def _update_state(self, db: Session, user_id: int, report: dict) -> float:
        risk = report.get("risk_score", 0)
        research = report.get("research_done", 0)
        health = max(0.0, 1.0 - (risk / 15))
        research_bonus = min(0.2, research * 0.1)
        confidence = round(0.5 * health + 0.3 + research_bonus, 3)
        confidence = max(0.0, min(1.0, confidence))
        new_state = {"risk_score": risk, "dep_vulnerabilities": report.get("dep_vulnerabilities", 0),
                     "security_issues": report.get("security_issues", 0),
                     "api_issues": report.get("api_issues", 0),
                     "last_cycle": datetime.utcnow().isoformat()}
        self.save_state(db, user_id, new_state, confidence)
        return confidence

    def _publish_summary(self, db: Session, user_id: int, report: dict) -> None:
        summary = {"type": "cycle_complete", "risk_score": report.get("risk_score", 0),
                   "confidence": report.get("confidence", 0)}
        for target in ["project_manager", "architect", "devops"]:
            self.send_message(db, user_id, target, "cycle_summary", summary)

    def get_chat_context(self, db: Session, user_id: int) -> str:
        parts = [f"[Project Brain — {self.label}]", self.role_prompt]
        state = self.get_state(db, user_id)
        if state and state.state_json:
            try:
                s = json.loads(state.state_json)
                parts.append(f"Risk score: {s.get('risk_score', 0)}/10, "
                             f"dep vulns: {s.get('dep_vulnerabilities', 0)}, "
                             f"code issues: {s.get('security_issues', 0)}")
            except Exception:
                pass
            parts.append(f"Confidence: {state.confidence:.0%}")
        findings = self.get_findings(db, user_id, limit=3)
        if findings:
            parts.append("Recent security findings:")
            for f in findings:
                parts.append(f"  [{f.severity}] {f.title}: {f.description[:80]}")
        return "\n".join(parts)
