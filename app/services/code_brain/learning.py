"""Code Brain learning cycle orchestrator."""
from __future__ import annotations

import logging
import time
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session
from sqlalchemy import func

from ...config import settings
from ...models.code_brain import (
    CodeHotspot, CodeInsight, CodeLearningEvent, CodeRepo, CodeSnapshot,
)
from . import indexer, analyzer, git_miner, insights as insights_mod

logger = logging.getLogger(__name__)

_learning_status: Dict[str, Any] = {
    "running": False,
    "last_run": None,
    "phase": "idle",
    "steps_completed": 0,
    "total_steps": 4,
    "current_step": "",
    "repos_processed": 0,
    "insights_found": 0,
    "started_at": None,
    "step_timings": {},
    "error": None,
}


def get_code_learning_status() -> Dict[str, Any]:
    status = dict(_learning_status)
    if status.get("running") and status.get("started_at"):
        try:
            started = datetime.fromisoformat(status["started_at"])
            status["elapsed_seconds"] = int((datetime.utcnow() - started).total_seconds())
        except Exception:
            pass
    return status


def _log_event(db: Session, repo_id: Optional[int], event_type: str, description: str, user_id: Optional[int] = None):
    ev = CodeLearningEvent(
        user_id=user_id,
        repo_id=repo_id,
        event_type=event_type,
        description=description,
    )
    db.add(ev)
    try:
        db.commit()
    except Exception:
        db.rollback()


def run_code_learning_cycle(db: Session, user_id: Optional[int] = None) -> Dict[str, Any]:
    """Full code brain learning cycle: index -> analyze -> mine git -> discover insights."""
    if _learning_status["running"]:
        return {"ok": False, "reason": "Code learning cycle already in progress"}

    _learning_status["running"] = True
    _learning_status["phase"] = "starting"
    _learning_status["steps_completed"] = 0
    _learning_status["total_steps"] = 4
    _learning_status["repos_processed"] = 0
    _learning_status["insights_found"] = 0
    _learning_status["started_at"] = datetime.utcnow().isoformat()
    _learning_status["step_timings"] = {}
    _learning_status["error"] = None

    start = time.time()
    report: Dict[str, Any] = {}

    try:
        repos = db.query(CodeRepo).filter(CodeRepo.active.is_(True)).all()

        if not repos and settings.code_brain_repos:
            for rp in settings.code_brain_repos.split(","):
                rp = rp.strip()
                if rp:
                    result = indexer.register_repo(db, rp, user_id=user_id)
                    logger.info("[code-brain] Auto-registered repo: %s -> %s", rp, result)
            repos = db.query(CodeRepo).filter(CodeRepo.active.is_(True)).all()

        if not repos:
            _learning_status["running"] = False
            _learning_status["phase"] = "idle"
            _learning_status["error"] = "No repos registered"
            return {"ok": False, "reason": "No repos registered. Add repos via Brain UI or CODE_BRAIN_REPOS in .env"}

        report["repo_count"] = len(repos)
        total_insights = 0

        for i, repo in enumerate(repos):
            logger.info("[code-brain] Processing repo %d/%d: %s", i + 1, len(repos), repo.name)

            # Step 1: Index
            _learning_status["phase"] = "indexing"
            _learning_status["current_step"] = f"Indexing {repo.name}"
            t0 = time.time()
            idx_result = indexer.scan_repo(db, repo.id)
            _learning_status["step_timings"][f"index_{repo.name}"] = round(time.time() - t0, 1)
            logger.info("[code-brain] Indexed %s: %s", repo.name, idx_result)

            # Step 2: Analyze
            _learning_status["phase"] = "analyzing"
            _learning_status["current_step"] = f"Analyzing {repo.name}"
            t0 = time.time()
            analysis = analyzer.analyze_repo_files(db, repo.id)
            _learning_status["step_timings"][f"analyze_{repo.name}"] = round(time.time() - t0, 1)
            logger.info("[code-brain] Analyzed %s: %s", repo.name, analysis)

            # Step 3: Mine git
            _learning_status["phase"] = "mining_git"
            _learning_status["current_step"] = f"Mining git history for {repo.name}"
            t0 = time.time()
            git_result = git_miner.mine_git_history(db, repo.id)
            _learning_status["step_timings"][f"git_{repo.name}"] = round(time.time() - t0, 1)
            logger.info("[code-brain] Git mined %s: %s", repo.name, git_result)

            # Step 4: Discover insights
            _learning_status["phase"] = "discovering"
            _learning_status["current_step"] = f"Discovering patterns in {repo.name}"
            t0 = time.time()
            insight_result = insights_mod.mine_insights(db, repo.id, user_id=user_id)
            total_insights += insight_result.get("discovered", 0)
            _learning_status["step_timings"][f"insights_{repo.name}"] = round(time.time() - t0, 1)
            logger.info("[code-brain] Insights for %s: %s", repo.name, insight_result)

            _learning_status["repos_processed"] = i + 1
            _log_event(db, repo.id, "index", f"Completed learning cycle for {repo.name}: {idx_result.get('file_count', 0)} files, {insight_result.get('discovered', 0)} insights", user_id)

        _learning_status["insights_found"] = total_insights
        _learning_status["steps_completed"] = 4

        elapsed = round(time.time() - start, 1)
        report["elapsed_seconds"] = elapsed
        report["insights_discovered"] = total_insights
        report["ok"] = True

        _log_event(db, None, "cycle", f"Code learning cycle completed in {elapsed}s: {len(repos)} repos, {total_insights} insights", user_id)

    except Exception as e:
        logger.exception("[code-brain] Learning cycle error: %s", e)
        _learning_status["error"] = str(e)
        report["ok"] = False
        report["error"] = str(e)
        _log_event(db, None, "error", f"Code learning cycle error: {e}", user_id)
    finally:
        _learning_status["running"] = False
        _learning_status["phase"] = "idle"
        _learning_status["last_run"] = datetime.utcnow().isoformat()
        _learning_status["current_step"] = ""

    return report


def get_code_brain_metrics(db: Session, user_id: Optional[int] = None) -> Dict[str, Any]:
    """Aggregate metrics for the Code Brain dashboard."""
    repos = db.query(CodeRepo).filter(CodeRepo.active.is_(True)).all()
    total_files = sum(r.file_count for r in repos)
    total_lines = sum(r.total_lines for r in repos)

    insight_count = db.query(func.count(CodeInsight.id)).filter(CodeInsight.active.is_(True)).scalar() or 0
    hotspot_count = db.query(func.count(CodeHotspot.id)).scalar() or 0

    avg_complexity = (
        db.query(func.avg(CodeSnapshot.complexity_score))
        .filter(CodeSnapshot.complexity_score > 0)
        .scalar()
    ) or 0.0

    lang_totals: Dict[str, int] = {}
    for r in repos:
        if r.language_stats:
            try:
                stats = __import__("json").loads(r.language_stats)
                for lang, count in stats.items():
                    lang_totals[lang] = lang_totals.get(lang, 0) + count
            except Exception:
                pass

    top_hotspots = (
        db.query(CodeHotspot)
        .order_by(CodeHotspot.combined_score.desc())
        .limit(10)
        .all()
    )

    recent_events = (
        db.query(CodeLearningEvent)
        .order_by(CodeLearningEvent.created_at.desc())
        .limit(20)
        .all()
    )

    return {
        "repos": len(repos),
        "total_files": total_files,
        "total_lines": total_lines,
        "insight_count": insight_count,
        "hotspot_count": hotspot_count,
        "avg_complexity": round(avg_complexity, 2),
        "languages": lang_totals,
        "top_hotspots": [
            {
                "file": h.file_path,
                "churn": round(h.churn_score, 3),
                "complexity": round(h.complexity_score, 3),
                "combined": round(h.combined_score, 3),
                "commits": h.commit_count,
            }
            for h in top_hotspots
        ],
        "recent_events": [
            {
                "type": e.event_type,
                "description": e.description,
                "created_at": e.created_at.isoformat() if e.created_at else None,
            }
            for e in recent_events
        ],
        "learning_status": get_code_learning_status(),
    }


def get_code_chat_context(db: Session, user_id: Optional[int] = None) -> str:
    """Return a short natural-language summary of the Code Brain state.

    Designed to be appended to the LLM system prompt so Chili has persistent
    knowledge of the user's codebases without re-scanning on every request.
    """
    repos = (
        db.query(CodeRepo)
        .filter(CodeRepo.active.is_(True))
        .order_by(CodeRepo.name.asc())
        .all()
    )
    if not repos:
        return ""

    metrics = get_code_brain_metrics(db, user_id=user_id)
    parts: list[str] = []

    # Repos overview
    repo_names = [r.name or (r.path.split("/")[-1] or r.path) for r in repos]
    parts.append(
        "Code Brain currently tracks the following local repositories: "
        + ", ".join(sorted(set(repo_names)))
        + "."
    )

    total_files = metrics.get("total_files") or 0
    total_lines = metrics.get("total_lines") or 0
    avg_complexity = metrics.get("avg_complexity") or 0.0
    insight_count = metrics.get("insight_count") or 0
    hotspot_count = metrics.get("hotspot_count") or 0

    parts.append(
        f"In total it has indexed approximately {total_files:,} files and {total_lines:,} lines of code "
        f"with an average complexity score of {avg_complexity:.2f}."
    )

    # Languages summary
    lang_totals: Dict[str, int] = metrics.get("languages") or {}
    if lang_totals:
        top_langs = sorted(lang_totals.items(), key=lambda kv: kv[1], reverse=True)[:3]
        lang_bits = [f"{name} (~{count} files)" for name, count in top_langs]
        parts.append("Primary languages detected: " + ", ".join(lang_bits) + ".")

    if insight_count:
        parts.append(
            f"Code Brain has discovered {insight_count} active coding conventions and patterns "
            "across these repositories."
        )

    if hotspot_count:
        parts.append(
            f"It currently tracks {hotspot_count} hotspot file(s) with high churn and complexity; "
            "be especially careful and incremental when modifying these areas."
        )

    # A small sample of hotspot files to bias the model toward known tricky areas.
    top_hotspots = (metrics.get("top_hotspots") or [])[:5]
    files = [h.get("file") for h in top_hotspots if h.get("file")]
    if files:
        parts.append(
            "Notable hotspot files include: "
            + ", ".join(files)
            + ". Prefer small, well-tested changes when working here."
        )

    return "\n".join(parts)
