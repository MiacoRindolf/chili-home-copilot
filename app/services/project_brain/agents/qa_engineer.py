"""QA Engineer Agent — generates tests, detects bugs, and validates quality.

Self-evolving: generates test scenarios from requirements, analyzes code for
logical bugs and edge cases, runs browser-based tests via Playwright, uses
vision LLM for UI bug detection, and coordinates with PM/PO for priorities.
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
    AgentFinding, PORequirement, ProjectAgentState,
    QATestCase, QATestRun, QABugReport,
)
from ...llm_caller import call_llm

logger = logging.getLogger(__name__)

_QA_ROLE_PROMPT = (
    "You are Chili's QA Engineer agent — a self-evolving AI that ensures software quality "
    "through comprehensive testing strategies. You generate test scenarios from requirements, "
    "detect bugs through code analysis (even when no errors are visible), execute browser-based "
    "tests via Playwright, analyze screenshots for visual/UI bugs, and run accessibility checks. "
    "You stay current with the latest testing frameworks, E2E patterns, and quality assurance "
    "best practices. You work closely with the PM and PO to prioritize bug fixes and validate "
    "feature completeness. You think like a human tester — exploring edge cases, unexpected "
    "inputs, and user flow interruptions."
)


class QAEngineerAgent(AgentBase):
    name = "qa"
    label = "QA"
    icon = "\U0001F9EA"
    role_prompt = _QA_ROLE_PROMPT
    active = True

    def run_cycle(self, db: Session, user_id: int) -> Dict[str, Any]:
        steps_done = 0
        report: Dict[str, Any] = {}

        msgs = self._process_messages(db, user_id)
        report["messages_processed"] = msgs
        steps_done += 1

        new_tests = self._generate_test_scenarios(db, user_id)
        report["new_test_cases"] = new_tests
        steps_done += 1

        browser_results = self._execute_browser_tests(db, user_id)
        report["browser_tests_run"] = browser_results.get("tests_run", 0)
        report["browser_tests_passed"] = browser_results.get("passed", 0)
        steps_done += 1

        screenshot_bugs = self._analyze_screenshots(db, user_id)
        report["visual_bugs"] = screenshot_bugs
        steps_done += 1

        a11y_issues = self._check_accessibility(db, user_id)
        report["accessibility_issues"] = a11y_issues
        steps_done += 1

        code_bugs = self._detect_code_bugs(db, user_id)
        report["code_bugs_detected"] = code_bugs
        steps_done += 1

        self._prioritize_with_pm(db, user_id, report)
        steps_done += 1

        research_count = self._research_trends(db, user_id)
        report["research_done"] = research_count
        steps_done += 1

        old_conf = self._get_confidence(db, user_id)
        new_conf = self._update_state(db, user_id, report)
        report["confidence"] = new_conf
        steps_done += 1

        self._publish_summary(db, user_id, report)
        steps_done += 1

        if abs(new_conf - old_conf) > 0.01:
            self.evolve(db, user_id, "qa_expertise",
                        f"Cycle: {new_tests} test cases, {code_bugs} code bugs, {screenshot_bugs} visual bugs",
                        old_conf, new_conf, trigger="learning_cycle")

        return {"steps": steps_done, **report}

    def _process_messages(self, db: Session, user_id: int) -> int:
        messages = self.get_messages(db, user_id)
        for msg in messages:
            self.acknowledge_message(db, msg)
        return len(messages)

    def _generate_test_scenarios(self, db: Session, user_id: int) -> int:
        reqs = (
            db.query(PORequirement)
            .filter(PORequirement.user_id == user_id, PORequirement.status.in_(["ready", "in_planner"]))
            .limit(5)
            .all()
        )
        if not reqs:
            return 0

        existing_titles = {
            tc.name for tc in
            db.query(QATestCase.name).filter(QATestCase.user_id == user_id).all()
        }

        reqs_text = "\n".join(
            f"- [{r.priority}] {r.title}: {r.description[:100]}"
            f" (AC: {r.acceptance_criteria[:80] if r.acceptance_criteria else 'TBD'})"
            for r in reqs
        )
        prompt = (
            "You are a QA engineer creating test scenarios from requirements.\n\n"
            f"Requirements:\n{reqs_text}\n\n"
            "Generate 3-5 test scenarios. Each should have:\n"
            "- name: short test name\n"
            "- steps: array of human-readable test steps\n"
            "- expected: what should happen if the test passes\n"
            "- priority: critical/high/medium/low\n\n"
            "Return ONLY valid JSON:\n"
            "{\"tests\": [{\"name\": \"...\", \"steps\": [\"...\"], \"expected\": \"...\", \"priority\": \"...\"}]}\n"
        )
        reply = call_llm(messages=[{"role": "user", "content": prompt}], max_tokens=700, trace_id="qa-testgen", cacheable=True)
        if not reply:
            return 0

        try:
            data = json.loads(reply[reply.find("{"):reply.rfind("}") + 1])
            tests = data.get("tests", [])
        except Exception:
            return 0

        added = 0
        for t in tests:
            name = t.get("name", "").strip()
            if not name or name in existing_titles:
                continue
            db.add(QATestCase(
                user_id=user_id,
                name=name,
                steps_json=json.dumps(t.get("steps", []), ensure_ascii=False),
                expected_json=json.dumps(t.get("expected", ""), ensure_ascii=False),
                priority=t.get("priority", "medium"),
            ))
            existing_titles.add(name)
            added += 1

        if added:
            db.commit()
        return added

    def _execute_browser_tests(self, db: Session, user_id: int) -> Dict[str, Any]:
        try:
            from ..playwright_runner import run_smoke_tests
            results = run_smoke_tests()
            tests_run = len(results)
            passed = sum(1 for r in results if r.get("passed"))

            for r in results:
                db.add(QATestRun(
                    user_id=user_id,
                    test_name=r.get("name", "smoke"),
                    passed=r.get("passed", False),
                    errors_json=json.dumps(r.get("errors", []), ensure_ascii=False),
                    duration_ms=r.get("duration_ms", 0),
                    screenshot_path=r.get("screenshot_path"),
                ))
            if results:
                db.commit()

            return {"tests_run": tests_run, "passed": passed}
        except Exception as e:
            logger.info("[qa] Browser tests skipped: %s", e)
            return {"tests_run": 0, "passed": 0}

    def _analyze_screenshots(self, db: Session, user_id: int) -> int:
        try:
            from ..playwright_runner import screenshot_pages, analyze_screenshot
            screenshots = screenshot_pages()
            bugs_found = 0
            for ss in screenshots:
                analysis = analyze_screenshot(ss.get("path", ""))
                if analysis and analysis.get("bugs"):
                    for bug in analysis["bugs"]:
                        db.add(QABugReport(
                            user_id=user_id,
                            title=bug.get("title", "Visual bug"),
                            description=bug.get("description", ""),
                            severity=bug.get("severity", "warn"),
                            screenshot_path=ss.get("path"),
                            reproduction_steps=f"Page: {ss.get('url', 'unknown')}",
                        ))
                        bugs_found += 1
            if bugs_found:
                db.commit()
            return bugs_found
        except Exception as e:
            logger.info("[qa] Screenshot analysis skipped: %s", e)
            return 0

    def _check_accessibility(self, db: Session, user_id: int) -> int:
        try:
            from ..playwright_runner import run_accessibility_check
            results = run_accessibility_check()
            issues = 0
            for r in results:
                if r.get("violations"):
                    for v in r["violations"][:3]:
                        self.publish_finding(db, user_id, category="accessibility",
                                             title=f"A11y: {v.get('id', 'unknown')}",
                                             description=v.get("description", ""),
                                             severity="warn")
                        issues += 1
            return issues
        except Exception as e:
            logger.info("[qa] Accessibility check skipped: %s", e)
            return 0

    def _detect_code_bugs(self, db: Session, user_id: int) -> int:
        try:
            from ....models.code_brain import CodeHotspot
            hotspots = (
                db.query(CodeHotspot)
                .filter(CodeHotspot.combined_score > 0.5)
                .order_by(CodeHotspot.combined_score.desc())
                .limit(10)
                .all()
            )
        except Exception:
            hotspots = []

        if not hotspots:
            return 0

        files_text = "\n".join(
            f"- {h.file_path} (complexity={round(h.combined_score, 2)}, commits={h.commit_count})"
            for h in hotspots[:8]
        )
        prompt = (
            "You are a QA engineer analyzing high-complexity code files for hidden bugs.\n\n"
            f"High-risk files:\n{files_text}\n\n"
            "Identify 2-4 potential bugs that may NOT produce visible errors — race conditions, "
            "edge cases, off-by-one errors, null handling, state management bugs, resource leaks.\n\n"
            "Return ONLY valid JSON:\n"
            "{\"bugs\": [{\"file\": \"...\", \"title\": \"...\", \"description\": \"...\", "
            "\"severity\": \"info|warn|critical\"}]}\n"
        )
        reply = call_llm(messages=[{"role": "user", "content": prompt}], max_tokens=500, trace_id="qa-codebugs", cacheable=True)
        if not reply:
            return 0
        try:
            data = json.loads(reply[reply.find("{"):reply.rfind("}") + 1])
            bugs = data.get("bugs", [])
        except Exception:
            return 0

        added = 0
        for b in bugs:
            title = b.get("title", "").strip()
            if not title:
                continue
            db.add(QABugReport(
                user_id=user_id,
                title=title,
                description=b.get("description", ""),
                severity=b.get("severity", "warn"),
                reproduction_steps=f"File: {b.get('file', 'unknown')}",
            ))
            added += 1
        if added:
            db.commit()
        return added

    def _prioritize_with_pm(self, db: Session, user_id: int, report: dict) -> None:
        total_bugs = (report.get("code_bugs_detected", 0) +
                      report.get("visual_bugs", 0) +
                      report.get("accessibility_issues", 0))
        if total_bugs > 0:
            self.send_message(db, user_id, "project_manager", "qa_report", {
                "total_bugs": total_bugs,
                "code_bugs": report.get("code_bugs_detected", 0),
                "visual_bugs": report.get("visual_bugs", 0),
                "a11y_issues": report.get("accessibility_issues", 0),
                "tests_passed": report.get("browser_tests_passed", 0),
                "tests_run": report.get("browser_tests_run", 0),
            })

    def _research_trends(self, db: Session, user_id: int) -> int:
        topics = [
            "automated E2E testing best practices Playwright 2026",
            "AI-driven QA and visual regression testing 2026",
        ]
        results = self.research(db, user_id, topics[:2], trace_id="qa-research")
        return len(results)

    def _get_confidence(self, db: Session, user_id: int) -> float:
        state = self.get_state(db, user_id)
        return state.confidence if state else 0.0

    def _update_state(self, db: Session, user_id: int, report: dict) -> float:
        test_cases = report.get("new_test_cases", 0)
        bugs = (report.get("code_bugs_detected", 0) + report.get("visual_bugs", 0))
        tests_run = report.get("browser_tests_run", 0)
        tests_passed = report.get("browser_tests_passed", 0)

        test_coverage = min(1.0, test_cases / 10)
        pass_rate = (tests_passed / tests_run) if tests_run > 0 else 0.5
        bug_penalty = min(0.2, bugs * 0.03)
        confidence = round(0.3 * test_coverage + 0.3 * pass_rate + 0.2 - bug_penalty + 0.1, 3)
        confidence = max(0.0, min(1.0, confidence))

        new_state = {
            "test_cases_generated": test_cases,
            "bugs_detected": bugs,
            "browser_tests_run": tests_run,
            "browser_tests_passed": tests_passed,
            "last_cycle": datetime.utcnow().isoformat(),
        }
        self.save_state(db, user_id, new_state, confidence)
        return confidence

    def _publish_summary(self, db: Session, user_id: int, report: dict) -> None:
        summary = {"type": "cycle_complete",
                   "test_cases": report.get("new_test_cases", 0),
                   "bugs": (report.get("code_bugs_detected", 0) + report.get("visual_bugs", 0)),
                   "confidence": report.get("confidence", 0)}
        for target in ["project_manager", "product_owner", "frontend", "ux"]:
            self.send_message(db, user_id, target, "cycle_summary", summary)

    # ── QA-specific API methods ─────────────────────────────────────
    def get_test_cases(self, db: Session, user_id: int, limit: int = 20) -> list:
        return db.query(QATestCase).filter(QATestCase.user_id == user_id).order_by(QATestCase.created_at.desc()).limit(limit).all()

    def get_test_runs(self, db: Session, user_id: int, limit: int = 20) -> list:
        return db.query(QATestRun).filter(QATestRun.user_id == user_id).order_by(QATestRun.created_at.desc()).limit(limit).all()

    def get_bug_reports(self, db: Session, user_id: int, limit: int = 20) -> list:
        return db.query(QABugReport).filter(QABugReport.user_id == user_id).order_by(QABugReport.created_at.desc()).limit(limit).all()

    def get_metrics(self, db: Session, user_id: int) -> Dict[str, Any]:
        base = super().get_metrics(db, user_id)
        total_tests = db.query(func.count(QATestCase.id)).filter(QATestCase.user_id == user_id).scalar() or 0
        total_bugs = db.query(func.count(QABugReport.id)).filter(QABugReport.user_id == user_id).scalar() or 0
        open_bugs = db.query(func.count(QABugReport.id)).filter(
            QABugReport.user_id == user_id, QABugReport.status == "open").scalar() or 0
        base.update({"total_test_cases": total_tests, "total_bugs": total_bugs, "open_bugs": open_bugs})
        return base

    def get_chat_context(self, db: Session, user_id: int) -> str:
        parts = [f"[Project Brain — {self.label}]", self.role_prompt]
        state = self.get_state(db, user_id)
        if state and state.state_json:
            try:
                s = json.loads(state.state_json)
                parts.append(f"Tests: {s.get('test_cases_generated', 0)}, "
                             f"bugs: {s.get('bugs_detected', 0)}, "
                             f"browser tests: {s.get('browser_tests_passed', 0)}/{s.get('browser_tests_run', 0)}")
            except Exception:
                pass
            parts.append(f"Confidence: {state.confidence:.0%}")
        findings = self.get_findings(db, user_id, limit=3)
        if findings:
            parts.append("Recent QA findings:")
            for f in findings:
                parts.append(f"  [{f.severity}] {f.title}: {f.description[:80]}")
        return "\n".join(parts)
