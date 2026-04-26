"""LLM-based refinement for desktop PC voice commands.

Stage 1: Correct ASR errors in the transcription before NLU/planner.
Stage 2: Normalize extracted app names to canonical aliases the client knows.
"""
import re

from ..config import settings
from ..logger import log_info
from . import desktop_app_aliases as alias_module

# Heuristic: message looks like open/launch/close/run/quit + something
_DESKTOP_VERB = re.compile(
    r"(?i)^\s*(?:open|launch|start|run|close|quit|kill|exit|stop)\s+(?:the\s+)?(?:my\s+)?\S",
)


def looks_like_desktop_command(message: str) -> bool:
    """True if the message likely is a desktop command (open/close app, etc.)."""
    if not message or not message.strip():
        return False
    return bool(_DESKTOP_VERB.search(message.strip()))


def refine_desktop_transcription(message: str, trace_id: str = "desktop_refine") -> str:
    """Correct speech-to-text errors in the phrase. Returns refined message or original on failure."""
    if not settings.desktop_refinement_enabled:
        return message
    if not message or not message.strip():
        return message

    from .. import openai_client

    if not openai_client.is_configured():
        return message

    system = (
        "Task: Correct speech-to-text errors in the user's phrase. "
        "Output ONLY the corrected phrase, nothing else. No quotes, no explanation. "
        "Common corrections: phonetic (vizual→visual, notepad→notepad), spacing (note pad→notepad)."
    )
    user_content = f"User said: {message.strip()}"
    try:
        try:
            from .context_brain.llm_gateway import gateway_chat
            result = gateway_chat(
                messages=[{"role": "user", "content": user_content}],
                purpose='desktop_refine_speech',
                system_prompt=system,
                trace_id=trace_id,
                user_message=message,
            )
        except Exception:
            result = openai_client.chat(
                messages=[{"role": "user", "content": user_content}],
                system_prompt=system,
                trace_id=trace_id,
                user_message=message,
            )
        reply = (result.get("reply") or "").strip()
        if reply:
            log_info(trace_id, f"desktop_refine original={message!r} refined={reply!r}")
            return reply
    except Exception as e:
        log_info(trace_id, f"desktop_refine_error={e}")
    return message


def normalize_app_name(app_name: str, trace_id: str = "desktop_norm") -> str:
    """Map extracted app name to a canonical alias key the client can resolve. Returns original on failure."""
    if not app_name or not app_name.strip():
        return app_name
    if not settings.desktop_refinement_enabled:
        return app_name.strip()

    from .. import openai_client

    if not openai_client.is_configured():
        return app_name.strip()

    canonical_list = ", ".join(alias_module.DESKTOP_APP_ALIAS_KEYS)
    system = (
        "Map the user's app name to the best matching canonical name from the list. "
        "Output ONLY the canonical app name, or the original phrase if no reasonable match. "
        "No quotes, no explanation."
    )
    user_content = f"User said: {app_name.strip()}\nCanonical apps: {canonical_list}"
    try:
        try:
            from .context_brain.llm_gateway import gateway_chat
            result = gateway_chat(
                messages=[{"role": "user", "content": user_content}],
                purpose='desktop_normalize_app',
                system_prompt=system,
                trace_id=trace_id,
                user_message=app_name,
            )
        except Exception:
            result = openai_client.chat(
                messages=[{"role": "user", "content": user_content}],
                system_prompt=system,
                trace_id=trace_id,
                user_message=app_name,
            )
        reply = (result.get("reply") or "").strip()
        if reply:
            log_info(trace_id, f"desktop_norm original={app_name!r} canonical={reply!r}")
            return reply
    except Exception as e:
        log_info(trace_id, f"desktop_norm_error={e}")
    return app_name.strip()
