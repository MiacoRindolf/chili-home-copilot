"""CHILI-specific fire-and-forget launcher for teacher-escalation skill learning.

Bridges a live chat turn to the decoupled pure module ``app/teacher_escalation.py``:
when a turn fails AND ``teacher_escalation_enabled`` is set, it spawns a daemon
thread that runs the async escalation (strong-model call + skill persist) so it
NEVER blocks or raises into the user's response.

Why a daemon thread (not asyncio.create_task on the request loop): the escalation
makes a BLOCKING strong-model call; running it on the FastAPI event loop would
stall every other request. A dedicated thread with its own ``asyncio.run`` loop
keeps it off the hot path entirely.

Default-dormant: with ``teacher_escalation_enabled`` off (the default),
``maybe_fire_teacher_escalation`` is a cheap no-op (one getattr). The
CHILI-specific LLM adapter lives here so ``teacher_escalation.py`` stays pure and
unit-testable.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _make_strong_llm_caller(trace_id: str):
    """Build an async llm_caller(prompt)->str|None backed by CHILI's strong model."""
    async def _caller(prompt: str) -> Optional[str]:
        try:
            from .context_brain.llm_gateway import gateway_chat
            res = gateway_chat(
                messages=[{"role": "user", "content": prompt}],
                purpose="teacher_escalation",
                system_prompt=("You are a precise senior engineer. Follow the "
                               "requested output format exactly."),
                trace_id=f"teacher:{trace_id}",
                max_tokens=2048,
            )
            if isinstance(res, dict):
                return res.get("reply") or None
            return None
        except Exception as e:  # best-effort — a teacher that can't be reached just yields no skill
            logger.warning("[teacher_hook] strong-model call failed: %s", e)
            return None
    return _caller


def _spawn_escalation(user_request: str, tool_results: List[Dict[str, Any]],
                      agent_reply: str, reason: str, trace_id: str) -> None:
    """Run the async escalation in a daemon thread (off the event loop)."""
    def _run():
        try:
            import asyncio
            from .. import teacher_escalation as te
            asyncio.run(te.escalate_and_learn(
                user_request, tool_results, agent_reply, reason,
                llm_caller=_make_strong_llm_caller(trace_id),
            ))
        except Exception as e:  # pragma: no cover - thread-internal best-effort
            logger.warning("[teacher_hook] escalation thread error: %s", e)

    threading.Thread(target=_run, daemon=True, name="teacher-escalation").start()


def maybe_fire_teacher_escalation(user_request: str,
                                  tool_results: List[Dict[str, Any]],
                                  agent_reply: str,
                                  trace_id: str = "chat") -> bool:
    """Spawn a background teacher escalation iff enabled AND the turn looks failed.

    Returns True if an escalation was spawned. NEVER raises — safe to call
    unconditionally from the chat path.
    """
    try:
        from ..config import settings
        if not getattr(settings, "teacher_escalation_enabled", False):
            return False
        from .. import teacher_escalation as te
        ok, reason = te.should_escalate(tool_results, agent_reply)
        if not ok:
            return False
        _spawn_escalation(user_request, tool_results, agent_reply, reason or "failure", trace_id)
        logger.info("[teacher_hook] spawned teacher escalation (reason=%s)", reason)
        return True
    except Exception as e:
        logger.warning("[teacher_hook] maybe_fire failed: %s", e)
        return False
