"""Canonical ``promotion_changed`` emission when ScanPattern promotion/lifecycle surface mutates."""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

from .emitters import emit_promotion_changed_outcome

logger = logging.getLogger(__name__)


def emit_promotion_surface_change(
    db: Session,
    *,
    scan_pattern_id: int,
    old_promotion_status: str,
    old_lifecycle_stage: str,
    new_promotion_status: str,
    new_lifecycle_stage: str,
    source: str,
    extra: Optional[dict] = None,
) -> int | None:
    """Emit outcome if promotion or lifecycle string actually changed."""
    op = (old_promotion_status or "").strip()
    np = (new_promotion_status or "").strip()
    ol = (old_lifecycle_stage or "").strip()
    nl = (new_lifecycle_stage or "").strip()
    if op == np and ol == nl:
        return None
    try:
        return emit_promotion_changed_outcome(
            db,
            scan_pattern_id=int(scan_pattern_id),
            old_promotion_status=op,
            new_promotion_status=np,
            old_lifecycle_stage=ol,
            new_lifecycle_stage=nl,
            source=source,
            extra=extra,
        )
    except Exception:
        logger.debug("[promotion_surface] emit failed for pattern %s", scan_pattern_id, exc_info=True)
        return None


def emit_promotion_surface_if_pattern_changed(
    db: Session,
    *,
    scan_pattern_id: int,
    before_promotion: str,
    before_lifecycle: str,
    source: str,
    extra: Optional[dict] = None,
) -> int | None:
    """Load pattern and emit if stored surface differs from *before_* snapshot."""
    from ...models.trading import ScanPattern

    p = db.query(ScanPattern).filter(ScanPattern.id == int(scan_pattern_id)).first()
    if not p:
        return None
    return emit_promotion_surface_change(
        db,
        scan_pattern_id=int(scan_pattern_id),
        old_promotion_status=before_promotion,
        old_lifecycle_stage=before_lifecycle,
        new_promotion_status=(p.promotion_status or "").strip(),
        new_lifecycle_stage=(p.lifecycle_stage or "").strip(),
        source=source,
        extra=extra,
    )
