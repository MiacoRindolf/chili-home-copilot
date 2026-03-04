"""Voice assistant service: Whisper STT, edge-tts TTS, and voice command routing.

STT backends (tried in order):
  1. OpenAI/Groq Whisper API (requires API key)
  2. Returns error if not available

TTS backend:
  - Microsoft Edge TTS (free neural voices via edge-tts)
"""
import asyncio
import io
import json
import tempfile
import re
from pathlib import Path
from typing import Optional

import requests
from sqlalchemy.orm import Session

from ..logger import log_info, new_trace_id

TTS_VOICE = "aria"
TTS_VOICE_EDGE = "en-US-AriaNeural"
UNCLOSEAI_TTS_URL = "https://speech.ai.unturf.com/v1"

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
        from openai import OpenAI
        if not openai_client.is_configured():
            return None

        ext = "webm" if "webm" in mime_type else "wav" if "wav" in mime_type else "mp3"
        with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as f:
            f.write(audio_bytes)
            tmp_path = f.name

        is_groq = "groq" in openai_client.LLM_BASE_URL.lower()
        whisper_model = "whisper-large-v3-turbo" if is_groq else "whisper-1"

        try:
            client = OpenAI(
                api_key=openai_client.LLM_API_KEY,
                base_url=openai_client.LLM_BASE_URL,
            )
            with open(tmp_path, "rb") as audio_file:
                transcript = client.audio.transcriptions.create(
                    model=whisper_model,
                    file=audio_file,
                )
            text = transcript.text.strip()
            return {"ok": True, "text": text, "backend": f"whisper-{'groq' if is_groq else 'openai'}"}
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    except Exception as e:
        log_info(new_trace_id(), f"[voice] Whisper transcription failed: {e}")
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
            "qwen3_tts": True,
            "edge_tts": True,
        },
    }


def _clean_text_for_tts(text: str, max_sentences: int = 3) -> Optional[str]:
    """Strip HTML/markdown and keep only the first few sentences for fast TTS."""
    clean = re.sub(r'<[^>]*>', '', text)
    clean = re.sub(r'\*\*', '', clean)
    clean = re.sub(r'[#_`\-\[\]]', '', clean)
    clean = re.sub(r'\n{2,}', '. ', clean)
    clean = re.sub(r'\n', ' ', clean)
    clean = re.sub(r'\s{2,}', ' ', clean)
    clean = clean.strip()
    if not clean:
        return None

    sentences = re.split(r'(?<=[.!?])\s+', clean)
    truncated = ' '.join(sentences[:max_sentences]).strip()
    if not truncated:
        truncated = clean[:300]
    if len(truncated) > 500:
        truncated = truncated[:500]
    return truncated


_qwen3_client = None

def _get_qwen3_client():
    global _qwen3_client
    if _qwen3_client is None:
        from openai import OpenAI
        from httpx import Timeout
        _qwen3_client = OpenAI(
            api_key="not-needed",
            base_url=UNCLOSEAI_TTS_URL,
            timeout=Timeout(8.0, connect=3.0),
        )
    return _qwen3_client


async def _try_qwen3_tts(text: str, voice: str) -> Optional[bytes]:
    """Primary TTS: Qwen3-TTS via uncloseai (free, human-cloned voices)."""
    try:
        loop = asyncio.get_event_loop()
        def _call():
            client = _get_qwen3_client()
            response = client.audio.speech.create(
                model="tts-1", voice=voice, input=text,
            )
            return response.read()
        audio_bytes = await loop.run_in_executor(None, _call)
        if audio_bytes and len(audio_bytes) > 100:
            return audio_bytes
        return None
    except Exception as e:
        log_info(new_trace_id(), f"[voice] Qwen3-TTS failed: {e}")
        return None


async def _try_edge_tts(text: str, voice: str) -> Optional[bytes]:
    """Fallback TTS: Microsoft Edge neural voices."""
    try:
        import edge_tts
        communicate = edge_tts.Communicate(text, voice)
        audio_chunks = []
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_chunks.append(chunk["data"])
        if not audio_chunks:
            return None
        return b"".join(audio_chunks)
    except Exception as e:
        log_info(new_trace_id(), f"[voice] Edge TTS failed: {e}")
        return None


async def text_to_speech(text: str, voice: str = TTS_VOICE) -> Optional[bytes]:
    """Generate speech audio. Edge TTS primary (~1s), Qwen3-TTS fallback."""
    clean = _clean_text_for_tts(text)
    if not clean:
        return None

    result = await _try_edge_tts(clean, TTS_VOICE_EDGE)
    if result:
        return result

    return await _try_qwen3_tts(clean, voice)
