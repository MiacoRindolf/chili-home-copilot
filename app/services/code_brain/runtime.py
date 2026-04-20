"""Runtime-aware repository path helpers for host + container execution."""
from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional

from ...config import settings
from ...models.code_brain import CodeRepo

_CONTAINER_WORKSPACE_ROOT = Path("/workspace")
_HOST_WORKSPACE_ROOT = Path(__file__).resolve().parents[3]


def _normalized_path(raw: str | Path | None) -> Path | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        return Path(text).resolve()
    except OSError:
        return None


def _infer_container_path(host_path: Path | None) -> str | None:
    if host_path is None:
        return None
    try:
        rel = host_path.relative_to(_HOST_WORKSPACE_ROOT)
    except ValueError:
        return None
    mapped = _CONTAINER_WORKSPACE_ROOT / rel
    return str(mapped).replace("\\", "/")


def canonical_host_path(path: str | Path | None) -> str | None:
    resolved = _normalized_path(path)
    return str(resolved) if resolved is not None else None


def infer_repo_runtime_fields(path: str | Path) -> dict[str, str | None]:
    host_path = _normalized_path(path)
    return {
        "host_path": str(host_path) if host_path is not None else None,
        "container_path": _infer_container_path(host_path),
    }


def candidate_runtime_paths(repo: CodeRepo) -> Iterable[Path]:
    seen: set[str] = set()
    for raw in (repo.container_path, repo.host_path, repo.path):
        candidate = _normalized_path(raw)
        if candidate is None:
            continue
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        yield candidate


def resolve_repo_runtime_path(repo: CodeRepo) -> Path | None:
    for candidate in candidate_runtime_paths(repo):
        if candidate.is_dir():
            return candidate
    return None


def current_runtime_reachable(repo: CodeRepo) -> bool:
    return resolve_repo_runtime_path(repo) is not None


def current_runtime_label() -> str:
    role = (settings.chili_scheduler_role or "").strip().lower()
    if role in {"worker", "all"}:
        return "scheduler"
    return "web"


def mark_runtime_reachability(repo: CodeRepo, reachable: bool) -> None:
    runtime = current_runtime_label()
    if runtime == "scheduler":
        repo.reachable_in_scheduler = bool(reachable)
    else:
        repo.reachable_in_web = bool(reachable)

