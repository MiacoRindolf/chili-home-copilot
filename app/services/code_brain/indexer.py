"""Repo scanner: indexes files, detects languages and frameworks."""
from __future__ import annotations

import json
import logging
import os
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from sqlalchemy import or_
from sqlalchemy.orm import Session

from ...config import settings
from ...models.code_brain import CodeRepo, CodeSnapshot
from .runtime import (
    current_runtime_reachable,
    infer_repo_runtime_fields,
    mark_runtime_reachability,
    resolve_repo_runtime_path,
)

logger = logging.getLogger(__name__)

LANG_EXTENSIONS: Dict[str, str] = {
    ".py": "python", ".pyx": "python", ".pyi": "python",
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript", ".jsx": "javascript",
    ".java": "java", ".kt": "kotlin", ".scala": "scala",
    ".go": "go", ".rs": "rust", ".c": "c", ".cpp": "cpp", ".h": "c",
    ".cs": "csharp", ".rb": "ruby", ".php": "php", ".swift": "swift",
    ".html": "html", ".css": "css", ".scss": "scss", ".less": "less",
    ".sql": "sql", ".sh": "shell", ".bash": "shell", ".ps1": "powershell",
    ".json": "json", ".yaml": "yaml", ".yml": "yaml", ".toml": "toml",
    ".xml": "xml", ".md": "markdown", ".rst": "rst",
    ".r": "r", ".R": "r", ".jl": "julia", ".lua": "lua",
    ".dart": "dart", ".vue": "vue", ".svelte": "svelte",
}

FRAMEWORK_MARKERS: Dict[str, List[str]] = {
    "fastapi": ["fastapi"],
    "django": ["django"],
    "flask": ["flask"],
    "express": ["express"],
    "react": ["react", "react-dom"],
    "nextjs": ["next"],
    "vue": ["vue"],
    "svelte": ["svelte"],
    "angular": ["@angular/core"],
    "sqlalchemy": ["sqlalchemy"],
    "prisma": ["prisma"],
    "pytorch": ["torch"],
    "tensorflow": ["tensorflow"],
    "spring": ["spring-boot"],
}

SKIP_DIRS = {
    ".git", "__pycache__", "node_modules", ".venv", "venv", "env",
    ".mypy_cache", ".pytest_cache", ".tox", "dist", "build",
    ".next", ".nuxt", ".svelte-kit", "target", "out", ".idea",
    ".vscode", ".cursor", "vendor", "coverage", ".turbo",
}

SKIP_EXTENSIONS = {
    ".pyc", ".pyo", ".class", ".o", ".obj", ".so", ".dll", ".dylib",
    ".exe", ".bin", ".whl", ".egg", ".tar", ".gz", ".zip", ".7z",
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".webp", ".bmp",
    ".mp3", ".mp4", ".wav", ".avi", ".mov", ".pdf", ".doc", ".docx",
    ".lock", ".map",
}


def _should_skip(path: Path) -> bool:
    return any(part in SKIP_DIRS for part in path.parts)


def _detect_language(path: Path) -> Optional[str]:
    return LANG_EXTENSIONS.get(path.suffix.lower())


def _detect_frameworks(repo_path: Path) -> List[str]:
    """Detect frameworks by scanning dependency files."""
    detected = []
    dep_files = {
        "requirements.txt": "text",
        "pyproject.toml": "toml",
        "setup.py": "text",
        "package.json": "json",
        "Cargo.toml": "toml",
        "go.mod": "text",
        "Gemfile": "text",
    }

    for fname, fmt in dep_files.items():
        fp = repo_path / fname
        if not fp.exists():
            continue
        try:
            content = fp.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        content_lower = content.lower()
        for framework, markers in FRAMEWORK_MARKERS.items():
            for marker in markers:
                if marker.lower() in content_lower:
                    if framework not in detected:
                        detected.append(framework)
    return detected


def _count_lines(path: Path) -> int:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return sum(1 for _ in f)
    except Exception:
        return 0


def scan_repo(db: Session, repo_id: int, max_files: int = 0) -> Dict:
    """Index all source files in a repo. Returns summary stats."""
    repo = db.query(CodeRepo).filter(CodeRepo.id == repo_id).first()
    if not repo:
        return {"error": "Repo not found"}

    repo_path = resolve_repo_runtime_path(repo)
    if repo_path is None or not repo_path.is_dir():
        repo.last_index_error = (
            "Registered workspace is not reachable from the current runtime. "
            "Check host/container path mapping before indexing."
        )
        repo.file_count = 0
        repo.total_lines = 0
        repo.last_indexed = None
        mark_runtime_reachability(repo, False)
        db.query(CodeSnapshot).filter(CodeSnapshot.repo_id == repo_id).delete()
        db.commit()
        return {"error": repo.last_index_error}

    cap = max_files or settings.code_brain_max_files
    lang_counter: Counter = Counter()
    total_lines = 0
    file_count = 0
    snapshots: List[CodeSnapshot] = []

    db.query(CodeSnapshot).filter(CodeSnapshot.repo_id == repo_id).delete()

    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for fname in files:
            if file_count >= cap:
                break
            fpath = Path(root) / fname
            if fpath.suffix.lower() in SKIP_EXTENSIONS:
                continue
            if _should_skip(fpath.relative_to(repo_path)):
                continue

            lang = _detect_language(fpath)
            if lang:
                lang_counter[lang] += 1

            lines = _count_lines(fpath)
            total_lines += lines
            file_count += 1

            try:
                mtime = datetime.fromtimestamp(fpath.stat().st_mtime)
            except Exception:
                mtime = None

            rel = str(fpath.relative_to(repo_path)).replace("\\", "/")
            snapshots.append(CodeSnapshot(
                repo_id=repo_id,
                file_path=rel,
                language=lang,
                line_count=lines,
                last_modified=mtime,
            ))
        if file_count >= cap:
            break

    db.bulk_save_objects(snapshots)

    frameworks = _detect_frameworks(repo_path)
    repo.language_stats = json.dumps(dict(lang_counter))
    repo.framework_tags = ",".join(frameworks) if frameworks else None
    repo.file_count = file_count
    repo.total_lines = total_lines
    repo.last_indexed = datetime.utcnow()
    repo.last_index_error = None
    repo.last_successful_indexed_at = repo.last_indexed
    repo.last_successful_file_count = file_count
    mark_runtime_reachability(repo, True)
    db.commit()

    return {
        "file_count": file_count,
        "total_lines": total_lines,
        "languages": dict(lang_counter),
        "frameworks": frameworks,
    }


def get_registered_repos(db: Session, user_id: Optional[int] = None) -> List[Dict]:
    q = db.query(CodeRepo).filter(CodeRepo.active.is_(True))
    if user_id is not None:
        q = q.filter(CodeRepo.user_id == user_id)
    repos = q.all()
    result = []
    for r in repos:
        result.append({
            "id": r.id,
            "path": r.path,
            "host_path": r.host_path,
            "container_path": r.container_path,
            "name": r.name,
            "file_count": r.file_count,
            "total_lines": r.total_lines,
            "language_stats": json.loads(r.language_stats) if r.language_stats else {},
            "framework_tags": r.framework_tags.split(",") if r.framework_tags else [],
            "last_indexed": r.last_indexed.isoformat() if r.last_indexed else None,
            "last_index_error": r.last_index_error,
            "last_successful_indexed_at": (
                r.last_successful_indexed_at.isoformat() if r.last_successful_indexed_at else None
            ),
            "last_successful_file_count": r.last_successful_file_count,
            "reachable_in_web": bool(r.reachable_in_web),
            "reachable_in_scheduler": bool(r.reachable_in_scheduler),
            "reachable_in_current_runtime": current_runtime_reachable(r),
        })
    return result


def register_repo(db: Session, path: str, name: Optional[str] = None, user_id: Optional[int] = None) -> Dict:
    """Register a new repository for Code Brain indexing."""
    p = Path(path).resolve()
    if not p.is_dir():
        return {"error": f"Directory not found: {path}"}

    runtime_fields = infer_repo_runtime_fields(p)
    filters = [CodeRepo.path == str(p), CodeRepo.host_path == str(p)]
    if runtime_fields.get("container_path"):
        filters.append(CodeRepo.container_path == runtime_fields.get("container_path"))
    existing = db.query(CodeRepo).filter(or_(*filters)).first()
    if existing:
        existing.host_path = runtime_fields.get("host_path")
        existing.container_path = runtime_fields.get("container_path")
        mark_runtime_reachability(existing, True)
        if not existing.active:
            existing.active = True
            db.commit()
            return {"id": existing.id, "name": existing.name, "reactivated": True}
        db.commit()
        return {"error": "Repo already registered", "id": existing.id}

    repo = CodeRepo(
        path=str(p),
        host_path=runtime_fields.get("host_path"),
        container_path=runtime_fields.get("container_path"),
        name=name or p.name,
        user_id=user_id,
    )
    mark_runtime_reachability(repo, True)
    db.add(repo)
    db.commit()
    db.refresh(repo)
    return {
        "id": repo.id,
        "name": repo.name,
        "path": str(p),
        "host_path": repo.host_path,
        "container_path": repo.container_path,
    }


def unregister_repo(db: Session, repo_id: int) -> Dict:
    repo = db.query(CodeRepo).filter(CodeRepo.id == repo_id).first()
    if not repo:
        return {"error": "Repo not found"}
    repo.active = False
    db.commit()
    return {"ok": True, "id": repo_id}
