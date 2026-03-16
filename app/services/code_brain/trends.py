"""Quality trends: record time-series snapshots and compute deltas."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from ...models.code_brain import (
    CodeHotspot,
    CodeInsight,
    CodeQualitySnapshot,
    CodeRepo,
    CodeSnapshot,
)

logger = logging.getLogger(__name__)

_TEST_PATTERNS = ("test_", "_test.", "tests/", "test/", "__tests__/", ".spec.", ".test.")


def _is_test_file(path: str) -> bool:
    pl = path.lower()
    return any(p in pl for p in _TEST_PATTERNS)


def record_quality_snapshot(db: Session, repo_id: int) -> Dict[str, Any]:
    """Capture aggregate metrics for trend tracking."""
    repo = db.query(CodeRepo).filter(CodeRepo.id == repo_id).first()
    if not repo:
        return {"error": "Repo not found"}

    snaps = db.query(CodeSnapshot).filter(CodeSnapshot.repo_id == repo_id).all()
    total_files = len(snaps)
    total_lines = sum(s.line_count for s in snaps)
    complexities = [s.complexity_score for s in snaps if s.complexity_score > 0]
    avg_cx = sum(complexities) / len(complexities) if complexities else 0.0
    max_cx = max(complexities) if complexities else 0.0
    test_count = sum(1 for s in snaps if _is_test_file(s.file_path))
    test_ratio = test_count / total_files if total_files else 0.0

    hotspot_count = (
        db.query(func.count(CodeHotspot.id))
        .filter(CodeHotspot.repo_id == repo_id)
        .scalar() or 0
    )
    insight_count = (
        db.query(func.count(CodeInsight.id))
        .filter(CodeInsight.repo_id == repo_id, CodeInsight.active.is_(True))
        .scalar() or 0
    )

    qs = CodeQualitySnapshot(
        repo_id=repo_id,
        total_files=total_files,
        total_lines=total_lines,
        avg_complexity=round(avg_cx, 3),
        max_complexity=round(max_cx, 3),
        test_file_count=test_count,
        test_ratio=round(test_ratio, 4),
        hotspot_count=hotspot_count,
        insight_count=insight_count,
    )
    db.add(qs)
    db.commit()
    return {"recorded": True, "total_files": total_files, "avg_complexity": round(avg_cx, 3)}


def get_quality_trends(db: Session, repo_id: int, limit: int = 30) -> List[Dict[str, Any]]:
    """Return the last N quality snapshots as a time series."""
    rows = (
        db.query(CodeQualitySnapshot)
        .filter(CodeQualitySnapshot.repo_id == repo_id)
        .order_by(CodeQualitySnapshot.recorded_at.desc())
        .limit(limit)
        .all()
    )
    rows.reverse()
    return [
        {
            "recorded_at": r.recorded_at.isoformat() if r.recorded_at else None,
            "total_files": r.total_files,
            "total_lines": r.total_lines,
            "avg_complexity": r.avg_complexity,
            "max_complexity": r.max_complexity,
            "test_file_count": r.test_file_count,
            "test_ratio": round(r.test_ratio * 100, 1),
            "hotspot_count": r.hotspot_count,
            "insight_count": r.insight_count,
        }
        for r in rows
    ]


def compute_trend_deltas(db: Session, repo_id: int) -> Dict[str, Any]:
    """Compare latest snapshot to ~7 days ago; return percentage changes and alerts."""
    latest = (
        db.query(CodeQualitySnapshot)
        .filter(CodeQualitySnapshot.repo_id == repo_id)
        .order_by(CodeQualitySnapshot.recorded_at.desc())
        .first()
    )
    if not latest:
        return {"available": False}

    week_ago = datetime.utcnow() - timedelta(days=7)
    baseline = (
        db.query(CodeQualitySnapshot)
        .filter(
            CodeQualitySnapshot.repo_id == repo_id,
            CodeQualitySnapshot.recorded_at <= week_ago,
        )
        .order_by(CodeQualitySnapshot.recorded_at.desc())
        .first()
    )
    if not baseline:
        return {"available": False, "latest": _snap_dict(latest)}

    def _pct(new: float, old: float) -> Optional[float]:
        if old == 0:
            return None
        return round((new - old) / old * 100, 1)

    deltas = {
        "complexity_delta_pct": _pct(latest.avg_complexity, baseline.avg_complexity),
        "files_delta_pct": _pct(latest.total_files, baseline.total_files),
        "lines_delta_pct": _pct(latest.total_lines, baseline.total_lines),
        "test_ratio_delta_pct": _pct(latest.test_ratio, baseline.test_ratio),
        "hotspot_delta_pct": _pct(latest.hotspot_count, baseline.hotspot_count),
    }

    alerts = []
    if deltas["complexity_delta_pct"] and deltas["complexity_delta_pct"] > 10:
        alerts.append({"metric": "complexity", "change": deltas["complexity_delta_pct"], "level": "warn"})
    if deltas["test_ratio_delta_pct"] and deltas["test_ratio_delta_pct"] < -5:
        alerts.append({"metric": "test_ratio", "change": deltas["test_ratio_delta_pct"], "level": "warn"})
    if deltas["hotspot_delta_pct"] and deltas["hotspot_delta_pct"] > 20:
        alerts.append({"metric": "hotspots", "change": deltas["hotspot_delta_pct"], "level": "warn"})

    return {
        "available": True,
        "latest": _snap_dict(latest),
        "baseline": _snap_dict(baseline),
        "deltas": deltas,
        "alerts": alerts,
    }


def _snap_dict(s: CodeQualitySnapshot) -> Dict[str, Any]:
    return {
        "recorded_at": s.recorded_at.isoformat() if s.recorded_at else None,
        "total_files": s.total_files,
        "total_lines": s.total_lines,
        "avg_complexity": s.avg_complexity,
        "test_ratio": round(s.test_ratio * 100, 1),
        "hotspot_count": s.hotspot_count,
    }
