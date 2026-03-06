import re
from dataclasses import dataclass
from datetime import date

@dataclass
class Action:
    type: str
    data: dict


def _strip_desktop_filler(text: str) -> str:
    """Strip common prefixes/suffixes so 'Um, can you open my notepad for me' -> 'open my notepad'."""
    t = text.strip()
    # Leading filler (order matters: longer phrases first); use re.IGNORECASE
    for prefix in (
        r"(?i)^(?:um|uh|well|so|like)\s*,?\s*",
        r"(?i)^(?:can you|could you|would you|will you|please)\s+",
        r"(?i)^(?:hey\s+)?chili\s*,?\s*",
        r"(?i)^(?:i need you to|i want you to|i'd like you to)\s+",
    ):
        t = re.sub(prefix, "", t)
    # Trailing filler
    t = re.sub(r"(?i)\s+(?:for me|for us|please|thanks|thank you|pls)\s*[.!?]*$", "", t)
    t = re.sub(r"\s*[.!?]+$", "", t)
    return t.strip()


def parse_message(text: str) -> Action:
    t = text.strip()

    # ── Instant replies (no LLM) ──

    # Greetings: hi, hello, hey, good morning/afternoon/evening, howdy
    if re.match(r"(?i)^\s*(hi|hello|hey|howdy|hi there|hey there|greetings?)\s*[!.]?\s*$", t):
        return Action(type="instant_greeting", data={})
    if re.match(r"(?i)^\s*(good\s+)?(morning|afternoon|evening)\s*[!.]?\s*$", t):
        return Action(type="instant_greeting", data={})

    # Thanks: thanks, thank you
    if re.match(r"(?i)^\s*(thanks?|thank\s+you)\s*[!.]?\s*$", t):
        return Action(type="instant_thanks", data={})

    # How are you / how's it going
    if re.match(r"(?i)^\s*(how\s+are\s+you|how('re|s)\s+it\s+going|how\s+do\s+you\s+do|what('s|s)\s+up)\s*[?.]?\s*$", t):
        return Action(type="instant_how_are_you", data={})

    # Time: what time is it, what's the time
    if re.match(r"(?i)^\s*(what('s|s)\s+)?(the\s+)?time\s*(\s+is\s+it)?\s*[?.]?\s*$", t):
        return Action(type="instant_get_time", data={})
    if re.match(r"(?i)^\s*time\s*[?.]?\s*$", t):
        return Action(type="instant_get_time", data={})

    # Weather: what's the weather, weather?
    if re.match(r"(?i)^\s*(what('s|s)\s+)?(the\s+)?weather\s*[?.]?\s*$", t):
        return Action(type="instant_get_weather", data={})
    if re.match(r"(?i)^\s*how('s|s)\s+the\s+weather\s*[?.]?\s*$", t):
        return Action(type="instant_get_weather", data={})

    # Add chore: "add chore take out trash" or "add a chore: take out trash"
    m = re.match(r"(?i)^\s*add\s+(a\s+)?chore\s*:?\s*(.+)$", t)
    if m:
        return Action(type="add_chore", data={"title": m.group(2).strip()})

    # List chores: "list chores" / "show chores"
    if re.match(r"(?i)^\s*(list|show)\s+chores\s*$", t):
        return Action(type="list_chores", data={})

    # List unfinished chores: "list unfinished chores"
    if re.match(r"(?i)^\s*(list|show)\s+(unfinished|pending)\s+chores\s*$", t):
        return Action(type="list_chores_pending", data={})

    # Mark done: "done 3" or "mark done 3"
    m = re.match(r"(?i)^\s*(done|mark\s+done)\s+(\d+)\s*$", t)
    if m:
        return Action(type="mark_chore_done", data={"id": int(m.group(2))})

    # Add birthday: "add birthday Mom 2026-05-12"
    m = re.match(r"(?i)^\s*add\s+birthday\s+(.+?)\s+(\d{4}-\d{2}-\d{2})\s*$", t)
    if m:
        name = m.group(1).strip()
        yyyy, mm, dd = map(int, m.group(2).split("-"))
        return Action(type="add_birthday", data={"name": name, "date": date(yyyy, mm, dd)})

    # List birthdays
    if re.match(r"(?i)^\s*(list|show)\s+birthdays\s*$", t):
        return Action(type="list_birthdays", data={})

    # Pair device: "pair", "pair my device", "link my device", "log in", "sign in"
    if re.match(r"(?i)^\s*(pair(\s+(my\s+)?device)?|link(\s+(my\s+)?device)?|log\s*in|sign\s*in)\s*$", t):
        return Action(type="pair_device", data={})

    # Intercom broadcast: "announce dinner is ready", "broadcast lights out"
    m = re.match(r"(?i)^\s*(announce|broadcast)\s*:?\s*(.+)$", t)
    if m:
        return Action(type="intercom_broadcast", data={"text": m.group(2).strip()})

    # Web search: "search for ...", "google ...", "look up ..."
    m = re.match(r"(?i)^\s*(?:search\s+(?:for\s+)?|google\s+|look\s+up\s+|web\s+search\s+(?:for\s+)?)(.+)$", t)
    if m:
        return Action(type="web_search", data={"query": m.group(1).strip()})

    # List projects: "list projects", "show projects", "my projects"
    if re.match(r"(?i)^\s*(list|show|my)\s+projects?\s*$", t):
        return Action(type="list_plan_projects", data={})

    # Create project: "create project Kitchen Reno", "new project Garden"
    m = re.match(r"(?i)^\s*(?:create|new|add)\s+project\s*:?\s*(.+)$", t)
    if m:
        return Action(type="add_plan_project", data={"name": m.group(1).strip()})

    # Add task to project: "add task Buy paint to Kitchen Reno"
    m = re.match(r"(?i)^\s*add\s+task\s+(.+?)\s+to\s+(?:project\s+)?(.+)$", t)
    if m:
        return Action(type="add_plan_task", data={"title": m.group(1).strip(), "project_name": m.group(2).strip()})

    # ── Desktop companion actions ──
    # Normalize conversational phrasing so "can you open my notepad for me" -> "open notepad"
    t_desktop = _strip_desktop_filler(t)

    # Open app: "open Notepad", "can you open my notepad for me", "please launch visual studio"
    m = re.match(
        r"(?i)^\s*(?:open|launch|start|run)\s+(?:the\s+)?(?:my\s+)?(.+?)(?:\s+app)?\s*$",
        t_desktop,
    )
    if m:
        app = re.sub(r"(?i)^my\s+", "", m.group(1).strip())
        if app and app.lower() not in ("browser", "url", "link", "a url", "a link"):
            return Action(type="desktop_open_app", data={"app_name": app})

    # Close app: "close Notepad", "quit Spotify", "could you close chrome for me"
    m = re.match(
        r"(?i)^\s*(?:close|quit|kill|exit|stop)\s+(?:the\s+)?(?:my\s+)?(.+?)(?:\s+app)?\s*$",
        t_desktop,
    )
    if m:
        app = re.sub(r"(?i)^my\s+", "", m.group(1).strip())
        if app:
            return Action(type="desktop_close_app", data={"app_name": app})

    # Play music: "play jazz on Spotify", "play lofi on YouTube", "play some music"
    m = re.match(r"(?i)^\s*play\s+(.+?)\s+(?:on|in|with|via)\s+(spotify|youtube)\s*$", t)
    if m:
        return Action(type="desktop_play_music", data={"query": m.group(1).strip(), "service": m.group(2).strip().lower()})
    m = re.match(r"(?i)^\s*play\s+(.+)$", t)
    if m:
        return Action(type="desktop_play_music", data={"query": m.group(1).strip(), "service": "spotify"})

    # Browser search: "search for weather", "google best pizza recipe"
    # (Note: the simpler web_search rule above catches "search for ..." already;
    #  these desktop variants target browser-based search specifically.)
    m = re.match(r"(?i)^\s*(?:browser\s+search|search\s+(?:in\s+)?(?:the\s+)?browser\s+(?:for\s+)?|google\s+in\s+browser\s+)(.+)$", t)
    if m:
        return Action(type="desktop_browser_search", data={"query": m.group(1).strip()})

    # Default
    return Action(type="unknown", data={"text": t})