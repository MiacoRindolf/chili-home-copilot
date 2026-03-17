"""AI Engineer Agent — LLM usage analysis, prompt optimization, and AI trends.

Self-evolving: reviews prompt templates, model selection, and token costs;
researches latest AI models, RAG improvements, and agent patterns;
recommends optimizations specific to the project's AI stack.
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

_AI_ROLE_PROMPT = (
    "You are Chili's AI Engineer agent — a self-evolving AI expert in LLM integration, "
    "prompt engineering, RAG systems, embedding models, fine-tuning strategies, and agentic "
    "AI patterns. You analyze the project's LLM usage — prompt templates, model selection, "
    "token costs, context window management, and structured output patterns. You detect "
    "prompt injection risks, evaluate prompt clarity, and suggest improvements. You stay "
    "current with the latest models (GPT-4o, Claude, Gemini, Llama, Mistral), vector "
    "databases, retrieval strategies, and multi-agent frameworks. You proactively recommend "
    "model upgrades, cost optimizations, and architecture improvements."
)


class AIEngineerAgent(AgentBase):
    name = "ai_eng"
    label = "AI Eng"
    icon = "\U0001F916"
    role_prompt = _AI_ROLE_PROMPT
    active = True

    def run_cycle(self, db: Session, user_id: int) -> Dict[str, Any]:
        steps_done = 0
        report: Dict[str, Any] = {}

        msgs = self._process_messages(db, user_id)
        report["messages_processed"] = msgs
        steps_done += 1

        snapshot = self._analyze_llm_usage(db)
        report["llm_files"] = snapshot.get("llm_files", 0)
        steps_done += 1

        prompt_quality = self._evaluate_prompts(db, user_id, snapshot)
        report["prompt_issues"] = prompt_quality.get("issues", 0)
        steps_done += 1

        research_count = self._research_trends(db, user_id)
        report["research_done"] = research_count
        steps_done += 1

        benchmarks = self._suggest_benchmarks(db, user_id, snapshot)
        report["benchmark_suggestions"] = benchmarks
        steps_done += 1

        self._cross_reference_requirements(db, user_id)
        steps_done += 1

        findings = self._generate_recommendations(db, user_id, snapshot, prompt_quality)
        report["new_findings"] = findings
        steps_done += 1

        old_conf = self._get_confidence(db, user_id)
        new_conf = self._update_state(db, user_id, snapshot, report)
        report["confidence"] = new_conf
        steps_done += 1

        self._publish_summary(db, user_id, report)

        if abs(new_conf - old_conf) > 0.01:
            self.evolve(db, user_id, "ai_expertise",
                        f"Cycle: {snapshot.get('llm_files', 0)} LLM files, {prompt_quality.get('issues', 0)} prompt issues",
                        old_conf, new_conf, trigger="learning_cycle")

        return {"steps": steps_done, **report}

    def _process_messages(self, db: Session, user_id: int) -> int:
        messages = self.get_messages(db, user_id)
        for msg in messages:
            self.acknowledge_message(db, msg)
        return len(messages)

    def _analyze_llm_usage(self, db: Session) -> Dict[str, Any]:
        try:
            from ....models.code_brain import CodeHotspot
            hotspots = db.query(CodeHotspot).order_by(CodeHotspot.combined_score.desc()).limit(50).all()
            ai_keywords = ("llm", "openai", "prompt", "embedding", "rag", "vector", "chat",
                           "completion", "token", "model", "agent", "chain", "langchain",
                           "chromadb", "chroma", "groq", "gemini", "claude")
            llm_files = [
                {"file": h.file_path, "score": round(h.combined_score, 3)}
                for h in hotspots
                if any(kw in h.file_path.lower() for kw in ai_keywords)
            ]
            return {"llm_files": len(llm_files), "files": llm_files[:10]}
        except Exception as e:
            logger.warning("[ai_eng] LLM usage analysis failed: %s", e)
            return {"llm_files": 0, "files": []}

    def _evaluate_prompts(self, db: Session, user_id: int, snapshot: dict) -> Dict[str, Any]:
        files = snapshot.get("files", [])
        if not files:
            return {"issues": 0}
        files_text = "\n".join(f"- {f['file']}" for f in files[:10])
        prompt = (
            "You are an AI/ML engineer reviewing LLM integration code.\n\n"
            f"AI-related files:\n{files_text}\n\n"
            "Identify 2-4 potential issues (prompt injection risk, unclear instructions, "
            "missing structured output, excessive token usage, no fallback model, "
            "missing rate limiting, no cost tracking).\n\n"
            "Return ONLY valid JSON:\n"
            "{\"issues\": [{\"file\": \"...\", \"issue\": \"...\", \"severity\": \"info|warn|critical\"}]}\n"
        )
        reply = call_llm(messages=[{"role": "user", "content": prompt}], max_tokens=500, trace_id="ai-prompts")
        if not reply:
            return {"issues": 0}
        try:
            data = json.loads(reply[reply.find("{"):reply.rfind("}") + 1])
            return {"issues": len(data.get("issues", [])), "details": data.get("issues", [])}
        except Exception:
            return {"issues": 0}

    def _research_trends(self, db: Session, user_id: int) -> int:
        topics = [
            "latest LLM models and capabilities comparison 2026 GPT Claude Gemini Llama",
            "RAG best practices vector database optimization retrieval 2026",
        ]
        results = self.research(db, user_id, topics[:2], trace_id="ai-research")
        return len(results)

    def _suggest_benchmarks(self, db: Session, user_id: int, snapshot: dict) -> int:
        files = snapshot.get("files", [])
        if not files:
            return 0
        prompt = (
            "You are an AI engineer. Suggest 2-3 benchmarks or experiments to improve the "
            "project's AI capabilities (model comparison, prompt A/B test, embedding quality "
            "evaluation, latency benchmarks, cost analysis).\n\n"
            "Return ONLY valid JSON:\n"
            "{\"benchmarks\": [{\"name\": \"...\", \"description\": \"...\", \"priority\": \"high|medium|low\"}]}\n"
        )
        reply = call_llm(messages=[{"role": "user", "content": prompt}], max_tokens=400, trace_id="ai-bench")
        if not reply:
            return 0
        try:
            data = json.loads(reply[reply.find("{"):reply.rfind("}") + 1])
            return len(data.get("benchmarks", []))
        except Exception:
            return 0

    def _cross_reference_requirements(self, db: Session, user_id: int) -> None:
        from ....models.project_brain import PORequirement
        reqs = (
            db.query(PORequirement)
            .filter(PORequirement.user_id == user_id, PORequirement.status.in_(["ready", "in_planner"]))
            .limit(5)
            .all()
        )
        ai_reqs = [r for r in reqs if any(kw in (r.title + " " + r.description).lower()
                                          for kw in ("ai", "ml", "llm", "chat", "predict", "recommend",
                                                     "intelligent", "smart", "automat"))]
        for req in ai_reqs[:2]:
            self.send_message(db, user_id, "project_manager", "ai_capability_review", {
                "requirement_id": req.id, "title": req.title,
                "ai_feasible": True,
            })

    def _generate_recommendations(self, db: Session, user_id: int, snapshot: dict,
                                  prompt_quality: dict) -> int:
        issues = prompt_quality.get("details", [])
        if not issues and not snapshot.get("files"):
            return 0
        context = "AI/LLM issues:\n" + "\n".join(
            f"- [{i.get('severity', 'info')}] {i.get('file', '?')}: {i.get('issue', '')}"
            for i in issues[:5]
        ) if issues else "No specific issues detected."

        prompt = (
            "You are a senior AI engineer. Generate 2-3 actionable recommendations.\n\n"
            f"{context}\n\n"
            "Findings can be: prompt_optimization, model_selection, rag_improvement, "
            "cost_reduction, safety_guardrail, architecture.\n"
            "Return ONLY valid JSON:\n"
            "{\"findings\": [{\"category\": \"...\", \"title\": \"...\", "
            "\"description\": \"...\", \"severity\": \"info|warn|critical\"}]}\n"
        )
        reply = call_llm(messages=[{"role": "user", "content": prompt}], max_tokens=500, trace_id="ai-findings")
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
            self.publish_finding(db, user_id, category=f.get("category", "ai"),
                                title=title, description=f.get("description", ""),
                                severity=f.get("severity", "info"))
            added += 1
        return added

    def _get_confidence(self, db: Session, user_id: int) -> float:
        state = self.get_state(db, user_id)
        return state.confidence if state else 0.0

    def _update_state(self, db: Session, user_id: int, snapshot: dict, report: dict) -> float:
        files = snapshot.get("llm_files", 0)
        issues = report.get("prompt_issues", 0)
        coverage = min(1.0, files / 10)
        penalty = min(0.2, issues * 0.04)
        confidence = round(0.4 * coverage + 0.4 - penalty + 0.1, 3)
        confidence = max(0.0, min(1.0, confidence))
        new_state = {"llm_files": files, "prompt_issues": issues,
                     "benchmark_suggestions": report.get("benchmark_suggestions", 0),
                     "last_cycle": datetime.utcnow().isoformat()}
        self.save_state(db, user_id, new_state, confidence)
        return confidence

    def _publish_summary(self, db: Session, user_id: int, report: dict) -> None:
        summary = {"type": "cycle_complete", "llm_files": report.get("llm_files", 0),
                   "prompt_issues": report.get("prompt_issues", 0),
                   "confidence": report.get("confidence", 0)}
        for target in ["architect", "project_manager"]:
            self.send_message(db, user_id, target, "cycle_summary", summary)

    def get_chat_context(self, db: Session, user_id: int) -> str:
        parts = [f"[Project Brain — {self.label}]", self.role_prompt]
        state = self.get_state(db, user_id)
        if state and state.state_json:
            try:
                s = json.loads(state.state_json)
                parts.append(f"LLM files: {s.get('llm_files', 0)}, prompt issues: {s.get('prompt_issues', 0)}")
            except Exception:
                pass
            parts.append(f"Confidence: {state.confidence:.0%}")
        findings = self.get_findings(db, user_id, limit=3)
        if findings:
            parts.append("Recent AI findings:")
            for f in findings:
                parts.append(f"  [{f.severity}] {f.title}: {f.description[:80]}")
        return "\n".join(parts)
