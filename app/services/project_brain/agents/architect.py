"""Architect Agent — deep architectural insight and improvement recommendations.

Analyzes dependency graphs, module coupling, circular dependencies, complexity
hotspots, and quality trends. Researches modern architecture patterns and
proactively suggests structural improvements.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session
from sqlalchemy import func

from ..base import AgentBase
from ....models.project_brain import AgentFinding, AgentGoal, ProjectAgentState
from ....models.code_brain import CodeDependency, CodeHotspot, CodeRepo, CodeQualitySnapshot
from ...llm_caller import call_llm

logger = logging.getLogger(__name__)

_ARCH_ROLE_PROMPT = (
    "You are Chili's Architect agent — a self-evolving AI that provides deep architectural "
    "insight into the codebase. You analyze dependency graphs, detect circular dependencies, "
    "measure module coupling, identify complexity hotspots, and track quality trends. "
    "You research modern architecture patterns (microservices, event-driven, hexagonal, etc.) "
    "and proactively recommend structural improvements, separation of concerns, and scalability "
    "strategies. You think at the system level — not just code, but repo boundaries, API "
    "contracts, and deployment topology."
)


class ArchitectAgent(AgentBase):
    name = "architect"
    label = "Architect"
    icon = "\U0001F3D7"  # building construction
    role_prompt = _ARCH_ROLE_PROMPT
    active = True

    # ── 8-Step Learning Cycle ─────────────────────────────────────────

    def run_cycle(self, db: Session, user_id: int) -> Dict[str, Any]:
        steps_done = 0
        report: Dict[str, Any] = {}

        # 0. Process incoming messages from PO and PM
        msgs_processed = self._process_incoming_messages(db, user_id)
        report["messages_processed"] = msgs_processed

        # 1. Gather architecture snapshot
        snapshot = self._gather_snapshot(db)
        report["repos"] = snapshot.get("repo_count", 0)
        steps_done += 1

        # 2. Analyze circular dependencies
        circular = self._analyze_circular_deps(db, snapshot)
        report["circular_deps"] = circular.get("total", 0)
        steps_done += 1

        # 3. Analyze module coupling
        coupling = self._analyze_coupling(db, snapshot)
        report["high_coupling_pairs"] = coupling.get("high_coupling_count", 0)
        steps_done += 1

        # 4. Analyze complexity hotspots
        hotspots = self._analyze_hotspots(db, snapshot)
        report["critical_hotspots"] = hotspots.get("critical_count", 0)
        steps_done += 1

        # 5. Check quality trends
        trends = self._check_trends(db, snapshot)
        report["trend_direction"] = trends.get("direction", "stable")
        steps_done += 1

        # 6. Research architecture patterns
        research_count = self._research_patterns(db, user_id, snapshot)
        report["research_done"] = research_count
        steps_done += 1

        # 7. Generate architectural recommendations
        old_conf = self._get_confidence(db, user_id)
        findings = self._generate_recommendations(db, user_id, snapshot, circular, coupling, hotspots, trends)
        report["new_findings"] = findings
        steps_done += 1

        # 8. Update state and evolve
        new_conf = self._update_state(db, user_id, snapshot, report)
        report["confidence"] = new_conf
        steps_done += 1

        if abs(new_conf - old_conf) > 0.01:
            self.evolve(db, user_id, "architecture_understanding",
                        f"Cycle: {circular.get('total', 0)} circular deps, {hotspots.get('critical_count', 0)} critical hotspots",
                        old_conf, new_conf, trigger="learning_cycle")

        # 9. Notify other agents
        summary = {
            "type": "cycle_complete",
            "circular_deps": circular.get("total", 0),
            "critical_hotspots": hotspots.get("critical_count", 0),
            "trend": trends.get("direction", "stable"),
            "confidence": new_conf,
        }
        for target in ["product_owner", "project_manager"]:
            self.send_message(db, user_id, target, "cycle_summary", summary)

        return {"steps": steps_done, **report}

    # ── Step implementations ──────────────────────────────────────────

    def _process_incoming_messages(self, db: Session, user_id: int) -> int:
        """Step 0: Read and acknowledge inter-agent messages (findings, cycle summaries)."""
        messages = self.get_messages(db, user_id)
        count = 0
        for msg in messages:
            self.acknowledge_message(db, msg)
            count += 1
        return count

    def _gather_snapshot(self, db: Session) -> Dict[str, Any]:
        """Step 1: Collect current architecture state from Code Brain data."""
        repos = db.query(CodeRepo).filter(CodeRepo.active.is_(True)).all()
        total_deps = db.query(func.count(CodeDependency.id)).scalar() or 0
        total_hotspots = db.query(func.count(CodeHotspot.id)).scalar() or 0

        repo_infos = []
        for r in repos:
            dep_count = db.query(func.count(CodeDependency.id)).filter(
                CodeDependency.repo_id == r.id
            ).scalar() or 0
            circ_count = db.query(func.count(CodeDependency.id)).filter(
                CodeDependency.repo_id == r.id, CodeDependency.is_circular.is_(True)
            ).scalar() or 0
            repo_infos.append({
                "id": r.id, "name": r.name or r.local_path,
                "deps": dep_count, "circular": circ_count,
            })

        return {
            "repo_count": len(repos),
            "repos": repo_infos,
            "total_deps": total_deps,
            "total_hotspots": total_hotspots,
        }

    def _analyze_circular_deps(self, db: Session, snapshot: dict) -> Dict[str, Any]:
        """Step 2: Deep analysis of circular dependency patterns."""
        circular_deps = (
            db.query(CodeDependency)
            .filter(CodeDependency.is_circular.is_(True))
            .all()
        )
        cycles: Dict[str, list] = {}
        for d in circular_deps:
            key = f"{d.source_file} <-> {d.target_file}"
            rev_key = f"{d.target_file} <-> {d.source_file}"
            cycle_key = key if key < rev_key else rev_key
            if cycle_key not in cycles:
                cycles[cycle_key] = []
            cycles[cycle_key].append(d.import_name or "unknown")

        return {
            "total": len(circular_deps),
            "unique_cycles": len(cycles),
            "top_cycles": list(cycles.keys())[:10],
        }

    def _analyze_coupling(self, db: Session, snapshot: dict) -> Dict[str, Any]:
        """Step 3: Analyze module coupling between directories."""
        from collections import defaultdict
        from pathlib import PurePosixPath

        deps = db.query(CodeDependency).all()
        dir_edges: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for d in deps:
            s_dir = str(PurePosixPath(d.source_file).parent)
            t_dir = str(PurePosixPath(d.target_file).parent)
            if s_dir != t_dir:
                dir_edges[s_dir][t_dir] += 1

        coupling_pairs = []
        for src, targets in dir_edges.items():
            for tgt, count in targets.items():
                coupling_pairs.append({"source": src, "target": tgt, "count": count})
        coupling_pairs.sort(key=lambda x: x["count"], reverse=True)

        high_coupling = [c for c in coupling_pairs if c["count"] > 15]

        return {
            "total_pairs": len(coupling_pairs),
            "high_coupling_count": len(high_coupling),
            "top_coupling": coupling_pairs[:10],
        }

    def _analyze_hotspots(self, db: Session, snapshot: dict) -> Dict[str, Any]:
        """Step 4: Identify critical complexity hotspots."""
        hotspots = (
            db.query(CodeHotspot)
            .order_by(CodeHotspot.combined_score.desc())
            .limit(20)
            .all()
        )
        critical = [h for h in hotspots if h.combined_score > 0.7]
        moderate = [h for h in hotspots if 0.4 < h.combined_score <= 0.7]

        return {
            "total": len(hotspots),
            "critical_count": len(critical),
            "moderate_count": len(moderate),
            "top_files": [
                {"file": h.file_path, "score": round(h.combined_score, 3), "commits": h.commit_count}
                for h in hotspots[:10]
            ],
        }

    def _check_trends(self, db: Session, snapshot: dict) -> Dict[str, Any]:
        """Step 5: Check quality trend direction."""
        from ...code_brain import trends as cb_trends
        repo_ids = [r["id"] for r in snapshot.get("repos", [])]
        if not repo_ids:
            return {"direction": "unknown"}

        try:
            deltas = cb_trends.compute_trend_deltas(db, repo_ids[0])
        except Exception:
            return {"direction": "unknown"}

        complexity_delta = deltas.get("complexity_change_pct", 0)
        if complexity_delta > 5:
            direction = "degrading"
        elif complexity_delta < -5:
            direction = "improving"
        else:
            direction = "stable"

        return {
            "direction": direction,
            "deltas": deltas,
        }

    def _research_patterns(self, db: Session, user_id: int, snapshot: dict) -> int:
        """Step 6: Research modern architecture patterns."""
        from ...config import settings
        topics = [
            "software architecture best practices 2026 modular monolith vs microservices",
        ]
        circular = snapshot.get("total_deps", 0)
        if circular > 20:
            topics.append("how to break circular dependencies in large Python codebases")

        max_searches = getattr(settings, "project_brain_max_web_searches", 5)
        results = self.research(db, user_id, topics[:max_searches], trace_id="arch-research")
        return len(results)

    def _get_confidence(self, db: Session, user_id: int) -> float:
        state = self.get_state(db, user_id)
        return state.confidence if state else 0.0

    def _generate_recommendations(
        self, db: Session, user_id: int,
        snapshot: dict, circular: dict, coupling: dict,
        hotspots: dict, trends: dict,
    ) -> int:
        """Step 7: LLM-driven architectural recommendations."""
        context = (
            f"Repos: {snapshot.get('repo_count', 0)}, Total deps: {snapshot.get('total_deps', 0)}\n"
            f"Circular deps: {circular.get('total', 0)} ({circular.get('unique_cycles', 0)} unique cycles)\n"
            f"High coupling pairs: {coupling.get('high_coupling_count', 0)}\n"
            f"Critical hotspots: {hotspots.get('critical_count', 0)}\n"
            f"Quality trend: {trends.get('direction', 'unknown')}\n"
        )
        top_cycles = circular.get("top_cycles", [])
        if top_cycles:
            context += "Top circular deps:\n" + "\n".join(f"  - {c}" for c in top_cycles[:5]) + "\n"
        top_hotspot_files = hotspots.get("top_files", [])
        if top_hotspot_files:
            context += "Hottest files:\n" + "\n".join(
                f"  - {h['file']} (score={h['score']}, {h['commits']} commits)"
                for h in top_hotspot_files[:5]
            ) + "\n"

        prompt = (
            "You are a senior software architect reviewing a codebase. Generate 2-4 actionable findings.\n\n"
            f"Architecture snapshot:\n{context}\n"
            "Findings can be: architecture_pattern, dependency_issue, complexity_warning, "
            "refactoring_opportunity, scalability_concern, separation_of_concerns.\n"
            "Return ONLY valid JSON:\n"
            "{\"findings\": [{\"category\": \"...\", \"title\": \"...\", "
            "\"description\": \"...\", \"severity\": \"info|warn|critical\"}]}\n"
        )

        reply = call_llm(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=600,
            trace_id="arch-findings",
            cacheable=True,
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
                category=f.get("category", "architecture"),
                title=title,
                description=f.get("description", ""),
                severity=f.get("severity", "info"),
            )
            added += 1
        return added

    def _update_state(self, db: Session, user_id: int, snapshot: dict, report: dict) -> float:
        """Step 8: Recalculate confidence and save state."""
        circular = report.get("circular_deps", 0)
        critical_hotspots = report.get("critical_hotspots", 0)
        trend = report.get("trend_direction", "unknown")

        health_score = 1.0
        if circular > 0:
            health_score -= min(0.3, circular * 0.01)
        if critical_hotspots > 0:
            health_score -= min(0.2, critical_hotspots * 0.04)
        if trend == "degrading":
            health_score -= 0.1

        data_coverage = min(1.0, snapshot.get("total_deps", 0) / 100)
        confidence = round(0.5 * health_score + 0.3 * data_coverage + 0.2, 3)
        confidence = max(0.0, min(1.0, confidence))

        new_state = {
            "repos": snapshot.get("repo_count", 0),
            "total_deps": snapshot.get("total_deps", 0),
            "circular_deps": circular,
            "critical_hotspots": critical_hotspots,
            "high_coupling_pairs": report.get("high_coupling_pairs", 0),
            "trend_direction": trend,
            "health_score": round(health_score, 3),
            "last_cycle": datetime.utcnow().isoformat(),
        }

        self.save_state(db, user_id, new_state, confidence)
        return confidence

    # ── Architect-specific API methods ────────────────────────────────

    def get_architecture_health(self, db: Session, user_id: int) -> Dict[str, Any]:
        """Return a comprehensive architecture health report."""
        snapshot = self._gather_snapshot(db)
        circular = self._analyze_circular_deps(db, snapshot)
        coupling = self._analyze_coupling(db, snapshot)
        hotspots = self._analyze_hotspots(db, snapshot)
        trends = self._check_trends(db, snapshot)

        return {
            "snapshot": snapshot,
            "circular": circular,
            "coupling": coupling,
            "hotspots": hotspots,
            "trends": trends,
        }

    def get_chat_context(self, db: Session, user_id: int) -> str:
        """Richer Architect context: health score, hotspots, circular deps, trends."""
        parts = [f"[Project Brain — {self.label}]", self.role_prompt]
        state = self.get_state(db, user_id)
        if state and state.state_json:
            try:
                s = json.loads(state.state_json)
                parts.append(
                    f"Architecture health: {s.get('health_score', 0):.0%} — "
                    f"{s.get('circular_deps', 0)} circular deps, "
                    f"{s.get('critical_hotspots', 0)} critical hotspots, "
                    f"trend: {s.get('trend_direction', 'unknown')}"
                )
                parts.append(f"Repos: {s.get('repos', 0)}, total deps: {s.get('total_deps', 0)}")
            except Exception:
                pass
            parts.append(f"Architect confidence: {state.confidence:.0%}")

        findings = self.get_findings(db, user_id, limit=3)
        if findings:
            parts.append("Recent architectural findings:")
            for f in findings:
                parts.append(f"  [{f.severity}] {f.title}: {f.description[:100]}")

        return "\n".join(parts)

    def get_metrics(self, db: Session, user_id: int) -> Dict[str, Any]:
        base = super().get_metrics(db, user_id)
        state = self.get_state(db, user_id)
        if state and state.state_json:
            try:
                s = json.loads(state.state_json)
                base.update({
                    "circular_deps": s.get("circular_deps", 0),
                    "critical_hotspots": s.get("critical_hotspots", 0),
                    "high_coupling_pairs": s.get("high_coupling_pairs", 0),
                    "trend_direction": s.get("trend_direction", "unknown"),
                    "health_score": s.get("health_score", 0),
                })
            except Exception:
                pass
        return base
