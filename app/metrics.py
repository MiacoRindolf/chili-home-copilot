from datetime import datetime, date, timedelta
from sqlalchemy.orm import Session
from sqlalchemy import func, distinct
from .models import Chore, Birthday, ChatMessage, ChatLog, Conversation, User, Device, HousemateProfile, ActivityLog

# In-memory latency tracking (resets on server restart, keeps last 500)
_LATENCIES_MS: list[tuple[float, int]] = []  # (timestamp, ms)

def record_latency(ms: int):
    _LATENCIES_MS.append((datetime.utcnow().timestamp(), ms))
    if len(_LATENCIES_MS) > 500:
        del _LATENCIES_MS[:-500]

def latency_stats() -> dict:
    if not _LATENCIES_MS:
        return {"count": 0, "avg_ms": None, "p95_ms": None}

    xs = sorted(m for _, m in _LATENCIES_MS)
    n = len(xs)
    avg = sum(xs) / n
    p95 = xs[int(0.95 * (n - 1))]
    return {"count": n, "avg_ms": int(avg), "p95_ms": int(p95)}

def latency_history() -> list[dict]:
    """Return recent latencies as [{timestamp, ms}] for chart rendering."""
    return [{"t": int(t * 1000), "ms": ms} for t, ms in _LATENCIES_MS[-100:]]

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
            "email": u.email or "",
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


def messages_per_day(db: Session, days: int = 14) -> list[dict]:
    """Daily message counts for the last N days."""
    cutoff = datetime.utcnow() - timedelta(days=days)
    day_col = func.strftime("%Y-%m-%d", ChatMessage.created_at).label("day")
    rows = (
        db.query(day_col, func.count(ChatMessage.id))
        .filter(ChatMessage.created_at >= cutoff)
        .group_by("day")
        .order_by("day")
        .all()
    )
    return [{"date": d or "unknown", "count": c} for d, c in rows]


def hourly_activity(db: Session, days: int = 7) -> list[dict]:
    """Message counts by hour of day (0-23) for the last N days."""
    cutoff = datetime.utcnow() - timedelta(days=days)
    rows = (
        db.query(
            func.strftime("%H", ChatMessage.created_at).label("hour"),
            func.count(ChatMessage.id),
        )
        .filter(ChatMessage.created_at >= cutoff)
        .group_by("hour")
        .order_by("hour")
        .all()
    )
    hour_map = {int(h): c for h, c in rows if h is not None}
    return [{"hour": h, "count": hour_map.get(h, 0)} for h in range(24)]


def action_type_stats(db: Session) -> dict:
    """Count messages by action_type."""
    rows = (
        db.query(ChatMessage.action_type, func.count(ChatMessage.id))
        .filter(ChatMessage.role == "assistant", ChatMessage.action_type.isnot(None))
        .group_by(ChatMessage.action_type)
        .all()
    )
    return {action or "unknown": count for action, count in rows}


def feature_usage(db: Session) -> dict:
    """Aggregate counts for key features: web_search, wellness, crisis, vision, intercom, general_chat."""
    stats = action_type_stats(db)
    return {
        "web_search": stats.get("web_search", 0),
        "wellness_support": stats.get("wellness_support", 0),
        "crisis_support": stats.get("crisis_support", 0),
        "vision": stats.get("vision", 0),
        "intercom_broadcast": stats.get("intercom_broadcast", 0),
        "general_chat": stats.get("general_chat", 0),
        "tool_actions": sum(
            v for k, v in stats.items()
            if k in ("add_chore", "list_chores", "list_chores_pending",
                      "mark_chore_done", "add_birthday", "list_birthdays",
                      "answer_from_docs", "pair_device")
        ),
    }


def response_time_trend(db: Session, days: int = 7) -> list[dict]:
    """Average response time per day from ChatLog (approximated from trace_id timestamps)."""
    if not _LATENCIES_MS:
        return []
    cutoff_ts = (datetime.utcnow() - timedelta(days=days)).timestamp()
    day_buckets: dict[str, list[int]] = {}
    for ts, ms in _LATENCIES_MS:
        if ts >= cutoff_ts:
            day = datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")
            day_buckets.setdefault(day, []).append(ms)
    return [
        {"date": d, "avg_ms": int(sum(vals) / len(vals)), "count": len(vals)}
        for d, vals in sorted(day_buckets.items())
    ]


def conversation_stats(db: Session) -> dict:
    """Conversation-level analytics."""
    total = db.query(Conversation).count()
    if total == 0:
        return {"total": 0, "avg_messages": 0, "longest": 0}

    msg_counts = (
        db.query(func.count(ChatMessage.id))
        .filter(ChatMessage.conversation_id.isnot(None))
        .group_by(ChatMessage.conversation_id)
        .all()
    )
    counts = [c for (c,) in msg_counts]
    return {
        "total": total,
        "avg_messages": round(sum(counts) / len(counts), 1) if counts else 0,
        "longest": max(counts) if counts else 0,
    }


def top_users(db: Session, limit: int = 5) -> list[dict]:
    """Top users by message count."""
    rows = (
        db.query(ChatMessage.convo_key, func.count(ChatMessage.id).label("cnt"))
        .filter(ChatMessage.convo_key.like("user:%"))
        .group_by(ChatMessage.convo_key)
        .order_by(func.count(ChatMessage.id).desc())
        .limit(limit)
        .all()
    )
    result = []
    for convo_key, cnt in rows:
        user_id = int(convo_key.replace("user:", "")) if convo_key.startswith("user:") else None
        name = "Unknown"
        if user_id:
            user = db.query(User).filter(User.id == user_id).first()
            if user:
                name = user.name
        result.append({"name": name, "messages": cnt})
    return result


def per_user_chore_stats(db: Session) -> list[dict]:
    """Chore completion stats per user (assigned chores)."""
    users = db.query(User).all()
    results = []
    for u in users:
        assigned = db.query(Chore).filter(Chore.assigned_to == u.id).count()
        done = db.query(Chore).filter(Chore.assigned_to == u.id, Chore.done == True).count()
        rate = round((done / assigned) * 100, 1) if assigned > 0 else 0
        results.append({
            "name": u.name, "assigned": assigned, "done": done,
            "rate": rate,
        })
    return results


def system_alerts(db: Session) -> list[dict]:
    """Generate system-level alerts for admin."""
    alerts = []

    overdue = db.query(Chore).filter(
        Chore.done == False, Chore.due_date != None,
        Chore.due_date < date.today(),
    ).count()
    if overdue:
        alerts.append({"level": "warning", "text": f"{overdue} chore{'s' if overdue != 1 else ''} overdue"})

    from .health import check_ollama
    ollama = check_ollama()
    if not ollama.get("ok"):
        alerts.append({"level": "error", "text": "Ollama is offline"})

    from . import openai_client
    if not openai_client.is_configured():
        alerts.append({"level": "info", "text": "No OpenAI/Groq API key configured"})

    rag = rag_stats()
    if not rag.get("available"):
        alerts.append({"level": "info", "text": "RAG knowledge base not ingested"})

    lat = latency_stats()
    if lat.get("p95_ms") and lat["p95_ms"] > 5000:
        alerts.append({"level": "warning", "text": f"High P95 latency: {lat['p95_ms']}ms"})

    if not alerts:
        alerts.append({"level": "ok", "text": "All systems operational"})

    return alerts


def admin_dashboard_json(db: Session) -> dict:
    """Full admin dashboard data as JSON for AJAX rendering."""
    from .health import check_db, check_ollama
    from . import openai_client

    return {
        "health": {
            "db": check_db(db),
            "ollama": check_ollama(),
            "openai_configured": openai_client.is_configured(),
            "openai_model": openai_client.OPENAI_MODEL,
        },
        "totals": total_stats(db),
        "counts": get_counts(db),
        "latency": latency_stats(),
        "latency_history": latency_history(),
        "model_stats": model_stats(db),
        "action_types": action_type_stats(db),
        "features": feature_usage(db),
        "messages_per_day": messages_per_day(db),
        "hourly_activity": hourly_activity(db),
        "response_time_trend": response_time_trend(db),
        "conversation_stats": conversation_stats(db),
        "top_users": top_users(db),
        "per_user_chores": per_user_chore_stats(db),
        "rag": rag_stats(),
        "alerts": system_alerts(db),
    }


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