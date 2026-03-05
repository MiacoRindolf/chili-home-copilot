"""Core chat logic shared by /api/chat and /api/chat/stream."""
import json as json_mod
import re
from datetime import date, timedelta
from math import ceil

from sqlalchemy.orm import Session

from ..db import SessionLocal
from ..models import Chore, Birthday, ChatLog, ChatMessage, Conversation, PlanProject, PlanTask, ProjectMember
from ..llm_planner import plan_action
from ..chili_nlu import parse_message as nlu_parse
from ..logger import log_info
from .. import rag as rag_module
from .. import personality as personality_module
from .. import web_search as web_search_module
from .. import memory as memory_module
from .. import openai_client
from . import project_file_service as pfs_module
from . import planner_service


def nlu_fallback(message: str) -> dict | None:
    """Try the rule-based NLU parser as fallback when Ollama is offline.

    Returns a planner-compatible dict if a known action is matched, else None.
    """
    action = nlu_parse(message)
    if action.type != "unknown":
        return {"type": action.type, "data": action.data, "reply": ""}
    return None


# Pattern: "create/make (a) project (for me)? (for)? X" and message also mentions tasks
_CREATE_PROJECT_AND_TASKS = re.compile(
    r"(?i)\b(?:create|make)\s+(?:a\s+)?project\s+(?:for\s+(?:me\s+)?)?(?:for\s+)?([^!.\n]+?)(?:\s*[!.]|\s+also\s+add|\s+and\s+add|\s+add\s+in\s+the\s+tasks|\s*$)",
)
_ADD_TASKS_PATTERN = re.compile(
    r"(?i)\b(?:add\s+(?:in\s+)?the\s+)?tasks?\b|tasks?\s+(?:you\s+think\s+)?(?:i\s+)?need\s+to\s+do|add\s+in\s+the\s+tasks|suggest\s+tasks|tasks?\s+in\s+which|what\s+tasks?\s+(?:i\s+)?(?:need\s+to\s+)?do"
)


def detect_create_project_with_tasks_intent(message: str) -> tuple[bool, str | None]:
    """If the user clearly wants to create a project and add tasks but the planner returned unknown,
    return (True, project_name). Otherwise (False, None). Used as fallback so the project is actually created.
    """
    msg = (message or "").strip()
    if not msg or not _ADD_TASKS_PATTERN.search(msg):
        return False, None
    m = _CREATE_PROJECT_AND_TASKS.search(msg)
    if not m:
        return False, None
    name = m.group(1).strip()
    if len(name) < 2:
        return False, None
    # Title-case for display (e.g. "software engineering job hunting" -> "Software Engineering Job Hunting")
    name = name.title()
    return True, name


def generate_tasks_for_project(
    db: Session, project_id: int, project_name: str, user_id: int, trace_id: str,
) -> int:
    """Use the cloud LLM to suggest tasks with well-researched ETAs, then create them with start/end dates for Gantt. Returns number of tasks added."""
    if not openai_client.is_configured():
        return 0
    today = date.today().isoformat()
    prompt = (
        f'For a project called "{project_name}", suggest 6 to 12 concrete, actionable tasks. '
        'Use well-researched, realistic time estimates (industry benchmarks, common studies: e.g. resume update 2-4 hours, job application 1-2 hours each, interview prep 3-5 hours). '
        'Return ONLY a JSON array. Each object must have: "title" (string), "description" (string, include Complexity, Duration, Reasoning), and "estimated_days" (number, working days to complete). '
        'estimated_days: use decimals for part-days (e.g. 0.25 = ~2 hours, 0.5 = half day, 1 = one full day). Minimum 0.25. Be accurate based on typical task duration research. '
        f'Today is {today}. Tasks will be scheduled sequentially starting from today. '
        'Example: [{"title": "Update resume", "description": "Complexity: Low. Duration: 2-3 hours. Reasoning: ATS-friendly resume increases callback rate.", "estimated_days": 0.25}, {"title": "Apply to 5 target companies", "description": "Complexity: Medium. Duration: 5-10 hours total. Reasoning: Quality applications take 1-2 hrs each (research, tailoring).", "estimated_days": 1.5}]'
    )
    try:
        result = openai_client.chat(
            messages=[{"role": "user", "content": prompt}],
            system_prompt="You are a project planning assistant. Return only a valid JSON array. Every task must have title, description, and estimated_days (number).",
            trace_id=trace_id,
        )
        text = (result.get("reply") or "").strip()
    except Exception as e:
        log_info(trace_id, f"generate_tasks_for_project_error={e}")
        return 0
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return 0
    try:
        items = json_mod.loads(text[start : end + 1])
    except json_mod.JSONDecodeError:
        return 0
    if not isinstance(items, list):
        return 0
    cursor = date.today()
    added = 0
    for item in items[:20]:
        if isinstance(item, dict) and item.get("title"):
            title = str(item.get("title", "")).strip()
            desc = str(item.get("description", "")).strip()
            raw_days = item.get("estimated_days")
            try:
                days = max(0.25, min(365, float(raw_days))) if raw_days is not None else 1.0
            except (TypeError, ValueError):
                days = 1.0
            task_start = cursor
            # Span in calendar days (min 1 so the Gantt bar is visible)
            span_days = max(1, int(ceil(days)))
            task_end = cursor + timedelta(days=span_days - 1)
            cursor = task_end + timedelta(days=1)
            start_str = task_start.isoformat()
            end_str = task_end.isoformat()
            if title and planner_service.create_task(
                db, project_id, user_id, title,
                description=desc,
                start_date=start_str,
                end_date=end_str,
            ):
                added += 1
        elif isinstance(item, str) and item.strip():
            start_str = cursor.isoformat()
            end_str = cursor.isoformat()
            if planner_service.create_task(
                db, project_id, user_id, item.strip(),
                description="",
                start_date=start_str,
                end_date=end_str,
            ):
                added += 1
                cursor += timedelta(days=1)
    return added


def execute_tool(db: Session, action_type: str, action_data: dict, llm_reply: str, is_guest: bool, user_id: int | None = None):
    """Execute a tool action and return (reply, executed, action_type)."""
    WRITE_ACTIONS = {"add_chore", "mark_chore_done", "add_birthday", "add_plan_project", "add_plan_project_with_tasks", "add_plan_task"}
    if is_guest and action_type in WRITE_ACTIONS:
        return "Guest mode is read-only. Click **Link your device** at the top to pair, or ask the admin to add you.", False, "guest_blocked"

    executed = False

    if action_type == "add_chore":
        title = action_data["title"]
        db.add(Chore(title=title, done=False))
        db.commit()
        executed = True
        if not llm_reply:
            llm_reply = f"Added chore: {title}"

    elif action_type == "list_chores":
        chores = db.query(Chore).order_by(Chore.id.desc()).all()
        executed = True
        if not llm_reply:
            if chores:
                lines = [f"#{c.id} {'[done]' if c.done else '[todo]'} {c.title}" for c in chores]
                llm_reply = "Chores:\n" + "\n".join(lines)
            else:
                llm_reply = "No chores yet."

    elif action_type == "list_chores_pending":
        chores = db.query(Chore).filter(Chore.done == False).order_by(Chore.id.desc()).all()
        executed = True
        if not llm_reply:
            if chores:
                lines = [f"#{c.id} {c.title}" for c in chores]
                llm_reply = "Pending chores:\n" + "\n".join(lines)
            else:
                llm_reply = "No pending chores. Nice!"

    elif action_type == "mark_chore_done":
        chore_id = action_data["id"]
        chore = db.query(Chore).filter(Chore.id == chore_id).first()
        if chore:
            chore.done = True
            db.commit()
            executed = True
            if not llm_reply:
                llm_reply = f"Marked chore #{chore_id} as done."
        else:
            if not llm_reply:
                llm_reply = f"Couldn't find chore #{chore_id}."

    elif action_type == "add_birthday":
        name = action_data["name"]
        bday = date.fromisoformat(action_data["date"])
        db.add(Birthday(name=name, date=bday))
        db.commit()
        executed = True
        if not llm_reply:
            llm_reply = f"Added birthday: {name} on {bday.isoformat()}"

    elif action_type == "list_birthdays":
        birthdays = db.query(Birthday).order_by(Birthday.date.asc()).all()
        executed = True
        if not llm_reply:
            if birthdays:
                lines = [f"{b.name} - {b.date.isoformat()}" for b in birthdays]
                llm_reply = "Birthdays:\n" + "\n".join(lines)
            else:
                llm_reply = "No birthdays yet."

    elif action_type == "answer_from_docs":
        executed = True
        source = action_data.get("source", "")
        if source and llm_reply:
            llm_reply = f"{llm_reply}\n(source: {source})"

    elif action_type == "pair_device":
        executed = True
        if is_guest:
            llm_reply = (
                "To pair your device, click the **Link your device** banner at the top of this page. "
                "You'll enter the email your admin registered for you, receive a verification code, "
                "and you're in! You can also go to `/pair` for manual pairing."
            )
        else:
            llm_reply = "Your device is already paired! You're all set."

    elif action_type == "intercom_broadcast":
        executed = True
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

    elif action_type == "web_search":
        query = action_data.get("query", "")
        if query:
            results = web_search_module.search(query)
            executed = True
            if results:
                formatted = web_search_module.format_results(results)
                llm_reply = f"Here's what I found for **\"{query}\"**:\n\n{formatted}"
            else:
                llm_reply = f"I searched for \"{query}\" but couldn't find any results. Try rephrasing your query."
        else:
            llm_reply = "What would you like me to search for?"

    elif action_type == "add_plan_project":
        name = action_data.get("name", "")
        if name and not is_guest and user_id:
            p = planner_service.create_project(db, user_id, name)
            executed = True
            llm_reply = llm_reply or f'Created project **"{name}"**! View it in the [Project Planner](/planner).'
        elif is_guest:
            llm_reply = "Project management is only available for paired housemates."
        elif not user_id:
            llm_reply = "You need to be a paired housemate to create projects."
        else:
            llm_reply = "What would you like to name the project?"

    elif action_type == "add_plan_project_with_tasks":
        name = (action_data.get("name") or "").strip()
        description = (action_data.get("description") or "").strip()
        raw_tasks = action_data.get("tasks") or []
        if not isinstance(raw_tasks, list):
            raw_tasks = []
        # Each task can be str (title only) or dict with title + description (complexity, duration, reasoning)
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
            executed = True
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
        elif is_guest:
            llm_reply = "Project management is only available for paired housemates."
        elif not user_id:
            llm_reply = "You need to be a paired housemate to create projects."
        else:
            llm_reply = "What would you like to name the project?"

    elif action_type == "add_plan_task":
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
                t = planner_service.create_task(db, project.id, user_id, title)
                executed = True
                llm_reply = llm_reply or f'Added task **"{title}"** to project **{project.name}**. View in [Project Planner](/planner).'
            else:
                llm_reply = f'I couldn\'t find a project matching "{project_name}". Check your [Project Planner](/planner) or create one first.'
        elif is_guest:
            llm_reply = "Project management is only available for paired housemates."
        else:
            llm_reply = "Please specify both the task title and which project to add it to."

    elif action_type == "list_plan_projects":
        executed = True
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

    return llm_reply, executed, action_type


def init_chat(db: Session, convo_key: str, conversation_id, message: str, identity: dict, trace_id: str, image_path: str | None = None, project_id: int | None = None):
    """Create conversation if needed, store user message, load memory. Always safe (no LLM call)."""
    is_guest = identity["is_guest"]

    if not is_guest and conversation_id is None:
        convo = Conversation(convo_key=convo_key, title="New Chat", project_id=project_id)
        db.add(convo)
        db.commit()
        db.refresh(convo)
        conversation_id = convo.id

    db.add(ChatMessage(convo_key=convo_key, conversation_id=conversation_id, role="user", content=message, trace_id=trace_id, image_path=image_path))
    db.commit()

    mem_filter = ChatMessage.conversation_id == conversation_id if conversation_id else ChatMessage.convo_key == convo_key
    recent = list(reversed(
        db.query(ChatMessage).filter(mem_filter).order_by(ChatMessage.id.desc()).limit(24).all()
    ))

    return {"conversation_id": conversation_id, "recent": recent}


def plan_and_enrich(
    db: Session,
    message: str,
    identity: dict,
    recent,
    trace_id: str,
    project_id: int | None = None,
    planner_current_project: dict | None = None,
):
    """Run RAG search, personality lookup, and LLM planner. May raise if Ollama is offline.
    planner_current_project: optional {"name": str, "id": int} when user is on planner page with a project selected."""
    is_guest = identity["is_guest"]
    user_id = identity.get("user_id")
    context = "\n".join([f"{m.role.upper()}: {m.content}" for m in recent])

    if planner_current_project and planner_current_project.get("name"):
        message = (
            message
            + "\n\n[Planner context: User is on the Planner page viewing project \""
            + str(planner_current_project.get("name", ""))
            + "\" (id "
            + str(planner_current_project.get("id", ""))
            + "). When they say 'add a task' or 'add task X' without naming a project, use project_name=\""
            + str(planner_current_project.get("name", ""))
            + "\".]"
        )
        log_info(trace_id, f"planner_current_project_injected name={planner_current_project.get('name')}")

    rag_context = None
    rag_hits = rag_module.search(message, n_results=3, trace_id=trace_id)
    if rag_hits and rag_hits[0]["distance"] < 1.0:
        rag_context = "\n---\n".join(f"[{h['source']}]: {h['text']}" for h in rag_hits)
        log_info(trace_id, f"rag_context_injected sources={[h['source'] for h in rag_hits]}")

    if project_id:
        proj_hits = pfs_module.search_project(project_id, message, n_results=3, trace_id=trace_id)
        if proj_hits:
            proj_context = "\n---\n".join(f"[project:{h['source']}]: {h['text']}" for h in proj_hits)
            rag_context = f"{proj_context}\n---\n{rag_context}" if rag_context else proj_context
            rag_hits = proj_hits + (rag_hits or [])
            log_info(trace_id, f"project_rag_injected project={project_id} sources={[h['source'] for h in proj_hits]}")

    personality_context = None
    memory_context = None
    if user_id and not is_guest:
        personality_context = personality_module.get_profile_context(user_id, db)
        if personality_context:
            log_info(trace_id, f"personality_injected user_id={user_id}")
        memory_context = memory_module.get_memory_context(user_id, db)
        if memory_context:
            log_info(trace_id, f"memory_context_injected user_id={user_id}")
            if personality_context:
                personality_context += "\n\n" + memory_context
            else:
                personality_context = memory_context

    project_context = None
    if user_id and not is_guest:
        project_context = planner_service.get_user_project_summary(db, user_id)
        if project_context:
            log_info(trace_id, f"project_context_injected user_id={user_id}")

    planned = plan_action(
        f"Conversation so far:\n{context}\n\nNew user message: {message}",
        rag_context=rag_context,
        personality_context=personality_context,
        project_context=project_context,
    )

    return {
        "planned": planned,
        "rag_context": rag_context,
        "rag_hits": rag_hits if rag_context else [],
        "personality_context": personality_context,
    }


PLANNER_PAGE_CONTEXT = """
LOCATION: The user is chatting from the **Project Planner** page (the right-hand CHILI panel on the planner).

When they ask what you can do, what you can help with, what your capabilities are, or what you can do "here" / "on this page", **lead with your planner-related capabilities**:
- **Create projects** (e.g. "create a project for kitchen reno")
- **Add tasks** to the current project or a named one (e.g. "add task buy paint", "add task call contractor to Kitchen")
- **List projects and tasks** (e.g. "what are my projects?", "what's left to do?")
- **Assign tasks** to housemates, **set due dates**, and **manage the plan from chat**

Then mention you can also help with general household stuff (chores, birthdays, recipes, research, etc.). Keep the reply focused and scannable (bullets or short list). Do NOT give a long generic list that ignores the planner.

When you need more info to act (e.g. project name, task details, timeline), ask one short clarifying question instead of guessing.
"""


def build_openai_prompt(
    user_name: str,
    personality_context: str | None,
    rag_context: str | None,
    base_system_prompt: str = "",
    planner_context: bool = False,
) -> str:
    """Build the OpenAI system prompt with personality, RAG, and optional planner-page context."""
    openai_system = base_system_prompt
    openai_system += f"\n\nYou are talking to: {user_name}."
    if personality_context:
        openai_system += f"\n\n{personality_context}"
    if rag_context:
        openai_system += f"\n\nHousehold document context (use ONLY if the user asks about these topics -- do NOT volunteer this info unprompted):\n{rag_context}"
    if planner_context:
        openai_system += "\n\n" + PLANNER_PAGE_CONTEXT
    return openai_system


def sse_event(data: dict) -> str:
    """Format a dict as an SSE data line."""
    return f"data: {json_mod.dumps(data)}\n\n"


def store_and_title(convo_key, conversation_id, content, trace_id, action_type, model_used, client_ip, message):
    """Store assistant message and auto-title in a fresh DB session (safe for generators)."""
    s = SessionLocal()
    try:
        s.add(ChatMessage(
            convo_key=convo_key, conversation_id=conversation_id,
            role="assistant", content=content, trace_id=trace_id,
            action_type=action_type, model_used=model_used,
        ))
        if conversation_id:
            c = s.query(Conversation).filter(Conversation.id == conversation_id).first()
            if c and c.title == "New Chat":
                c.title = message[:40].strip() + ("..." if len(message) > 40 else "")
        s.add(ChatLog(client_ip=client_ip, trace_id=trace_id, message=message, action_type=action_type))
        s.commit()
    finally:
        s.close()


def try_personality_update(user_id, is_guest, db, trace_id):
    """Check if personality profile needs updating, and extract if so."""
    if user_id and not is_guest:
        try:
            if personality_module.should_update(user_id, db):
                personality_module.extract_profile(user_id, db, trace_id=trace_id)
        except Exception as e:
            log_info(trace_id, f"personality_extraction_error={e}")


def try_memory_extraction(
    user_id, is_guest, user_message, assistant_reply, action_type, db, trace_id,
    source_message_id=None,
):
    """Extract personal facts from a conversation turn (non-blocking)."""
    if not user_id or is_guest:
        return
    try:
        memory_module.extract_facts(
            user_message=user_message,
            assistant_reply=assistant_reply,
            user_id=user_id,
            db=db,
            action_type=action_type,
            source_message_id=source_message_id,
            trace_id=trace_id,
        )
    except Exception as e:
        log_info(trace_id, f"memory_extraction_error={e}")


def store_and_title_with_memory(
    convo_key, conversation_id, content, trace_id, action_type, model_used,
    client_ip, message, user_id=None, is_guest=True,
):
    """Store assistant message, auto-title, and extract memories (for streaming generators)."""
    s = SessionLocal()
    try:
        s.add(ChatMessage(
            convo_key=convo_key, conversation_id=conversation_id,
            role="assistant", content=content, trace_id=trace_id,
            action_type=action_type, model_used=model_used,
        ))
        if conversation_id:
            c = s.query(Conversation).filter(Conversation.id == conversation_id).first()
            if c and c.title == "New Chat":
                c.title = message[:40].strip() + ("..." if len(message) > 40 else "")
        s.add(ChatLog(client_ip=client_ip, trace_id=trace_id, message=message, action_type=action_type))
        s.commit()

        if user_id and not is_guest:
            try:
                memory_module.extract_facts(
                    user_message=message,
                    assistant_reply=content,
                    user_id=user_id,
                    db=s,
                    action_type=action_type,
                    trace_id=trace_id,
                )
            except Exception as e:
                log_info(trace_id, f"memory_extraction_error={e}")

            try:
                if personality_module.should_update(user_id, s):
                    personality_module.extract_profile(user_id, s, trace_id=trace_id)
            except Exception as e:
                log_info(trace_id, f"personality_extraction_error={e}")
    finally:
        s.close()
