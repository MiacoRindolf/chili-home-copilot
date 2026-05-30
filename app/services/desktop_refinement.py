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
_DESKTOP_COMMAND_RE = re.compile(
    r"(?i)^\s*(?P<verb>open|launch|start|run|close|quit|kill|exit|stop)"
    r"\s+(?:the\s+)?(?:my\s+)?(?P<app>.+?)(?:\s+app)?\s*$",
)
_APP_NAME_CLEAN_RE = re.compile(r"[^a-z0-9+]+")
_SPOKEN_ALIAS_OVERRIDES = {
    "note pad": "notepad",
    "notepad plus plus": "notepad++",
    "notepad plus": "notepad++",
    "calculator": "calculator",
    "calc": "calc",
    "file explorer": "file explorer",
    "explorer": "explorer",
    "command prompt": "command prompt",
    "cmd": "cmd",
    "power shell": "powershell",
    "powershell": "powershell",
    "windows terminal": "windows terminal",
    "task manager": "task manager",
    "snipping tool": "snipping tool",
    "google chrome": "google chrome",
    "chrome": "chrome",
    "microsoft edge": "microsoft edge",
    "edge": "edge",
    "fire fox": "firefox",
    "firefox": "firefox",
    "visual studio code": "visual studio code",
    "vs code": "vs code",
    "v s code": "vs code",
    "vscode": "vscode",
    "visual studio": "visual studio",
    "microsoft teams": "microsoft teams",
    "teams": "teams",
    "microsoft word": "microsoft word",
    "word": "word",
    "microsoft excel": "microsoft excel",
    "excel": "excel",
}


def _normalize_app_alias_key(value: str) -> str:
    cleaned = _APP_NAME_CLEAN_RE.sub(" ", (value or "").lower()).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return re.sub(r"^(?:the|my)\s+", "", cleaned).strip()


def _deterministic_app_alias(app_name: str) -> str | None:
    normalized = _normalize_app_alias_key(app_name)
    if not normalized:
        return None

    alias_by_key = {
        _normalize_app_alias_key(alias): alias
        for alias in alias_module.DESKTOP_APP_ALIAS_KEYS
    }
    direct = alias_by_key.get(normalized)
    if direct:
        return direct

    override = _SPOKEN_ALIAS_OVERRIDES.get(normalized)
    if override and override in alias_module.DESKTOP_APP_ALIAS_KEYS:
        return override
    return None


def _deterministic_desktop_transcription(message: str) -> str | None:
    match = _DESKTOP_COMMAND_RE.match(message or "")
    if not match:
        return None

    app_name = re.sub(r"(?i)^my\s+", "", match.group("app").strip()).strip()
    alias = _deterministic_app_alias(app_name)
    if not alias:
        return None

    verb = match.group("verb").lower()
    normalized_verb = "open" if verb in {"open", "launch", "start", "run"} else "close"
    return f"{normalized_verb} {alias}"


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

    deterministic = _deterministic_desktop_transcription(message)
    if deterministic:
        if deterministic != message.strip():
            log_info(trace_id, f"desktop_refine_mechanical original={message!r} refined={deterministic!r}")
        else:
            log_info(trace_id, f"desktop_refine_mechanical original={message!r}")
        return deterministic

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
        except Exception as gw_error:
            log_info(
                trace_id,
                f"desktop_refine_gateway_error={gw_error}; direct_openai_bypass_disabled",
            )
            result = {"reply": "", "model": "gateway_error"}
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

    deterministic = _deterministic_app_alias(app_name)
    if deterministic:
        log_info(
            trace_id,
            f"desktop_norm_mechanical original={app_name!r} canonical={deterministic!r}",
        )
        return deterministic

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
        except Exception as gw_error:
            log_info(
                trace_id,
                f"desktop_norm_gateway_error={gw_error}; direct_openai_bypass_disabled",
            )
            result = {"reply": "", "model": "gateway_error"}
        reply = (result.get("reply") or "").strip()
        if reply:
            log_info(trace_id, f"desktop_norm original={app_name!r} canonical={reply!r}")
            return reply
    except Exception as e:
        log_info(trace_id, f"desktop_norm_error={e}")
    return app_name.strip()
