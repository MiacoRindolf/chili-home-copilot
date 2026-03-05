"""Wellness detection and mental health support routing.

Two-layer detection:
  1. Crisis keywords  → immediate hotline response (no LLM call)
  2. Mental health topics → Ollama with therapeutic prompt (local, private)

All mental health conversations stay on-device when Ollama is running.
Groq is the fallback only if Ollama is offline.
"""
import re
import requests

from .config import settings
from .logger import log_info

_OLLAMA_CHAT_URL = f"{settings.ollama_host}/api/chat"
_OLLAMA_MODEL = settings.wellness_model

# ---------------------------------------------------------------------------
# Crisis detection (layer 1) — pure regex, never depends on an LLM
# ---------------------------------------------------------------------------

_CRISIS_PATTERNS = re.compile(
    r"(?i)"
    r"(\bsuicid(?:e|al)\b)"
    r"|(\bkill\s+(?:my\s*self|herself|himself|themselves)\b)"
    r"|(\bwant\s+to\s+die\b)"
    r"|(\bend\s+(?:my|it\s+all|everything)\b)"
    r"|(\bself[- ]?harm\w*\b)"
    r"|(\bcutting\s+(?:my\s*self|myself)\b)"
    r"|(\bno\s+reason\s+to\s+live\b)"
    r"|(\bbetter\s+off\s+(?:dead|without\s+me)\b)"
    r"|(\bdon'?t\s+want\s+to\s+(?:be\s+)?alive\b)"
    r"|(\bhurt\s+(?:my\s*self|myself)\b)"
    r"|(\boverdose\b)"
)

CRISIS_RESPONSE = (
    "I hear you, and I want you to know that what you're feeling matters. "
    "You don't have to go through this alone.\n\n"
    "**Please reach out to someone who can help right now:**\n\n"
    "- **988 Suicide & Crisis Lifeline** — call or text **988** (US, 24/7)\n"
    "- **Crisis Text Line** — text **HOME** to **741741**\n"
    "- **International Association for Suicide Prevention** — https://www.iasp.info/resources/Crisis_Centres/\n\n"
    "These are free, confidential, and available 24/7. "
    "A real person will listen.\n\n"
    "*I'm here if you want to keep talking, but a trained counselor "
    "can give you the support you deserve right now.*"
)

# ---------------------------------------------------------------------------
# Mental health topic detection (layer 2)
# ---------------------------------------------------------------------------

_WELLNESS_PATTERNS = re.compile(
    r"(?i)"
    r"(\bfeel(?:ing)?\s+(?:sad|down|low|empty|numb|hopeless|worthless|lost|alone|lonely)\b)"
    r"|(\b(?:so|really|very|extremely)\s+(?:stressed|anxious|overwhelmed|exhausted|tired|burned?\s*out)\b)"
    r"|(\bdepress(?:ed|ion)\b)"
    r"|(\banxi(?:ety|ous)\b)"
    r"|(\bpanic\s+attack\b)"
    r"|(\bcan'?t\s+(?:sleep|stop\s+(?:crying|worrying|thinking))\b)"
    r"|(\bmental\s+health\b)"
    r"|(\b(?:hate|don'?t\s+like)\s+my\s*self\b)"
    r"|(\bno\s+(?:one|body)\s+(?:cares|loves|understands)\b)"
    r"|(\bfeel(?:ing)?\s+(?:trapped|stuck|broken|scared)\b)"
    r"|(\blonely|loneliness\b)"
    r"|(\bgrief|grieving|mourning\b)"
    r"|(\btrauma(?:tic|tized)?\b)"
    r"|(\binsomnia\b)"
    r"|(\bstress(?:ed)?\b)"
    r"|(\bburnout|burn(?:ed|t)\s*out\b)"
    r"|(\bcoping\b)"
    r"|(\btherapy|therapist|counseling|counselor\b)"
    r"|(\b(?:i\s+)?need\s+(?:help|someone\s+to\s+talk\s+to)\b)"
    r"|(\bbreakdown\b)"
    r"|(\bpanic(?:king)?\b)"
    r"|(\bworried\s+(?:about|all\s+the\s+time)\b)"
)

# ---------------------------------------------------------------------------
# Therapeutic system prompt
# ---------------------------------------------------------------------------

THERAPEUTIC_PROMPT = """You are CHILI (Conversational Home Interface & Life Intelligence), acting as a compassionate, non-judgmental listener for a housemate who needs emotional support.

Your approach:
- ALWAYS validate their feelings first. Never dismiss, minimize, or rush past what they're experiencing.
- Use active listening: reflect back what they've said to show you understand.
- Ask gentle, open-ended follow-up questions: "What does that feel like for you?" or "How long have you been feeling this way?"
- Suggest evidence-based coping techniques when appropriate:
  * Deep breathing (4-7-8 technique)
  * Grounding (5-4-3-2-1 senses exercise)
  * Journaling
  * Gentle movement or a short walk
  * Talking to someone they trust
- Reference their household context naturally when it helps ("It sounds like things have been a lot lately").
- Keep responses warm but concise — don't overwhelm them with text.
- Use their name when you know it.

You must NEVER:
- Diagnose any condition
- Prescribe medication or specific treatments
- Claim to be a therapist, counselor, or medical professional
- Dismiss their feelings with "just think positive" or "it could be worse"
- Provide specific medical advice

If the conversation suggests they need more support than you can provide, gently encourage them to reach out to a professional:
"It sounds like you're carrying a lot right now. Have you thought about talking to a counselor or therapist? They can offer support that goes beyond what I can."

End every response by reminding them (naturally, not robotically) that you're here and they're not alone."""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def detect_crisis(text: str) -> bool:
    """Return True if the message contains crisis-level keywords."""
    return bool(_CRISIS_PATTERNS.search(text))


def detect_wellness_topic(text: str) -> bool:
    """Return True if the message is about a mental health topic."""
    return bool(_WELLNESS_PATTERNS.search(text))


def wellness_chat(
    messages: list[dict],
    user_name: str = "friend",
    personality_context: str | None = None,
    trace_id: str = "wellness",
) -> dict:
    """Route mental health conversation through Ollama (local, private).

    Falls back to Groq via openai_client if Ollama is unreachable.
    Returns {"reply": str, "model": str}.
    """
    system = THERAPEUTIC_PROMPT
    if personality_context:
        system += f"\n\nHousemate context:\n{personality_context}"

    try:
        payload = {
            "model": _OLLAMA_MODEL,
            "messages": [{"role": "system", "content": system}] + messages,
            "stream": False,
            "options": {"temperature": 0.7},
        }
        r = requests.post(_OLLAMA_CHAT_URL, json=payload, timeout=60)
        r.raise_for_status()
        reply = r.json()["message"]["content"].strip()
        log_info(trace_id, f"wellness_ollama model={_OLLAMA_MODEL} len={len(reply)}")
        return {"reply": reply, "model": f"{_OLLAMA_MODEL}-wellness"}

    except Exception as e:
        log_info(trace_id, f"wellness_ollama_error={e}, falling back to groq")
        from . import openai_client
        if openai_client.is_configured():
            result = openai_client.chat(
                messages=messages,
                system_prompt=system,
                trace_id=trace_id,
                user_message=messages[-1]["content"] if messages else "",
            )
            if result["reply"]:
                return {"reply": result["reply"], "model": result["model"] + "-wellness"}
        return {
            "reply": "I can tell something's on your mind, and I want to be here for you. "
                     "My connection is having trouble right now — could you try again in a moment?",
            "model": "offline-wellness",
        }


def wellness_chat_stream(
    messages: list[dict],
    user_name: str = "friend",
    personality_context: str | None = None,
    trace_id: str = "wellness-stream",
):
    """Stream mental health conversation via Ollama (local).

    Yields (token, model) tuples. Falls back to Groq if Ollama is down.
    """
    system = THERAPEUTIC_PROMPT
    if personality_context:
        system += f"\n\nHousemate context:\n{personality_context}"

    model_name = f"{_OLLAMA_MODEL}-wellness"

    try:
        payload = {
            "model": _OLLAMA_MODEL,
            "messages": [{"role": "system", "content": system}] + messages,
            "stream": True,
            "options": {"temperature": 0.7},
        }
        r = requests.post(_OLLAMA_CHAT_URL, json=payload, timeout=60, stream=True)
        r.raise_for_status()

        for line in r.iter_lines():
            if not line:
                continue
            import json
            chunk = json.loads(line)
            if chunk.get("done"):
                break
            content = chunk.get("message", {}).get("content", "")
            if content:
                yield content, model_name

        log_info(trace_id, f"wellness_ollama_stream_complete model={_OLLAMA_MODEL}")

    except Exception as e:
        log_info(trace_id, f"wellness_ollama_stream_error={e}, falling back to groq")
        from . import openai_client
        if openai_client.is_configured():
            for tok, model in openai_client.chat_stream(
                messages=messages,
                system_prompt=system,
                trace_id=trace_id,
                user_message=messages[-1]["content"] if messages else "",
            ):
                yield tok, model + "-wellness"
