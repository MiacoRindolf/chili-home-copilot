from sqlalchemy.orm import Session
from .models import Chore, Birthday

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