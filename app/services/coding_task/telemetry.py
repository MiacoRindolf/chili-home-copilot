"""Structured logging for the project-domain coding lifecycle (G2).

One logger per lifecycle step (bootstrap, agent cycle, suggest, apply,
validate) so operators can filter or grep by step. Each ``log_event`` call
emits a single ``key=value`` line that is cheap to tail and easy to ingest
into structured collectors later without a dependency change.

Fields — always passed as kwargs so every call site stays structured:
  task_id, code_repo_id, user_id, step, duration_ms, outcome, reason
"""
from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Any

_LOGGER_NAME = "chili.project_domain.coding"
_logger = logging.getLogger(_LOGGER_NAME)


def _fmt(fields: dict[str, Any]) -> str:
    parts: list[str] = []
    for k, v in fields.items():
        if v is None:
            continue
        s = str(v)
        if " " in s or "=" in s or '"' in s:
            s = '"' + s.replace('"', '\\"') + '"'
        parts.append(f"{k}={s}")
    return " ".join(parts)


def log_event(step: str, outcome: str, **fields: Any) -> None:
    """Emit one structured line for a lifecycle step.

    ``step``: one of ``bootstrap``, ``agent_cycle``, ``suggest``,
    ``save_snapshot``, ``dry_run``, ``apply``, ``validate``.
    ``outcome``: ``ok``, ``blocked``, ``failed``, ``timeout``.
    """
    payload = {"step": step, "outcome": outcome, **fields}
    _logger.info(_fmt(payload))


@contextmanager
def timed_step(step: str, **fields: Any):
    """Context manager that logs ``ok`` or ``failed`` with a ``duration_ms`` field.

    Re-raises the original exception after logging — callers observe normal
    error flow; the log is a side effect only.
    """
    start = time.monotonic()
    try:
        yield
    except Exception as exc:
        duration_ms = int((time.monotonic() - start) * 1000)
        log_event(
            step,
            "failed",
            duration_ms=duration_ms,
            error=type(exc).__name__,
            reason=str(exc)[:200],
            **fields,
        )
        raise
    else:
        duration_ms = int((time.monotonic() - start) * 1000)
        log_event(step, "ok", duration_ms=duration_ms, **fields)
