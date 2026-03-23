"""Lightweight read-only snapshot of trading brain state for the Brain Assistant.

Do not call get_brain_stats() here — it runs heavy backfill and large loops.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from ...models.trading import LearningEvent, ScanPattern, TradingInsight
from .backtest_queue import get_queue_status

logger = logging.getLogger(__name__)

_BRAIN_WORKER_STATUS_FILE = Path("data/brain_worker_status.json")
_SNAPSHOT_CACHE_TTL = 45  # seconds
_snapshot_cache: dict[int, tuple[float, dict[str, Any]]] = {}
_snapshot_cache_lock = threading.Lock()

PATTERNS_SUMMARY_LIMIT = 25
PATTERNS_SEARCH_LIMIT = 20
RECENT_ACTIVITY_LIMIT = 15
RECENT_INSIGHTS_LIMIT = 10


def _read_worker_status() -> dict[str, Any]:
    """Read brain worker status from disk (same shape as /api/trading/brain/worker/status)."""
    out: dict[str, Any] = {
        "status": "stopped",
        "pid": None,
        "current_step": "",
        "current_progress": "",
        "last_cycle": {},
        "totals": {},
        "paused": False,
    }
    if not _BRAIN_WORKER_STATUS_FILE.exists():
        return out
    try:
        with open(_BRAIN_WORKER_STATUS_FILE, "r") as f:
            data = json.load(f)
        out.update(data)
    except (FileNotFoundError, json.JSONDecodeError, OSError) as e:
        logger.debug("[brain_assistant_context] Worker status read failed: %s", e)
    return out


def _recent_activity(db: Session, limit: int = RECENT_ACTIVITY_LIMIT) -> list[dict[str, Any]]:
    """Last 24h learning events (same logic as worker/recent-activity)."""
    cutoff = datetime.utcnow() - timedelta(hours=24)
    events = (
        db.query(LearningEvent)
        .filter(LearningEvent.created_at >= cutoff)
        .order_by(LearningEvent.created_at.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "id": e.id,
            "type": e.event_type,
            "summary": (e.description or "")[:200],
            "created_at": e.created_at.isoformat() if e.created_at else None,
        }
        for e in events
    ]


def _patterns_summary(
    db: Session,
    user_id: int | None,
    limit: int = PATTERNS_SUMMARY_LIMIT,
    keyword_filter: str | None = None,
) -> list[dict[str, Any]]:
    """Top patterns for snapshot; optional keyword/ticker filter on name/description."""
    from sqlalchemy import or_
    q = db.query(ScanPattern).filter(ScanPattern.active.is_(True))
    if keyword_filter:
        term = f"%{keyword_filter}%"
        q = q.filter(
            or_(
                ScanPattern.name.ilike(term),
                (ScanPattern.description.isnot(None) & ScanPattern.description.ilike(term)),
            )
        )
        limit = min(limit, PATTERNS_SEARCH_LIMIT)
    q = q.order_by(
        ScanPattern.backtest_priority.desc(),
        ScanPattern.confidence.desc().nullslast(),
        ScanPattern.last_backtest_at.asc().nullsfirst(),
    ).limit(limit)
    rows = q.all()
    return [
        {
            "id": p.id,
            "name": p.name,
            "active": p.active,
            "last_backtest_at": p.last_backtest_at.isoformat() if p.last_backtest_at else None,
            "backtest_priority": p.backtest_priority,
            "win_rate": round(p.win_rate, 4) if p.win_rate is not None else None,
            "confidence": round(p.confidence, 2) if p.confidence is not None else None,
            "origin": p.origin,
        }
        for p in rows
    ]


def _insights_summary(db: Session, _user_id: int | None) -> dict[str, Any]:
    """Light counts and last N insight titles (shared Brain pool — not filtered by owner)."""
    q = db.query(TradingInsight).filter(TradingInsight.active.is_(True))
    total = q.count()
    recent = (
        q.order_by(TradingInsight.created_at.desc())
        .limit(RECENT_INSIGHTS_LIMIT)
        .all()
    )
    return {
        "total_active": total,
        "recent": [
            {"id": i.id, "preview": (i.pattern_description or "")[:80], "confidence": round(i.confidence, 2)}
            for i in recent
        ],
    }


def _extract_keyword_from_message(message: str) -> str | None:
    """If user message looks like a search (e.g. 'patterns for BTC', 'search X'), return a token to filter by."""
    if not message or len(message.strip()) < 2:
        return None
    # "search for X" / "patterns mentioning X" / "ticker AAPL" / just "BTC" or "AAPL"
    m = re.search(r"(?:search|find|patterns?|mentioning|for|ticker)\s+(?:for\s+)?[\"']?([A-Za-z0-9\-\.]+)[\"']?", message, re.I)
    if m:
        return m.group(1).strip()
    # Single ticker-like word (2–6 chars, optional -USD)
    words = message.strip().split()
    for w in words:
        if 2 <= len(w) <= 10 and re.match(r"^[A-Za-z0-9\-\.]+$", w):
            return w
    return None


def build_snapshot(
    db: Session,
    user_id: int | None,
    user_message: str | None = None,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Build a compact snapshot for the Trading Brain Assistant (read-only, no get_brain_stats)."""
    cache_key = user_id if user_id is not None else 0
    if use_cache:
        with _snapshot_cache_lock:
            entry = _snapshot_cache.get(cache_key)
            if entry:
                ts, data = entry
                if (time.time() - ts) < _SNAPSHOT_CACHE_TTL:
                    return data.copy()
                del _snapshot_cache[cache_key]

    queue = get_queue_status(db)
    worker = _read_worker_status()
    activity = _recent_activity(db)
    keyword = _extract_keyword_from_message(user_message or "") if user_message else None
    patterns = _patterns_summary(db, user_id, keyword_filter=keyword)
    insights = _insights_summary(db, user_id)

    snapshot = {
        "backtest_queue": queue,
        "worker": {
            "status": worker.get("status", "stopped"),
            "pid": worker.get("pid"),
            "current_step": worker.get("current_step", ""),
            "current_progress": worker.get("current_progress", ""),
            "paused": worker.get("paused", False),
            "last_cycle": worker.get("last_cycle") or {},
            "totals": worker.get("totals") or {},
        },
        "recent_activity": activity,
        "patterns_summary": patterns,
        "patterns_keyword": keyword,
        "insights": insights,
        "snapshot_at": datetime.utcnow().isoformat() + "Z",
    }

    if use_cache:
        with _snapshot_cache_lock:
            _snapshot_cache[cache_key] = (time.time(), snapshot)

    return snapshot
