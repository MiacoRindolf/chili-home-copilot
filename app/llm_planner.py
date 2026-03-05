import json
import requests

from .config import settings
from .schemas import validate_plan
from .prompts import load_prompt

OLLAMA_URL = f"{settings.ollama_host}/api/chat"
MODEL = settings.ollama_model

SYSTEM_BASE = load_prompt("planner_system")

RAG_CONTEXT_TEMPLATE = """
DOCUMENT CONTEXT (retrieved from household documents):
{context}

Use type="answer_from_docs" ONLY when the user is specifically asking about something covered in the documents above (e.g. "what's the wifi password?", "who's the landlord?", "when is trash pickup?").
Do NOT use answer_from_docs for greetings, casual chat, or general questions unrelated to the documents.
Do NOT include sensitive info (passwords, phone numbers) unless the user explicitly asks for it.
"""


PERSONALITY_TEMPLATE = """
HOUSEMATE CONTEXT:
{personality}

Personalize your "reply" to match this housemate's preferences when possible.
"""

PROJECT_CONTEXT_TEMPLATE = """
ACTIVE PROJECTS & TASKS:
{projects}

Use this context when the user asks about their projects, tasks, progress, or deadlines.
When adding tasks, match the project_name to an existing project above if possible.
"""


def _build_system_prompt(
    rag_context: str | None = None,
    personality_context: str | None = None,
    project_context: str | None = None,
) -> str:
    prompt = SYSTEM_BASE
    if rag_context:
        prompt += RAG_CONTEXT_TEMPLATE.format(context=rag_context)
    if personality_context:
        prompt += PERSONALITY_TEMPLATE.format(personality=personality_context)
    if project_context:
        prompt += PROJECT_CONTEXT_TEMPLATE.format(projects=project_context)
    return prompt


def plan_action(
    user_message: str,
    rag_context: str | None = None,
    personality_context: str | None = None,
    project_context: str | None = None,
) -> dict:
    system_prompt = _build_system_prompt(rag_context, personality_context, project_context)
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

    # BasePlan.reply has max_length=200; truncate so validation does not fail
    if isinstance(candidate.get("reply"), str) and len(candidate["reply"]) > 200:
        candidate["reply"] = candidate["reply"][:197].rstrip() + "..."

    # Validate schema
    validated = validate_plan(candidate)
    if validated:
        return validated

    # ✅ If invalid, return safe fallback
    return {
        "type": "unknown",
        "data": {"reason": "Invalid plan schema", "raw": candidate},
        "reply": "I had trouble understanding that. Try: add chore..., list chores, add birthday Name YYYY-MM-DD.",
    }