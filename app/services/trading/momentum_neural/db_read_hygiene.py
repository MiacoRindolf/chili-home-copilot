"""Read-only SQLAlchemy session hygiene for momentum operator payloads."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from sqlalchemy.orm import Session

_log = logging.getLogger(__name__)


def _iter_detach_candidates(value: Any):
    if value is None or isinstance(value, (str, bytes, dict)):
        return
    if isinstance(value, Iterable):
        for item in value:
            yield from _iter_detach_candidates(item)
        return
    yield value


def detach_loaded_instances(db: Session, *values: Any) -> None:
    """Detach loaded ORM rows so a read-only rollback will not expire them."""
    for obj in _iter_detach_candidates(values):
        try:
            db.expunge(obj)
        except Exception:
            continue


def end_read_only_transaction(db: Session, *, context: str) -> None:
    """End implicit read transactions so API payload assembly does not hold DB sockets."""
    try:
        in_transaction = getattr(db, "in_transaction", None)
        if callable(in_transaction) and not in_transaction():
            return
    except Exception:
        pass
    try:
        db.rollback()
    except Exception:
        _log.debug("[momentum_db_read_hygiene] rollback failed context=%s", context, exc_info=True)
