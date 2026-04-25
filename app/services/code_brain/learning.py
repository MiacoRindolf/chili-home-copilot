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
    CodeDepAlert, CodeDependency, CodeHotspot, CodeInsight,
    CodeLearningEvent, CodeQualitySnapshot, CodeRepo, CodeReview, CodeSearchEntry, CodeSnapshot,
)
from ..project_domain_runs import finish_run, record_completed_run, start_run
from . import indexer, analyzer, git_miner, insights as insights_mod
from . import graph as graph_mod, trends as trends_mod, reviewer as reviewer_mod
from . import deps_scanner as deps_mod, search as search_mod
from .events import learning_event_visibility_clause, log_learning_event
from .runtime import mark_runtime_reachability, resolve_repo_runtime_path

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


def _log_event(
    db: Session,
    repo_id: Optional[int],
    event_type: str,
    description: str,
    user_id: Optional[int] = None,
    *,
    repo: CodeRepo | None = None,
    repos: list[CodeRepo] | None = None,
):
    log_learning_event(
        db,
        repo_id=repo_id,
        event_type=event_type,
        description=description,
        explicit_user_id=user_id,
        repo=repo,
        repos=repos,
    )


def _repo_has_live_index(repo: CodeRepo | None) -> bool:
    return bool(
        repo
        and not repo.last_index_error
        and resolve_repo_runtime_path(repo) is not None
        and (
            repo.last_indexed
            or repo.last_successful_indexed_at
            or (repo.last_successful_file_count or repo.file_count or 0) > 0
        )
    )


def _fresh_metric_repo_ids(repos: list[CodeRepo]) -> list[int]:
    return [int(repo.id) for repo in repos if _repo_has_live_index(repo)]


def _first_live_repo(repos: list[CodeRepo]) -> CodeRepo | None:
    for repo in repos:
        if _repo_has_live_index(repo):
            return repo
    return None


def _mark_repo_stale(db: Session, repo: CodeRepo, reason: str) -> None:
    repo.last_index_error = reason[:4000]
    repo.language_stats = None
    repo.framework_tags = None
    repo.file_count = 0
    repo.total_lines = 0
    repo.last_indexed = None
    db.query(CodeSnapshot).filter(CodeSnapshot.repo_id == repo.id).delete()
    db.query(CodeHotspot).filter(CodeHotspot.repo_id == repo.id).delete()
    db.query(CodeDependency).filter(CodeDependency.repo_id == repo.id).delete()
    db.query(CodeSearchEntry).filter(CodeSearchEntry.repo_id == repo.id).delete()
    db.query(CodeDepAlert).filter(CodeDepAlert.repo_id == repo.id).delete()
    db.query(CodeReview).filter(CodeReview.repo_id == repo.id).delete()
    db.query(CodeQualitySnapshot).filter(CodeQualitySnapshot.repo_id == repo.id).delete()
    db.query(CodeInsight).filter(CodeInsight.repo_id == repo.id).update({"active": False})
    db.flush()


def run_code_learning_cycle(db: Session, user_id: Optional[int] = None) -> Dict[str, Any]:
    """Full code brain learning cycle: index -> analyze -> mine git -> discover insights."""
    if _learning_status["running"]:
        return {"ok": False, "reason": "Code learning cycle already in progress"}

    _learning_status["running"] = True
    _learning_status["phase"] = "starting"
    _learning_status["steps_completed"] = 0
    _learning_status["total_steps"] = 8
    _learning_status["repos_processed"] = 0
    _learning_status["insights_found"] = 0
    _learning_status["started_at"] = datetime.utcnow().isoformat()
    _learning_status["step_timings"] = {}
    _learning_status["error"] = None

    start = time.time()
    report: Dict[str, Any] = {}
    repos: list[CodeRepo] = []

    try:
        repos = indexer.get_accessible_repos(db, user_id=user_id, include_shared=True)

        if not repos and settings.code_brain_repos:
            for rp in settings.code_brain_repos.split(","):
                rp = rp.strip()
                if rp:
                    result = indexer.register_repo(db, rp, user_id=user_id)
                    logger.info("[code-brain] Auto-registered repo: %s -> %s", rp, result)
            repos = indexer.get_accessible_repos(db, user_id=user_id, include_shared=True)

        if not repos:
            _learning_status["running"] = False
            _learning_status["phase"] = "idle"
            _learning_status["error"] = "No repos registered"
            return {"ok": False, "reason": "No repos registered. Add repos via Brain UI or CODE_BRAIN_REPOS in .env"}

        # Deduplicate repos that resolve to the same runtime path in this process.
        unique_repos: list[CodeRepo] = []
        seen_runtime_paths: dict[str, int] = {}
        for repo in repos:
            runtime_path = resolve_repo_runtime_path(repo)
            if runtime_path is None:
                unique_repos.append(repo)
                continue
            key = str(runtime_path)
            if key in seen_runtime_paths:
                repo.last_index_error = (
                    f"Skipped duplicate runtime path; canonical repo id {seen_runtime_paths[key]} owns {key}."
                )
                mark_runtime_reachability(repo, True)
                db.flush()
                record_completed_run(
                    db,
                    "index",
                    status="skipped",
                    user_id=(user_id if user_id is not None else repo.user_id),
                    repo_id=repo.id,
                    title=f"Skipped duplicate repo {repo.name}",
                    detail={"duplicate_of_repo_id": seen_runtime_paths[key], "runtime_path": key},
                )
                db.commit()
                continue
            seen_runtime_paths[key] = repo.id
            unique_repos.append(repo)

        repos = unique_repos
        report["repo_count"] = len(repos)
        total_insights = 0

        for i, repo in enumerate(repos):
            logger.info("[code-brain] Processing repo %d/%d: %s", i + 1, len(repos), repo.name)
            runtime_path = resolve_repo_runtime_path(repo)
            if runtime_path is None:
                reason = (
                    "Registered workspace is not reachable from this runtime. "
                    "Indexing requires a shared host/container path mapping."
                )
                mark_runtime_reachability(repo, False)
                _mark_repo_stale(db, repo, reason)
                record_completed_run(
                    db,
                    "index",
                    status="failed",
                    user_id=(user_id if user_id is not None else repo.user_id),
                    repo_id=repo.id,
                    title=f"Index {repo.name}",
                    error_message=reason,
                )
                db.commit()
                _log_event(db, repo.id, "index_error", f"{repo.name}: {reason}", user_id, repo=repo)
                continue

            run = start_run(
                db,
                "index",
                user_id=(user_id if user_id is not None else repo.user_id),
                repo_id=repo.id,
                title=f"Index {repo.name}",
                detail={"runtime_path": str(runtime_path)},
            )
            db.commit()

            # Step 1: Index
            _learning_status["phase"] = "indexing"
            _learning_status["current_step"] = f"Indexing {repo.name}"
            t0 = time.time()
            idx_result = indexer.scan_repo(db, repo.id)
            _learning_status["step_timings"][f"index_{repo.name}"] = round(time.time() - t0, 1)
            logger.info("[code-brain] Indexed %s: %s", repo.name, idx_result)
            if idx_result.get("error"):
                reason = str(idx_result.get("error"))
                _mark_repo_stale(db, repo, reason)
                mark_runtime_reachability(repo, False)
                finish_run(
                    db,
                    run,
                    status="failed",
                    detail={"runtime_path": str(runtime_path), "error": reason},
                    error_message=reason,
                )
                db.commit()
                _log_event(db, repo.id, "index_error", f"{repo.name}: {reason}", user_id, repo=repo)
                continue

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

            # Step 4: Build dependency graph
            _learning_status["phase"] = "graphing"
            _learning_status["current_step"] = f"Building dependency graph for {repo.name}"
            t0 = time.time()
            try:
                graph_result = graph_mod.build_dependency_graph(db, repo.id)
                logger.info("[code-brain] Graph for %s: %s", repo.name, graph_result)
            except Exception as ge:
                logger.warning("[code-brain] Graph failed for %s: %s", repo.name, ge)
                graph_result = {}
            _learning_status["step_timings"][f"graph_{repo.name}"] = round(time.time() - t0, 1)

            # Step 5: Review recent diffs
            _learning_status["phase"] = "reviewing"
            _learning_status["current_step"] = f"Reviewing recent commits for {repo.name}"
            t0 = time.time()
            try:
                review_result = reviewer_mod.review_recent_commits(db, repo.id, user_id=user_id)
                logger.info("[code-brain] Reviews for %s: %s", repo.name, review_result)
            except Exception as re_:
                logger.warning("[code-brain] Review failed for %s: %s", repo.name, re_)
                review_result = {}
            _learning_status["step_timings"][f"review_{repo.name}"] = round(time.time() - t0, 1)

            # Step 6: Scan dependencies
            _learning_status["phase"] = "scanning_deps"
            _learning_status["current_step"] = f"Scanning dependencies for {repo.name}"
            t0 = time.time()
            try:
                deps_result = deps_mod.scan_dependencies(db, repo.id)
                logger.info("[code-brain] Deps for %s: %s", repo.name, deps_result)
            except Exception as de:
                logger.warning("[code-brain] Deps scan failed for %s: %s", repo.name, de)
                deps_result = {}
            _learning_status["step_timings"][f"deps_{repo.name}"] = round(time.time() - t0, 1)

            # Step 7: Record quality snapshot
            _learning_status["phase"] = "trends"
            _learning_status["current_step"] = f"Recording quality trends for {repo.name}"
            t0 = time.time()
            try:
                trends_result = trends_mod.record_quality_snapshot(db, repo.id)
                logger.info("[code-brain] Trends for %s: %s", repo.name, trends_result)
            except Exception as te:
                logger.warning("[code-brain] Trends failed for %s: %s", repo.name, te)
            _learning_status["step_timings"][f"trends_{repo.name}"] = round(time.time() - t0, 1)

            # Step 8: Discover insights
            _learning_status["phase"] = "discovering"
            _learning_status["current_step"] = f"Discovering patterns in {repo.name}"
            t0 = time.time()
            insight_result = insights_mod.mine_insights(db, repo.id, user_id=user_id)
            total_insights += insight_result.get("discovered", 0)
            _learning_status["step_timings"][f"insights_{repo.name}"] = round(time.time() - t0, 1)
            logger.info("[code-brain] Insights for %s: %s", repo.name, insight_result)

            # Index symbols for search (piggyback on the cycle)
            try:
                search_mod.index_symbols(db, repo.id)
            except Exception as se:
                logger.warning("[code-brain] Symbol indexing failed for %s: %s", repo.name, se)

            finish_run(
                db,
                run,
                status="completed",
                detail={
                    "runtime_path": str(runtime_path),
                    "file_count": idx_result.get("file_count", 0),
                    "insights": insight_result.get("discovered", 0),
                },
            )
            db.commit()
            _learning_status["repos_processed"] = i + 1
            _log_event(
                db,
                repo.id,
                "index",
                (
                    f"Completed learning cycle for {repo.name}: "
                    f"{idx_result.get('file_count', 0)} files, {insight_result.get('discovered', 0)} insights"
                ),
                user_id,
                repo=repo,
            )

        _learning_status["insights_found"] = total_insights
        _learning_status["steps_completed"] = 8

        elapsed = round(time.time() - start, 1)
        report["elapsed_seconds"] = elapsed
        report["insights_discovered"] = total_insights
        report["ok"] = True

        _log_event(
            db,
            None,
            "cycle",
            f"Code learning cycle completed in {elapsed}s: {len(repos)} repos, {total_insights} insights",
            user_id,
            repos=repos,
        )

    except Exception as e:
        logger.exception("[code-brain] Learning cycle error: %s", e)
        _learning_status["error"] = str(e)
        report["ok"] = False
        report["error"] = str(e)
        _log_event(
            db,
            None,
            "error",
            f"Code learning cycle error: {e}",
            user_id,
            repos=repos,
        )
    finally:
        _learning_status["running"] = False
        _learning_status["phase"] = "idle"
        _learning_status["last_run"] = datetime.utcnow().isoformat()
        _learning_status["current_step"] = ""

    return report


def get_code_brain_metrics(db: Session, user_id: Optional[int] = None) -> Dict[str, Any]:
    """Aggregate metrics for the Code Brain dashboard."""
    repos = indexer.get_accessible_repos(db, user_id=user_id, include_shared=True)
    repo_ids = _fresh_metric_repo_ids(repos)
    total_files = sum(r.file_count for r in repos)
    total_lines = sum(r.total_lines for r in repos)

    if repo_ids:
        insight_count = (
            db.query(func.count(CodeInsight.id))
            .filter(CodeInsight.active.is_(True), CodeInsight.repo_id.in_(repo_ids))
            .scalar()
            or 0
        )
        hotspot_count = (
            db.query(func.count(CodeHotspot.id))
            .filter(CodeHotspot.repo_id.in_(repo_ids))
            .scalar()
            or 0
        )
    else:
        insight_count = 0
        hotspot_count = 0

    avg_q = db.query(func.avg(CodeSnapshot.complexity_score)).filter(CodeSnapshot.complexity_score > 0)
    if repo_ids:
        avg_q = avg_q.filter(CodeSnapshot.repo_id.in_(repo_ids))
    else:
        avg_q = avg_q.filter(CodeSnapshot.repo_id == -1)
    avg_complexity = avg_q.scalar() or 0.0

    lang_totals: Dict[str, int] = {}
    for r in repos:
        if r.language_stats and int(r.id) in repo_ids:
            try:
                stats = __import__("json").loads(r.language_stats)
                for lang, count in stats.items():
                    lang_totals[lang] = lang_totals.get(lang, 0) + count
            except Exception:
                pass

    top_hotspots_q = db.query(CodeHotspot)
    if repo_ids:
        top_hotspots_q = top_hotspots_q.filter(CodeHotspot.repo_id.in_(repo_ids))
    else:
        top_hotspots_q = top_hotspots_q.filter(CodeHotspot.repo_id == -1)
    top_hotspots = top_hotspots_q.order_by(CodeHotspot.combined_score.desc()).limit(10).all()

    recent_events_q = db.query(CodeLearningEvent)
    visibility_clause = learning_event_visibility_clause(user_id=user_id, repo_ids=repo_ids)
    if visibility_clause is not None:
        recent_events_q = recent_events_q.filter(visibility_clause)
    if repo_ids:
        recent_events_q = recent_events_q.filter(
            (CodeLearningEvent.user_id == user_id)
            | (CodeLearningEvent.repo_id.is_(None))
            | (CodeLearningEvent.repo_id.in_(repo_ids))
        )
    else:
        recent_events_q = recent_events_q.filter(
            (CodeLearningEvent.user_id == user_id) | (CodeLearningEvent.repo_id.is_(None))
        )
    recent_events = recent_events_q.order_by(CodeLearningEvent.created_at.desc()).limit(20).all()

    circular_q = db.query(func.count(CodeDependency.id)).filter(CodeDependency.is_circular.is_(True))
    dep_alert_q = db.query(func.count(CodeDepAlert.id)).filter(CodeDepAlert.resolved.is_(False))
    review_q = db.query(func.count(CodeReview.id))
    if repo_ids:
        circular_q = circular_q.filter(CodeDependency.repo_id.in_(repo_ids))
        dep_alert_q = dep_alert_q.filter(CodeDepAlert.repo_id.in_(repo_ids))
        review_q = review_q.filter(CodeReview.repo_id.in_(repo_ids))
    else:
        circular_q = circular_q.filter(CodeDependency.repo_id == -1)
        dep_alert_q = dep_alert_q.filter(CodeDepAlert.repo_id == -1)
        review_q = review_q.filter(CodeReview.repo_id == -1)
    circular_count = circular_q.scalar() or 0
    dep_alert_count = dep_alert_q.scalar() or 0
    review_count = review_q.scalar() or 0

    trend_deltas = {}
    trend_repo = _first_live_repo(repos)
    if trend_repo is not None:
        try:
            trend_deltas = trends_mod.compute_trend_deltas(db, trend_repo.id)
        except Exception:
            pass
    else:
        trend_deltas = {"available": False}

    return {
        "repos": len(repos),
        "total_files": total_files,
        "total_lines": total_lines,
        "insight_count": insight_count,
        "hotspot_count": hotspot_count,
        "avg_complexity": round(avg_complexity, 2),
        "circular_dep_count": circular_count,
        "dep_alert_count": dep_alert_count,
        "review_count": review_count,
        "languages": lang_totals,
        "trend_deltas": trend_deltas,
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
    repos = indexer.get_accessible_repos(db, user_id=user_id, include_shared=True)
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

    # Circular dependency warnings
    circ = metrics.get("circular_dep_count") or 0
    if circ:
        parts.append(
            f"Warning: {circ} circular import edges detected. "
            "Consider breaking these cycles to improve maintainability."
        )

    # Dependency health alerts
    dep_alerts = metrics.get("dep_alert_count") or 0
    if dep_alerts:
        parts.append(
            f"Dependency health: {dep_alerts} package(s) are outdated. "
            "Check the Brain dashboard for details."
        )

    # Quality trends
    td = metrics.get("trend_deltas") or {}
    if td.get("available"):
        deltas = td.get("deltas") or {}
        alerts = td.get("alerts") or []
        if alerts:
            alert_msgs = [f"{a['metric']} changed {a['change']:+.1f}%" for a in alerts]
            parts.append("Quality trend alerts: " + "; ".join(alert_msgs) + ".")

    # Recent review findings
    try:
        metric_repo_ids = _fresh_metric_repo_ids(repos)
        reviews = reviewer_mod.get_recent_reviews(db, repo_ids=metric_repo_ids, limit=3)
        critical_findings = []
        for rev in reviews:
            for f in (rev.get("findings") or []):
                if f.get("severity") in ("critical", "warn"):
                    critical_findings.append(f"{f.get('category', 'issue')}: {f.get('message', '')}")
        if critical_findings:
            parts.append(
                "Recent code review findings: "
                + "; ".join(critical_findings[:3])
                + "."
            )
    except Exception:
        pass

    return "\n".join(parts)


# ── Project-level wrappers (lens-aware) ──────────────────────────────

def get_project_metrics(db: Session, user_id: Optional[int] = None, lens: Optional[str] = None) -> Dict[str, Any]:
    """Return metrics, optionally filtered through a role lens."""
    if lens:
        from .lenses import get_lens_metrics
        repos = indexer.get_accessible_repos(db, user_id=user_id, include_shared=True)
        repo = _first_live_repo(repos)
        repo_id = repo.id if repo is not None else -1
        return get_lens_metrics(db, lens, repo_id=repo_id, user_id=user_id)
    return get_code_brain_metrics(db, user_id=user_id)


def get_project_chat_context(db: Session, user_id: Optional[int] = None, lens: Optional[str] = None) -> str:
    """Return chat context, optionally filtered through a role lens."""
    if lens:
        from .lenses import get_lens_chat_context
        return get_lens_chat_context(db, lens, user_id=user_id)
    return get_code_chat_context(db, user_id=user_id)
