"""Dedicated SQLAlchemy sessions for `brain_cycle_lease` I/O (Phase 3 single-flight).

Lease mutations never use the main learning `Session`, avoiding mixed dirty state and
interaction with Phase 2 shadow-final commits on that session.
"""

from __future__ import annotations

import logging
import os
import socket

from sqlalchemy.orm import Session

from .lease_sqlalchemy import SqlAlchemyBrainCycleLeasePort

logger = logging.getLogger(__name__)

_SCOPE_GLOBAL = "global"


def brain_lease_holder_id() -> str:
    return f"{os.getpid()}@{socket.gethostname()}"


def _lease_port() -> SqlAlchemyBrainCycleLeasePort:
    return SqlAlchemyBrainCycleLeasePort()


def brain_lease_enforcement_try_acquire_dedicated(
    *,
    holder_id: str,
    lease_seconds: int,
) -> bool:
    """Acquire or extend the global lease in an isolated session. Returns False if another holder holds a valid lease."""
    from ...db import SessionLocal

    ls: Session = SessionLocal()
    try:
        got = _lease_port().try_acquire(
            ls,
            scope_key=_SCOPE_GLOBAL,
            cycle_run_id=None,
            holder_id=holder_id,
            lease_seconds=lease_seconds,
        )
        ls.commit()
        return got
    except Exception:
        ls.rollback()
        raise
    finally:
        ls.close()


def brain_lease_enforcement_log_peer_on_denial() -> None:
    """Best-effort read of current lease row for logs when acquire is denied."""
    from ...db import SessionLocal

    ls = SessionLocal()
    try:
        dto = _lease_port().current_holder(ls, scope_key=_SCOPE_GLOBAL)
        logger.warning(
            "[brain_lease_enforcement] lease_acquire_denied scope=%s peer_holder=%r peer_expires=%s peer_cycle_run_id=%s",
            _SCOPE_GLOBAL,
            (dto.holder_id if dto else None) or "",
            dto.expires_at if dto else None,
            dto.cycle_run_id if dto else None,
        )
    except Exception as e:
        logger.warning("[brain_lease_enforcement] lease_acquire_denied (peer lookup failed): %s", e)
    finally:
        ls.close()


def brain_lease_enforcement_refresh_soft_dedicated(
    *,
    holder_id: str,
    lease_seconds: int,
) -> None:
    """Phase 3 policy: refresh failure is soft — log only, cycle continues."""
    from ...config import settings

    if not getattr(settings, "brain_cycle_lease_enforcement_enabled", False):
        return
    from ...db import SessionLocal

    ls = SessionLocal()
    try:
        ok = _lease_port().refresh(
            ls,
            scope_key=_SCOPE_GLOBAL,
            holder_id=holder_id,
            lease_seconds=lease_seconds,
        )
        ls.commit()
        if not ok:
            logger.warning(
                "[brain_lease_enforcement] lease_refresh_skipped scope=%s holder_id=%r (mismatch or missing row)",
                _SCOPE_GLOBAL,
                holder_id,
            )
    except Exception as e:
        ls.rollback()
        logger.warning("[brain_lease_enforcement] lease_refresh_failed (soft): %s", e)
    finally:
        ls.close()


def brain_lease_enforcement_release_dedicated(*, holder_id: str) -> None:
    from ...db import SessionLocal

    ls = SessionLocal()
    try:
        _lease_port().release(ls, scope_key=_SCOPE_GLOBAL, holder_id=holder_id)
        ls.commit()
    except Exception as e:
        ls.rollback()
        logger.warning("[brain_lease_enforcement] lease_release_failed (ignored): %s", e)
    finally:
        ls.close()


__all__ = [
    "brain_lease_enforcement_log_peer_on_denial",
    "brain_lease_enforcement_refresh_soft_dedicated",
    "brain_lease_enforcement_release_dedicated",
    "brain_lease_enforcement_try_acquire_dedicated",
    "brain_lease_holder_id",
]
