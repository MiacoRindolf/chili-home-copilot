"""Phase M.2 — shared helpers for the three authoritative consumer
slices (tilt / promotion gate / kill-switch).

Specifically:

* Mode validation (``off`` / ``shadow`` / ``compare`` / ``authoritative``).
* Governance-approval lookup: a slice cannot flip to ``authoritative``
  unless there is a live, un-expired row in
  ``trading_governance_approvals`` with ``status='approved'`` and
  ``decision='allow'`` for the slice's ``action_type``. Missing /
  expired approvals force the service to refuse and emit a
  ``*_refused_authoritative`` ops line.
* Evaluation-id generation: deterministic per (slice, pattern,
  as_of_date, context_hash) so the three slices can link their audit
  rows on identical evidence.
"""
from __future__ import annotations

import hashlib
from datetime import date, datetime
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

_ALLOWED_MODES = ("off", "shadow", "compare", "authoritative")


def normalize_mode(value: Optional[str]) -> str:
    m = (value or "off").strip().lower()
    return m if m in _ALLOWED_MODES else "off"


def mode_is_active(mode: Optional[str]) -> bool:
    return normalize_mode(mode) != "off"


def mode_is_authoritative(mode: Optional[str]) -> bool:
    return normalize_mode(mode) == "authoritative"


def has_live_approval(db: Session, *, action_type: str) -> bool:
    """Return True iff a live, un-expired approval row exists for
    ``action_type``. Callers MUST gate authoritative mode on this.

    Expiration semantics:
      * ``expires_at`` IS NULL  -> approval is perpetual (operator
        responsibility to revoke explicitly).
      * ``expires_at > NOW()``  -> approval is still valid.
      * ``expires_at <= NOW()`` -> expired, NOT a live approval.

    The lookup is cheap (indexed by status/action_type). If the DB
    is momentarily unreachable, the function treats that as "no
    approval" (fail closed) and returns False.
    """
    try:
        row = db.execute(
            text(
                """
                SELECT 1
                FROM trading_governance_approvals
                WHERE action_type = :action_type
                  AND status = 'approved'
                  AND decision = 'allow'
                  AND (expires_at IS NULL OR expires_at > NOW())
                ORDER BY decided_at DESC NULLS LAST, submitted_at DESC
                LIMIT 1
                """
            ),
            {"action_type": action_type},
        ).fetchone()
        return row is not None
    except Exception:
        return False


def make_evaluation_id(
    *,
    slice_name: str,
    pattern_id: int,
    as_of_date: date,
    context_hash: Optional[str] = None,
) -> str:
    """Deterministic 16-char evaluation id for a single slice decision.

    Same inputs (slice, pattern, date, context_hash) always produce
    the same id. This lets the three slices share an id when they
    operate on identical resolved contexts on the same day.
    """
    blob = (
        f"{slice_name}|{int(pattern_id)}|{as_of_date.isoformat()}"
        f"|{context_hash or ''}"
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


__all__ = [
    "normalize_mode",
    "mode_is_active",
    "mode_is_authoritative",
    "has_live_approval",
    "make_evaluation_id",
]
