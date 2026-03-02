from sqlalchemy.orm import Session
from sqlalchemy import func
from .models import Chore, Birthday, ChatMessage

# Simple in-memory latency tracking (resets when server restarts)
_LATENCIES_MS = []

def record_latency(ms: int):
    _LATENCIES_MS.append(ms)
    # keep only last 100 to avoid unbounded growth
    if len(_LATENCIES_MS) > 100:
        del _LATENCIES_MS[:-100]

def latency_stats() -> dict:
    if not _LATENCIES_MS:
        return {"count": 0, "avg_ms": None, "p95_ms": None}

    xs = sorted(_LATENCIES_MS)
    n = len(xs)
    avg = sum(xs) / n
    p95 = xs[int(0.95 * (n - 1))]
    return {"count": n, "avg_ms": int(avg), "p95_ms": int(p95)}

def get_counts(db: Session) -> dict:
    total_chores = db.query(Chore).count()
    pending_chores = db.query(Chore).filter(Chore.done == False).count()
    done_chores = db.query(Chore).filter(Chore.done == True).count()
    total_birthdays = db.query(Birthday).count()

    return {
        "chores": {
            "total": total_chores,
            "pending": pending_chores,
            "done": done_chores,
        },
        "birthdays": {
            "total": total_birthdays,
        },
    }


def model_stats(db: Session) -> dict:
    """Count assistant messages by model_used."""
    rows = (
        db.query(ChatMessage.model_used, func.count(ChatMessage.id))
        .filter(ChatMessage.role == "assistant")
        .group_by(ChatMessage.model_used)
        .all()
    )
    return {model or "unknown": count for model, count in rows}