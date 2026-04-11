"""Workspace-first bootstrap payloads for the Project Brain domain."""
from __future__ import annotations

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..models import PlanTask
from ..models.project_brain import AgentMessage
from . import planner_service
from .code_brain import indexer as cb_indexer
from .code_brain import learning as cb_learning
from .coding_task.service import build_handoff_dict
from .project_brain import learning as pb_learning
from .project_brain import registry as pb_registry


def _capability(enabled: bool, reason: str | None = None) -> dict:
    return {"enabled": bool(enabled), "reason": None if enabled else reason}


def _task_for_bootstrap(
    db: Session,
    *,
    planner_task_id: int | None,
    user_id: int | None,
    is_guest: bool,
) -> PlanTask | None:
    if planner_task_id is None or is_guest or user_id is None:
        return None
    task = db.query(PlanTask).filter(PlanTask.id == planner_task_id).first()
    if not task:
        return None
    if not planner_service._user_can_access(db, task.project_id, user_id):
        return None
    return task


def build_project_bootstrap_payload(
    db: Session,
    *,
    user_id: int | None,
    is_guest: bool,
    planner_task_id: int | None = None,
) -> dict:
    code_status = cb_learning.get_code_learning_status()
    project_status = pb_learning.get_project_brain_status()

    repos = [] if is_guest else cb_indexer.get_registered_repos(db, user_id=user_id)
    repo_count = len(repos)
    indexed_repo_count = sum(
        1
        for repo in repos
        if repo.get("last_indexed") or (repo.get("file_count") or 0) > 0
    )

    task = _task_for_bootstrap(
        db,
        planner_task_id=planner_task_id,
        user_id=user_id,
        is_guest=is_guest,
    )
    handoff = build_handoff_dict(db, task, user_id=user_id) if task is not None else None
    profile = (handoff or {}).get("profile") or {}
    ops_hints = (handoff or {}).get("ops_hints") or {}
    workspace_bound = bool(profile.get("workspace_bound"))
    workspace_indexed = bool(ops_hints.get("workspace_indexed"))
    cwd_resolvable = bool(ops_hints.get("cwd_resolvable"))

    unread_messages = None
    recent_feed_count = 0
    if user_id is not None and not is_guest:
        unread_messages = (
            db.query(func.count(AgentMessage.id))
            .filter(
                AgentMessage.user_id == user_id,
                AgentMessage.acknowledged.is_(False),
            )
            .scalar()
            or 0
        )
        recent_feed_count = len(pb_registry.get_message_feed(db, user_id, limit=20))

    checklist = [
        {
            "key": "register_repo",
            "label": "Register a workspace repo",
            "done": repo_count > 0,
        },
        {
            "key": "index_repo",
            "label": "Index at least one repo for search and agent context",
            "done": indexed_repo_count > 0,
        },
    ]
    if planner_task_id is not None:
        checklist.append(
            {
                "key": "bind_task",
                "label": "Bind this planner task to a registered workspace",
                "done": workspace_bound,
            }
        )

    if is_guest:
        suggest = _capability(False, "Pair this device to work with repos and task handoffs.")
        apply = _capability(False, "Pair this device to modify a workspace.")
        validate = _capability(False, "Pair this device to run workspace validation.")
    elif planner_task_id is None:
        suggest = _capability(False, "Select a planner task to enable implementation handoff.")
        apply = _capability(False, "Select a planner task to enable snapshot apply.")
        validate = _capability(False, "Select a planner task to enable validation.")
    elif not workspace_bound:
        reason = ops_hints.get("workspace_reason") or "Bind the task to a registered workspace first."
        suggest = _capability(False, reason)
        apply = _capability(False, reason)
        validate = _capability(False, reason)
    else:
        suggest = _capability(
            workspace_indexed,
            "Index the bound workspace before running agent suggest.",
        )
        apply = _capability(
            cwd_resolvable,
            "The bound workspace path is not currently resolvable.",
        )
        validate = _capability(
            cwd_resolvable,
            "The bound workspace path is not currently resolvable.",
        )

    return {
        "is_guest": is_guest,
        "project_status": project_status,
        "code_status": code_status,
        "workspace": {
            "repo_count": repo_count,
            "indexed_repo_count": indexed_repo_count,
            "repos": repos,
            "empty_state": repo_count == 0,
            "setup_checklist": checklist,
        },
        "planner_handoff": {
            "planner_task_id": planner_task_id,
            "available": handoff is not None,
            "task": handoff.get("task") if handoff else None,
            "summary": handoff,
        },
        "agents": {
            "registered_count": len(pb_registry.AGENT_REGISTRY),
            "active_count": sum(1 for agent in pb_registry.AGENT_REGISTRY.values() if agent.active),
            "running": bool(project_status.get("running")),
            "unread_messages": unread_messages,
        },
        "feed": {
            "recent_count": recent_feed_count,
        },
        "capabilities": {
            "register_repo": _capability(not is_guest, "Pair this device to register workspaces."),
            "search": _capability(not is_guest and indexed_repo_count > 0, "Index a workspace to enable search."),
            "suggest": suggest,
            "apply": apply,
            "validate": validate,
        },
    }
