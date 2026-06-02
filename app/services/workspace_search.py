"""Command-palette search for CHILI OS (⌘K).

Read-only and defensive: searches across workspace destinations/actions, trading
patterns (scan_patterns), the user's tickers, the user's research topics, and
the user's planner projects/tasks.
Every DB query is wrapped so a failure degrades that group to empty rather than
erroring the palette.

Returns a flat, ranked list of result dicts:
    {"type", "label", "sub", "icon", "app"?, "url", "blank"?}
where `app` (if set) opens that surface as an OS window and `url` is the
fallback/navigation target.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Static workspace destinations + actions (always available, filtered by query).
_DESTINATIONS = [
    {"type": "app", "label": "Dashboard", "sub": "Workspace home", "icon": "🗂️", "app": "dashboard", "url": "/workspace"},
    {"type": "app", "label": "Chat", "sub": "Ask Chili", "icon": "💬", "app": "chat", "url": "/chat"},
    {"type": "app", "label": "Trading Desk", "sub": "Live desk", "icon": "📈", "app": "trading", "url": "/trading"},
    {"type": "app", "label": "Brain", "sub": "Learning + patterns", "icon": "🧠", "app": "brain", "url": "/brain"},
    {"type": "app", "label": "Research", "sub": "Research digest", "icon": "🔎", "app": "research", "url": "/api/brain/reasoning/research/report"},
    {"type": "app", "label": "Planner", "sub": "Projects + tasks", "icon": "🗓️", "app": "planner", "url": "/planner"},
]
_ACTIONS = [
    {"type": "action", "label": "Open daily trading brief", "sub": "Today's P/L + closes", "icon": "🌶️", "url": "/api/brain/trading/brief", "blank": True},
    {"type": "action", "label": "Open research digest", "sub": "Stored research", "icon": "📰", "app": "research", "url": "/api/brain/reasoning/research/report"},
]


def _patterns(db: Session, q: str, limit: int) -> List[Dict[str, Any]]:
    try:
        from ..models import ScanPattern
        rows = (
            db.query(ScanPattern.id, ScanPattern.name)
            .filter(ScanPattern.name.ilike(f"%{q}%"))
            .order_by(ScanPattern.trade_count.desc().nullslast())
            .limit(limit)
            .all()
        )
        return [{"type": "pattern", "label": name, "sub": f"Pattern #{pid}", "icon": "⚡",
                 "app": "brain", "url": f"/brain?pattern={pid}"} for pid, name in rows if name]
    except Exception as e:
        logger.warning("[workspace_search] pattern search failed: %s", e)
        return []


def _tickers(db: Session, user_id: Optional[int], q: str, limit: int) -> List[Dict[str, Any]]:
    if not user_id:
        return []
    try:
        from sqlalchemy import distinct
        from ..models import Trade
        rows = (
            db.query(distinct(Trade.ticker))
            .filter(Trade.user_id == user_id, Trade.ticker.ilike(f"%{q}%"))
            .limit(limit)
            .all()
        )
        return [{"type": "ticker", "label": r[0], "sub": "Open on the desk", "icon": "🎯",
                 "app": "trading", "url": f"/trading?ticker={r[0]}"} for r in rows if r and r[0]]
    except Exception as e:
        logger.warning("[workspace_search] ticker search failed: %s", e)
        return []


def _research(db: Session, user_id: Optional[int], q: str, limit: int) -> List[Dict[str, Any]]:
    if not user_id:
        return []
    try:
        from ..models import ReasoningResearch
        rows = (
            db.query(ReasoningResearch.topic)
            .filter(
                ReasoningResearch.user_id == user_id,
                ReasoningResearch.stale.is_(False),
                ReasoningResearch.topic.ilike(f"%{q}%"),
            )
            .order_by(ReasoningResearch.relevance_score.desc(),
                      ReasoningResearch.searched_at.desc())
            .limit(limit)
            .all()
        )
        return [{"type": "research", "label": r[0], "sub": "Research", "icon": "🔎",
                 "app": "research", "url": "/api/brain/reasoning/research/report"}
                for r in rows if r and r[0]]
    except Exception as e:
        logger.warning("[workspace_search] research search failed: %s", e)
        return []


def _planner(db: Session, user_id: Optional[int], q: str, limit: int) -> List[Dict[str, Any]]:
    if not user_id:
        return []
    out: List[Dict[str, Any]] = []
    # Projects the user is a member of, matched by name. No deep-link: the planner
    # page only honors ?project_id=&task_id= together (both required), so a
    # project-only link would not focus anything — open the planner as-is.
    try:
        from ..models import PlanProject, ProjectMember
        rows = (
            db.query(PlanProject.id, PlanProject.name)
            .join(ProjectMember, ProjectMember.project_id == PlanProject.id)
            .filter(ProjectMember.user_id == user_id, PlanProject.name.ilike(f"%{q}%"))
            .order_by(PlanProject.updated_at.desc().nullslast())
            .limit(limit)
            .all()
        )
        out += [{"type": "project", "label": name, "sub": "Project", "icon": "🗂",
                 "app": "planner", "url": "/planner"} for pid, name in rows if name]
    except Exception as e:
        logger.warning("[workspace_search] planner project search failed: %s", e)
    # Tasks in those projects, matched by title. Deep-link is honored:
    # ?project_id=<pid>&task_id=<tid> selects the project and opens the task.
    try:
        from ..models import PlanProject, PlanTask, ProjectMember
        rows = (
            db.query(PlanTask.id, PlanTask.title, PlanTask.project_id)
            .join(PlanProject, PlanProject.id == PlanTask.project_id)
            .join(ProjectMember, ProjectMember.project_id == PlanProject.id)
            .filter(ProjectMember.user_id == user_id, PlanTask.title.ilike(f"%{q}%"))
            .order_by(PlanTask.updated_at.desc().nullslast())
            .limit(limit)
            .all()
        )
        out += [{"type": "task", "label": title, "sub": "Task", "icon": "✓",
                 "app": "planner", "url": f"/planner?project_id={proj_id}&task_id={tid}"}
                for tid, title, proj_id in rows if title]
    except Exception as e:
        logger.warning("[workspace_search] planner task search failed: %s", e)
    return out[:limit]


def search(db: Session, user_id: Optional[int], q: str, limit: int = 10) -> List[Dict[str, Any]]:
    """Return ranked palette results for query `q` (empty q → destinations)."""
    q = (q or "").strip()
    if not q:
        return _DESTINATIONS[:limit]

    ql = q.lower()
    out: List[Dict[str, Any]] = []
    # destinations + actions whose label matches
    out += [d for d in _DESTINATIONS if ql in d["label"].lower()]
    out += [a for a in _ACTIONS if ql in a["label"].lower()]
    # live data (best-effort)
    out += _tickers(db, user_id, q, 5)
    out += _patterns(db, q, 5)
    out += _research(db, user_id, q, 5)
    out += _planner(db, user_id, q, 5)
    return out[:limit]
