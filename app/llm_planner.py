import json
import os
import requests

from .planner_schema import validate_plan

_OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")
OLLAMA_URL = f"{_OLLAMA_HOST}/api/chat"
MODEL = "llama3"  # change if you're using a different one

SYSTEM_BASE = """You are CHILI, an action planner for a household assistant.
You MUST output ONLY valid JSON (no markdown, no extra text).

CRITICAL RULES:
- For casual conversation (greetings, "how are you", jokes, opinions, advice, general questions), ALWAYS output type="unknown". These go to the conversational AI.
- Only output a specific action type when the user is CLEARLY requesting that action.
- Do NOT volunteer private information (passwords, phone numbers, addresses) in the "reply" field.
- Do NOT guess. Ask for clarification via the "reply" field when unsure.

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
- answer_from_docs: {"source": str}  -- use ONLY when the user is explicitly asking about household info AND DOCUMENT CONTEXT is provided. NEVER use for greetings or casual chat.
- pair_device: {}  -- use when the user asks to pair/link their device, log in, sign in, or create an account
- intercom_broadcast: {"text": str}  -- use when the user asks to announce/broadcast a message to all housemates via intercom
- web_search: {"query": str}  -- use when the user asks to search the web, look something up online, find links, get current/latest info, or browse the internet. Put a clear search query in "query".
- add_plan_project: {"name": str}  -- use when the user wants to create a new project in the planner (e.g. "create a project for kitchen renovation", "new project: garden redesign")
- add_plan_task: {"project_name": str, "title": str}  -- use when the user wants to add a task to a project (e.g. "add task buy paint to kitchen renovation project")
- list_plan_projects: {}  -- use when the user asks to see their projects, project status, or what they're working on

For anything conversational, general knowledge, or not a clear action request:
{"type":"unknown","data":{},"reply":""}
"""

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