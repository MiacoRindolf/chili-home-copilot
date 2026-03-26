"""Resolve allowed repo roots from settings; validate cwd for validation runs (fail closed)."""
from __future__ import annotations

import os
from pathlib import Path

from ...config import settings


def list_code_repo_roots() -> list[Path]:
    raw = (getattr(settings, "code_brain_repos", "") or "").strip()
    if not raw:
        return []
    roots: list[Path] = []
    for part in raw.split(","):
        p = Path(part.strip()).resolve()
        if p.is_dir():
            roots.append(p)
    return roots


def resolve_task_cwd(repo_index: int, sub_path: str) -> Path:
    """
    Return absolute working directory for a task. sub_path is relative to the chosen repo root;
    must not escape the root (no .. components).
    """
    roots = list_code_repo_roots()
    if not roots:
        raise ValueError("No code_brain_repos configured; cannot resolve task repo cwd.")
    if repo_index < 0 or repo_index >= len(roots):
        raise ValueError("Invalid repo_index for configured code_brain_repos.")
    root = roots[repo_index]
    rel = (sub_path or "").strip().replace("\\", "/").strip("/")
    if not rel:
        cwd = root
    else:
        if ".." in Path(rel).parts:
            raise ValueError("sub_path must not contain '..'")
        cwd = (root / rel).resolve()
    try:
        root_res = root.resolve()
        cwd.relative_to(root_res)
    except ValueError as e:
        raise ValueError("Resolved cwd escapes allowed repo root.") from e
    if not cwd.is_dir():
        raise ValueError("Resolved cwd is not a directory.")
    return cwd


def truncate_text(s: str, max_bytes: int = 100_000) -> tuple[str, int]:
    raw = s.encode("utf-8", errors="replace")
    if len(raw) <= max_bytes:
        return s, len(raw)
    cut = raw[: max_bytes - 20].decode("utf-8", errors="ignore")
    return cut + "\n…[truncated]", max_bytes


def subprocess_safe_env() -> dict[str, str]:
    """Minimal environment for validator subprocesses (no user-controlled env maps in Phase 1)."""
    keep = {
        "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "TEMP", "TMP",
        "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
    }
    out: dict[str, str] = {}
    for k, v in os.environ.items():
        if k.upper() in keep:
            out[k] = v
    return out
