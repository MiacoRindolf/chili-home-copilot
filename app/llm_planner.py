import json
import os
import requests

from .planner_schema import validate_plan

_OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")
OLLAMA_URL = f"{_OLLAMA_HOST}/api/chat"
MODEL = "llama3"  # change if you're using a different one

SYSTEM_BASE = """You are CHILI, an action planner for a household assistant.
You MUST output ONLY valid JSON (no markdown, no extra text).

IMPORTANT:
- If the user request is ambiguous, underspecified, or not clearly one allowed action, output type="unknown".
- Do NOT guess.
- Ask for clarification via the "reply" field when unknown.

Output MUST be a single JSON object with EXACT keys:
- "type": one of the allowed action types
- "data": an object containing only the required fields for that action
- "reply": a short, friendly sentence for the user (max 20 words)

Allowed actions and required data:
- add_chore: {"title": str}
- list_chores: {}
- list_chores_pending: {}
- mark_chore_done: {"id": int}
- add_birthday: {"name": str, "date": "YYYY-MM-DD"}
- list_birthdays: {}
- answer_from_docs: {"source": str}  -- use ONLY when DOCUMENT CONTEXT is provided below

If the request is unclear or not supported:
{"type":"unknown","data":{"reason":"ambiguous"},"reply":"What would you like me to do\u2014add a chore, list chores, or add a birthday reminder?"}
"""

RAG_CONTEXT_TEMPLATE = """
DOCUMENT CONTEXT (retrieved from household documents):
{context}

When the user's question can be answered using the DOCUMENT CONTEXT above,
use type="answer_from_docs", put the filename in data.source, and answer in "reply".
"""


PERSONALITY_TEMPLATE = """
HOUSEMATE CONTEXT:
{personality}

Personalize your "reply" to match this housemate's preferences when possible.
"""


def _build_system_prompt(
    rag_context: str | None = None,
    personality_context: str | None = None,
) -> str:
    prompt = SYSTEM_BASE
    if rag_context:
        prompt += RAG_CONTEXT_TEMPLATE.format(context=rag_context)
    if personality_context:
        prompt += PERSONALITY_TEMPLATE.format(personality=personality_context)
    return prompt


def plan_action(
    user_message: str,
    rag_context: str | None = None,
    personality_context: str | None = None,
) -> dict:
    system_prompt = _build_system_prompt(rag_context, personality_context)
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        "stream": False,
        "options": {"temperature": 0},
    }

    r = requests.post(OLLAMA_URL, json=payload, timeout=60)
    r.raise_for_status()

    # Ollama chat returns: {"message": {"role": "...", "content": "..."}, ...}
    text = r.json()["message"]["content"].strip()

    try:
        candidate = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            candidate = json.loads(text[start:end+1])
        else:
            return {
                "type": "unknown",
                "data": {"reason": "Planner returned invalid JSON", "raw": text},
                "reply": "I had trouble understanding that. Try: add chore..., list chores, add birthday Name YYYY-MM-DD.",
            }

    # ✅ NEW: Validate schema
    validated = validate_plan(candidate)
    if validated:
        return validated

    # ✅ If invalid, return safe fallback
    return {
        "type": "unknown",
        "data": {"reason": "Invalid plan schema", "raw": candidate},
        "reply": "I had trouble understanding that. Try: add chore..., list chores, add birthday Name YYYY-MM-DD.",
    }