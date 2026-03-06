"""Core chat logic shared by /api/chat and /api/chat/stream."""
import json as json_mod
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

from sqlalchemy.orm import Session

from ..db import SessionLocal

# In-memory LRU-style cache for repeated/greeting-like queries (max 100 entries)
_response_cache: dict[str, str] = {}
_RESPONSE_CACHE_MAX = 100


def get_cached_reply(message: str) -> str | None:
    """Return cached reply for a normalized message, or None."""
    key = message.lower().strip()[:200]
    return _response_cache.get(key)


def set_cached_reply(message: str, reply: str) -> None:
    """Store reply in cache; evict oldest if at capacity."""
    key = message.lower().strip()[:200]
    if len(_response_cache) >= _RESPONSE_CACHE_MAX:
        _response_cache.pop(next(iter(_response_cache)))
    _response_cache[key] = reply
from ..models import ChatLog, ChatMessage, Conversation
from ..llm_planner import plan_action
from ..chili_nlu import parse_message as nlu_parse
from ..logger import log_info
from .. import rag as rag_module
from .. import personality as personality_module
from .. import web_search as web_search_module
from .. import memory as memory_module
from .. import openai_client
from ..modules import is_module_enabled
from . import project_file_service as pfs_module
from . import planner_service
from .tool_handlers import execute_tool


def nlu_fallback(message: str) -> dict | None:
    """Try the rule-based NLU parser as fallback when Ollama is offline.

    Returns a planner-compatible dict if a known action is matched, else None.
    """
    action = nlu_parse(message)
    if action.type != "unknown":
        return {"type": action.type, "data": action.data, "reply": ""}
    return None


if is_module_enabled("planner"):
    from ..modules.planner import hooks as planner_hooks  # type: ignore[assignment]
else:
    planner_hooks = None


def resolve_response(
    db: Session,
    message: str,
    recent: list,
    identity: dict,
    ctx: dict | None,
    on_planner_page: bool,
    trace_id: str,
    stream: bool = False,
) -> dict:
    """Compute reply, action_type, executed, model_used, rag_sources, personality_used.
    When ctx is None (plan_and_enrich failed), runs fallback (create project, NLU, or OpenAI).
    When stream=True and response would be from OpenAI, returns continue_stream=True so router can call chat_stream."""
    user_name = identity["user_name"]
    is_guest = identity["is_guest"]
    user_id = identity.get("user_id")
    rag_sources = []
    personality_used = False

    if ctx is None:
        # Exception path: Ollama failed
        if planner_hooks and not is_guest and user_id and is_module_enabled("planner"):
            ok, project_name = planner_hooks.detect_create_project_with_tasks_intent(message)
            if ok and project_name:
                llm_reply, executed, action_type, client_action = execute_tool(
                    db, "add_plan_project_with_tasks",
                    {"name": project_name, "description": "", "tasks": []},
                    "", is_guest, user_id=user_id,
                )
                if executed and openai_client.is_configured() and planner_hooks:
                    projects = planner_service.list_projects(db, user_id)
                    proj = next((p for p in projects if p["name"] == project_name), None)
                    if proj:
                        added = planner_hooks.generate_tasks_for_project(db, proj["id"], project_name, user_id, trace_id)
                        if added:
                            llm_reply = f'Created project **"{project_name}"** with {added} task(s). Open each task in the Planner to see complexity, duration, and reasoning. [Project Planner](/planner).'
                return {"reply": llm_reply, "action_type": action_type, "executed": executed, "model_used": "fallback", "rag_sources": [], "personality_used": False, "client_action": client_action}

        nlu_result = nlu_fallback(message)
        if nlu_result:
            llm_reply, executed, action_type, client_action = execute_tool(db, nlu_result["type"], nlu_result["data"], "", is_guest, user_id=user_id)
            return {"reply": llm_reply, "action_type": action_type, "executed": executed, "model_used": "nlu-fallback", "rag_sources": [], "personality_used": False, "client_action": client_action}

        if openai_client.is_configured():
            openai_messages = [{"role": m.role, "content": m.content} for m in recent]
            openai_system = build_openai_prompt(user_name, None, None, openai_client.SYSTEM_PROMPT, planner_context=on_planner_page)
            result = openai_client.chat(messages=openai_messages, system_prompt=openai_system, trace_id=trace_id, user_message=message)
            if result.get("reply"):
                return {"reply": result["reply"], "action_type": "general_chat", "executed": True, "model_used": result["model"], "rag_sources": [], "personality_used": False, "client_action": None}

        return {"reply": "CHILI's brain is offline. Start Ollama to use chat: ollama serve", "action_type": "llm_offline", "executed": False, "model_used": "offline", "rag_sources": [], "personality_used": False, "client_action": None}

    planned = ctx["planned"]
    if ctx.get("rag_context"):
        rag_sources = [h["source"] for h in (ctx.get("rag_hits") or [])]
    personality_used = bool(ctx.get("personality_context"))

    action_type = planned.get("type", "unknown")
    action_data = planned.get("data", {})
    llm_reply = planned.get("reply") or ""

    if action_type == "unknown" and web_search_module.detect_search_intent(message):
        search_query = web_search_module.extract_search_query(message)
        action_data = {"query": search_query}
        action_type = "web_search"
        llm_reply = ""

    # Natural-language helper: allow users to say things like
    # "install the planner module" or "add voice control" and route to the
    # install_module tool when the slug is clear.
    if action_type == "unknown":
        lowered = message.lower()
        if "install" in lowered or "add " in lowered or "enable" in lowered:
            # Very lightweight heuristic mapping for first-party style slugs.
            if "planner" in lowered:
                action_type = "install_module"
                action_data = {"slug": "planner"}
                llm_reply = ""
            elif "voice" in lowered:
                action_type = "install_module"
                action_data = {"slug": "voice"}
                llm_reply = ""
            elif "intercom" in lowered:
                action_type = "install_module"
                action_data = {"slug": "intercom"}
                llm_reply = ""

    fallback_project_name = None
    if planner_hooks and is_module_enabled("planner") and action_type == "unknown" and not is_guest and user_id:
        ok, project_name = planner_hooks.detect_create_project_with_tasks_intent(message)
        if ok and project_name:
            fallback_project_name = project_name
            action_type = "add_plan_project_with_tasks"
            action_data = {"name": project_name, "description": "", "tasks": []}
            llm_reply = ""
            log_info(trace_id, f"create_project_with_tasks_fallback name={project_name!r}")

    llm_reply, executed, action_type, client_action = execute_tool(db, action_type, action_data, llm_reply, is_guest, user_id=user_id)

    if (
        planner_hooks
        and is_module_enabled("planner")
        and action_type == "add_plan_project_with_tasks"
        and executed
        and fallback_project_name
        and openai_client.is_configured()
    ):
        projects = planner_service.list_projects(db, user_id)
        proj = next((p for p in projects if p["name"] == fallback_project_name), None)
        if proj:
            added = planner_hooks.generate_tasks_for_project(db, proj["id"], fallback_project_name, user_id, trace_id)
            if added:
                llm_reply = f'Created project **"{fallback_project_name}"** with {added} task(s). Open each task in the Planner to see complexity, duration, and reasoning. [Project Planner](/planner).'

    model_used = "llama3"
    if action_type == "web_search" and executed and openai_client.is_configured():
        search_context = llm_reply
        openai_messages = [{"role": m.role, "content": m.content} for m in recent]
        search_system = (
            openai_client.SYSTEM_PROMPT +
            f"\n\nThe user asked to search the web. Here are the search results:\n\n{search_context}\n\n"
            "Using these search results, provide a helpful, well-formatted answer. "
            "Include relevant links from the results. Be specific and actionable."
        )
        if stream:
            return {"continue_stream": True, "messages": openai_messages, "system_prompt": search_system, "action_type": "web_search", "model_used": model_used, "rag_sources": rag_sources, "personality_used": personality_used, "fallback_reply": llm_reply, "client_action": client_action}
        result = openai_client.chat(messages=openai_messages, system_prompt=search_system, trace_id=trace_id, user_message=message)
        if result.get("reply"):
            llm_reply = result["reply"]
            model_used = result["model"]
            log_info(trace_id, f"web_search_synthesized model={model_used}")

    elif action_type == "unknown" and openai_client.is_configured():
        openai_messages = [{"role": m.role, "content": m.content} for m in recent]
        openai_system = build_openai_prompt(user_name, ctx["personality_context"], ctx["rag_context"], openai_client.SYSTEM_PROMPT, planner_context=on_planner_page)
        if llm_reply and llm_reply.strip():
            openai_system += f'\n\nThe planner suggested asking the user for more info. You may use or expand this naturally: "{llm_reply.strip()}"'
        if stream:
            return {"continue_stream": True, "messages": openai_messages, "system_prompt": openai_system, "action_type": "general_chat", "model_used": "openai", "rag_sources": rag_sources, "personality_used": personality_used, "fallback_reply": "I'm not sure what to do with that.", "client_action": client_action}
        result = openai_client.chat(messages=openai_messages, system_prompt=openai_system, trace_id=trace_id, user_message=message)
        if result.get("reply"):
            llm_reply = result["reply"]
            action_type = "general_chat"
            model_used = result["model"]
            executed = True
            log_info(trace_id, f"llm_fallback tokens={result['tokens_used']} model={model_used}")

    if not llm_reply:
        llm_reply = "I'm not sure what to do with that. Try: add chore, list chores, add birthday, list birthdays."
    if action_type == "web_search" and not model_used:
        model_used = "duckduckgo"
    return {"reply": llm_reply, "action_type": action_type, "executed": executed, "model_used": model_used, "rag_sources": rag_sources, "personality_used": personality_used, "client_action": client_action}


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
        db.query(ChatMessage).filter(mem_filter).order_by(ChatMessage.id.desc()).limit(8).all()
    ))

    return {"conversation_id": conversation_id, "recent": recent}


def _thread_get_personality_memory(user_id: int) -> str | None:
    """Thread-safe: use own session for personality + memory context."""
    s = SessionLocal()
    try:
        personality = personality_module.get_profile_context(user_id, s)
        memory = memory_module.get_memory_context(user_id, s)
        if memory and personality:
            personality = personality + "\n\n" + memory
        elif memory:
            personality = memory
        return personality
    finally:
        s.close()


def _thread_get_project_summary(user_id: int) -> str | None:
    """Thread-safe: use own session for planner project summary."""
    if not is_module_enabled("planner"):
        return None
    s = SessionLocal()
    try:
        return planner_service.get_user_project_summary(s, user_id)
    finally:
        s.close()


def _gather_context_parallel(
    message: str,
    identity: dict,
    trace_id: str,
    project_id: int | None = None,
) -> tuple[str | None, list, str | None, str | None]:
    """Run RAG, project RAG, personality+memory, and project summary in parallel.
    Returns (rag_context, rag_hits, personality_context, project_context)."""
    is_guest = identity["is_guest"]
    user_id = identity.get("user_id")
    rag_context = None
    rag_hits = []
    personality_context = None
    project_context = None

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {}
        futures["rag"] = executor.submit(rag_module.search, message, 3, trace_id)
        if project_id:
            futures["proj_rag"] = executor.submit(
                pfs_module.search_project, project_id, message, 3, trace_id
            )
        if user_id and not is_guest:
            futures["personality"] = executor.submit(_thread_get_personality_memory, user_id)
            if is_module_enabled("planner"):
                futures["project"] = executor.submit(_thread_get_project_summary, user_id)

        rag_hits = futures["rag"].result()
        if rag_hits and rag_hits[0]["distance"] < 1.0:
            rag_context = "\n---\n".join(f"[{h['source']}]: {h['text']}" for h in rag_hits)
            log_info(trace_id, f"rag_context_injected sources={[h['source'] for h in rag_hits]}")

        if project_id and "proj_rag" in futures:
            proj_hits = futures["proj_rag"].result()
            if proj_hits:
                proj_context = "\n---\n".join(f"[project:{h['source']}]: {h['text']}" for h in proj_hits)
                rag_context = f"{proj_context}\n---\n{rag_context}" if rag_context else proj_context
                rag_hits = proj_hits + (rag_hits or [])
                log_info(trace_id, f"project_rag_injected project={project_id}")

        if user_id and not is_guest and "personality" in futures:
            personality_context = futures["personality"].result()
            if personality_context:
                log_info(trace_id, f"personality_injected user_id={user_id}")
            if "project" in futures:
                project_context = futures["project"].result()
                if project_context:
                    log_info(trace_id, f"project_context_injected user_id={user_id}")
                    personality_context = (personality_context or "") + "\n\n" + project_context

    return (rag_context, rag_hits, personality_context, project_context)


def gather_context_only(
    db: Session,
    message: str,
    identity: dict,
    trace_id: str,
    project_id: int | None = None,
):
    """Gather RAG, personality, memory, and project context without calling Ollama.
    Used when Groq is configured to skip the slow local planner and go straight to one LLM call."""
    rag_context, rag_hits, personality_context, _ = _gather_context_parallel(
        message, identity, trace_id, project_id
    )
    return {
        "planned": {"type": "unknown", "data": {}, "reply": ""},
        "rag_context": rag_context,
        "rag_hits": rag_hits if rag_context else [],
        "personality_context": personality_context,
    }


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

    rag_context, rag_hits, personality_context, project_context = _gather_context_parallel(
        message, identity, trace_id, project_id
    )

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


def _get_planner_page_context() -> str:
    from ..prompts import load_prompt
    return load_prompt("planner_page_context")


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
        openai_system += "\n\n" + _get_planner_page_context()
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


def run_personality_and_memory_in_background(
    user_id, is_guest, message, llm_reply, action_type, trace_id,
):
    """Run personality update and memory extraction in a background task (uses own DB session)."""
    if not user_id or is_guest:
        return
    db = SessionLocal()
    try:
        try_personality_update(user_id, is_guest, db, trace_id)
        try_memory_extraction(
            user_id, is_guest, message, llm_reply, action_type, db, trace_id,
        )
    finally:
        db.close()


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


def process_message_get_reply(
    db: Session,
    convo_key: str,
    identity: dict,
    client_ip: str,
    message: str,
    trace_id: str,
    conversation_id=None,
):
    """Run full chat pipeline (init, wellness, plan, resolve, store) and return (reply, conversation_id)."""
    from .. import wellness

    user_name = identity["user_name"]
    is_guest = identity["is_guest"]
    user_id = identity.get("user_id")

    chat_init = init_chat(
        db, convo_key, conversation_id, message, identity, trace_id, image_path=None
    )
    conversation_id = chat_init["conversation_id"]
    recent = chat_init["recent"]

    if wellness.detect_crisis(message):
        llm_reply = wellness.CRISIS_RESPONSE
        action_type = "crisis_support"
        model_used = "crisis-detector"
        db.add(
            ChatMessage(
                convo_key=convo_key,
                conversation_id=conversation_id,
                role="assistant",
                content=llm_reply,
                trace_id=trace_id,
                action_type=action_type,
                model_used=model_used,
            )
        )
        db.commit()
        db.add(ChatLog(client_ip=client_ip, trace_id=trace_id, message=message, action_type=action_type))
        db.commit()
        try_personality_update(user_id, is_guest, db, trace_id)
        return llm_reply, conversation_id

    if wellness.detect_wellness_topic(message):
        wellness_msgs = [{"role": m.role, "content": m.content} for m in recent]
        result = wellness.wellness_chat(
            messages=wellness_msgs, user_name=user_name, trace_id=trace_id
        )
        llm_reply = result["reply"]
        action_type = "wellness_support"
        model_used = result["model"]
        db.add(
            ChatMessage(
                convo_key=convo_key,
                conversation_id=conversation_id,
                role="assistant",
                content=llm_reply,
                trace_id=trace_id,
                action_type=action_type,
                model_used=model_used,
            )
        )
        db.commit()
        db.add(ChatLog(client_ip=client_ip, trace_id=trace_id, message=message, action_type=action_type))
        db.commit()
        try_personality_update(user_id, is_guest, db, trace_id)
        return llm_reply, conversation_id

    try:
        ctx = plan_and_enrich(db, message, identity, recent, trace_id)
    except Exception as e:
        log_info(trace_id, f"plan_and_enrich_error={e}, using fallback")
        ctx = None

    result = resolve_response(
        db, message, recent, identity, ctx, on_planner_page=False, trace_id=trace_id, stream=False
    )
    llm_reply = result["reply"]
    action_type = result["action_type"]
    executed = result["executed"]
    model_used = result["model_used"]

    db.add(
        ChatMessage(
            convo_key=convo_key,
            conversation_id=conversation_id,
            role="assistant",
            content=llm_reply,
            trace_id=trace_id,
            action_type=action_type,
            model_used=model_used,
        )
    )
    db.commit()
    if conversation_id:
        convo_obj = db.query(Conversation).filter(Conversation.id == conversation_id).first()
        if convo_obj and convo_obj.title == "New Chat":
            convo_obj.title = message[:40].strip() + ("..." if len(message) > 40 else "")
            db.commit()
    db.add(ChatLog(client_ip=client_ip, trace_id=trace_id, message=message, action_type=action_type))
    db.commit()
    try_personality_update(user_id, is_guest, db, trace_id)
    try_memory_extraction(
        user_id, is_guest, message, llm_reply, action_type, db, trace_id
    )
    return llm_reply, conversation_id
