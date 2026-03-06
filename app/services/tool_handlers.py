"""Registry of tool action handlers.

Each handler returns (reply, executed, action_type) or optionally
(reply, executed, action_type, client_action) where client_action is a dict
the desktop app should execute locally (open app, open URL, etc.).
"""
from __future__ import annotations

import random
from datetime import date, datetime
from typing import Any, Callable
from urllib.parse import quote_plus

from sqlalchemy.orm import Session

from ..models import Birthday, Chore, PlanProject, ProjectMember
from .. import web_search as web_search_module
from . import marketplace_service, planner_service
from ..modules import is_module_enabled

_HANDLERS: dict[str, Callable[..., tuple]] = {}

WRITE_ACTIONS = {
    "add_chore",
    "mark_chore_done",
    "add_birthday",
    # Planner write actions are only effective when the planner module is enabled.
    "add_plan_project",
    "add_plan_project_with_tasks",
    "add_plan_task",
    # Module install is a write action that can change server state.
    "install_module",
}


def register(action_type: str, module: str | None = None):
    def decorator(fn: Callable[..., tuple[str, bool, str]]):
        # Core handlers (no module) are always registered.
        # Module-scoped handlers (e.g. planner, intercom) only register when the
        # corresponding module is enabled via CHILI_MODULES.
        if module is None or is_module_enabled(module):
            _HANDLERS[action_type] = fn
        return fn
    return decorator


# ── Instant NLU replies (no LLM) ──

_GREETINGS = (
    "Hi! How can I help?",
    "Hey! What can I do for you?",
    "Hello! Ready when you are.",
    "Hi there! Need anything?",
)


@register("instant_greeting")
def _handle_instant_greeting(db: Session, action_data: dict, llm_reply: str, is_guest: bool, user_id: int | None) -> tuple[str, bool, str]:
    return random.choice(_GREETINGS), True, "instant_greeting"


@register("instant_thanks")
def _handle_instant_thanks(db: Session, action_data: dict, llm_reply: str, is_guest: bool, user_id: int | None) -> tuple[str, bool, str]:
    return "You're welcome! Anything else?", True, "instant_thanks"


@register("instant_how_are_you")
def _handle_instant_how_are_you(db: Session, action_data: dict, llm_reply: str, is_guest: bool, user_id: int | None) -> tuple[str, bool, str]:
    return "Doing great, thanks! How can I help you today?", True, "instant_how_are_you"


@register("instant_get_time")
def _handle_instant_get_time(db: Session, action_data: dict, llm_reply: str, is_guest: bool, user_id: int | None) -> tuple[str, bool, str]:
    now = datetime.now()
    return f"It's {now.strftime('%I:%M %p')}.", True, "instant_get_time"


@register("instant_get_weather")
def _handle_instant_get_weather(db: Session, action_data: dict, llm_reply: str, is_guest: bool, user_id: int | None) -> tuple[str, bool, str]:
    try:
        from .home_service import get_weather
        from ..config import settings
        w = get_weather(settings.weather_location or None)
        if w:
            desc = w.get("description", "") or "Clear"
            temp = w.get("temp_f", "")
            loc = w.get("location", "your area")
            return f"{desc}, {temp}°F in {loc}.", True, "instant_get_weather"
    except Exception:
        pass
    return "Set a default location in settings for quick weather, or ask \"what's the weather in Boston?\" and I'll look it up.", True, "instant_get_weather"


@register("add_chore")
def _handle_add_chore(db: Session, action_data: dict, llm_reply: str, is_guest: bool, user_id: int | None) -> tuple[str, bool, str]:
    title = action_data["title"]
    db.add(Chore(title=title, done=False))
    db.commit()
    if not llm_reply:
        llm_reply = f"Added chore: {title}"
    return llm_reply, True, "add_chore"


@register("list_chores")
def _handle_list_chores(db: Session, action_data: dict, llm_reply: str, is_guest: bool, user_id: int | None) -> tuple[str, bool, str]:
    chores = db.query(Chore).order_by(Chore.id.desc()).all()
    if not llm_reply:
        if chores:
            lines = [f"#{c.id} {'[done]' if c.done else '[todo]'} {c.title}" for c in chores]
            llm_reply = "Chores:\n" + "\n".join(lines)
        else:
            llm_reply = "No chores yet."
    return llm_reply, True, "list_chores"


@register("list_chores_pending")
def _handle_list_chores_pending(db: Session, action_data: dict, llm_reply: str, is_guest: bool, user_id: int | None) -> tuple[str, bool, str]:
    chores = db.query(Chore).filter(Chore.done == False).order_by(Chore.id.desc()).all()
    if not llm_reply:
        if chores:
            lines = [f"#{c.id} {c.title}" for c in chores]
            llm_reply = "Pending chores:\n" + "\n".join(lines)
        else:
            llm_reply = "No pending chores. Nice!"
    return llm_reply, True, "list_chores_pending"


@register("mark_chore_done")
def _handle_mark_chore_done(db: Session, action_data: dict, llm_reply: str, is_guest: bool, user_id: int | None) -> tuple[str, bool, str]:
    chore_id = action_data["id"]
    chore = db.query(Chore).filter(Chore.id == chore_id).first()
    if chore:
        chore.done = True
        db.commit()
        if not llm_reply:
            llm_reply = f"Marked chore #{chore_id} as done."
        return llm_reply, True, "mark_chore_done"
    if not llm_reply:
        llm_reply = f"Couldn't find chore #{chore_id}."
    return llm_reply, False, "mark_chore_done"


@register("add_birthday")
def _handle_add_birthday(db: Session, action_data: dict, llm_reply: str, is_guest: bool, user_id: int | None) -> tuple[str, bool, str]:
    name = action_data["name"]
    bday = date.fromisoformat(action_data["date"])
    db.add(Birthday(name=name, date=bday))
    db.commit()
    if not llm_reply:
        llm_reply = f"Added birthday: {name} on {bday.isoformat()}"
    return llm_reply, True, "add_birthday"


@register("list_birthdays")
def _handle_list_birthdays(db: Session, action_data: dict, llm_reply: str, is_guest: bool, user_id: int | None) -> tuple[str, bool, str]:
    birthdays = db.query(Birthday).order_by(Birthday.date.asc()).all()
    if not llm_reply:
        if birthdays:
            lines = [f"{b.name} - {b.date.isoformat()}" for b in birthdays]
            llm_reply = "Birthdays:\n" + "\n".join(lines)
        else:
            llm_reply = "No birthdays yet."
    return llm_reply, True, "list_birthdays"


@register("answer_from_docs")
def _handle_answer_from_docs(db: Session, action_data: dict, llm_reply: str, is_guest: bool, user_id: int | None) -> tuple[str, bool, str]:
    source = action_data.get("source", "")
    if source and llm_reply:
        llm_reply = f"{llm_reply}\n(source: {source})"
    return llm_reply, True, "answer_from_docs"


@register("pair_device")
def _handle_pair_device(db: Session, action_data: dict, llm_reply: str, is_guest: bool, user_id: int | None) -> tuple[str, bool, str]:
    if is_guest:
        llm_reply = (
            "To pair your device, click the **Link your device** banner at the top of this page. "
            "You'll enter the email your admin registered for you, receive a verification code, "
            "and you're in! You can also go to `/pair` for manual pairing."
        )
    else:
        llm_reply = "Your device is already paired! You're all set."
    return llm_reply, True, "pair_device"


@register("intercom_broadcast", module="intercom")
def _handle_intercom_broadcast(db: Session, action_data: dict, llm_reply: str, is_guest: bool, user_id: int | None) -> tuple[str, bool, str]:
    broadcast_text = action_data.get("text", "")
    if is_guest:
        llm_reply = "Intercom broadcast is only available for paired housemates."
    elif broadcast_text:
        llm_reply = (
            f'Broadcast queued: **"{broadcast_text}"**\n\n'
            "Open the [Intercom page](/intercom) to send voice broadcasts, "
            "or your housemates will hear this as a text notification."
        )
    else:
        llm_reply = "What would you like to announce? Try: `announce dinner is ready`"
    return llm_reply, True, "intercom_broadcast"


@register("web_search")
def _handle_web_search(db: Session, action_data: dict, llm_reply: str, is_guest: bool, user_id: int | None) -> tuple[str, bool, str]:
    query = action_data.get("query", "")
    if query:
        results = web_search_module.search(query)
        if results:
            formatted = web_search_module.format_results(results)
            llm_reply = f"Here's what I found for **\"{query}\"**:\n\n{formatted}"
        else:
            llm_reply = f"I searched for \"{query}\" but couldn't find any results. Try rephrasing your query."
        return llm_reply, True, "web_search"
    llm_reply = "What would you like me to search for?"
    return llm_reply, False, "web_search"


@register("add_plan_project", module="planner")
def _handle_add_plan_project(db: Session, action_data: dict, llm_reply: str, is_guest: bool, user_id: int | None) -> tuple[str, bool, str]:
    name = action_data.get("name", "")
    if name and not is_guest and user_id:
        planner_service.create_project(db, user_id, name)
        llm_reply = llm_reply or f'Created project **"{name}"**! View it in the [Project Planner](/planner).'
        return llm_reply, True, "add_plan_project"
    if is_guest:
        llm_reply = "Project management is only available for paired housemates."
    elif not user_id:
        llm_reply = "You need to be a paired housemate to create projects."
    else:
        llm_reply = "What would you like to name the project?"
    return llm_reply, False, "add_plan_project"


@register("add_plan_project_with_tasks", module="planner")
def _handle_add_plan_project_with_tasks(db: Session, action_data: dict, llm_reply: str, is_guest: bool, user_id: int | None) -> tuple[str, bool, str]:
    name = (action_data.get("name") or "").strip()
    description = (action_data.get("description") or "").strip()
    raw_tasks = action_data.get("tasks") or []
    if not isinstance(raw_tasks, list):
        raw_tasks = []
    task_specs = []
    for t in raw_tasks[:30]:
        if isinstance(t, str) and t.strip():
            task_specs.append({"title": t.strip(), "description": ""})
        elif isinstance(t, dict) and (t.get("title") or t.get("description")):
            task_specs.append({
                "title": (str(t.get("title", "")).strip() or "Task"),
                "description": str(t.get("description", "")).strip(),
            })
    if name and not is_guest and user_id:
        p = planner_service.create_project(db, user_id, name, description=description)
        added = 0
        for spec in task_specs:
            title = spec.get("title", "").strip()
            desc = (spec.get("description") or "").strip()
            if title:
                planner_service.create_task(db, p["id"], user_id, title, description=desc)
                added += 1
        if added:
            llm_reply = llm_reply or f'Created project **"{name}"** with {added} task(s). Open each task in the Planner to see complexity, duration, and reasoning. [Project Planner](/planner).'
        else:
            llm_reply = llm_reply or f'Created project **"{name}"**! View it in the [Project Planner](/planner).'
        return llm_reply, True, "add_plan_project_with_tasks"
    if is_guest:
        llm_reply = "Project management is only available for paired housemates."
    elif not user_id:
        llm_reply = "You need to be a paired housemate to create projects."
    else:
        llm_reply = "What would you like to name the project?"
    return llm_reply, False, "add_plan_project_with_tasks"


@register("add_plan_task", module="planner")
def _handle_add_plan_task(db: Session, action_data: dict, llm_reply: str, is_guest: bool, user_id: int | None) -> tuple[str, bool, str]:
    project_name = action_data.get("project_name", "")
    title = action_data.get("title", "")
    if project_name and title and not is_guest and user_id:
        project = (
            db.query(PlanProject)
            .join(ProjectMember, ProjectMember.project_id == PlanProject.id)
            .filter(ProjectMember.user_id == user_id, PlanProject.name.ilike(f"%{project_name}%"))
            .first()
        )
        if project:
            planner_service.create_task(db, project.id, user_id, title)
            llm_reply = llm_reply or f'Added task **"{title}"** to project **{project.name}**. View in [Project Planner](/planner).'
            return llm_reply, True, "add_plan_task"
        llm_reply = f'I couldn\'t find a project matching "{project_name}". Check your [Project Planner](/planner) or create one first.'
    elif is_guest:
        llm_reply = "Project management is only available for paired housemates."
    else:
        llm_reply = "Please specify both the task title and which project to add it to."
    return llm_reply, False, "add_plan_task"


@register("list_plan_projects", module="planner")
def _handle_list_plan_projects(db: Session, action_data: dict, llm_reply: str, is_guest: bool, user_id: int | None) -> tuple[str, bool, str]:
    if is_guest or not user_id:
        llm_reply = "Project management is only available for paired housemates."
    else:
        projects = planner_service.list_projects(db, user_id)
        if projects:
            lines = []
            for p in projects:
                pct = round(p["done_count"] / p["task_count"] * 100) if p["task_count"] else 0
                lines.append(f"- **{p['name']}** ({p['done_count']}/{p['task_count']} tasks, {pct}% done)")
            llm_reply = "Your projects:\n" + "\n".join(lines) + "\n\nView details in the [Project Planner](/planner)."
        else:
            llm_reply = "You don't have any projects yet. Create one in the [Project Planner](/planner) or say \"create project [name]\"."
    return llm_reply, True, "list_plan_projects"


@register("install_module")
def _handle_install_module(db: Session, action_data: dict, llm_reply: str, is_guest: bool, user_id: int | None) -> tuple[str, bool, str]:
    slug_raw = action_data.get("slug")
    slug = (str(slug_raw or "")).strip()
    if not slug:
        return "Which module would you like to install or enable?", False, "install_module"

    trace_id = "install_module_tool"
    try:
        mod, installed_now = marketplace_service.install_from_registry(db, slug, trace_id)
    except Exception as exc:
        # Surface a friendly error but avoid leaking internal details.
        return f"I couldn't install the module \"{slug}\": {exc}", False, "install_module"

    if installed_now:
        reply = (
            f'Installed and enabled module **"{mod.name}"** (slug: `{mod.slug}`). '
            "You may need to refresh this page to see its navigation link."
        )
    else:
        reply = (
            f'Module **"{mod.name}"** is now enabled. '
            "If it exposes a page, you should see it in the navigation."
        )

    return reply, True, "install_module"


# ── Desktop companion actions ────────────────────────────────────────────
# These return a 4th element: client_action dict for the desktop app.

@register("desktop_open_app")
def _handle_desktop_open_app(db: Session, action_data: dict, llm_reply: str, is_guest: bool, user_id: int | None):
    app_name = (action_data.get("app_name") or "").strip()
    if not app_name:
        return "Which app would you like me to open?", False, "desktop_open_app", None
    reply = llm_reply or f"Opening **{app_name}**."
    return reply, True, "desktop_open_app", {"type": "open_app", "app_name": app_name}


@register("desktop_close_app")
def _handle_desktop_close_app(db: Session, action_data: dict, llm_reply: str, is_guest: bool, user_id: int | None):
    app_name = (action_data.get("app_name") or "").strip()
    if not app_name:
        return "Which app would you like me to close?", False, "desktop_close_app", None
    reply = llm_reply or f"Closing **{app_name}**."
    return reply, True, "desktop_close_app", {"type": "close_app", "app_name": app_name}


@register("desktop_browser_search")
def _handle_desktop_browser_search(db: Session, action_data: dict, llm_reply: str, is_guest: bool, user_id: int | None):
    query = (action_data.get("query") or "").strip()
    if not query:
        return "What would you like me to search for?", False, "desktop_browser_search", None
    url = f"https://www.google.com/search?q={quote_plus(query)}"
    reply = llm_reply or f"Searching for **\"{query}\"** in your browser."
    return reply, True, "desktop_browser_search", {"type": "open_url", "url": url}


@register("desktop_open_url")
def _handle_desktop_open_url(db: Session, action_data: dict, llm_reply: str, is_guest: bool, user_id: int | None):
    url = (action_data.get("url") or "").strip()
    if not url:
        return "Which URL would you like me to open?", False, "desktop_open_url", None
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    reply = llm_reply or f"Opening **{url}** in your browser."
    return reply, True, "desktop_open_url", {"type": "open_url", "url": url}


@register("desktop_play_music")
def _handle_desktop_play_music(db: Session, action_data: dict, llm_reply: str, is_guest: bool, user_id: int | None):
    query = (action_data.get("query") or "").strip()
    service = (action_data.get("service") or "spotify").strip().lower()
    if not query:
        return "What would you like me to play?", False, "desktop_play_music", None
    encoded = quote_plus(query)
    if service == "youtube":
        url = f"https://www.youtube.com/results?search_query={encoded}"
        reply = llm_reply or f"Searching YouTube for **\"{query}\"**."
    else:
        url = f"https://open.spotify.com/search/{encoded}"
        reply = llm_reply or f"Searching Spotify for **\"{query}\"**."
    return reply, True, "desktop_play_music", {"type": "open_url", "url": url}


def execute_tool(
    db: Session,
    action_type: str,
    action_data: dict,
    llm_reply: str,
    is_guest: bool,
    user_id: int | None = None,
) -> tuple[str, bool, str, dict | None]:
    """Execute a tool action via registry.

    Returns (reply, executed, action_type, client_action).
    client_action is None for most actions; desktop_* handlers populate it
    so the Flutter desktop client can execute OS-level commands locally.
    """
    if is_guest and action_type in WRITE_ACTIONS:
        return "Guest mode is read-only. Click **Link your device** at the top to pair, or ask the admin to add you.", False, "guest_blocked", None

    handler = _HANDLERS.get(action_type)
    if handler:
        result = handler(db, action_data, llm_reply, is_guest, user_id)
        if len(result) == 4:
            return result
        return result[0], result[1], result[2], None
    return llm_reply, False, action_type, None
