"""Voice assistant service: Whisper STT integration and voice command routing.

Transcription backends (tried in order):
  1. Ollama whisper model (local, free)
  2. OpenAI Whisper API (requires API key)
  3. Returns error if neither available
"""
import io
import json
import tempfile
import re
from pathlib import Path
from typing import Optional

import requests
from sqlalchemy.orm import Session

from ..logger import log_info

OLLAMA_URL = "http://localhost:11434"
VOICE_COMMANDS: list[tuple[str, dict]] = [
    (r"add (?:a )?(?:chore|todo|task)\s+(.+)", {
        "action": "add_chore", "description": "Add a chore",
    }),
    (r"(?:what(?:'s| are| is) the )?(chore|chores|todo|todos|task|tasks)", {
        "action": "list_chores", "description": "List chores",
    }),
    (r"(?:what(?:'s| is) the )?weather", {
        "action": "weather", "description": "Get weather",
    }),
    (r"(?:who(?:'s| are| is))?\s*(?:birthday|birthdays)", {
        "action": "list_birthdays", "description": "List birthdays",
    }),
    (r"(?:set|change|update) (?:my )?status (?:to )?(.+)", {
        "action": "set_status", "description": "Set status",
    }),
    (r"(?:turn|switch) (?:on|off) (?:do not disturb|dnd)", {
        "action": "toggle_dnd", "description": "Toggle DND",
    }),
]


def transcribe_audio(audio_bytes: bytes, mime_type: str = "audio/webm") -> dict:
    """Transcribe audio using available backend. Returns {ok, text, backend}."""
    result = _try_openai_whisper(audio_bytes, mime_type)
    if result:
        return result

    return {"ok": False, "text": "", "error": "No transcription backend available. Configure OpenAI API key for Whisper."}


def _try_openai_whisper(audio_bytes: bytes, mime_type: str) -> Optional[dict]:
    """Use OpenAI/Groq Whisper API if configured."""
    try:
        from .. import openai_client
        if not openai_client.is_configured():
            return None

        ext = "webm" if "webm" in mime_type else "wav" if "wav" in mime_type else "mp3"
        with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as f:
            f.write(audio_bytes)
            tmp_path = f.name

        try:
            client = openai_client.get_client()
            with open(tmp_path, "rb") as audio_file:
                transcript = client.audio.transcriptions.create(
                    model="whisper-1",
                    file=audio_file,
                )
            text = transcript.text.strip()
            return {"ok": True, "text": text, "backend": "openai-whisper"}
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    except Exception as e:
        log_info(f"[voice] OpenAI Whisper failed: {e}")
        return None


def parse_voice_command(text: str) -> dict:
    """Parse transcribed text for known voice commands.

    Returns {matched, action, data, original_text}.
    If no command matches, returns matched=False so it can be routed to general chat.
    """
    text_lower = text.lower().strip()

    for pattern, cmd in VOICE_COMMANDS:
        m = re.search(pattern, text_lower)
        if m:
            data = {}
            if cmd["action"] == "add_chore" and m.lastindex and m.lastindex >= 1:
                data["title"] = m.group(1).strip()
            elif cmd["action"] == "set_status" and m.lastindex and m.lastindex >= 1:
                data["status"] = m.group(1).strip()
            return {
                "matched": True,
                "action": cmd["action"],
                "description": cmd["description"],
                "data": data,
                "original_text": text,
            }

    return {
        "matched": False,
        "action": "general_chat",
        "description": "General conversation",
        "data": {},
        "original_text": text,
    }


def get_voice_capabilities() -> dict:
    """Check which voice backends are available."""
    openai_available = False
    try:
        from .. import openai_client
        openai_available = openai_client.is_configured()
    except Exception:
        pass

    return {
        "stt_backends": {
            "openai_whisper": openai_available,
            "browser_speech_api": True,
        },
        "tts_backends": {
            "browser_speech_synthesis": True,
        },
    }
