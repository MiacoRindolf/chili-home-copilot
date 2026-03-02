import os
import requests
from sqlalchemy import text
from sqlalchemy.orm import Session
from .models import Chore, Birthday

_OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")
OLLAMA_TAGS_URL = f"{_OLLAMA_HOST}/api/tags"

def reset_demo_data(db: Session) -> dict:
    try:
        db.query(Chore).delete()
        db.query(Birthday).delete()
        db.commit()
        return {"ok": True}
    except Exception as e:
        db.rollback()
        return {"ok": False, "error": str(e)}

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