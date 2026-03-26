"""Phase 18: read-only metadata list for snapshot apply attempts (not a second detail view)."""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from ...models.coding_task import CodingAgentSuggestion, CodingAgentSuggestionApply
from .envelope import truncate_text

_APPLY_ATTEMPTS_LIST_DEFAULT_LIMIT = 15
_APPLY_ATTEMPTS_LIST_MAX_LIMIT = 50
_MESSAGE_PREVIEW_MAX_BYTES = 240


def list_apply_attempts_metadata_dict(
    db: Session,
    task_id: int,
    suggestion_id: int,
    limit: int | None,
) -> list[dict[str, Any]] | None:
    """
    Metadata-only rows for GET .../apply-attempts. Returns None if suggestion not found for task.
    message_preview is truncated on this route only (skinny list; full detail stays on suggestion GET).
    """
    snap = (
        db.query(CodingAgentSuggestion)
        .filter(
            CodingAgentSuggestion.id == suggestion_id,
            CodingAgentSuggestion.task_id == task_id,
        )
        .first()
    )
    if not snap:
        return None

    if limit is None:
        n = _APPLY_ATTEMPTS_LIST_DEFAULT_LIMIT
    else:
        try:
            n = int(limit)
        except (TypeError, ValueError):
            n = _APPLY_ATTEMPTS_LIST_DEFAULT_LIMIT
    if n < 1:
        n = _APPLY_ATTEMPTS_LIST_DEFAULT_LIMIT
    n = min(n, _APPLY_ATTEMPTS_LIST_MAX_LIMIT)

    rows = (
        db.query(CodingAgentSuggestionApply)
        .filter(
            CodingAgentSuggestionApply.suggestion_id == suggestion_id,
            CodingAgentSuggestionApply.task_id == task_id,
        )
        .order_by(CodingAgentSuggestionApply.id.desc())
        .limit(n)
        .all()
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        raw_msg = r.message or ""
        preview, _ = truncate_text(raw_msg, _MESSAGE_PREVIEW_MAX_BYTES)
        out.append(
            {
                "id": r.id,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "user_id": r.user_id,
                "dry_run": bool(r.dry_run),
                "status": r.status,
                "message_preview": preview,
            }
        )
    return out