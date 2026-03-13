"""Repo scanner: indexes files, detects languages and frameworks."""
from __future__ import annotations

import json
import logging
import os
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from ...config import settings
from ...models.code_brain import CodeRepo, CodeSnapshot

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

    repo_path = Path(repo.path)
    if not repo_path.is_dir():
        return {"error": f"Path not found: {repo.path}"}

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
            "name": r.name,
            "file_count": r.file_count,
            "total_lines": r.total_lines,
            "language_stats": json.loads(r.language_stats) if r.language_stats else {},
            "framework_tags": r.framework_tags.split(",") if r.framework_tags else [],
            "last_indexed": r.last_indexed.isoformat() if r.last_indexed else None,
        })
    return result


def register_repo(db: Session, path: str, name: Optional[str] = None, user_id: Optional[int] = None) -> Dict:
    """Register a new repository for Code Brain indexing."""
    p = Path(path).resolve()
    if not p.is_dir():
        return {"error": f"Directory not found: {path}"}

    existing = db.query(CodeRepo).filter(CodeRepo.path == str(p)).first()
    if existing:
        if not existing.active:
            existing.active = True
            db.commit()
            return {"id": existing.id, "name": existing.name, "reactivated": True}
        return {"error": "Repo already registered", "id": existing.id}

    repo = CodeRepo(
        path=str(p),
        name=name or p.name,
        user_id=user_id,
    )
    db.add(repo)
    db.commit()
    db.refresh(repo)
    return {"id": repo.id, "name": repo.name, "path": str(p)}


def unregister_repo(db: Session, repo_id: int) -> Dict:
    repo = db.query(CodeRepo).filter(CodeRepo.id == repo_id).first()
    if not repo:
        return {"error": "Repo not found"}
    repo.active = False
    db.commit()
    return {"ok": True, "id": repo_id}
