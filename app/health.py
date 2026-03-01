import requests
from sqlalchemy import text
from sqlalchemy.orm import Session

OLLAMA_TAGS_URL = "http://127.0.0.1:11434/api/tags"

def check_db(db: Session) -> dict:
    try:
        db.execute(text("SELECT 1"))
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def check_ollama(timeout_s: int = 2) -> dict:
    try:
        r = requests.get(OLLAMA_TAGS_URL, timeout=timeout_s)
        r.raise_for_status()
        data = r.json()
        models = [m.get("name") for m in data.get("models", [])]
        return {"ok": True, "models": models}
    except Exception as e:
        return {"ok": False, "error": str(e)}