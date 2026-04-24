"""Role-based lens system for the Project Management domain.

Each lens filters and presents the shared Code Brain engine's data through
a specific role's perspective (e.g. Backend Dev, QA, DevOps).
"""
from __future__ import annotations

import fnmatch
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session
from sqlalchemy import func

from ...models.code_brain import (
    CodeDepAlert, CodeDependency, CodeHotspot, CodeInsight,
    CodeLearningEvent, CodeRepo, CodeReview, CodeSnapshot,
)
from .events import learning_event_visibility_clause
from .runtime import resolve_repo_runtime_path

logger = logging.getLogger(__name__)


@dataclass
class Lens:
    name: str
    label: str
    icon: str
    file_patterns: List[str]
    insight_categories: List[str]
    review_severities: List[str]
    context_role: str
    planner_integration: bool = False
    extra_sections: List[str] = field(default_factory=list)


LENS_REGISTRY: Dict[str, Lens] = {}


def _register(lens: Lens) -> None:
    LENS_REGISTRY[lens.name] = lens


_register(Lens(
    name="architect",
    label="Dev Architect",
    icon="\U0001f3d7",
    file_patterns=["*"],
    insight_categories=["architecture", "convention", "dependency", "pattern", "quality"],
    review_severities=["critical", "warn", "info"],
    context_role="You are advising as a Software Architect focused on dependency structure, module coupling, and code complexity.",
    extra_sections=["graph", "circular_deps"],
))

_register(Lens(
    name="backend",
    label="Backend Dev",
    icon="\u2699",
    file_patterns=["app/routers/*", "app/services/*", "app/services/**/*", "app/models/*", "app/db.py", "app/config.py", "app/deps.py"],
    insight_categories=["convention", "pattern", "quality", "architecture"],
    review_severities=["critical", "warn"],
    context_role="You are advising as a Backend Developer focused on API endpoints, services, models, and database patterns.",
))

_register(Lens(
    name="frontend",
    label="Frontend Dev",
    icon="\U0001f5a5",
    file_patterns=["app/templates/*", "app/static/*", "app/static/**/*", "*.html", "*.js", "*.css"],
    insight_categories=["convention", "pattern", "quality"],
    review_severities=["critical", "warn"],
    context_role="You are advising as a Frontend Developer focused on templates, JavaScript, CSS, and UI patterns.",
))

_register(Lens(
    name="ux",
    label="UX Dev",
    icon="\U0001f3a8",
    file_patterns=["app/templates/*", "*.css", "*.html"],
    insight_categories=["convention", "pattern"],
    review_severities=["warn", "info"],
    context_role="You are advising as a UX Developer focused on user experience, design consistency, accessibility, and style patterns.",
))

_register(Lens(
    name="qa",
    label="QA",
    icon="\U0001f9ea",
    file_patterns=["tests/*", "tests/**/*", "*test*", "*_test.*", "test_*"],
    insight_categories=["quality", "pattern"],
    review_severities=["critical", "warn"],
    context_role="You are advising as a QA Engineer focused on test coverage, quality trends, bug findings, and regression risk.",
    extra_sections=["test_coverage"],
))

_register(Lens(
    name="devops",
    label="DevOps",
    icon="\U0001f680",
    file_patterns=["Dockerfile*", ".github/*", ".github/**/*", "*.yml", "*.yaml", "certs/*", "requirements*.txt", "pyproject.toml", "package.json"],
    insight_categories=["dependency", "quality"],
    review_severities=["critical", "warn"],
    context_role="You are advising as a DevOps Engineer focused on CI/CD, deployment, infrastructure, and dependency health.",
    extra_sections=["dep_health"],
))

_register(Lens(
    name="security",
    label="Security",
    icon="\U0001f512",
    file_patterns=["certs/*", "*auth*", "*security*", "app/config.py", "*.env*", "app/pairing.py"],
    insight_categories=["dependency", "quality", "architecture"],
    review_severities=["critical"],
    context_role="You are advising as a Security Engineer focused on vulnerabilities, dependency alerts, auth patterns, and secure coding.",
    extra_sections=["dep_health"],
))

_register(Lens(
    name="pm",
    label="Project Manager",
    icon="\U0001f4cb",
    file_patterns=["*"],
    insight_categories=["architecture", "quality", "convention", "dependency", "pattern"],
    review_severities=["critical", "warn"],
    context_role="You are advising as a Project Manager focused on team velocity, technical debt, timeline, and delivery risks.",
    planner_integration=True,
    extra_sections=["planner_summary"],
))

_register(Lens(
    name="po",
    label="Product Owner",
    icon="\U0001f4e6",
    file_patterns=["*"],
    insight_categories=["architecture", "quality", "pattern"],
    review_severities=["critical"],
    context_role="You are advising as a Product Owner focused on feature completeness, roadmap alignment, and acceptance criteria.",
    planner_integration=True,
    extra_sections=["planner_summary"],
))

_register(Lens(
    name="ai_eng",
    label="AI Engineer",
    icon="\U0001f916",
    file_patterns=["app/openai_client.py", "app/services/chat_service.py", "app/services/chat_context.py",
                    "app/services/llm_caller.py", "app/services/code_brain/*", "app/services/code_brain/**/*",
                    "app/services/reasoning_brain/*", "app/services/reasoning_brain/**/*",
                    "*agent*", "*llm*", "*brain*"],
    insight_categories=["architecture", "pattern", "convention"],
    review_severities=["critical", "warn"],
    context_role="You are advising as an AI Engineer focused on LLM integration, prompt design, AI service architecture, and brain domain health.",
))


# ── Query helpers ─────────────────────────────────────────────────────

def _matches_any(file_path: str, patterns: List[str]) -> bool:
    """Return True if file_path matches any of the glob patterns."""
    normalized = file_path.replace("\\", "/")
    for pat in patterns:
        if pat == "*":
            return True
        if fnmatch.fnmatch(normalized, pat):
            return True
        basename = normalized.rsplit("/", 1)[-1] if "/" in normalized else normalized
        if fnmatch.fnmatch(basename, pat):
            return True
    return False


def _repo_freshness(db: Session, repo_id: Optional[int]) -> dict[str, Any]:
    if repo_id is None:
        return {"trusted": True, "reason": None}
    repo = db.query(CodeRepo).filter(CodeRepo.id == repo_id).first()
    if repo is None:
        return {"trusted": False, "reason": "Repo not found."}
    if repo.last_index_error:
        return {"trusted": False, "reason": repo.last_index_error}
    if resolve_repo_runtime_path(repo) is None:
        return {
            "trusted": False,
            "reason": "Registered workspace is not reachable from this runtime.",
        }
    return {"trusted": True, "reason": None}


def get_lens(name: str) -> Optional[Lens]:
    return LENS_REGISTRY.get(name)


def list_lenses() -> List[Dict[str, Any]]:
    return [
        {"name": l.name, "label": l.label, "icon": l.icon,
         "planner": l.planner_integration, "extra_sections": l.extra_sections}
        for l in LENS_REGISTRY.values()
    ]


def get_lens_metrics(
    db: Session,
    lens_name: str,
    repo_id: Optional[int] = None,
    *,
    user_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Return metrics filtered through a specific lens."""
    lens = LENS_REGISTRY.get(lens_name)
    if not lens:
        return {"error": f"Unknown lens: {lens_name}"}
    freshness = _repo_freshness(db, repo_id)

    repo_filter = []
    if repo_id is not None:
        repo_filter = [CodeHotspot.repo_id == (repo_id if freshness.get("trusted") else -1)]

    all_hotspots = db.query(CodeHotspot).filter(*repo_filter).order_by(CodeHotspot.combined_score.desc()).limit(100).all()
    filtered_hotspots = [h for h in all_hotspots if _matches_any(h.file_path, lens.file_patterns)]

    insight_q = db.query(CodeInsight).filter(CodeInsight.active.is_(True))
    if repo_id is not None and freshness.get("trusted"):
        insight_q = insight_q.filter(CodeInsight.repo_id == repo_id)
    elif repo_id is not None:
        insight_q = insight_q.filter(CodeInsight.repo_id == -1)
    all_insights = insight_q.all()
    filtered_insights = [i for i in all_insights if i.category in lens.insight_categories]

    all_snapshots_q = db.query(CodeSnapshot)
    if repo_id is not None and freshness.get("trusted"):
        all_snapshots_q = all_snapshots_q.filter(CodeSnapshot.repo_id == repo_id)
    elif repo_id is not None:
        all_snapshots_q = all_snapshots_q.filter(CodeSnapshot.repo_id == -1)
    all_snapshots = all_snapshots_q.all()
    filtered_snapshots = [s for s in all_snapshots if _matches_any(s.file_path, lens.file_patterns)]

    total_files = len(filtered_snapshots)
    total_lines = sum(s.line_count for s in filtered_snapshots)
    avg_complexity = (sum(s.complexity_score for s in filtered_snapshots) / total_files) if total_files else 0.0

    test_files = sum(1 for s in filtered_snapshots if fnmatch.fnmatch(s.file_path.replace("\\", "/"), "tests/*") or "test_" in s.file_path or "_test." in s.file_path)

    dep_alerts = 0
    circular_count = 0
    if lens_name in ("architect", "devops", "security", "pm"):
        dep_alert_q = db.query(func.count(CodeDepAlert.id)).filter(CodeDepAlert.resolved.is_(False))
        if repo_id is not None and freshness.get("trusted"):
            dep_alert_q = dep_alert_q.filter(CodeDepAlert.repo_id == repo_id)
        elif repo_id is not None:
            dep_alert_q = dep_alert_q.filter(CodeDepAlert.repo_id == -1)
        dep_alerts = dep_alert_q.scalar() or 0
    if lens_name in ("architect", "pm"):
        circular_q = db.query(func.count(CodeDependency.id)).filter(CodeDependency.is_circular.is_(True))
        if repo_id is not None and freshness.get("trusted"):
            circular_q = circular_q.filter(CodeDependency.repo_id == repo_id)
        elif repo_id is not None:
            circular_q = circular_q.filter(CodeDependency.repo_id == -1)
        circular_count = circular_q.scalar() or 0

    review_q = db.query(func.count(CodeReview.id))
    if repo_id is not None and freshness.get("trusted"):
        review_q = review_q.filter(CodeReview.repo_id == repo_id)
    elif repo_id is not None:
        review_q = review_q.filter(CodeReview.repo_id == -1)
    review_count = review_q.scalar() or 0

    events_q = db.query(CodeLearningEvent)
    visibility_clause = learning_event_visibility_clause(
        user_id=user_id,
        repo_ids=([repo_id] if repo_id is not None and repo_id >= 0 else []),
    )
    if visibility_clause is not None:
        events_q = events_q.filter(visibility_clause)
    if repo_id is not None and freshness.get("trusted"):
        events_q = events_q.filter(CodeLearningEvent.repo_id == repo_id)
    elif repo_id is not None:
        events_q = events_q.filter(CodeLearningEvent.repo_id == -1)
    recent_events = events_q.order_by(CodeLearningEvent.created_at.desc()).limit(10).all()

    return {
        "lens": lens_name,
        "label": lens.label,
        "icon": lens.icon,
        "total_files": total_files,
        "total_lines": total_lines,
        "avg_complexity": round(avg_complexity, 2),
        "hotspot_count": len(filtered_hotspots),
        "insight_count": len(filtered_insights),
        "test_file_count": test_files,
        "dep_alert_count": dep_alerts,
        "circular_dep_count": circular_count,
        "review_count": review_count,
        "freshness": freshness,
        "top_hotspots": [
            {
                "file": h.file_path,
                "churn": round(h.churn_score, 3),
                "complexity": round(h.complexity_score, 3),
                "combined": round(h.combined_score, 3),
                "commits": h.commit_count,
            }
            for h in filtered_hotspots[:10]
        ],
        "top_insights": [
            {
                "id": i.id,
                "category": i.category,
                "description": i.description,
                "confidence": round(i.confidence, 2),
            }
            for i in sorted(filtered_insights, key=lambda x: x.confidence, reverse=True)[:10]
        ],
        "recent_events": [
            {
                "type": e.event_type,
                "description": e.description,
                "created_at": e.created_at.isoformat() if e.created_at else None,
            }
            for e in recent_events
        ],
    }


def get_lens_hotspots(db: Session, lens_name: str, repo_id: Optional[int] = None, limit: int = 30) -> List[Dict[str, Any]]:
    """Return hotspots filtered by a lens's file patterns."""
    lens = LENS_REGISTRY.get(lens_name)
    if not lens:
        return []
    if repo_id is not None and not _repo_freshness(db, repo_id).get("trusted"):
        return []
    q = db.query(CodeHotspot)
    if repo_id is not None:
        q = q.filter(CodeHotspot.repo_id == repo_id)
    all_hotspots = q.order_by(CodeHotspot.combined_score.desc()).limit(200).all()
    filtered = [h for h in all_hotspots if _matches_any(h.file_path, lens.file_patterns)]
    return [
        {
            "file": h.file_path,
            "churn": round(h.churn_score, 3),
            "complexity": round(h.complexity_score, 3),
            "combined": round(h.combined_score, 3),
            "commits": h.commit_count,
        }
        for h in filtered[:limit]
    ]


def get_lens_insights(db: Session, lens_name: str, repo_id: Optional[int] = None, limit: int = 20) -> List[Dict[str, Any]]:
    """Return insights filtered by a lens's categories."""
    lens = LENS_REGISTRY.get(lens_name)
    if not lens:
        return []
    if repo_id is not None and not _repo_freshness(db, repo_id).get("trusted"):
        return []
    q = db.query(CodeInsight).filter(
        CodeInsight.active.is_(True),
        CodeInsight.category.in_(lens.insight_categories),
    )
    if repo_id is not None:
        q = q.filter(CodeInsight.repo_id == repo_id)
    results = q.order_by(CodeInsight.confidence.desc()).limit(limit).all()
    return [
        {
            "id": i.id,
            "category": i.category,
            "description": i.description,
            "confidence": round(i.confidence, 2),
            "evidence_count": i.evidence_count,
        }
        for i in results
    ]


def get_lens_chat_context(db: Session, lens_name: str, user_id: Optional[int] = None) -> str:
    """Build role-specific natural-language context for the LLM system prompt."""
    lens = LENS_REGISTRY.get(lens_name)
    if not lens:
        return ""

    from ..coding_task.workspaces import first_reachable_workspace_repo

    repo = first_reachable_workspace_repo(db, user_id=user_id)
    metrics = get_lens_metrics(
        db,
        lens_name,
        repo_id=(int(repo.id) if repo is not None else -1),
        user_id=user_id,
    )
    parts: List[str] = [
        f"[Project Brain — {lens.label} perspective]",
        lens.context_role,
    ]

    total_files = metrics.get("total_files", 0)
    total_lines = metrics.get("total_lines", 0)
    if total_files:
        parts.append(f"Scope: {total_files:,} files, {total_lines:,} lines (avg complexity {metrics.get('avg_complexity', 0):.2f}).")

    hotspot_count = metrics.get("hotspot_count", 0)
    if hotspot_count:
        parts.append(f"{hotspot_count} hotspot file(s) flagged. Be careful modifying these.")
        files = [h["file"] for h in metrics.get("top_hotspots", [])[:5]]
        if files:
            parts.append("Key hotspots: " + ", ".join(files) + ".")

    insight_count = metrics.get("insight_count", 0)
    if insight_count:
        parts.append(f"{insight_count} code pattern(s)/convention(s) discovered.")

    circ = metrics.get("circular_dep_count", 0)
    if circ:
        parts.append(f"Warning: {circ} circular import edges detected.")

    dep_alerts = metrics.get("dep_alert_count", 0)
    if dep_alerts:
        parts.append(f"Dependency health: {dep_alerts} package alert(s).")

    test_count = metrics.get("test_file_count", 0)
    if lens_name == "qa" and total_files:
        ratio = (test_count / total_files * 100) if total_files else 0
        parts.append(f"Test files: {test_count}, test ratio: {ratio:.1f}%.")

    return "\n".join(parts)
