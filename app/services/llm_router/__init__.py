"""Multi-LLM router with logging.

Wraps app.openai_client.chat() so every call lands in llm_call_log for
distillation, and so cheap tiers run first when CHILI_LLM_LOCAL_FIRST=1.
"""
from __future__ import annotations

from .router import route_chat
from .log import log_call

__all__ = ["route_chat", "log_call"]
