"""Generate and persist per-user daily trading brief HTML reports.

An *isolated* orchestration layer that wires together three existing,
single-responsibility building blocks into a batch-friendly job:

1. ``build_trading_summary(db, user_id, window_hours)`` — read-only summary dict.
2. ``build_brief(summary)`` — pure markdown + metadata shaped for the renderer.
3. ``generate_report(...)`` — pure markdown -> self-contained HTML document.

The functions here add only:

- file persistence (one HTML file per user under a caller-supplied directory),
- per-user fault isolation (one user's failure never aborts the batch), and
- a thin, patchable ``_active_user_ids`` query helper so a scheduler job can
  fan out over all users.

Everything is defensive: ``persist_user_brief`` and the batch runner never
raise — they log a warning and degrade (``None`` / a skipped entry) so a nightly
scheduler job can never crash on one bad user or a transient DB hiccup. This
module does not touch the scheduler, brokers, or any live state.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from ...models import User

from ..trading_summary import build_trading_summary
from ..trading_brief import build_brief
from ...visual_report import generate_report

logger = logging.getLogger(__name__)


def generate_user_brief_html(db: Session, user_id: int, window_hours: int = 24) -> str:
    """Render a user's daily trading brief as a self-contained HTML document.

    Composes the read-only summary, the pure brief builder, and the HTML
    renderer. Returns the complete HTML string.
    """
    summary = build_trading_summary(db, user_id, window_hours)
    brief = build_brief(summary)
    return generate_report(
        brief["title"],
        brief["markdown"],
        subtitle=brief["subtitle"],
        label=brief["label"],
        stats=brief["stats"],
        sources=brief["sources"],
        category="brief",
    )


def persist_user_brief(
    db: Session, user_id: int, out_dir: str, window_hours: int = 24
) -> Optional[str]:
    """Generate a user's brief HTML and write it to ``out_dir``.

    Writes ``brief_user_<user_id>.html`` into ``out_dir`` (created if missing)
    and returns the file path. Defensive: on ANY exception logs a warning and
    returns ``None`` rather than raising, so a batch job never aborts on one
    user.
    """
    try:
        html = generate_user_brief_html(db, user_id, window_hours)
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, f"brief_user_{user_id}.html")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(html)
        logger.info("[daily_brief] wrote brief for user %s -> %s", user_id, path)
        return path
    except Exception as e:
        logger.warning("[daily_brief] failed to persist brief for user %s: %s", user_id, e)
        return None


def _active_user_ids(db: Session) -> List[int]:
    """Return the user ids to generate briefs for (thin, patchable helper).

    Queries distinct user ids. Defensive: on exception returns ``[]`` so the
    batch runner degrades to a no-op rather than crashing.
    """
    try:
        rows = db.query(User.id).all()
        return [int(r[0]) for r in rows if r and r[0] is not None]
    except Exception as e:
        logger.warning("[daily_brief] active-user-ids query failed: %s", e)
        return []


def run_daily_brief_for_all_users(
    db: Session, out_dir: str, window_hours: int = 24
) -> Dict[str, Any]:
    """Generate and persist a daily brief for every active user.

    Per-user fault isolation: one user's failure (raised or ``None`` return)
    never stops the rest. Returns ``{"generated", "failed", "paths"}``.
    """
    ids = _active_user_ids(db)
    paths: List[str] = []
    failed = 0
    for user_id in ids:
        try:
            path = persist_user_brief(db, user_id, out_dir, window_hours)
        except Exception as e:  # pragma: no cover - persist_user_brief is itself defensive
            logger.warning("[daily_brief] unexpected error for user %s: %s", user_id, e)
            path = None
        if path:
            paths.append(path)
        else:
            failed += 1
    logger.info(
        "[daily_brief] batch complete: generated=%d failed=%d", len(paths), failed
    )
    return {"generated": len(paths), "failed": failed, "paths": paths}
