"""Workspace-first bootstrap payloads for the Project Brain domain."""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..models import PlanTask
from . import planner_service
from .code_brain import indexer as cb_indexer
from .coding_task.service import build_handoff_dict
from .coding_task.workspaces import select_runtime_workspace_repo_for_task
from .project_analysis import latest_analysis_snapshot
from .project_brain import registry as pb_registry
from .project_domain_feed import count_unread_operator_messages, list_operator_feed
from .project_domain_runs import kind_status_payload, status_payload


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
    code_status = kind_status_payload(db, "index", user_id=user_id)
    project_status = status_payload(db, user_id=user_id)

    repos = [] if is_guest else cb_indexer.get_registered_repos(
        db,
        user_id=user_id,
        include_shared=True,
    )
    repo_count = len(repos)
    indexed_repo_count = sum(
        1
        for repo in repos
        if not repo.get("last_index_error")
        and (
            repo.get("last_successful_indexed_at")
            or repo.get("last_indexed")
            or (repo.get("last_successful_file_count") or repo.get("file_count") or 0) > 0
        )
    )

    task = _task_for_bootstrap(
        db,
        planner_task_id=planner_task_id,
        user_id=user_id,
        is_guest=is_guest,
    )
    handoff = build_handoff_dict(db, task, user_id=user_id) if task is not None else None
    selected_repo = {
        "id": None,
        "name": None,
        "path": None,
        "reachable": False,
        "indexed": False,
        "source": "none",
        "reason": "Pair this device to work with repos and task handoffs." if is_guest else "No reachable registered workspace is available.",
        "bound_repo_id": None,
        "bound_repo_name": None,
        "bound_repo_reachable": False,
    }
    if not is_guest:
        selected_repo = select_runtime_workspace_repo_for_task(
            db,
            task.id if task is not None else None,
            user_id=user_id,
        )
    profile = (handoff or {}).get("profile") or {}
    ops_hints = (handoff or {}).get("ops_hints") or {}
    workspace_bound = bool(profile.get("workspace_bound"))
    workspace_indexed = bool(ops_hints.get("workspace_indexed"))
    cwd_resolvable = bool(ops_hints.get("cwd_resolvable"))

    timeline = [] if is_guest else list_operator_feed(db, user_id=user_id, limit=20)
    latest_analysis = None if is_guest else latest_analysis_snapshot(
        db, user_id=user_id, planner_task_id=planner_task_id
    )
    agent_defs = pb_registry.list_agents()
    unread_messages = 0 if is_guest else count_unread_operator_messages(db, user_id=user_id)

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
            "selected_repo": selected_repo,
            "repos": repos,
            "empty_state": repo_count == 0,
            "web_reachable_count": sum(1 for repo in repos if repo.get("reachable_in_web")),
            "scheduler_reachable_count": sum(1 for repo in repos if repo.get("reachable_in_scheduler")),
            "setup_checklist": checklist,
        },
        "planner_handoff": {
            "planner_task_id": planner_task_id,
            "available": handoff is not None,
            "task": handoff.get("task") if handoff else None,
            "summary": handoff,
        },
        "agents": {
            "registered_count": len(agent_defs),
            "active_count": sum(1 for agent in agent_defs if agent.get("active")),
            "running": bool(project_status.get("running")),
            "unread_messages": unread_messages,
        },
        "feed": {
            "recent_count": len(timeline),
            "timeline": timeline,
        },
        "analysis": {
            "available": latest_analysis is not None,
            "latest": latest_analysis,
        },
        "capabilities": {
            "register_repo": _capability(not is_guest, "Pair this device to register workspaces."),
            "search": _capability(not is_guest and indexed_repo_count > 0, "Index a workspace to enable search."),
            "suggest": suggest,
            "apply": apply,
            "validate": validate,
        },
    }
