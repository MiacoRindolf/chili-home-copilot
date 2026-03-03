import re
from dataclasses import dataclass
from datetime import date

@dataclass
class Action:
    type: str
    data: dict

def parse_message(text: str) -> Action:
    t = text.strip()

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

    # Default
    return Action(type="unknown", data={"text": t})