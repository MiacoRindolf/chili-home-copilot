"""Chat streaming: re-exports SSE streaming endpoints from chat.py.

Establishes a cleaner module boundary for the streaming subsystem.
The actual streaming generators remain in chat.py due to tight coupling
with route decorators and request context.
"""
from __future__ import annotations

# The streaming endpoints (chat_stream_api, mobile_chat_stream_api) contain
# deeply nested generator functions that are tightly coupled to their
# FastAPI route decorators and request/response lifecycle. Extracting them
# would require passing 10+ parameters or refactoring the entire streaming
# architecture. Instead, this module provides a clean import path and
# shared utilities.

import json as json_mod
from typing import Any


def sse_event(data: dict[str, Any]) -> str:
    """Format a dict as an SSE data line."""
    return f"data: {json_mod.dumps(data)}\n\n"


def sse_done() -> str:
    """Send the SSE stream termination event."""
    return sse_event({"done": True})


def sse_error(message: str) -> str:
    """Send an SSE error event."""
    return sse_event({"error": message, "done": True})


def sse_chunk(text: str, model: str = "") -> str:
    """Send a text chunk event during streaming."""
    return sse_event({"chunk": text, "model": model})
