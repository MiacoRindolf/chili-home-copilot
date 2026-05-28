"""Durable Project Brain Local Autopilot orchestration.

The orchestrator is intentionally local-first and safety-first:

* every run gets its own git worktree and integration branch;
* file and merge leases prevent concurrent autonomous edits from colliding;
* model calls prefer local Ollama models, with premium fallback left outside
  this module;
* merge only happens after validation and explicit gates pass.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable

from sqlalchemy import or_
from sqlalchemy.orm import Session

from ...models import (
    ProjectAutonomyArtifact,
    ProjectAutonomyLearningSample,
    ProjectAutonomyLease,
    ProjectAutonomyRun,
    ProjectAutonomyStep,
    ProjectDomainRun,
)
from ...models.code_brain import CodeRepo
from ..code_brain import indexer as cb_indexer
from ..code_brain import insights as insights_mod
from ..code_brain.agent import (
    _MAX_FILES_PER_EDIT,
    _build_edit_prompt,
    _gather_context,
    _parse_plan_json,
    _read_file_content,
    _validate_diff,
)
from ..code_brain.runtime import resolve_repo_runtime_path
from ..code_dispatch import frozen_scope
from ..coding_task import workspaces as workspace_mod
from ..coding_task.envelope import subprocess_safe_env, truncate_text
from ..coding_task.validator_runner import (
    StepResult,
    run_ast_syntax,
    run_mypy_check,
    run_pytest_targeted,
    run_ruff_check,
)
from ..context_brain import ollama_client
from ..project_domain_runs import finish_run, start_run

TERMINAL_STATUSES = frozenset({"merged", "completed", "blocked", "failed", "cancelled"})
ACTIVE_STATUSES = frozenset({"queued", "running", "validating", "merging"})
AUTONOMOUS_KIND = "autonomous"

_MODEL_PREFERENCE = (
    "chili-coder:current",
    "qwen2.5-coder:7b",
    "qwen2.5-coder",
    "qwen3-coder",
    "qwen3:4b",
    "phi4-mini:latest",
    "llama3:latest",
    "llama3.2:1b",
)

_PLAN_TIMEOUT_SEC = float(os.environ.get("CHILI_PROJECT_AUTOPILOT_PLAN_TIMEOUT_SEC") or "150")
_PLAN_NUM_PREDICT = int(os.environ.get("CHILI_PROJECT_AUTOPILOT_PLAN_NUM_PREDICT") or "900")
_PLAN_PROMPT_CHAR_LIMIT = int(os.environ.get("CHILI_PROJECT_AUTOPILOT_PLAN_PROMPT_CHARS") or "9000")

_STAGE_ORDER = (
    "classify",
    "repo_scan",
    "plan",
    "assign_roles",
    "implement",
    "integrate",
    "validate",
    "repair",
    "merge",
    "learn",
)


class AutonomyBlocked(RuntimeError):
    """Expected stop condition that leaves the branch/worktree for review."""


class AutonomyCancelled(RuntimeError):
    """Raised when the operator cancels an active run."""


def _utcnow() -> datetime:
    return datetime.utcnow()


def _json_text(value: Any) -> str:
    return json.dumps(value, default=str)


def _json_load(raw: str | None, fallback: Any) -> Any:
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except Exception:
        return fallback


def _clip(text: str | None, limit: int = 6000) -> str:
    return truncate_text(text or "", max_bytes=limit)[0]


def _safe_rel_path(path: str | None) -> str | None:
    raw = (path or "").replace("\\", "/").strip()
    raw = raw.lstrip("/")
    if not raw or raw.startswith("../") or "/../" in raw or raw == "..":
        return None
    return raw


def _run_payload(row: ProjectAutonomyRun) -> dict[str, Any]:
    return {
        "id": row.id,
        "run_id": row.run_id,
        "project_run_id": row.project_run_id,
        "user_id": row.user_id,
        "repo_id": row.repo_id,
        "prompt": row.prompt,
        "status": row.status,
        "current_stage": row.current_stage,
        "autonomy_level": row.autonomy_level,
        "model_policy": row.model_policy,
        "target_branch": row.target_branch,
        "base_branch": row.base_branch,
        "base_sha": row.base_sha,
        "integration_branch": row.integration_branch,
        "worktree_path": row.worktree_path,
        "merge_status": row.merge_status,
        "merge_message": row.merge_message,
        "plan": _json_load(row.plan_json, {}),
        "agents": _json_load(row.agents_json, []),
        "files": _json_load(row.files_json, []),
        "commands": _json_load(row.commands_json, []),
        "validation": _json_load(row.validation_json, []),
        "learning": _json_load(row.learning_json, {}),
        "error_message": row.error_message,
        "cancel_requested": bool(row.cancel_requested),
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _step_payload(row: ProjectAutonomyStep) -> dict[str, Any]:
    return {
        "id": row.id,
        "run_id": row.run_id,
        "step_index": row.step_index,
        "stage": row.stage,
        "agent_name": row.agent_name,
        "status": row.status,
        "title": row.title,
        "detail": _json_load(row.detail_json, {}),
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _artifact_payload(row: ProjectAutonomyArtifact) -> dict[str, Any]:
    return {
        "id": row.id,
        "run_id": row.run_id,
        "artifact_type": row.artifact_type,
        "name": row.name,
        "content": row.content,
        "content_json": _json_load(row.content_json, None),
        "byte_length": row.byte_length,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def run_payload(db: Session, row: ProjectAutonomyRun, *, include_events: bool = False) -> dict[str, Any]:
    payload = _run_payload(row)
    if include_events:
        payload["steps"] = [
            _step_payload(step)
            for step in (
                db.query(ProjectAutonomyStep)
                .filter(ProjectAutonomyStep.run_id == row.run_id)
                .order_by(ProjectAutonomyStep.id.asc())
                .limit(300)
                .all()
            )
        ]
        payload["artifacts"] = [
            _artifact_payload(artifact)
            for artifact in (
                db.query(ProjectAutonomyArtifact)
                .filter(ProjectAutonomyArtifact.run_id == row.run_id)
                .order_by(ProjectAutonomyArtifact.id.asc())
                .limit(80)
                .all()
            )
        ]
    return payload


def list_runs(
    db: Session,
    *,
    user_id: int | None = None,
    repo_id: int | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    q = db.query(ProjectAutonomyRun)
    if user_id is not None:
        q = q.filter(ProjectAutonomyRun.user_id == user_id)
    if repo_id is not None:
        q = q.filter(ProjectAutonomyRun.repo_id == int(repo_id))
    rows = q.order_by(ProjectAutonomyRun.created_at.desc(), ProjectAutonomyRun.id.desc()).limit(limit).all()
    return [run_payload(db, row, include_events=False) for row in rows]


def get_run(
    db: Session,
    run_id: str,
    *,
    user_id: int | None = None,
    include_events: bool = True,
) -> dict[str, Any] | None:
    row = _get_run_row(db, run_id, user_id=user_id)
    if row is None:
        return None
    return run_payload(db, row, include_events=include_events)


def events_after(
    db: Session,
    run_id: str,
    *,
    after_step_id: int = 0,
    after_artifact_id: int = 0,
) -> dict[str, Any]:
    steps = (
        db.query(ProjectAutonomyStep)
        .filter(ProjectAutonomyStep.run_id == run_id, ProjectAutonomyStep.id > int(after_step_id or 0))
        .order_by(ProjectAutonomyStep.id.asc())
        .limit(100)
        .all()
    )
    artifacts = (
        db.query(ProjectAutonomyArtifact)
        .filter(ProjectAutonomyArtifact.run_id == run_id, ProjectAutonomyArtifact.id > int(after_artifact_id or 0))
        .order_by(ProjectAutonomyArtifact.id.asc())
        .limit(50)
        .all()
    )
    return {
        "steps": [_step_payload(step) for step in steps],
        "artifacts": [_artifact_payload(artifact) for artifact in artifacts],
        "after_step_id": max([int(step.id) for step in steps], default=int(after_step_id or 0)),
        "after_artifact_id": max([int(artifact.id) for artifact in artifacts], default=int(after_artifact_id or 0)),
    }


def _get_run_row(db: Session, run_id: str, *, user_id: int | None = None) -> ProjectAutonomyRun | None:
    q = db.query(ProjectAutonomyRun).filter(ProjectAutonomyRun.run_id == run_id)
    if user_id is not None:
        q = q.filter(ProjectAutonomyRun.user_id == user_id)
    return q.first()


def _resolve_repo_for_run(db: Session, repo_id: int | None, *, user_id: int | None) -> CodeRepo | None:
    if repo_id is not None:
        return cb_indexer.get_accessible_repo(
            db,
            int(repo_id),
            user_id=user_id,
            include_shared=True,
        )
    return workspace_mod.first_reachable_workspace_repo(db, user_id=user_id)


def create_run(
    db: Session,
    *,
    prompt: str,
    repo_id: int | None = None,
    user_id: int | None = None,
    autonomy_level: str = "full_local",
    model_policy: str = "local_first",
) -> ProjectAutonomyRun:
    clean_prompt = (prompt or "").strip()
    if not clean_prompt:
        raise ValueError("Prompt is required.")
    repo = _resolve_repo_for_run(db, repo_id, user_id=user_id)
    if repo is None:
        raise ValueError("No reachable registered repo is available for Project Autopilot.")
    if resolve_repo_runtime_path(repo) is None:
        raise ValueError("The selected repo is registered but not reachable from this runtime.")

    run_id = "pa_" + uuid.uuid4().hex[:14]
    project_run = start_run(
        db,
        AUTONOMOUS_KIND,
        user_id=user_id,
        repo_id=int(repo.id),
        trigger_source="project_autopilot",
        title="Autopilot queued",
        detail={
            "run_id": run_id,
            "prompt_preview": clean_prompt[:200],
            "repo_name": repo.name,
        },
    )
    row = ProjectAutonomyRun(
        run_id=run_id,
        project_run_id=project_run.id,
        user_id=user_id,
        repo_id=int(repo.id),
        prompt=clean_prompt,
        status="queued",
        current_stage="queued",
        autonomy_level=autonomy_level,
        model_policy=model_policy,
        merge_status="pending",
    )
    db.add(row)
    db.flush()
    _record_step(
        db,
        row,
        "queued",
        "Autopilot run queued",
        status="completed",
        detail={"repo_id": int(repo.id), "repo_name": repo.name},
        commit=False,
    )
    _add_artifact(
        db,
        row.run_id,
        "prompt",
        "operator_prompt",
        content=clean_prompt,
        commit=False,
    )
    db.commit()
    db.refresh(row)
    return row


def request_cancel(db: Session, run_id: str, *, user_id: int | None = None) -> dict[str, Any] | None:
    row = _get_run_row(db, run_id, user_id=user_id)
    if row is None:
        return None
    row.cancel_requested = True
    if row.status in ACTIVE_STATUSES:
        _record_step(
            db,
            row,
            row.current_stage or "cancel",
            "Cancel requested",
            status="completed",
            detail={"requested_at": _utcnow().isoformat()},
            commit=False,
        )
    db.commit()
    db.refresh(row)
    return run_payload(db, row, include_events=True)


def merge_run(db: Session, run_id: str, *, user_id: int | None = None) -> dict[str, Any] | None:
    row = _get_run_row(db, run_id, user_id=user_id)
    if row is None:
        return None
    if not row.integration_branch:
        row.merge_status = "blocked"
        row.merge_message = "No integration branch is recorded for this run."
        db.commit()
        return run_payload(db, row, include_events=True)
    repo = _repo_for_row(db, row)
    repo_path = resolve_repo_runtime_path(repo) if repo is not None else None
    if repo is None or repo_path is None:
        row.merge_status = "blocked"
        row.merge_message = "Selected repo is no longer reachable."
        db.commit()
        return run_payload(db, row, include_events=True)
    changed_files = [str(x) for x in _json_load(row.files_json, [])]
    try:
        result = _attempt_merge(db, row, repo_path, changed_files)
        db.commit()
        return run_payload(db, row, include_events=True) | {"merge_result": result}
    finally:
        release_run_leases(db, row.run_id)
        db.commit()


def _record_step(
    db: Session,
    run: ProjectAutonomyRun,
    stage: str,
    title: str,
    *,
    status: str = "running",
    agent_name: str = "architect",
    detail: dict[str, Any] | None = None,
    commit: bool = True,
) -> ProjectAutonomyStep:
    idx = (
        db.query(ProjectAutonomyStep)
        .filter(ProjectAutonomyStep.run_id == run.run_id)
        .count()
    )
    now = _utcnow()
    step = ProjectAutonomyStep(
        run_id=run.run_id,
        step_index=idx + 1,
        stage=stage,
        agent_name=agent_name,
        status=status,
        title=title,
        detail_json=_json_text(detail or {}),
        started_at=now,
        finished_at=now if status in {"completed", "failed", "blocked", "cancelled"} else None,
    )
    db.add(step)
    run.current_stage = stage
    run.updated_at = now
    _sync_project_run(db, run, title=title)
    db.flush()
    if commit:
        db.commit()
    return step


def _add_artifact(
    db: Session,
    run_id: str,
    artifact_type: str,
    name: str,
    *,
    content: str | None = None,
    content_json: Any | None = None,
    commit: bool = True,
) -> ProjectAutonomyArtifact:
    text_json = _json_text(content_json) if content_json is not None else None
    length = len((content or text_json or "").encode("utf-8", errors="replace"))
    row = ProjectAutonomyArtifact(
        run_id=run_id,
        artifact_type=artifact_type,
        name=name,
        content=content,
        content_json=text_json,
        byte_length=length,
    )
    db.add(row)
    db.flush()
    if commit:
        db.commit()
    return row


def _sync_project_run(db: Session, run: ProjectAutonomyRun, *, title: str | None = None) -> None:
    if not run.project_run_id:
        return
    project_run = db.query(ProjectDomainRun).filter(ProjectDomainRun.id == run.project_run_id).first()
    if project_run is None:
        return
    if title:
        project_run.title = title
    project_run.detail_json = _json_text(
        {
            "run_id": run.run_id,
            "status": run.status,
            "stage": run.current_stage,
            "merge_status": run.merge_status,
            "repo_id": run.repo_id,
            "branch": run.integration_branch,
        }
    )


def _finish(
    db: Session,
    run: ProjectAutonomyRun,
    *,
    status: str,
    stage: str,
    title: str,
    error_message: str | None = None,
    merge_status: str | None = None,
    merge_message: str | None = None,
) -> ProjectAutonomyRun:
    now = _utcnow()
    run.status = status
    run.current_stage = stage
    run.error_message = error_message
    if merge_status is not None:
        run.merge_status = merge_status
    if merge_message is not None:
        run.merge_message = merge_message
    run.finished_at = now
    run.updated_at = now
    _record_step(
        db,
        run,
        stage,
        title,
        status=status if status in {"failed", "blocked", "cancelled"} else "completed",
        detail={"error_message": error_message, "merge_message": merge_message},
        commit=False,
    )
    if run.project_run_id:
        project_run = db.query(ProjectDomainRun).filter(ProjectDomainRun.id == run.project_run_id).first()
        if project_run is not None:
            finish_run(
                db,
                project_run,
                status=status,
                detail={
                    "run_id": run.run_id,
                    "stage": stage,
                    "merge_status": run.merge_status,
                    "merge_message": run.merge_message,
                    "branch": run.integration_branch,
                    "validation": _json_load(run.validation_json, []),
                },
                error_message=error_message,
            )
    db.commit()
    return run


def _repo_for_row(db: Session, row: ProjectAutonomyRun) -> CodeRepo | None:
    if row.repo_id is None:
        return None
    return cb_indexer.get_accessible_repo(
        db,
        int(row.repo_id),
        user_id=row.user_id,
        include_shared=True,
    )


def _check_cancel(db: Session, run: ProjectAutonomyRun) -> None:
    db.refresh(run)
    if run.cancel_requested:
        raise AutonomyCancelled("Run cancelled by operator.")


def select_local_model() -> dict[str, Any]:
    models = ollama_client.list_models()
    for preferred in _MODEL_PREFERENCE:
        exact = next((model for model in models if model == preferred), None)
        if exact:
            return {
                "model": exact,
                "available": True,
                "installed_models": models,
                "recommendation": None,
            }
        if ":" in preferred:
            continue
        prefix = f"{preferred}:"
        for model in models:
            if model == preferred or model.startswith(prefix):
                return {
                    "model": model,
                    "available": True,
                    "installed_models": models,
                    "recommendation": None,
                }
    return {
        "model": None,
        "available": False,
        "installed_models": models,
        "recommendation": "Pull a local coder model, for example: ollama pull qwen2.5-coder:7b",
    }


def _candidate_exists(repo_path: Path | None, rel_path: str) -> bool:
    rel = _safe_rel_path(rel_path)
    if rel is None:
        return False
    if repo_path is None:
        return True
    return (repo_path / rel).is_file()


def _plan_candidate_files(context: dict[str, Any], repo_path: Path | None, prompt: str) -> list[str]:
    prompt_lower = (prompt or "").lower()
    seeded: list[str] = []
    if any(token in prompt_lower for token in ("desktop", "flutter", "native", "ui", "screen", "autopilot")):
        seeded.extend(
            [
                "chili_mobile/lib/src/brain/brain_dispatch_screen.dart",
                "chili_mobile/lib/src/network/chili_api_client.dart",
                "chili_mobile/lib/src/network/network_error_message.dart",
            ]
        )
    if any(token in prompt_lower for token in ("project brain", "project autopilot", "autonomy", "autonomous")):
        seeded.extend(
            [
                "app/services/project_autonomy/orchestrator.py",
                "app/routers/brain_project.py",
                "tests/test_project_autonomy_service.py",
            ]
        )

    context_candidates: list[str] = []
    for item in context.get("relevant_files") or []:
        if isinstance(item, dict):
            context_candidates.append(str(item.get("file") or ""))
    for item in context.get("hotspots") or []:
        if isinstance(item, dict):
            context_candidates.append(str(item.get("file") or ""))

    out: list[str] = []
    seen: set[str] = set()
    for raw in seeded + context_candidates:
        rel = _safe_rel_path(raw)
        if rel is None or rel in seen or not _candidate_exists(repo_path, rel):
            continue
        seen.add(rel)
        out.append(rel)
        if len(out) >= 12:
            break
    return out


def _build_autonomy_plan_prompt(context: dict[str, Any], repo_path: Path | None) -> str:
    request = str(context.get("operator_request") or "")
    candidates = _plan_candidate_files(context, repo_path, request)
    parts = [
        "Produce compact JSON only for a safe local autonomous code run.",
        "Choose concrete files, keep scope small, and avoid speculative rewrites.",
        "",
        "Operator request:",
        request,
        "",
        "Repositories:",
    ]
    for repo in (context.get("repos") or [])[:3]:
        if not isinstance(repo, dict):
            continue
        langs = repo.get("languages") if isinstance(repo.get("languages"), dict) else {}
        lang_bits = ", ".join(f"{k}:{v}" for k, v in list(langs.items())[:5])
        parts.append(
            f"- {repo.get('name')} path={repo.get('runtime_path') or repo.get('path')} "
            f"files={repo.get('file_count')} languages={lang_bits}"
        )

    if candidates:
        parts.extend(["", "Candidate files:"])
        parts.extend(f"- {path}" for path in candidates[:12])

    insights = [
        str(item.get("description") or "")
        for item in (context.get("insights") or [])
        if isinstance(item, dict) and item.get("description")
    ][:6]
    if insights:
        parts.extend(["", "Repo patterns:"])
        parts.extend(f"- {_clip(item, 220)}" for item in insights)

    parts.extend(
        [
            "",
            "Return this JSON shape only:",
            '{"analysis":"one short paragraph","files":[{"path":"relative/path","action":"modify|create","description":"specific change"}],"notes":"caveats"}',
            "Rules: max 4 files, prefer existing candidate files, include only repo-relative paths.",
        ]
    )
    return _clip("\n".join(parts), _PLAN_PROMPT_CHAR_LIMIT)


def _fallback_plan_from_context(
    context: dict[str, Any],
    repo_path: Path | None,
    prompt: str,
    reason: str,
) -> dict[str, Any]:
    files = _plan_candidate_files(context, repo_path, prompt)[:3]
    if not files:
        return {
            "analysis": f"Local model planning was unavailable ({reason}); no safe candidate files were identified.",
            "files": [],
            "notes": "Heuristic fallback could not continue without candidate files.",
        }
    return {
        "analysis": (
            f"Local model planning was unavailable ({reason}), so the architect fell back to a conservative "
            "repo-index plan using the operator request and known project files."
        ),
        "files": [
            {
                "path": rel,
                "action": "modify",
                "description": (
                    "Make a small, low-risk implementation that directly responds to the operator request: "
                    f"{_clip(prompt, 320)}. Preserve existing behavior and local project conventions."
                ),
            }
            for rel in files
        ],
        "notes": "Heuristic fallback plan; generated diffs and validation gates still decide whether the run may merge.",
    }


def build_local_plan(db: Session, run: ProjectAutonomyRun, repo: CodeRepo) -> dict[str, Any]:
    model_info = select_local_model()
    if not model_info.get("model"):
        raise AutonomyBlocked(str(model_info.get("recommendation") or "No local Ollama model is available."))
    context = _gather_context(db, int(repo.id), run.prompt, user_id=run.user_id)
    context["operator_request"] = run.prompt
    repo_path = resolve_repo_runtime_path(repo)
    prompt = _build_autonomy_plan_prompt(context, repo_path)
    messages = [
        {
            "role": "system",
            "content": (
                "You are a senior coding architect. Produce compact JSON only. "
                "Plan for safe autonomous implementation in a git worktree."
            ),
        },
        {"role": "user", "content": prompt},
    ]
    result = ollama_client.chat(
        messages,
        str(model_info["model"]),
        temperature=0.15,
        timeout_sec=_PLAN_TIMEOUT_SEC,
        options={"num_predict": _PLAN_NUM_PREDICT},
    )
    _add_artifact(
        db,
        run.run_id,
        "model_call",
        "plan_model_call",
        content_json={
            "model": model_info["model"],
            "ok": result.ok,
            "latency_ms": result.latency_ms,
            "error": result.error,
            "installed_models": model_info.get("installed_models"),
            "prompt_chars": len(prompt),
        },
    )
    if not result.ok:
        fallback = _fallback_plan_from_context(context, repo_path, run.prompt, result.error or "unknown error")
        _add_artifact(db, run.run_id, "plan", "heuristic_plan_fallback", content_json=fallback)
        if fallback.get("files"):
            return fallback
        raise AutonomyBlocked(f"Local model planning failed: {result.error or 'unknown error'}")
    plan = _parse_plan_json(result.text)
    if not plan:
        fallback = _fallback_plan_from_context(context, repo_path, run.prompt, "unusable model JSON")
        _add_artifact(db, run.run_id, "plan", "heuristic_plan_fallback", content_json=fallback)
        if fallback.get("files"):
            return fallback
        raise AutonomyBlocked("Local model did not return a usable implementation plan.")
    plan.setdefault("analysis", "")
    plan.setdefault("files", [])
    plan.setdefault("notes", "")
    return plan


def _plan_files(plan: dict[str, Any]) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in plan.get("files") or []:
        if not isinstance(item, dict):
            continue
        rel = _safe_rel_path(str(item.get("path") or ""))
        if rel is None or rel in seen:
            continue
        seen.add(rel)
        files.append(
            {
                "path": rel,
                "action": str(item.get("action") or "modify"),
                "description": str(item.get("description") or ""),
            }
        )
        if len(files) >= _MAX_FILES_PER_EDIT:
            break
    return files


def assign_agent_lanes(files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    lanes: dict[str, set[str]] = {"architect": set()}
    for item in files:
        path = str(item.get("path") or "")
        lower = path.lower()
        role = "backend"
        if lower.startswith("tests/") or "/tests/" in lower or Path(lower).name.startswith("test_"):
            role = "qa"
        elif lower.endswith((".html", ".css", ".js", ".jsx", ".ts", ".tsx")) or lower.startswith(("app/static/", "app/templates/")):
            role = "frontend"
        elif any(token in lower for token in ("auth", "token", "secret", "credential", "permission", "security")):
            role = "security"
        elif lower.startswith((".github/", "scripts/", "docker")) or lower in {"docker-compose.yml", "dockerfile"}:
            role = "devops"
        lanes.setdefault(role, set()).add(path)
        lanes["architect"].add(path)

    out: list[dict[str, Any]] = [
        {
            "name": "architect",
            "role": "lead",
            "status": "lead",
            "files": sorted(lanes.get("architect") or []),
        }
    ]
    for role in ("backend", "frontend", "qa", "security", "devops"):
        paths = sorted(lanes.get(role) or [])
        if not paths:
            continue
        out.append({"name": role, "role": role, "status": "assigned", "files": paths})
    return out


def acquire_file_leases(
    db: Session,
    run: ProjectAutonomyRun,
    repo_id: int,
    files: Iterable[str],
    *,
    holder: str = "architect",
    ttl_minutes: int = 120,
) -> list[ProjectAutonomyLease]:
    acquired: list[ProjectAutonomyLease] = []
    now = _utcnow()
    expires_at = _utcnow() + timedelta(minutes=ttl_minutes)
    for raw in files:
        rel = _safe_rel_path(raw)
        if rel is None:
            continue
        lease_key = f"repo:{repo_id}:file:{rel}"
        conflict = (
            db.query(ProjectAutonomyLease)
            .filter(
                ProjectAutonomyLease.repo_id == int(repo_id),
                ProjectAutonomyLease.lease_key == lease_key,
                ProjectAutonomyLease.status == "active",
                ProjectAutonomyLease.run_id != run.run_id,
                or_(ProjectAutonomyLease.expires_at.is_(None), ProjectAutonomyLease.expires_at > now),
            )
            .first()
        )
        if conflict is not None:
            raise AutonomyBlocked(f"File is already leased by another run: {rel}")
        lease = ProjectAutonomyLease(
            run_id=run.run_id,
            repo_id=int(repo_id),
            lease_key=lease_key,
            file_path=rel,
            holder=holder,
            status="active",
            expires_at=expires_at,
        )
        db.add(lease)
        acquired.append(lease)
    db.flush()
    return acquired


def acquire_repo_lease(db: Session, run: ProjectAutonomyRun, repo_id: int) -> ProjectAutonomyLease:
    lease = ProjectAutonomyLease(
        run_id=run.run_id,
        repo_id=int(repo_id),
        lease_key=f"repo:{repo_id}:run:{run.run_id}",
        holder="architect",
        status="active",
        expires_at=_utcnow() + timedelta(minutes=120),
    )
    db.add(lease)
    db.flush()
    return lease


def acquire_agent_file_leases(
    db: Session,
    run: ProjectAutonomyRun,
    repo_id: int,
    agents: list[dict[str, Any]],
) -> list[ProjectAutonomyLease]:
    by_file: dict[str, str] = {}
    for agent in agents:
        name = str(agent.get("name") or "architect")
        if name == "architect":
            continue
        for raw in agent.get("files") or []:
            rel = _safe_rel_path(str(raw))
            if rel and rel not in by_file:
                by_file[rel] = name
    if not by_file:
        architect_files = []
        for agent in agents:
            if str(agent.get("name") or "") == "architect":
                architect_files = [str(x) for x in (agent.get("files") or [])]
                break
        return acquire_file_leases(db, run, repo_id, architect_files, holder="architect")

    leases: list[ProjectAutonomyLease] = []
    for rel, holder in by_file.items():
        leases.extend(acquire_file_leases(db, run, repo_id, [rel], holder=holder))
    return leases


def acquire_merge_lease(db: Session, run: ProjectAutonomyRun, repo_id: int) -> ProjectAutonomyLease:
    lease_key = f"repo:{repo_id}:merge"
    now = _utcnow()
    conflict = (
        db.query(ProjectAutonomyLease)
        .filter(
            ProjectAutonomyLease.repo_id == int(repo_id),
            ProjectAutonomyLease.lease_key == lease_key,
            ProjectAutonomyLease.status == "active",
            ProjectAutonomyLease.run_id != run.run_id,
            or_(ProjectAutonomyLease.expires_at.is_(None), ProjectAutonomyLease.expires_at > now),
        )
        .first()
    )
    if conflict is not None:
        raise AutonomyBlocked("The repo merge lease is held by another autonomy run.")
    lease = ProjectAutonomyLease(
        run_id=run.run_id,
        repo_id=int(repo_id),
        lease_key=lease_key,
        holder="architect",
        status="active",
        expires_at=_utcnow() + timedelta(minutes=30),
    )
    db.add(lease)
    db.flush()
    return lease


def release_run_leases(db: Session, run_id: str) -> None:
    now = _utcnow()
    rows = (
        db.query(ProjectAutonomyLease)
        .filter(ProjectAutonomyLease.run_id == run_id, ProjectAutonomyLease.status == "active")
        .all()
    )
    for row in rows:
        row.status = "released"
        row.released_at = now
    db.flush()


def generate_diffs_from_plan(
    db: Session,
    run: ProjectAutonomyRun,
    repo_path: Path,
    files: list[dict[str, Any]],
    *,
    validation_context: str | None = None,
) -> list[str]:
    model_info = select_local_model()
    if not model_info.get("model"):
        raise AutonomyBlocked(str(model_info.get("recommendation") or "No local Ollama model is available."))
    conventions = [
        str(ins.get("description") or "")
        for ins in insights_mod.get_insights(db, repo_id=run.repo_id)[:8]
        if ins.get("description")
    ]
    diffs: list[str] = []
    for item in files:
        rel = str(item.get("path") or "")
        desc = str(item.get("description") or "")
        content = _read_file_content(str(repo_path), rel)
        if validation_context:
            desc = desc + "\n\nValidation failure to repair:\n" + validation_context
        prompt = _build_edit_prompt(rel, content or "", desc, conventions)
        result = ollama_client.chat(
            [
                {"role": "system", "content": "Return a single unified diff. No prose."},
                {"role": "user", "content": prompt},
            ],
            str(model_info["model"]),
            temperature=0.1,
            timeout_sec=120,
            options={"num_predict": 2400},
        )
        _add_artifact(
            db,
            run.run_id,
            "model_call",
            f"edit_{rel}",
            content_json={
                "model": model_info["model"],
                "file": rel,
                "ok": result.ok,
                "latency_ms": result.latency_ms,
                "error": result.error,
            },
        )
        if not result.ok:
            continue
        diff = _extract_diff(result.text)
        if not diff:
            continue
        validity = _validate_diff(diff, rel, content)
        if not validity.get("valid"):
            _add_artifact(db, run.run_id, "diff_rejected", rel, content_json=validity)
            continue
        diffs.append(diff)
        _add_artifact(db, run.run_id, "diff", rel, content=diff)
    return diffs


def _extract_diff(text: str) -> str | None:
    raw = (text or "").strip()
    m = re.search(r"```(?:diff)?\s*\n(.*?)\n```", raw, re.DOTALL)
    if m:
        raw = m.group(1).strip()
    if "--- " not in raw or "+++ " not in raw:
        return None
    return raw + ("\n" if not raw.endswith("\n") else "")


def _git(cwd: Path, args: list[str], *, input_text: str | None = None, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git"] + args,
        cwd=str(cwd),
        input=input_text,
        text=True,
        capture_output=True,
        timeout=timeout,
        env=subprocess_safe_env(),
    )


def _git_text(cwd: Path, args: list[str], *, timeout: int = 120) -> str:
    proc = _git(cwd, args, timeout=timeout)
    if proc.returncode != 0:
        raise AutonomyBlocked((proc.stderr or proc.stdout or "git command failed").strip()[:600])
    return (proc.stdout or "").strip()


def _ensure_git_repo(path: Path) -> None:
    if _git(path, ["rev-parse", "--is-inside-work-tree"]).returncode != 0:
        raise AutonomyBlocked("Selected repo is not a git worktree.")


def integration_branch_name(run_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(run_id or "").strip()).strip(".-")
    if not safe:
        safe = uuid.uuid4().hex[:14]
    return f"project-auto-{safe}"


def _create_run_worktree(repo_path: Path, run: ProjectAutonomyRun, base_sha: str) -> tuple[str, Path]:
    base = Path(os.environ.get("CHILI_PROJECT_AUTOPILOT_WORKTREE_DIR") or tempfile.gettempdir()) / "chili-project-autopilot"
    base.mkdir(parents=True, exist_ok=True)
    worktree = base / run.run_id
    branch = integration_branch_name(run.run_id)

    if worktree.exists():
        _git(repo_path, ["worktree", "remove", "--force", str(worktree)], timeout=120)
        shutil.rmtree(worktree, ignore_errors=True)
    _git(repo_path, ["worktree", "unlock", str(worktree)], timeout=30)
    _git(repo_path, ["worktree", "prune"], timeout=60)
    proc = _git(repo_path, ["worktree", "add", "-B", branch, str(worktree), base_sha], timeout=180)
    if proc.returncode != 0:
        raise AutonomyBlocked(f"Could not create isolated worktree: {(proc.stderr or proc.stdout or '').strip()[:900]}")
    return branch, worktree


def _apply_diffs(worktree: Path, diffs: list[str]) -> None:
    if not diffs:
        raise AutonomyBlocked("No implementation diffs were generated.")
    patch = "\n".join(diff.rstrip() for diff in diffs) + "\n"
    check = _git(worktree, ["apply", "--check"], input_text=patch, timeout=120)
    if check.returncode != 0:
        raise AutonomyBlocked(f"Generated diff did not apply cleanly: {(check.stderr or check.stdout or '').strip()[:900]}")
    applied = _git(worktree, ["apply"], input_text=patch, timeout=120)
    if applied.returncode != 0:
        raise AutonomyBlocked(f"Could not apply generated diff: {(applied.stderr or applied.stdout or '').strip()[:900]}")


def _changed_files(worktree: Path) -> list[str]:
    proc = _git(worktree, ["diff", "--name-only"], timeout=60)
    files = []
    for line in (proc.stdout or "").splitlines():
        rel = _safe_rel_path(line)
        if rel:
            files.append(rel)
    return sorted(dict.fromkeys(files))


def _commit_if_needed(worktree: Path, run: ProjectAutonomyRun) -> str | None:
    _git(worktree, ["add", "-A"], timeout=120)
    quiet = _git(worktree, ["diff", "--cached", "--quiet"], timeout=60)
    if quiet.returncode == 0:
        return None
    message = (
        f"[project-autopilot] {run.prompt[:90].replace(chr(10), ' ').strip() or run.run_id}\n\n"
        f"Generated by Project Brain Local Autopilot run {run.run_id}."
    )
    proc = _git(
        worktree,
        [
            "-c",
            "user.name=CHILI Autopilot",
            "-c",
            "user.email=chili-autopilot@local",
            "commit",
            "-m",
            message,
        ],
        timeout=180,
    )
    if proc.returncode != 0:
        raise AutonomyBlocked(f"Commit failed in integration branch: {(proc.stderr or proc.stdout or '').strip()[:900]}")
    return _git_text(worktree, ["rev-parse", "HEAD"], timeout=60)


def command_allowed(argv: list[str], cwd: Path) -> tuple[bool, str | None]:
    if not argv:
        return False, "empty command"
    normalized = [str(part).strip() for part in argv if str(part).strip()]
    lowered = [part.lower() for part in normalized]
    if not normalized:
        return False, "empty command"
    dangerous = {"rm", "del", "erase", "rmdir", "format", "curl", "wget", "pip", "poetry", "uv", "pnpm", "yarn"}
    if lowered[0] in dangerous:
        return False, "installs, network, and destructive commands require escalation"
    if lowered[:3] == [sys.executable.lower(), "-m", "pytest"] or lowered[:2] == ["python", "-m"]:
        return True, None
    allowed_prefixes = (
        ("pytest",),
        ("ruff", "check"),
        ("mypy",),
        ("npm", "test"),
        ("npm", "run", "lint"),
        ("npm", "run", "test"),
        ("npm", "run", "build"),
        ("git", "status"),
        ("git", "diff"),
    )
    for prefix in allowed_prefixes:
        if tuple(lowered[: len(prefix)]) == prefix:
            return True, None
    if lowered[:2] == ["npm", "run"] and len(lowered) >= 3:
        scripts = _package_scripts(cwd)
        if lowered[2] in {"lint", "test", "build"} and lowered[2] in scripts:
            return True, None
    return False, "command is not in the Project Autopilot allowlist"


def _package_scripts(cwd: Path) -> set[str]:
    pkg = cwd / "package.json"
    if not pkg.is_file():
        return set()
    try:
        data = json.loads(pkg.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return set()
    scripts = data.get("scripts") if isinstance(data, dict) else None
    if not isinstance(scripts, dict):
        return set()
    return {str(k).lower() for k in scripts.keys()}


def _run_allowlisted(argv: list[str], cwd: Path, *, timeout: int = 300) -> StepResult:
    ok, reason = command_allowed(argv, cwd)
    key = "_".join(part.replace("-", "") for part in argv[:3])[:60] or "command"
    if not ok:
        return StepResult(key, 0, False, "", "", True, reason)
    env = subprocess_safe_env()
    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(cwd),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError:
        return StepResult(key, 0, False, "", f"executable not found: {argv[0]}", True, f"{argv[0]} not available")
    try:
        out, err = proc.communicate(timeout=timeout)
        out_t = _clip(out)
        err_t = _clip(err)
        return StepResult(key, proc.returncode or 0, False, out_t, err_t, False, None)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
        return StepResult(key, -1, True, _clip(out), _clip((err or "") + "\n[timeout]"), False, None)


def _step_result_payload(result: StepResult) -> dict[str, Any]:
    return {
        "step_key": result.step_key,
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "skipped": result.skipped,
        "skip_reason": result.skip_reason,
        "passed": result.exit_code == 0,
    }


def run_validation(worktree: Path, changed_files: list[str]) -> list[dict[str, Any]]:
    results: list[StepResult] = [
        run_ast_syntax(worktree),
        run_ruff_check(worktree),
        run_pytest_targeted(worktree, changed_files),
        run_mypy_check(worktree),
    ]
    scripts = _package_scripts(worktree)
    npm = shutil.which("npm")
    if npm:
        for script in ("lint", "test", "build"):
            if script in scripts:
                results.append(_run_allowlisted(["npm", "run", script], worktree, timeout=300))
    else:
        for script in ("lint", "test", "build"):
            if script in scripts:
                results.append(StepResult(f"npm_run_{script}", 0, False, "", "", True, "npm not available"))
    return [_step_result_payload(result) for result in results]


def validation_passed(results: list[dict[str, Any]]) -> bool:
    return all(int(item.get("exit_code") or 0) == 0 for item in results)


def _validation_failure_text(results: list[dict[str, Any]]) -> str:
    failed = [r for r in results if int(r.get("exit_code") or 0) != 0]
    return "\n\n".join(
        f"{r.get('step_key')} exit={r.get('exit_code')}\n{r.get('stdout') or ''}\n{r.get('stderr') or ''}"
        for r in failed[:4]
    )[:5000]


def _dirty_files(repo_path: Path) -> list[str]:
    proc = _git(repo_path, ["status", "--porcelain"], timeout=60)
    files: list[str] = []
    for line in (proc.stdout or "").splitlines():
        rel = _safe_rel_path(line[3:])
        if rel:
            files.append(rel)
    return sorted(dict.fromkeys(files))


def _intersects(a: Iterable[str], b: Iterable[str]) -> bool:
    aa = {_safe_rel_path(x) for x in a}
    bb = {_safe_rel_path(x) for x in b}
    aa.discard(None)
    bb.discard(None)
    return bool(aa & bb)


def _attempt_merge(db: Session, run: ProjectAutonomyRun, repo_path: Path, changed_files: list[str]) -> dict[str, Any]:
    if run.repo_id is None:
        raise AutonomyBlocked("Run has no repo id.")
    acquire_merge_lease(db, run, int(run.repo_id))
    frozen_hits = frozen_scope.diff_touches_frozen_scope(changed_files)
    if frozen_scope.is_blocked(frozen_hits) or frozen_scope.requires_review(frozen_hits):
        msg = "Frozen-scope gate requires manual review."
        run.merge_status = "blocked"
        run.merge_message = msg
        _add_artifact(
            db,
            run.run_id,
            "merge_gate",
            "frozen_scope",
            content_json=[hit.__dict__ for hit in frozen_hits],
            commit=False,
        )
        return {"ok": False, "reason": msg}

    current_branch = _git_text(repo_path, ["branch", "--show-current"], timeout=60)
    current_head = _git_text(repo_path, ["rev-parse", "HEAD"], timeout=60)
    if run.base_branch and current_branch != run.base_branch:
        msg = f"Target checkout is on {current_branch!r}, expected {run.base_branch!r}."
        run.merge_status = "blocked"
        run.merge_message = msg
        return {"ok": False, "reason": msg}
    if run.base_sha and current_head != run.base_sha:
        msg = "Target branch moved since the autonomy run started."
        run.merge_status = "blocked"
        run.merge_message = msg
        return {"ok": False, "reason": msg}
    dirty = _dirty_files(repo_path)
    if dirty and _intersects(dirty, changed_files):
        msg = "Target checkout has dirty changes touching the autopilot scope."
        run.merge_status = "blocked"
        run.merge_message = msg
        return {"ok": False, "reason": msg, "dirty_files": dirty}
    proc = _git(repo_path, ["merge", "--ff-only", str(run.integration_branch)], timeout=180)
    if proc.returncode != 0:
        msg = f"Merge was not clean: {(proc.stderr or proc.stdout or '').strip()[:900]}"
        run.merge_status = "blocked"
        run.merge_message = msg
        return {"ok": False, "reason": msg}
    run.merge_status = "merged"
    run.merge_message = f"Merged {run.integration_branch} into {run.base_branch or current_branch}."
    return {"ok": True, "message": run.merge_message}


def _record_learning(
    db: Session,
    run: ProjectAutonomyRun,
    *,
    outcome: str,
    plan: dict[str, Any],
    validation: list[dict[str, Any]],
) -> dict[str, Any]:
    payload = {
        "evidence_gated": True,
        "fine_tune_candidate": outcome in {"merged", "blocked", "completed"} and bool(validation),
        "promotion_status": "pending_eval",
        "outcome": outcome,
        "branch": run.integration_branch,
        "validation_passed": validation_passed(validation) if validation else False,
    }
    run.learning_json = _json_text(payload)
    db.add(
        ProjectAutonomyLearningSample(
            run_id=run.run_id,
            repo_id=run.repo_id,
            sample_type="trajectory",
            prompt=run.prompt,
            outcome=outcome,
            payload_json=_json_text({"plan": plan, "validation": validation, "learning": payload}),
            promoted=False,
        )
    )
    _add_artifact(db, run.run_id, "learning", "trajectory_sample", content_json=payload, commit=False)
    db.flush()
    return payload


def run_autonomy_sync(db: Session, run_id: str, on_event: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    run = _get_run_row(db, run_id)
    if run is None:
        raise ValueError(f"Unknown autonomy run: {run_id}")
    repo = _repo_for_row(db, run)
    repo_path = resolve_repo_runtime_path(repo) if repo is not None else None
    plan: dict[str, Any] = {}
    validation: list[dict[str, Any]] = []
    changed_files: list[str] = []
    try:
        if repo is None or repo_path is None:
            raise AutonomyBlocked("Selected repo is no longer reachable.")
        run.status = "running"
        run.started_at = _utcnow()
        db.commit()

        _record_step(db, run, "classify", "Classifying request", detail={"prompt_preview": run.prompt[:240]})
        _check_cancel(db, run)
        _ensure_git_repo(repo_path)
        base_branch = _git_text(repo_path, ["branch", "--show-current"], timeout=60)
        base_sha = _git_text(repo_path, ["rev-parse", "HEAD"], timeout=60)
        run.base_branch = base_branch
        run.target_branch = base_branch
        run.base_sha = base_sha
        db.commit()

        _record_step(db, run, "repo_scan", "Scanning repository context", detail={"repo": repo.name, "path": str(repo_path)})
        _check_cancel(db, run)
        branch, worktree = _create_run_worktree(repo_path, run, base_sha)
        run.integration_branch = branch
        run.worktree_path = str(worktree)
        acquire_repo_lease(db, run, int(repo.id))
        db.commit()
        _add_artifact(db, run.run_id, "worktree", "integration_worktree", content_json={"branch": branch, "path": str(worktree)})

        _record_step(db, run, "plan", "Architect is drafting an implementation plan")
        _check_cancel(db, run)
        plan = build_local_plan(db, run, repo)
        files = _plan_files(plan)
        if not files:
            raise AutonomyBlocked("The plan did not identify concrete files to change.")
        run.plan_json = _json_text(plan)
        run.files_json = _json_text([item["path"] for item in files])
        _add_artifact(db, run.run_id, "plan", "architect_plan", content_json=plan, commit=False)
        db.commit()

        agents = assign_agent_lanes(files)
        run.agents_json = _json_text(agents)
        _record_step(db, run, "assign_roles", "Architect assigned agent lanes", detail={"agents": agents})
        acquire_agent_file_leases(db, run, int(repo.id), agents)
        db.commit()

        _record_step(db, run, "implement", "Generating local implementation diffs", detail={"files": [f["path"] for f in files]})
        _check_cancel(db, run)
        diffs = generate_diffs_from_plan(db, run, worktree, files)
        _apply_diffs(worktree, diffs)
        changed_files = _changed_files(worktree)
        run.files_json = _json_text(changed_files)
        _record_step(db, run, "integrate", "Integrated generated diffs in isolated worktree", detail={"files": changed_files})
        db.commit()

        run.status = "validating"
        _record_step(db, run, "validate", "Running allowlisted validation commands", detail={"files": changed_files})
        validation = run_validation(worktree, changed_files)
        run.validation_json = _json_text(validation)
        run.commands_json = _json_text([{"step_key": item.get("step_key"), "exit_code": item.get("exit_code")} for item in validation])
        _add_artifact(db, run.run_id, "validation", "validation_results", content_json=validation, commit=False)
        db.commit()

        if not validation_passed(validation):
            _record_step(db, run, "repair", "Validation failed; attempting one local repair pass", status="completed")
            repair_context = _validation_failure_text(validation)
            repair_diffs = generate_diffs_from_plan(db, run, worktree, files, validation_context=repair_context)
            if repair_diffs:
                _apply_diffs(worktree, repair_diffs)
                changed_files = _changed_files(worktree)
                validation = run_validation(worktree, changed_files)
                run.files_json = _json_text(changed_files)
                run.validation_json = _json_text(validation)
                _add_artifact(db, run.run_id, "validation", "repair_validation_results", content_json=validation, commit=False)
                db.commit()

        commit_sha = _commit_if_needed(worktree, run)
        _add_artifact(db, run.run_id, "commit", "integration_commit", content_json={"commit_sha": commit_sha, "branch": branch})

        _record_step(db, run, "learn", "Recording evidence-gated learning sample")
        _record_learning(
            db,
            run,
            outcome="validated" if validation_passed(validation) else "validation_failed",
            plan=plan,
            validation=validation,
        )
        db.commit()

        if not validation_passed(validation):
            return run_payload(
                db,
                _finish(
                    db,
                    run,
                    status="blocked",
                    stage="validate",
                    title="Autopilot blocked by validation",
                    error_message=_validation_failure_text(validation),
                    merge_status="blocked",
                    merge_message="Validation failed after repair.",
                ),
                include_events=True,
            )

        run.status = "merging"
        _record_step(db, run, "merge", "Checking merge gates")
        merge_result = _attempt_merge(db, run, repo_path, changed_files)
        if merge_result.get("ok"):
            _record_learning(db, run, outcome="merged", plan=plan, validation=validation)
            return run_payload(
                db,
                _finish(
                    db,
                    run,
                    status="merged",
                    stage="merge",
                    title="Autopilot merged safely",
                    merge_status="merged",
                    merge_message=str(merge_result.get("message") or "Merged."),
                ),
                include_events=True,
            )
        _record_learning(db, run, outcome="blocked", plan=plan, validation=validation)
        return run_payload(
            db,
            _finish(
                db,
                run,
                status="blocked",
                stage="merge",
                title="Autopilot produced a validated branch",
                merge_status="blocked",
                merge_message=str(merge_result.get("reason") or "Merge gate blocked."),
            ),
            include_events=True,
        )
    except AutonomyCancelled as exc:
        return run_payload(
            db,
            _finish(
                db,
                run,
                status="cancelled",
                stage=run.current_stage or "cancelled",
                title="Autopilot cancelled",
                error_message=str(exc),
                merge_status="cancelled",
                merge_message="Cancelled by operator.",
            ),
            include_events=True,
        )
    except AutonomyBlocked as exc:
        _record_learning(db, run, outcome="blocked", plan=plan, validation=validation)
        return run_payload(
            db,
            _finish(
                db,
                run,
                status="blocked",
                stage=run.current_stage or "blocked",
                title="Autopilot blocked",
                error_message=str(exc),
                merge_status="blocked",
                merge_message=str(exc),
            ),
            include_events=True,
        )
    except Exception as exc:
        return run_payload(
            db,
            _finish(
                db,
                run,
                status="failed",
                stage=run.current_stage or "failed",
                title="Autopilot failed",
                error_message=str(exc),
                merge_status="failed",
                merge_message=str(exc),
            ),
            include_events=True,
        )
    finally:
        try:
            release_run_leases(db, run.run_id)
            db.commit()
        except Exception:
            db.rollback()
