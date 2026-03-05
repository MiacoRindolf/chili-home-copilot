"""Home dashboard service: aggregation, weather, activity feed, insights, calendar."""
import logging
from datetime import date, datetime, timedelta
from typing import Optional

import requests
from sqlalchemy.orm import Session
from sqlalchemy import func, desc

from ..config import settings
from ..models import (
    Chore, Birthday, User, UserStatus, ChatMessage,
    Conversation, ActivityLog, UserMemory,
)

log = logging.getLogger(__name__)

# ── Weather (free wttr.in API, no key needed) ────────────────────────────────

_WEATHER_CACHE: dict = {"data": None, "expires": 0.0}


def get_weather(location: str | None = None) -> dict | None:
    loc = location or settings.weather_location
    if not loc:
        return None

    now = datetime.now().timestamp()
    if _WEATHER_CACHE["data"] and now < _WEATHER_CACHE["expires"]:
        return _WEATHER_CACHE["data"]

    try:
        url = f"https://wttr.in/{loc}?format=j1"
        resp = requests.get(url, timeout=5, headers={"User-Agent": "chili-home"})
        resp.raise_for_status()
        data = resp.json()
        current = data.get("current_condition", [{}])[0]
        result = {
            "temp_f": current.get("temp_F", ""),
            "temp_c": current.get("temp_C", ""),
            "feels_like_f": current.get("FeelsLikeF", ""),
            "feels_like_c": current.get("FeelsLikeC", ""),
            "description": current.get("weatherDesc", [{}])[0].get("value", ""),
            "humidity": current.get("humidity", ""),
            "wind_mph": current.get("windspeedMiles", ""),
            "icon": _weather_icon(current.get("weatherCode", "")),
            "location": loc,
        }
        _WEATHER_CACHE["data"] = result
        _WEATHER_CACHE["expires"] = now + 1800  # 30 min cache
        return result
    except Exception as e:
        log.warning("weather_fetch_failed: %s", e)
        return _WEATHER_CACHE.get("data")


def _weather_icon(code: str) -> str:
    code = str(code)
    sunny = {"113"}
    cloudy = {"116", "119", "122"}
    rain = {"176", "263", "266", "293", "296", "299", "302", "305", "308", "353", "356", "359"}
    snow = {"179", "182", "185", "227", "230", "323", "326", "329", "332", "335", "338", "368", "371", "374", "377"}
    thunder = {"200", "386", "389", "392", "395"}
    if code in sunny:
        return "sun"
    if code in cloudy:
        return "cloud"
    if code in rain:
        return "rain"
    if code in snow:
        return "snow"
    if code in thunder:
        return "thunder"
    return "cloud"


# ── Activity Feed ─────────────────────────────────────────────────────────────

def log_activity(
    db: Session, event_type: str, description: str,
    user_id: int | None = None, user_name: str | None = None,
    icon: str = "",
):
    entry = ActivityLog(
        user_id=user_id,
        user_name=user_name or "",
        event_type=event_type,
        description=description,
        icon=icon,
    )
    db.add(entry)
    db.commit()


def get_activity_feed(db: Session, limit: int = 20, before_id: int | None = None) -> list[dict]:
    q = db.query(ActivityLog).order_by(desc(ActivityLog.id))
    if before_id:
        q = q.filter(ActivityLog.id < before_id)
    entries = q.limit(limit).all()
    return [
        {
            "id": e.id,
            "event_type": e.event_type,
            "description": e.description,
            "user_name": e.user_name or "",
            "icon": e.icon,
            "created_at": e.created_at.isoformat() if e.created_at else "",
        }
        for e in entries
    ]


# ── Smart Insights ────────────────────────────────────────────────────────────

def get_insights(db: Session, user_id: int | None = None) -> list[dict]:
    insights = []

    overdue = db.query(Chore).filter(
        Chore.done == False,
        Chore.due_date != None,
        Chore.due_date < date.today(),
    ).count()
    if overdue:
        insights.append({
            "type": "warning",
            "icon": "alert",
            "text": f"{overdue} chore{'s' if overdue != 1 else ''} overdue",
        })

    pending = db.query(Chore).filter(Chore.done == False).count()
    if pending == 0:
        insights.append({
            "type": "success",
            "icon": "check",
            "text": "All chores are done! Great job!",
        })
    elif pending >= 5:
        insights.append({
            "type": "info",
            "icon": "list",
            "text": f"{pending} chores pending - time to get busy!",
        })

    today = date.today()
    upcoming = db.query(Birthday).all()
    for b in upcoming:
        this_year = b.date.replace(year=today.year)
        if this_year < today:
            this_year = this_year.replace(year=today.year + 1)
        days = (this_year - today).days
        if days == 0:
            insights.append({
                "type": "celebration",
                "icon": "cake",
                "text": f"It's {b.name}'s birthday today!",
            })
        elif days <= 3:
            insights.append({
                "type": "reminder",
                "icon": "calendar",
                "text": f"{b.name}'s birthday is in {days} day{'s' if days != 1 else ''}",
            })

    due_today = db.query(Chore).filter(
        Chore.done == False,
        Chore.due_date == today,
    ).count()
    if due_today:
        insights.append({
            "type": "info",
            "icon": "clock",
            "text": f"{due_today} chore{'s' if due_today != 1 else ''} due today",
        })

    if user_id:
        memories = db.query(UserMemory).filter(
            UserMemory.user_id == user_id,
            UserMemory.superseded == False,
        ).count()
        if memories and memories % 10 == 0:
            insights.append({
                "type": "info",
                "icon": "brain",
                "text": f"CHILI has learned {memories} things about you!",
            })

    return insights


# ── Calendar Data ─────────────────────────────────────────────────────────────

def days_until(bday_date: date) -> int:
    today = date.today()
    this_year = bday_date.replace(year=today.year)
    if this_year < today:
        this_year = this_year.replace(year=today.year + 1)
    return (this_year - today).days


def get_calendar_events(db: Session, year: int, month: int) -> list[dict]:
    from calendar import monthrange
    _, last_day = monthrange(year, month)
    start = date(year, month, 1)
    end = date(year, month, last_day)

    events = []

    chores = db.query(Chore).filter(
        Chore.due_date != None,
        Chore.due_date >= start,
        Chore.due_date <= end,
    ).all()
    for c in chores:
        events.append({
            "date": c.due_date.isoformat(),
            "type": "chore",
            "title": c.title,
            "done": c.done,
            "priority": c.priority or "medium",
        })

    birthdays = db.query(Birthday).all()
    for b in birthdays:
        bday_this = b.date.replace(year=year)
        if start <= bday_this <= end:
            events.append({
                "date": bday_this.isoformat(),
                "type": "birthday",
                "title": f"{b.name}'s birthday",
            })

    return sorted(events, key=lambda e: e["date"])


# ── Dashboard Summary ─────────────────────────────────────────────────────────

def get_dashboard_data(db: Session, identity: dict) -> dict:
    chores = db.query(Chore).order_by(Chore.id.desc()).all()
    pending_chores = sum(1 for c in chores if not c.done)
    overdue_chores = sum(
        1 for c in chores
        if not c.done and c.due_date and c.due_date < date.today()
    )

    birthdays = db.query(Birthday).all()
    bday_list = sorted(
        [
            {
                "id": b.id, "name": b.name,
                "date": b.date.isoformat(),
                "days_until": days_until(b.date),
            }
            for b in birthdays
        ],
        key=lambda x: x["days_until"],
    )
    upcoming_bdays = sum(1 for b in bday_list if b["days_until"] <= 7)

    chore_list = []
    for c in chores:
        assignee_name = ""
        if c.assigned_to and c.assignee:
            assignee_name = c.assignee.name
        chore_list.append({
            "id": c.id, "title": c.title, "done": c.done,
            "priority": c.priority or "medium",
            "due_date": c.due_date.isoformat() if c.due_date else None,
            "recurrence": c.recurrence or "none",
            "assigned_to": c.assigned_to,
            "assignee_name": assignee_name,
            "created_at": c.created_at.isoformat() if c.created_at else None,
            "completed_at": c.completed_at.isoformat() if c.completed_at else None,
        })

    housemates = []
    housemates_online = 0
    users_list = []
    if not identity["is_guest"]:
        users = db.query(User).all()
        users_list = [{"id": u.id, "name": u.name} for u in users]
        for u in users:
            if u.id == identity.get("user_id"):
                continue
            st = db.query(UserStatus).filter(UserStatus.user_id == u.id).first()
            status = "available"
            if st:
                if st.status == "dnd" and st.dnd_until and datetime.utcnow() > st.dnd_until:
                    status = "available"
                else:
                    status = st.status
            housemates.append({"name": u.name, "status": status})
            if status == "available":
                housemates_online += 1

    today = date.today()
    week_ago = today - timedelta(days=7)
    chores_done_week = db.query(Chore).filter(
        Chore.done == True,
        Chore.completed_at != None,
        func.date(Chore.completed_at) >= week_ago,
    ).count()

    total_conversations = 0
    if not identity["is_guest"]:
        convo_key = f"user:{identity['user_id']}"
        total_conversations = db.query(Conversation).filter(
            Conversation.convo_key == convo_key,
        ).count()

    return {
        "chores": chore_list,
        "birthdays": bday_list,
        "pending_chores": pending_chores,
        "overdue_chores": overdue_chores,
        "upcoming_bdays": upcoming_bdays,
        "housemates": housemates,
        "housemates_online": housemates_online,
        "users": users_list,
        "chores_done_week": chores_done_week,
        "total_conversations": total_conversations,
        "insights": get_insights(db, identity.get("user_id")),
        "weather": get_weather(),
    }
