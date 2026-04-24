"""Canonical workspace binding helpers for planner coding tasks."""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import or_
from sqlalchemy.orm import Query, Session

from ...models.code_brain import CodeRepo
from ...models.coding_task import PlanTaskCodingProfile
from ..code_brain.runtime import resolve_repo_runtime_path
from . import envelope


class WorkspaceUnbound(ValueError):
    """Raised when a mutation is attempted on a task whose workspace is unbound
    or points to an inactive/deleted code repo. Router layer maps this to
    HTTP 409 so UI surfaces can show the operator the actionable reason.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _workspace_query(db: Session, user_id: int | None = None) -> Query:
    q = db.query(CodeRepo).filter(CodeRepo.active.is_(True))
    if user_id is None:
        return q
    return q.filter(or_(CodeRepo.user_id == user_id, CodeRepo.user_id.is_(None)))


def list_accessible_workspace_repos(
    db: Session,
    *,
    user_id: int | None = None,
) -> list[CodeRepo]:
    return _workspace_query(db, user_id=user_id).order_by(CodeRepo.id.asc()).all()


def first_reachable_workspace_repo(
    db: Session,
    *,
    user_id: int | None = None,
) -> CodeRepo | None:
    for repo in list_accessible_workspace_repos(db, user_id=user_id):
        if resolve_repo_runtime_path(repo) is not None:
            return repo
    return None


def _repo_indexed(repo: CodeRepo | None) -> bool:
    return bool(
        repo
        and not repo.last_index_error
        and (
            repo.last_successful_indexed_at
            or repo.last_indexed
            or (repo.last_successful_file_count or repo.file_count or 0) > 0
        )
    )


def _repo_display_path(repo: CodeRepo | None) -> str | None:
    if repo is None:
        return None
    runtime_path = resolve_repo_runtime_path(repo)
    if runtime_path is not None:
        return str(runtime_path)
    raw = repo.host_path or repo.path
    if not raw:
        return None
    try:
        return str(Path(raw).resolve())
    except OSError:
        return str(raw)


def _repo_summary(repo: CodeRepo | None) -> dict:
    runtime_path = resolve_repo_runtime_path(repo) if repo is not None else None
    return {
        "id": int(repo.id) if repo is not None else None,
        "name": repo.name if repo is not None else None,
        "path": _repo_display_path(repo),
        "reachable": runtime_path is not None,
        "indexed": _repo_indexed(repo),
    }


def list_workspace_roots() -> list[Path]:
    return envelope.list_code_repo_roots()


def legacy_workspace_root(repo_index: int | None) -> Path | None:
    if repo_index is None:
        return None
    roots = list_workspace_roots()
    try:
        idx = int(repo_index)
    except (TypeError, ValueError):
        return None
    if idx < 0 or idx >= len(roots):
        return None
    return roots[idx]


def legacy_workspace_index_for_path(path: str | Path | None) -> int | None:
    if not path:
        return None
    try:
        target = Path(path).resolve()
    except OSError:
        return None
    for idx, root in enumerate(list_workspace_roots()):
        try:
            if root.resolve() == target:
                return idx
        except OSError:
            continue
    return None


def get_active_workspace_repo(
    db: Session,
    code_repo_id: int | None,
    *,
    user_id: int | None = None,
) -> CodeRepo | None:
    if code_repo_id is None:
        return None
    return (
        _workspace_query(db, user_id=user_id)
        .filter(CodeRepo.id == int(code_repo_id))
        .first()
    )


def get_bound_workspace_repo_for_profile(
    db: Session,
    profile: PlanTaskCodingProfile | None,
    *,
    user_id: int | None = None,
) -> CodeRepo | None:
    if profile is None:
        return None
    return get_active_workspace_repo(db, profile.code_repo_id, user_id=user_id)


def resolve_workspace_repo_from_legacy_index(
    db: Session,
    repo_index: int | None,
    *,
    user_id: int | None = None,
) -> CodeRepo | None:
    root = legacy_workspace_root(repo_index)
    if root is None:
        return None
    for row in _workspace_query(db, user_id=user_id).all():
        try:
            if Path(row.path).resolve() == root.resolve():
                return row
        except OSError:
            continue
    return None


def resolve_workspace_repo_by_name(
    db: Session,
    repo_name: str | None,
    *,
    user_id: int | None = None,
) -> CodeRepo | None:
    name = (repo_name or "").strip().lower()
    if not name:
        return None
    matches = [
        row
        for row in _workspace_query(db, user_id=user_id).all()
        if (row.name or "").strip().lower() == name
    ]
    if len(matches) > 1:
        raise ValueError("Multiple active registered repos share that name; choose by code_repo_id.")
    return matches[0] if matches else None


def lookup_workspace_repo_for_profile(
    db: Session,
    profile: PlanTaskCodingProfile | None,
    *,
    user_id: int | None = None,
) -> CodeRepo | None:
    if profile is None:
        return None
    repo = get_bound_workspace_repo_for_profile(db, profile, user_id=user_id)
    if repo is not None:
        return repo
    return resolve_workspace_repo_from_legacy_index(db, profile.repo_index, user_id=user_id)


def workspace_binding_reason(
    db: Session,
    profile: PlanTaskCodingProfile | None,
    *,
    user_id: int | None = None,
) -> str | None:
    repo = get_bound_workspace_repo_for_profile(db, profile, user_id=user_id)
    if repo is not None:
        if resolve_repo_runtime_path(repo) is None:
            return "The bound workspace is registered but not reachable from this runtime."
        return None
    if profile is None:
        return "No workspace is bound to this task yet."
    if profile.code_repo_id is not None:
        return "The bound workspace is inactive or unavailable."
    legacy_repo = resolve_workspace_repo_from_legacy_index(db, profile.repo_index, user_id=user_id)
    if legacy_repo is not None:
        return "This task still relies on a legacy repo_index binding. Rebind it to a registered workspace."
    if legacy_workspace_root(profile.repo_index) is not None:
        return "The legacy repo_index points to a directory that is not registered as a workspace."
    return "No registered workspace matches this task profile."


def build_workspace_binding_dict(
    db: Session,
    profile: PlanTaskCodingProfile | None,
    *,
    user_id: int | None = None,
) -> dict:
    repo = get_bound_workspace_repo_for_profile(db, profile, user_id=user_id)
    legacy_root = legacy_workspace_root(profile.repo_index if profile else 0)
    effective_repo_id = int(repo.id) if repo is not None else (
        int(profile.code_repo_id) if profile and profile.code_repo_id is not None else None
    )
    return {
        "repo_index": int(profile.repo_index) if profile is not None else 0,
        "code_repo_id": effective_repo_id,
        "repo_name": repo.name if repo is not None else None,
        "repo_path": (
            str(Path(repo.host_path or repo.path).resolve())
            if repo is not None
            else (str(legacy_root) if legacy_root is not None else None)
        ),
        "sub_path": (profile.sub_path or "") if profile is not None else "",
        "workspace_bound": repo is not None,
    }


def select_runtime_workspace_repo(
    db: Session,
    profile: PlanTaskCodingProfile | None,
    *,
    user_id: int | None = None,
) -> dict:
    bound_repo = get_bound_workspace_repo_for_profile(db, profile, user_id=user_id)
    legacy_repo = None
    if bound_repo is None and profile is not None and profile.code_repo_id is None:
        legacy_repo = resolve_workspace_repo_from_legacy_index(db, profile.repo_index, user_id=user_id)
    selected_repo = None
    source = "none"
    reason = ""

    if bound_repo is not None and resolve_repo_runtime_path(bound_repo) is not None:
        selected_repo = bound_repo
        source = "task_bound"
        reason = "Using the task's bound workspace."
    elif legacy_repo is not None and resolve_repo_runtime_path(legacy_repo) is not None:
        selected_repo = legacy_repo
        source = "legacy_profile_fallback"
        reason = (
            "This task still relies on a legacy repo_index binding. "
            "Read-only views are using that workspace until you rebind it."
        )
    else:
        fallback_repo = first_reachable_workspace_repo(db, user_id=user_id)
        if fallback_repo is not None:
            selected_repo = fallback_repo
            source = "reachable_fallback"
            if bound_repo is not None:
                reason = (
                    "The bound workspace is not reachable from this runtime, "
                    "so read-only views are using the first reachable workspace."
                )
            elif legacy_repo is not None:
                reason = (
                    "This task still relies on a legacy repo_index binding. "
                    "Read-only views are using the first reachable workspace until you rebind it."
                )
            elif profile is not None:
                binding_reason = workspace_binding_reason(db, profile, user_id=user_id)
                if binding_reason:
                    reason = binding_reason + " Read-only views are using the first reachable workspace."
                else:
                    reason = "Read-only views are using the first reachable workspace."
            else:
                reason = "Using the first reachable registered workspace."
        else:
            binding_reason = workspace_binding_reason(db, profile, user_id=user_id)
            reason = binding_reason or "No reachable registered workspace is available."

    selected = _repo_summary(selected_repo)
    bound = _repo_summary(bound_repo)
    return {
        **selected,
        "source": source,
        "reason": reason,
        "bound_repo_id": bound.get("id"),
        "bound_repo_name": bound.get("name"),
        "bound_repo_reachable": bool(bound.get("reachable")),
    }


def select_runtime_workspace_repo_for_task(
    db: Session,
    task_id: int | None,
    *,
    user_id: int | None = None,
) -> dict:
    profile = None
    if task_id is not None:
        profile = (
            db.query(PlanTaskCodingProfile)
            .filter(PlanTaskCodingProfile.task_id == task_id)
            .first()
        )
    return select_runtime_workspace_repo(db, profile, user_id=user_id)


def bind_profile_workspace(
    db: Session,
    profile: PlanTaskCodingProfile,
    *,
    code_repo_id: int | None = None,
    repo_name: str | None = None,
    repo_index: int | None = None,
    user_id: int | None = None,
) -> CodeRepo | None:
    """Bind a profile to a registered workspace. Canonical path writes only
    ``code_repo_id``. Legacy ``repo_index`` writes are deprecated (A2/D3) —
    when binding via ``code_repo_id``/``repo_name`` we no longer backfill
    ``repo_index``. Direct ``repo_index`` binding still works but logs a
    deprecation warning so operators can spot stragglers.
    """
    import logging

    _log = logging.getLogger("chili.coding_task.workspaces")
    repo: CodeRepo | None = None
    if code_repo_id is not None:
        repo = get_active_workspace_repo(db, code_repo_id, user_id=user_id)
        if repo is None:
            raise ValueError("code_repo_id does not point to an active registered workspace.")
        profile.code_repo_id = int(repo.id)
        return repo

    if repo_name is not None:
        repo = resolve_workspace_repo_by_name(db, repo_name, user_id=user_id)
        if repo is None:
            raise ValueError("repo_name does not match an active registered workspace.")
        profile.code_repo_id = int(repo.id)
        return repo

    if repo_index is not None:
        _log.warning(
            "bind_profile_workspace called with legacy repo_index=%s (deprecated); "
            "migrate caller to code_repo_id",
            repo_index,
        )
        profile.repo_index = int(repo_index)
        repo = resolve_workspace_repo_from_legacy_index(db, repo_index, user_id=user_id)
        profile.code_repo_id = int(repo.id) if repo is not None else None
        return repo

    return lookup_workspace_repo_for_profile(db, profile, user_id=user_id)


def require_bound_repo(
    db: Session,
    profile: PlanTaskCodingProfile | None,
    *,
    user_id: int | None = None,
) -> CodeRepo:
    """Return the active CodeRepo for ``profile`` or raise WorkspaceUnbound
    with an actionable reason. Use from mutation endpoints to fail closed.
    """
    repo = get_bound_workspace_repo_for_profile(db, profile, user_id=user_id)
    if repo is None:
        reason = workspace_binding_reason(db, profile, user_id=user_id) or (
            "Task workspace is not bound to an active registered repo."
        )
        raise WorkspaceUnbound(reason)
    return repo


def resolve_profile_cwd(
    db: Session,
    profile: PlanTaskCodingProfile | None,
    *,
    user_id: int | None = None,
) -> Path:
    repo = get_bound_workspace_repo_for_profile(db, profile, user_id=user_id)
    if repo is None:
        reason = workspace_binding_reason(db, profile, user_id=user_id) or (
            "Task workspace is not bound to an active registered repo. Rebind the task to a Project workspace first."
        )
        raise WorkspaceUnbound(reason)
    try:
        root = resolve_repo_runtime_path(repo)
    except OSError as exc:
        raise ValueError("Bound workspace path could not be resolved.") from exc
    if root is None:
        raise WorkspaceUnbound(
            "The bound workspace is registered but not reachable from this runtime."
        )
    rel = ((profile.sub_path if profile is not None else "") or "").strip().replace("\\", "/").strip("/")
    if not rel:
        cwd = root
    else:
        if ".." in Path(rel).parts:
            raise ValueError("sub_path must not contain '..'")
        cwd = (root / rel).resolve()
    try:
        cwd.relative_to(root)
    except ValueError as exc:
        raise ValueError("Resolved cwd escapes the bound workspace root.") from exc
    if not cwd.is_dir():
        raise ValueError("Resolved cwd is not a directory.")
    return cwd
