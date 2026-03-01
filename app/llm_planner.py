import json
import requests

OLLAMA_URL = "http://127.0.0.1:11434/api/chat"
MODEL = "llama3"  # change if you're using a different one

SYSTEM = """You are CHILI, an action planner for a household assistant.
You MUST output ONLY valid JSON (no markdown, no extra text).

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

If the request is unclear or not supported:
{"type":"unknown","data":{"reason":"..."},"reply":"I can help with chores and birthdays. Try: add chore..., list chores, add birthday Name YYYY-MM-DD."}
"""

def plan_action(user_message: str) -> dict:
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM},
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
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(text[start:end+1])
        return {"type": "unknown", "data": {"reason": "Planner returned invalid JSON", "raw": text}}