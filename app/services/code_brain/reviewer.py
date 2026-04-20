"""Smart diff reviewer: auto-review recent git commits with LLM."""
from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from ...models.code_brain import CodeInsight, CodeRepo, CodeReview
from .runtime import resolve_repo_runtime_path

logger = logging.getLogger(__name__)

_MAX_DIFF_CHARS = 12_000


def _run_git(repo_path: str, args: List[str], max_bytes: int = 50_000) -> str:
    try:
        result = subprocess.run(
            ["git"] + args,
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
        out = result.stdout or ""
        return out[:max_bytes]
    except Exception as e:
        logger.warning("[reviewer] git error: %s", e)
        return ""


def _get_recent_commits(repo_path: str, since_hash: Optional[str], max_commits: int = 10) -> List[Dict[str, str]]:
    """Return recent commits since the given hash."""
    range_spec = f"{since_hash}..HEAD" if since_hash else f"-{max_commits}"
    if since_hash:
        raw = _run_git(repo_path, ["log", range_spec, "--pretty=format:%H|%an|%s", f"-{max_commits}"])
    else:
        raw = _run_git(repo_path, ["log", f"-{max_commits}", "--pretty=format:%H|%an|%s"])

    commits = []
    for line in raw.strip().splitlines():
        parts = line.split("|", 2)
        if len(parts) >= 3:
            commits.append({"hash": parts[0], "author": parts[1], "message": parts[2]})
    return commits


def _get_commit_diff(repo_path: str, commit_hash: str) -> str:
    diff = _run_git(repo_path, ["show", "--stat", "--patch", "--no-color", commit_hash], max_bytes=_MAX_DIFF_CHARS)
    return diff


def _review_diff_with_llm(diff_text: str, repo_context: str, commit_info: Dict[str, str]) -> Dict[str, Any]:
    """Send diff to LLM for code review. Returns structured findings."""
    from ..llm_caller import call_llm

    system_prompt = (
        "You are a senior code reviewer. Analyze the following git diff and provide:\n"
        "1. A one-sentence summary of what the commit does.\n"
        "2. A JSON array of findings, each with: severity (info/warn/critical), "
        "category (bug/anti-pattern/style/error-handling/test-coverage/performance), "
        "message (concise description), file (affected file path).\n"
        "3. An overall quality score from 1-10.\n\n"
        "Format your response EXACTLY as:\n"
        "SUMMARY: <one sentence>\n"
        "FINDINGS: [<json array>]\n"
        "SCORE: <number>\n\n"
        f"Repository context:\n{repo_context}\n"
    )

    user_msg = f"Commit: {commit_info.get('hash', '')[:8]} by {commit_info.get('author', 'unknown')}\n"
    user_msg += f"Message: {commit_info.get('message', '')}\n\n"
    user_msg += f"Diff:\n{diff_text[:_MAX_DIFF_CHARS]}"

    text = call_llm(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg},
        ],
        max_tokens=800,
        trace_id="code-reviewer",
        cacheable=True,
    )
    if not text:
        return {"summary": "Review failed", "findings": [], "score": 5.0}

    summary = ""
    findings = []
    score = 5.0

    for line in text.splitlines():
        line = line.strip()
        if line.upper().startswith("SUMMARY:"):
            summary = line[len("SUMMARY:"):].strip()
        elif line.upper().startswith("FINDINGS:"):
            raw_json = line[len("FINDINGS:"):].strip()
            try:
                findings = json.loads(raw_json)
                if not isinstance(findings, list):
                    findings = []
            except Exception:
                findings = []
        elif line.upper().startswith("SCORE:"):
            try:
                score = float(line[len("SCORE:"):].strip())
                score = max(1.0, min(10.0, score))
            except Exception:
                score = 5.0

    return {"summary": summary, "findings": findings, "score": score}


def review_recent_commits(db: Session, repo_id: int, user_id: Optional[int] = None) -> Dict[str, Any]:
    """Review new commits since last review and store findings."""
    repo = db.query(CodeRepo).filter(CodeRepo.id == repo_id).first()
    if not repo:
        return {"error": "Repo not found"}

    runtime_path = resolve_repo_runtime_path(repo)
    if runtime_path is None or not runtime_path.is_dir():
        return {"error": "Registered workspace is not reachable from the current runtime."}
    repo_path = str(runtime_path)

    commits = _get_recent_commits(repo_path, repo.last_commit_hash, max_commits=5)
    if not commits:
        return {"reviewed": 0}

    already_reviewed = set()
    for c in commits:
        existing = db.query(CodeReview).filter(CodeReview.commit_hash == c["hash"]).first()
        if existing:
            already_reviewed.add(c["hash"])

    to_review = [c for c in commits if c["hash"] not in already_reviewed]
    if not to_review:
        return {"reviewed": 0, "skipped": len(already_reviewed)}

    # Build repo context for the reviewer
    insights = (
        db.query(CodeInsight)
        .filter(CodeInsight.repo_id == repo_id, CodeInsight.active.is_(True))
        .limit(10)
        .all()
    )
    context_parts = [f"Repo: {repo.name}"]
    if repo.framework_tags:
        context_parts.append(f"Frameworks: {repo.framework_tags}")
    for ins in insights[:5]:
        context_parts.append(f"- {ins.category}: {ins.description}")
    repo_context = "\n".join(context_parts)

    reviewed_count = 0
    for commit in to_review[:3]:
        diff = _get_commit_diff(repo_path, commit["hash"])
        if not diff.strip():
            continue

        result = _review_diff_with_llm(diff, repo_context, commit)
        review = CodeReview(
            repo_id=repo_id,
            user_id=user_id,
            commit_hash=commit["hash"],
            author=commit.get("author"),
            summary=result.get("summary", ""),
            findings_json=json.dumps(result.get("findings", [])),
            overall_score=result.get("score", 5.0),
        )
        db.add(review)
        reviewed_count += 1

    if commits:
        repo.last_commit_hash = commits[0]["hash"]

    db.commit()
    return {"reviewed": reviewed_count, "skipped": len(already_reviewed)}


def get_recent_reviews(db: Session, repo_id: Optional[int] = None, limit: int = 20) -> List[Dict[str, Any]]:
    q = db.query(CodeReview).order_by(CodeReview.reviewed_at.desc())
    if repo_id is not None:
        q = q.filter(CodeReview.repo_id == repo_id)
    rows = q.limit(limit).all()
    return [
        {
            "id": r.id,
            "repo_id": r.repo_id,
            "commit_hash": r.commit_hash,
            "author": r.author,
            "summary": r.summary,
            "findings": json.loads(r.findings_json) if r.findings_json else [],
            "overall_score": r.overall_score,
            "reviewed_at": r.reviewed_at.isoformat() if r.reviewed_at else None,
        }
        for r in rows
    ]
