"""Core chat logic shared by /api/chat and /api/chat/stream."""
import json as json_mod
from datetime import date

from sqlalchemy.orm import Session

from ..db import SessionLocal
from ..models import Chore, Birthday, ChatLog, ChatMessage, Conversation
from ..llm_planner import plan_action
from ..chili_nlu import parse_message as nlu_parse
from ..logger import log_info
from .. import rag as rag_module
from .. import personality as personality_module
from .. import web_search as web_search_module
from .. import memory as memory_module
from . import project_file_service as pfs_module


def nlu_fallback(message: str) -> dict | None:
    """Try the rule-based NLU parser as fallback when Ollama is offline.

    Returns a planner-compatible dict if a known action is matched, else None.
    """
    action = nlu_parse(message)
    if action.type != "unknown":
        return {"type": action.type, "data": action.data, "reply": ""}
    return None


def execute_tool(db: Session, action_type: str, action_data: dict, llm_reply: str, is_guest: bool):
    """Execute a tool action and return (reply, executed, action_type)."""
    WRITE_ACTIONS = {"add_chore", "mark_chore_done", "add_birthday"}
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
        db.query(ChatMessage).filter(mem_filter).order_by(ChatMessage.id.desc()).limit(12).all()
    ))

    return {"conversation_id": conversation_id, "recent": recent}


def plan_and_enrich(db: Session, message: str, identity: dict, recent, trace_id: str, project_id: int | None = None):
    """Run RAG search, personality lookup, and LLM planner. May raise if Ollama is offline."""
    is_guest = identity["is_guest"]
    user_id = identity.get("user_id")
    context = "\n".join([f"{m.role.upper()}: {m.content}" for m in recent])

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

    planned = plan_action(
        f"Conversation so far:\n{context}\n\nNew user message: {message}",
        rag_context=rag_context,
        personality_context=personality_context,
    )

    return {
        "planned": planned,
        "rag_context": rag_context,
        "rag_hits": rag_hits if rag_context else [],
        "personality_context": personality_context,
    }


def build_openai_prompt(user_name: str, personality_context: str | None, rag_context: str | None, base_system_prompt: str = "") -> str:
    """Build the OpenAI system prompt with personality and RAG context."""
    openai_system = base_system_prompt
    openai_system += f"\n\nYou are talking to: {user_name}."
    if personality_context:
        openai_system += f"\n\n{personality_context}"
    if rag_context:
        openai_system += f"\n\nHousehold document context (use ONLY if the user asks about these topics -- do NOT volunteer this info unprompted):\n{rag_context}"
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
