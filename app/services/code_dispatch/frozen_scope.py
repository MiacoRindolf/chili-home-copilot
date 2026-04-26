"""Frozen-scope guard.

Reads frozen_scope_paths and tells the dispatcher whether a diff is allowed
to merge automatically. Three severities:

- 'block': refuse. Run records decision='escalate'.
- 'review_required': allow agent to draft, but force PR + manual merge.
- 'warn': allow + audit warning.

Glob matching uses fnmatch-style globs against POSIX-normalized paths
relative to the repo root.
"""
from __future__ import annotations

import fnmatch
import logging
from dataclasses import dataclass
from typing import Iterable, Optional

from sqlalchemy import text

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FrozenHit:
    glob: str
    severity: str   # 'block' | 'review_required' | 'warn'
    reason: str
    file_path: str


def _load_globs() -> list[tuple[str, str, str]]:
    try:
        from ...db import SessionLocal

        sess = SessionLocal()
        try:
            rows = sess.execute(
                text("SELECT glob, severity, reason FROM frozen_scope_paths")
            ).fetchall()
            return [(r[0], r[1], r[2]) for r in rows]
        finally:
            sess.close()
    except Exception:
        logger.debug("[frozen_scope] load failed", exc_info=True)
        return []


def diff_touches_frozen_scope(file_paths: Iterable[str]) -> list[FrozenHit]:
    """Return list of FrozenHit records, one per (file, glob) match.

    Empty list means the diff is in the clear.
    """
    globs = _load_globs()
    if not globs:
        return []
    hits: list[FrozenHit] = []
    for raw in file_paths:
        path = raw.replace("\\", "/").lstrip("./")
        for glob, severity, reason in globs:
            if fnmatch.fnmatch(path, glob):
                hits.append(FrozenHit(glob=glob, severity=severity, reason=reason, file_path=path))
    return hits


def worst_severity(hits: list[FrozenHit]) -> Optional[str]:
    if not hits:
        return None
    order = {"warn": 0, "review_required": 1, "block": 2}
    return max(hits, key=lambda h: order.get(h.severity, -1)).severity


def is_blocked(hits: list[FrozenHit]) -> bool:
    return any(h.severity == "block" for h in hits)


def requires_review(hits: list[FrozenHit]) -> bool:
    return any(h.severity in ("block", "review_required") for h in hits)
