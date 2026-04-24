"""Single-report advisory analysis for the Project domain."""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from ..models import PlanTask, ProjectAnalysisSnapshot
from . import planner_service
from .code_brain import lenses as cb_lenses
from .coding_task.service import build_handoff_dict
from .coding_task.workspaces import select_runtime_workspace_repo_for_task
from .project_domain_feed import list_operator_feed

PERSPECTIVE_ORDER = (
    ("product", "Product", None),
    ("architecture", "Architecture", "architect"),
    ("backend", "Backend", "backend"),
    ("frontend", "Frontend", "frontend"),
    ("qa", "QA", "qa"),
    ("security", "Security", "security"),
    ("ops", "Ops", "devops"),
    ("ai", "AI", "ai_eng"),
)


def _task_for_analysis(
    db: Session,
    *,
    planner_task_id: int | None,
    user_id: int | None,
) -> PlanTask | None:
    if planner_task_id is None or user_id is None:
        return None
    task = db.query(PlanTask).filter(PlanTask.id == planner_task_id).first()
    if task is None:
        return None
    if not planner_service._user_can_access(db, task.project_id, user_id):
        return None
    return task


def _status_from_metrics(metrics: dict[str, Any]) -> str:
    hotspot_count = int(metrics.get("hotspot_count") or 0)
    dep_alert_count = int(metrics.get("dep_alert_count") or 0)
    if hotspot_count >= 8 or dep_alert_count >= 6:
        return "critical"
    if hotspot_count >= 3 or dep_alert_count >= 2:
        return "attention"
    return "healthy"


def _metric_line(metrics: dict[str, Any]) -> str:
    return (
        f"{metrics.get('total_files', 0)} files, "
        f"{metrics.get('hotspot_count', 0)} hotspots, "
        f"{metrics.get('insight_count', 0)} insights"
    )


def _perspective_from_metrics(key: str, label: str, metrics: dict[str, Any]) -> dict[str, Any]:
    hotspot_files = [item.get("file") for item in (metrics.get("top_hotspots") or []) if item.get("file")]
    insight_lines = [item.get("description") for item in (metrics.get("top_insights") or []) if item.get("description")]
    bullets = []
    if hotspot_files:
        bullets.append("Watch: " + ", ".join(hotspot_files[:3]))
    if insight_lines:
        bullets.append("Patterns: " + "; ".join(insight_lines[:2]))
    if not bullets:
        bullets.append("No fresh indexed evidence yet. Reindex the workspace before trusting this view.")
    return {
        "key": key,
        "label": label,
        "status": _status_from_metrics(metrics),
        "headline": _metric_line(metrics),
        "bullets": bullets,
        "metrics": metrics,
    }


def _product_perspective(handoff: dict | None) -> dict[str, Any]:
    if not handoff:
        return {
            "key": "product",
            "label": "Product",
            "status": "attention",
            "headline": "No planner task selected.",
            "bullets": [
                "Open a planner task to bind the analysis to one implementation track.",
                "Without a task, the cockpit can inspect the repo but cannot express delivery risk cleanly.",
            ],
        }
    blockers = [row.get("summary") for row in (handoff.get("blockers") or []) if row.get("summary")]
    clarifications = int((handoff.get("readiness_context") or {}).get("open_clarification_count") or 0)
    profile = handoff.get("profile") or {}
    bullets = [
        f"Workspace: {'bound' if profile.get('workspace_bound') else 'unbound'}",
        f"Open clarifications: {clarifications}",
    ]
    if blockers:
        bullets.append("Current blockers: " + "; ".join(blockers[:2]))
    return {
        "key": "product",
        "label": "Product",
        "status": "attention" if clarifications or not profile.get("workspace_bound") else "healthy",
        "headline": handoff.get("task", {}).get("title") or "Planner task selected",
        "bullets": bullets,
        "handoff": handoff,
    }


def build_analysis_payload(
    db: Session,
    *,
    user_id: int | None,
    planner_task_id: int | None = None,
) -> dict[str, Any]:
    task = _task_for_analysis(db, planner_task_id=planner_task_id, user_id=user_id)
    handoff = build_handoff_dict(db, task, user_id=user_id) if task is not None else None
    selected_repo = select_runtime_workspace_repo_for_task(
        db,
        task.id if task is not None else None,
        user_id=user_id,
    )
    repo_id = selected_repo.get("id")
    metrics_repo_id = repo_id if repo_id is not None else -1

    perspectives: dict[str, dict[str, Any]] = {
        "product": _product_perspective(handoff),
    }
    for key, label, lens_name in PERSPECTIVE_ORDER[1:]:
        metrics = cb_lenses.get_lens_metrics(db, lens_name, repo_id=metrics_repo_id, user_id=user_id)
        perspectives[key] = _perspective_from_metrics(key, label, metrics)

    timeline = list_operator_feed(db, user_id=user_id, limit=20)
    summary = {
        "planner_task_id": planner_task_id,
        "repo_id": repo_id,
        "repo_source": selected_repo.get("source"),
        "repo_reason": selected_repo.get("reason"),
        "perspective_count": len(perspectives),
        "timeline_count": len(timeline),
        "generated_from": "single_report",
    }
    return {
        "summary": summary,
        "selected_repo": selected_repo,
        "perspectives": perspectives,
        "timeline": timeline,
        "planner_handoff": handoff,
    }


def store_analysis_snapshot(
    db: Session,
    *,
    user_id: int | None,
    planner_task_id: int | None,
    repo_id: int | None,
    source_run_id: int | None,
    payload: dict[str, Any],
) -> ProjectAnalysisSnapshot:
    row = ProjectAnalysisSnapshot(
        user_id=user_id,
        task_id=planner_task_id,
        repo_id=repo_id,
        source_run_id=source_run_id,
        status="completed",
        summary_json=json.dumps(payload.get("summary") or {}, default=str),
        perspectives_json=json.dumps(payload.get("perspectives") or {}, default=str),
        timeline_json=json.dumps(payload.get("timeline") or [], default=str),
    )
    db.add(row)
    db.flush()
    return row


def latest_analysis_snapshot(
    db: Session,
    *,
    user_id: int | None,
    planner_task_id: int | None = None,
) -> dict[str, Any] | None:
    q = db.query(ProjectAnalysisSnapshot)
    if user_id is not None:
        q = q.filter(ProjectAnalysisSnapshot.user_id == user_id)
    if planner_task_id is not None:
        q = q.filter(ProjectAnalysisSnapshot.task_id == planner_task_id)
    row = q.order_by(ProjectAnalysisSnapshot.created_at.desc(), ProjectAnalysisSnapshot.id.desc()).first()
    if row is None:
        return None
    try:
        summary = json.loads(row.summary_json or "{}")
    except Exception:
        summary = {}
    try:
        perspectives = json.loads(row.perspectives_json or "{}")
    except Exception:
        perspectives = {}
    try:
        timeline = json.loads(row.timeline_json or "[]")
    except Exception:
        timeline = []
    return {
        "id": row.id,
        "status": row.status,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "summary": summary,
        "perspectives": perspectives,
        "timeline": timeline,
    }
