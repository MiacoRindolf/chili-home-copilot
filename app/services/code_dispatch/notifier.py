"""Notification surface — only fires when human attention is required."""
from __future__ import annotations

import logging
import os
from typing import Any

from sqlalchemy import text

logger = logging.getLogger(__name__)


def escalate_to_user(
    run_id: int,
    *,
    title: str,
    body: str,
    severity: str = "info",   # 'info' | 'warning' | 'critical'
    extras: dict[str, Any] | None = None,
) -> None:
    """Mark the run as needing user attention and emit notifications.

    Channels (best-effort, in order):
      1. Mark code_agent_runs.notify_user=true; brain UI surfaces it.
      2. Desktop toast via existing actions plugin (no-op if disabled).
      3. Mobile push via existing intercom channel (no-op if disabled).
    """
    payload = {"run_id": run_id, "title": title, "severity": severity, **(extras or {})}
    logger.warning("[code_dispatch.notifier] %s severity=%s body=%s extras=%s", title, severity, body, extras)
    _mark_run(run_id)
    if os.environ.get("CHILI_DISPATCH_NOTIFY_DESKTOP", "1") == "1":
        try:
            _desktop_toast(title, body, severity)
        except Exception:
            logger.debug("[code_dispatch.notifier] desktop toast failed", exc_info=True)
    if severity in ("warning", "critical") and os.environ.get("CHILI_DISPATCH_NOTIFY_MOBILE", "1") == "1":
        try:
            _mobile_push(title, body, severity)
        except Exception:
            logger.debug("[code_dispatch.notifier] mobile push failed", exc_info=True)


def _mark_run(run_id: int) -> None:
    from ...db import SessionLocal

    sess = SessionLocal()
    try:
        sess.execute(
            text(
                "UPDATE code_agent_runs SET notify_user = TRUE, notified_at = NOW() WHERE id = :id"
            ),
            {"id": run_id},
        )
        sess.commit()
    finally:
        sess.close()


def _desktop_toast(title: str, body: str, severity: str) -> None:
    # Lazy import to avoid hard dependency on the actions plugin.
    try:
        from ...services.intercom_actions import emit_desktop_toast  # type: ignore
    except Exception:
        return
    emit_desktop_toast(title=title, body=body, severity=severity)


def _mobile_push(title: str, body: str, severity: str) -> None:
    try:
        from ...services.intercom_push import push_to_user  # type: ignore
    except Exception:
        return
    push_to_user(title=title, body=body, severity=severity)
