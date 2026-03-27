"""Database-backed wake/stop/heartbeat for scripts/brain_worker.py.

File-based ``data/brain_worker_*`` can fail when the API process and worker
process resolve ``data/`` differently. The worker and the web app share
PostgreSQL — control plane here is reliable.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any, TYPE_CHECKING

from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from ..models.core import BrainWorkerControl as BrainWorkerControlRow

logger = logging.getLogger(__name__)

_CONTROL_ID = 1

# Worker should heartbeat every ~5s; API treats missing/stale heartbeat as "not really running"
# when combined with unknown PID or stale status file.
BRAIN_WORKER_HEARTBEAT_INTERVAL_S = 5
BRAIN_WORKER_HEARTBEAT_STALE_S = 20  # > 2× interval; allows missed ticks


def set_wake_requested(db: Session) -> None:
    """Queue a wake: worker will skip idle sleep on next check. Caller must commit."""
    from ..models.core import BrainWorkerControl

    row = db.get(BrainWorkerControl, _CONTROL_ID)
    now = datetime.utcnow()
    if row is None:
        db.add(
            BrainWorkerControl(
                id=_CONTROL_ID,
                wake_requested=True,
                stop_requested=False,
                updated_at=now,
            )
        )
    else:
        row.wake_requested = True
        row.updated_at = now


def set_stop_requested(db: Session) -> None:
    """API stop: worker polls this and cooperatively shuts down. Caller must commit."""
    from ..models.core import BrainWorkerControl

    row = db.get(BrainWorkerControl, _CONTROL_ID)
    now = datetime.utcnow()
    if row is None:
        db.add(
            BrainWorkerControl(
                id=_CONTROL_ID,
                wake_requested=False,
                stop_requested=True,
                updated_at=now,
            )
        )
    else:
        row.stop_requested = True
        row.updated_at = now


def clear_stop_requested(db: Session) -> None:
    """Worker acknowledged stop (or startup cleanup). Caller must commit."""
    from ..models.core import BrainWorkerControl

    row = db.get(BrainWorkerControl, _CONTROL_ID)
    if row is None:
        return
    row.stop_requested = False
    row.updated_at = datetime.utcnow()


def is_stop_requested(db: Session) -> bool:
    """Read-only: whether API requested stop."""
    from ..models.core import BrainWorkerControl

    row = db.get(BrainWorkerControl, _CONTROL_ID)
    return bool(row and row.stop_requested)


def peek_wake_requested(db: Session) -> bool:
    """Read-only: whether a DB wake is queued (do not clear)."""
    from ..models.core import BrainWorkerControl

    row = db.get(BrainWorkerControl, _CONTROL_ID)
    return bool(row and row.wake_requested)


def consume_db_wake(db: Session) -> bool:
    """If a DB wake was queued, clear it and return True. Commits on success."""
    from ..models.core import BrainWorkerControl

    try:
        row = db.get(BrainWorkerControl, _CONTROL_ID)
        if row is None or not row.wake_requested:
            return False
        row.wake_requested = False
        row.updated_at = datetime.utcnow()
        db.commit()
        return True
    except Exception as e:
        logger.warning("[brain_worker_signals] consume_db_wake: %s", e)
        try:
            db.rollback()
        except Exception:
            pass
        return False


def update_worker_heartbeat(db: Session) -> None:
    """Worker calls periodically while running. Caller must commit."""
    from ..models.core import BrainWorkerControl

    row = db.get(BrainWorkerControl, _CONTROL_ID)
    now = datetime.utcnow()
    if row is None:
        db.add(
            BrainWorkerControl(
                id=_CONTROL_ID,
                wake_requested=False,
                stop_requested=False,
                last_heartbeat_at=now,
                updated_at=now,
            )
        )
    else:
        row.last_heartbeat_at = now
        row.updated_at = now


def get_worker_control_snapshot(db: Session) -> "BrainWorkerControlRow | None":
    """Return BrainWorkerControl row or None (read-only; expire/refresh if needed)."""
    from ..models.core import BrainWorkerControl

    return db.get(BrainWorkerControl, _CONTROL_ID)


def persist_last_cycle_digest_json(db: Session, payload: dict[str, Any]) -> None:
    """Store compact last learning-cycle digest for Brain UI. Commits on success."""
    from ..models.core import BrainWorkerControl

    try:
        row = db.get(BrainWorkerControl, _CONTROL_ID)
        now = datetime.utcnow()
        blob = json.dumps(payload, default=str)
        if row is None:
            db.add(
                BrainWorkerControl(
                    id=_CONTROL_ID,
                    wake_requested=False,
                    stop_requested=False,
                    updated_at=now,
                    last_cycle_digest_json=blob,
                )
            )
        else:
            row.last_cycle_digest_json = blob
            row.updated_at = now
        db.commit()
    except Exception as e:
        logger.warning("[brain_worker_signals] persist_last_cycle_digest_json: %s", e)
        try:
            db.rollback()
        except Exception:
            pass


def persist_last_proposal_skips_json(db: Session, payload: dict[str, Any]) -> None:
    """Store aggregated proposal skip counts for Brain UI. Commits on success."""
    from ..models.core import BrainWorkerControl

    try:
        row = db.get(BrainWorkerControl, _CONTROL_ID)
        now = datetime.utcnow()
        blob = json.dumps(payload, default=str)
        if row is None:
            db.add(
                BrainWorkerControl(
                    id=_CONTROL_ID,
                    wake_requested=False,
                    stop_requested=False,
                    updated_at=now,
                    last_proposal_skips_json=blob,
                )
            )
        else:
            row.last_proposal_skips_json = blob
            row.updated_at = now
        db.commit()
    except Exception as e:
        logger.warning("[brain_worker_signals] persist_last_proposal_skips_json: %s", e)
        try:
            db.rollback()
        except Exception:
            pass


def heartbeat_is_stale(last_heartbeat_at: datetime | None) -> bool:
    """True if heartbeat missing or older than BRAIN_WORKER_HEARTBEAT_STALE_S."""
    if last_heartbeat_at is None:
        return True
    age = datetime.utcnow() - last_heartbeat_at
    return age > timedelta(seconds=BRAIN_WORKER_HEARTBEAT_STALE_S)


def clear_worker_heartbeat(db: Session) -> None:
    """Clear heartbeat (e.g. when worker process exits / API force start). Caller must commit."""
    from ..models.core import BrainWorkerControl

    row = db.get(BrainWorkerControl, _CONTROL_ID)
    if row is None:
        return
    row.last_heartbeat_at = None
    row.updated_at = datetime.utcnow()
