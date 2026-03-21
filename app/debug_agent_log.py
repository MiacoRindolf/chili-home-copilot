"""NDJSON debug sink for agent sessions (do not log secrets)."""
from __future__ import annotations

import json
import time
from pathlib import Path

_LOG_PATH = Path(__file__).resolve().parents[1] / "debug-f139e5.log"
_SESSION_ID = "f139e5"


def safe_db_fingerprint() -> str:
    """Host + database name only (no credentials) for comparing which DB a process uses."""
    try:
        from urllib.parse import urlparse

        from .config import settings

        u = settings.database_url or ""
        p = urlparse(u)
        dbname = (p.path or "/").rstrip("/").split("/")[-1] or "default"
        host = p.hostname or "?"
        port = p.port or ""
        return f"{p.scheme}://{host}:{port}/{dbname}"
    except Exception:
        return "unavailable"


def agent_log(hypothesis_id: str, location: str, message: str, data: dict | None = None) -> None:
    try:
        rec = {
            "sessionId": _SESSION_ID,
            "hypothesisId": hypothesis_id,
            "location": location,
            "message": message,
            "data": data or {},
            "timestamp": int(time.time() * 1000),
        }
        with open(_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=str) + "\n")
    except Exception:
        pass
