"""Git history mining: churn, hotspots, commit frequency, contributor analysis."""
from __future__ import annotations

import logging
import subprocess
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from ...models.code_brain import CodeHotspot, CodeRepo, CodeSnapshot

logger = logging.getLogger(__name__)

_GIT_LOG_FORMAT = "--format=%H|%ai|%an|%s"
_MAX_COMMITS = 2000


def _run_git(repo_path: str, args: List[str], max_lines: int = 10000) -> List[str]:
    try:
        result = subprocess.run(
            ["git", "-C", repo_path] + args,
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            logger.debug("git %s failed: %s", " ".join(args[:3]), result.stderr[:200])
            return []
        return result.stdout.strip().splitlines()[:max_lines]
    except Exception as e:
        logger.debug("git command error: %s", e)
        return []


def _get_head_hash(repo_path: str) -> Optional[str]:
    lines = _run_git(repo_path, ["rev-parse", "HEAD"])
    return lines[0].strip() if lines else None


def mine_git_history(db: Session, repo_id: int, max_commits: int = _MAX_COMMITS) -> Dict:
    """Mine git log to compute file churn, hotspots, and commit stats."""
    repo = db.query(CodeRepo).filter(CodeRepo.id == repo_id).first()
    if not repo:
        return {"error": "Repo not found"}

    repo_path = repo.path
    if not Path(repo_path).is_dir():
        return {"error": f"Path not found: {repo_path}"}

    head_hash = _get_head_hash(repo_path)
    repo.last_commit_hash = head_hash

    log_lines = _run_git(repo_path, [
        "log", _GIT_LOG_FORMAT, f"-n{max_commits}", "--name-only",
    ])

    file_commits: Counter = Counter()
    file_last_date: Dict[str, datetime] = {}
    commit_count = 0
    contributors: Counter = Counter()
    current_date: Optional[datetime] = None
    current_author: Optional[str] = None

    for line in log_lines:
        if not line.strip():
            continue
        if "|" in line and line.count("|") >= 3:
            parts = line.split("|", 3)
            try:
                current_date = datetime.fromisoformat(parts[1].strip()[:19])
            except Exception:
                current_date = None
            current_author = parts[2].strip()
            if current_author:
                contributors[current_author] += 1
            commit_count += 1
        else:
            fname = line.strip()
            if fname:
                file_commits[fname] += 1
                if current_date and (fname not in file_last_date or current_date > file_last_date[fname]):
                    file_last_date[fname] = current_date

    db.query(CodeHotspot).filter(CodeHotspot.repo_id == repo_id).delete()

    snapshots = {s.file_path: s for s in
                 db.query(CodeSnapshot).filter(CodeSnapshot.repo_id == repo_id).all()}

    max_churn = max(file_commits.values()) if file_commits else 1
    hotspots: List[CodeHotspot] = []

    for fname, count in file_commits.most_common(200):
        churn_norm = count / max_churn
        snap = snapshots.get(fname)
        complexity = snap.complexity_score if snap else 0.0
        complexity_norm = min(complexity / 100.0, 1.0)
        combined = churn_norm * 0.6 + complexity_norm * 0.4

        hotspots.append(CodeHotspot(
            repo_id=repo_id,
            file_path=fname,
            churn_score=round(churn_norm, 4),
            complexity_score=round(complexity_norm, 4),
            combined_score=round(combined, 4),
            commit_count=count,
            last_commit_date=file_last_date.get(fname),
        ))

    db.bulk_save_objects(hotspots)
    db.commit()

    return {
        "commit_count": commit_count,
        "files_with_commits": len(file_commits),
        "top_contributors": dict(contributors.most_common(10)),
        "head_hash": head_hash,
        "hotspot_count": len(hotspots),
    }
