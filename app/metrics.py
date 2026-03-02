from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from sqlalchemy import func, distinct
from .models import Chore, Birthday, ChatMessage, Conversation, User, Device, HousemateProfile

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


def total_stats(db: Session) -> dict:
    """Aggregate counts for the admin dashboard."""
    total_users = db.query(User).count()
    total_conversations = db.query(Conversation).count()
    total_messages = db.query(ChatMessage).count()

    week_ago = datetime.utcnow() - timedelta(days=7)
    active_convo_keys = (
        db.query(distinct(ChatMessage.convo_key))
        .filter(ChatMessage.created_at >= week_ago)
        .count()
    )

    return {
        "users": total_users,
        "conversations": total_conversations,
        "messages": total_messages,
        "active_users_7d": active_convo_keys,
    }


def user_stats(db: Session) -> list[dict]:
    """Per-user enriched stats for the admin users page."""
    users = db.query(User).order_by(User.name.asc()).all()

    msg_counts = dict(
        db.query(ChatMessage.convo_key, func.count(ChatMessage.id))
        .filter(ChatMessage.convo_key.like("user:%"))
        .group_by(ChatMessage.convo_key)
        .all()
    )
    convo_counts = dict(
        db.query(Conversation.convo_key, func.count(Conversation.id))
        .filter(Conversation.convo_key.like("user:%"))
        .group_by(Conversation.convo_key)
        .all()
    )

    results = []
    for u in users:
        key = f"user:{u.id}"
        devices = db.query(Device).filter(Device.user_id == u.id).all()
        profile = db.query(HousemateProfile).filter(HousemateProfile.user_id == u.id).first()

        results.append({
            "id": u.id,
            "name": u.name,
            "device_count": len(devices),
            "devices": [
                {
                    "id": d.id,
                    "label": d.label,
                    "last_ip": d.client_ip_last or "—",
                    "last_seen": d.last_seen_at.strftime("%Y-%m-%d %H:%M") if d.last_seen_at else "—",
                    "token_short": d.token[:8] + "..." if d.token else "—",
                }
                for d in devices
            ],
            "message_count": msg_counts.get(key, 0),
            "conversation_count": convo_counts.get(key, 0),
            "has_profile": profile is not None,
            "profile": {
                "interests": profile.interests or "—",
                "dietary": profile.dietary or "—",
                "tone": profile.tone or "—",
                "notes": profile.notes or "—",
                "last_extracted": profile.last_extracted_at.strftime("%Y-%m-%d %H:%M") if profile and profile.last_extracted_at else "Never",
            } if profile else None,
        })
    return results


def rag_stats() -> dict:
    """Get ChromaDB collection stats without requiring Ollama to be online."""
    try:
        from .rag import _get_collection, DOCS_DIR, COLLECTION_NAME
        from pathlib import Path
        import chromadb

        chroma_dir = Path(__file__).resolve().parents[1] / "data" / "chroma"
        if not chroma_dir.exists():
            return {"available": False, "reason": "ChromaDB directory not found"}

        client = chromadb.PersistentClient(path=str(chroma_dir))
        try:
            col = client.get_collection(name=COLLECTION_NAME)
        except Exception:
            return {"available": False, "reason": "No collection ingested yet"}

        count = col.count()
        sources = set()
        if count > 0:
            meta = col.get(include=["metadatas"])
            for m in meta.get("metadatas", []):
                if m and "source" in m:
                    sources.add(m["source"])

        doc_files = sorted(DOCS_DIR.glob("*.txt")) if DOCS_DIR.exists() else []

        return {
            "available": True,
            "collection": COLLECTION_NAME,
            "chunk_count": count,
            "source_files": sorted(sources),
            "docs_on_disk": [f.name for f in doc_files],
        }
    except Exception as e:
        return {"available": False, "reason": str(e)}