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
- When the user says they want to CREATE or MAKE a project but gives NO project name and NO clear goal (e.g. "I want to make a project", "create a project", "new project"), do NOT output add_plan_project. Output type="unknown" and in "reply" ask one short clarifying question, e.g. "What would you like to name the project, or what goal are you aiming for?"
- When the user gives a clear project name or goal (e.g. "create a project for X", "make a project for job hunting", "new project: garden"), you MUST output add_plan_project or add_plan_project_with_tasks — NEVER output unknown. Actually creating the project in the system is required; do not just describe it.
- When the user asks WHAT TASKS to add, or what they SHOULD DO, or wants YOU to suggest/recommend tasks (e.g. "add tasks you think I should do", "what should I do for job hunting", "create a project for X and add tasks"), use add_plan_project_with_tasks. For each task you MUST output an object with "title" and "description". The "description" MUST include: "Complexity: [Low|Medium|High]. Duration: [e.g. 2-3 hours, 1 week]. Reasoning: [1-3 sentences explaining why this task matters and how it contributes to success]." Be specific and realistic so the plan is doable and has real fruition; you are responsible for making the project actionable.
- If they only list task titles (no "what should I do"), you may use "tasks" as array of objects with "title" and "description" (description can be brief or full Complexity/Duration/Reasoning).
- When you need more information for an accurate action (e.g. which project to add a task to, scope, timeline), prefer outputting type="unknown" with a short "reply" that asks one clarifying question rather than guessing.
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
- add_plan_project: {"name": str}  -- use when the user wants to create a new project ONLY (no tasks listed). Name is the project name.
- add_plan_project_with_tasks: {"name": str, "description": str (optional), "tasks": [{"title": str, "description": str}, ...]}  -- use when the user wants to create a project AND add tasks. "name" is project name. "tasks" MUST be an array of objects: each object has "title" (short task name) and "description" (must include "Complexity: Low|Medium|High", "Duration: ...", and "Reasoning: ..." when the user asked what to do or for suggestions). Extract 6-15 tasks; make each task's description specific so the plan is credible and doable.
- add_plan_task: {"project_name": str, "title": str}  -- use when the user wants to add a single task to an existing project.
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