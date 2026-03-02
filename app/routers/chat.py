"""Chat routes: chat page, API chat, streaming, history, conversations."""
from fastapi import APIRouter, Depends, Form, Request, Query
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from typing import Optional
from sqlalchemy.orm import Session
import time

from ..deps import get_db, get_convo_key
from ..models import ChatMessage, ChatLog, Conversation
from ..logger import new_trace_id, log_info
from .. import openai_client
from ..metrics import record_latency
from ..pairing import DEVICE_COOKIE_NAME, get_identity_record
from ..services.chat_service import (
    execute_tool,
    init_chat,
    plan_and_enrich,
    build_openai_prompt,
    sse_event,
    store_and_title,
    try_personality_update,
)

router = APIRouter()
templates = None


def init_templates(t: Jinja2Templates):
    global templates
    templates = t


@router.get("/chat")
def chat_page(request: Request):
    return templates.TemplateResponse(
        request, "chat.html",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@router.get("/api/conversations", response_class=JSONResponse)
def list_conversations(request: Request, db: Session = Depends(get_db)):
    client_ip = request.client.host
    device_token = request.cookies.get(DEVICE_COOKIE_NAME)
    identity = get_identity_record(db, device_token)
    convo_key = get_convo_key(identity, device_token, client_ip)

    if identity["is_guest"]:
        return {"conversations": [], "is_guest": True}

    convos = (
        db.query(Conversation)
        .filter(Conversation.convo_key == convo_key)
        .order_by(Conversation.created_at.desc())
        .all()
    )
    return {
        "is_guest": False,
        "conversations": [
            {"id": c.id, "title": c.title, "created_at": str(c.created_at)}
            for c in convos
        ],
    }


@router.post("/api/conversations", response_class=JSONResponse)
def create_conversation(request: Request, db: Session = Depends(get_db)):
    client_ip = request.client.host
    device_token = request.cookies.get(DEVICE_COOKIE_NAME)
    identity = get_identity_record(db, device_token)
    convo_key = get_convo_key(identity, device_token, client_ip)

    if identity["is_guest"]:
        return JSONResponse({"error": "Guests cannot create conversations"}, status_code=403)

    convo = Conversation(convo_key=convo_key, title="New Chat")
    db.add(convo)
    db.commit()
    db.refresh(convo)
    return {"id": convo.id, "title": convo.title, "created_at": str(convo.created_at)}


@router.delete("/api/conversations/{convo_id}", response_class=JSONResponse)
def delete_conversation(convo_id: int, request: Request, db: Session = Depends(get_db)):
    client_ip = request.client.host
    device_token = request.cookies.get(DEVICE_COOKIE_NAME)
    identity = get_identity_record(db, device_token)
    convo_key = get_convo_key(identity, device_token, client_ip)

    convo = db.query(Conversation).filter(
        Conversation.id == convo_id,
        Conversation.convo_key == convo_key,
    ).first()

    if not convo:
        return JSONResponse({"error": "Not found"}, status_code=404)

    db.delete(convo)
    db.commit()
    return {"ok": True}


@router.get("/api/conversations/search", response_class=JSONResponse)
def search_conversations(request: Request, q: str = Query(""), db: Session = Depends(get_db)):
    client_ip = request.client.host
    device_token = request.cookies.get(DEVICE_COOKIE_NAME)
    identity = get_identity_record(db, device_token)
    convo_key = get_convo_key(identity, device_token, client_ip)

    if not q.strip() or identity.get("is_guest"):
        return {"results": []}

    pattern = f"%{q.strip()}%"
    matches = (
        db.query(ChatMessage.conversation_id, ChatMessage.content)
        .filter(
            ChatMessage.convo_key == convo_key,
            ChatMessage.conversation_id.isnot(None),
            ChatMessage.content.ilike(pattern),
        )
        .order_by(ChatMessage.id.desc())
        .limit(100)
        .all()
    )

    seen = {}
    for convo_id, content in matches:
        if convo_id not in seen:
            snippet = content[:80] + ("..." if len(content) > 80 else "")
            seen[convo_id] = snippet

    convo_ids = list(seen.keys())[:20]
    convos = db.query(Conversation).filter(Conversation.id.in_(convo_ids)).all()
    convo_map = {c.id: c.title for c in convos}

    results = [
        {"id": cid, "title": convo_map.get(cid, "Untitled"), "snippet": seen[cid]}
        for cid in convo_ids
        if cid in convo_map
    ]
    return {"results": results}


@router.get("/api/chat/history", response_class=JSONResponse)
def chat_history(
    request: Request,
    conversation_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
):
    client_ip = request.client.host
    device_token = request.cookies.get(DEVICE_COOKIE_NAME)
    identity = get_identity_record(db, device_token)
    convo_key = get_convo_key(identity, device_token, client_ip)

    q = db.query(ChatMessage).filter(
        ChatMessage.convo_key == convo_key,
        ChatMessage.content != "",
    )

    if conversation_id is not None:
        q = q.filter(ChatMessage.conversation_id == conversation_id)
    elif not identity["is_guest"]:
        latest = (
            db.query(Conversation)
            .filter(Conversation.convo_key == convo_key)
            .order_by(Conversation.created_at.desc())
            .first()
        )
        if latest:
            q = q.filter(ChatMessage.conversation_id == latest.id)
            conversation_id = latest.id
        else:
            q = q.filter(ChatMessage.conversation_id == None)

    msgs = q.order_by(ChatMessage.id.asc()).limit(50).all()

    return {
        "convo_key": convo_key,
        "user": identity["user_name"],
        "is_guest": identity["is_guest"],
        "messages": [
            {"role": m.role, "content": m.content, "created_at": str(m.created_at), "model_used": m.model_used}
            for m in msgs
        ],
    }


@router.post("/api/chat", response_class=JSONResponse)
def chat_api(
    request: Request,
    message: str = Form(...),
    conversation_id: Optional[int] = Form(None),
    db: Session = Depends(get_db),
):
    trace_id = new_trace_id()
    t0 = time.time()

    client_ip = request.client.host
    device_token = request.cookies.get(DEVICE_COOKIE_NAME)
    identity = get_identity_record(db, device_token)
    convo_key = get_convo_key(identity, device_token, client_ip)
    user_name = identity["user_name"]
    is_guest = identity["is_guest"]
    user_id = identity.get("user_id")

    log_info(trace_id, f"client_ip={client_ip} user={user_name} guest={is_guest} convo={convo_key} conversation_id={conversation_id}")
    log_info(trace_id, f"chat_message={message!r}")

    chat_init = init_chat(db, convo_key, conversation_id, message, identity, trace_id)
    conversation_id = chat_init["conversation_id"]
    recent = chat_init["recent"]

    try:
        ctx = plan_and_enrich(db, message, identity, recent, trace_id)
    except Exception as e:
        log_info(trace_id, f"llm_error={e}")
        llm_reply = "CHILI's brain is offline. Start Ollama to use chat: ollama serve"
        db.add(ChatMessage(convo_key=convo_key, conversation_id=conversation_id, role="assistant", content=llm_reply, trace_id=trace_id, action_type="llm_offline", model_used="offline"))
        db.commit()
        db.add(ChatLog(client_ip=client_ip, trace_id=trace_id, message=message, action_type="llm_offline"))
        db.commit()
        ms = int((time.time() - t0) * 1000)
        record_latency(ms)
        return {"trace_id": trace_id, "user": user_name, "is_guest": is_guest, "action_type": "llm_offline", "executed": False, "reply": llm_reply, "conversation_id": conversation_id}

    planned = ctx["planned"]

    action_type = planned.get("type", "unknown")
    action_data = planned.get("data", {})
    llm_reply = planned.get("reply") or ""

    llm_reply, executed, action_type = execute_tool(db, action_type, action_data, llm_reply, is_guest)

    model_used = "llama3"
    if action_type == "unknown" and openai_client.is_configured():
        openai_messages = [{"role": m.role, "content": m.content} for m in recent]
        openai_system = build_openai_prompt(user_name, ctx["personality_context"], ctx["rag_context"], openai_client.SYSTEM_PROMPT)
        result = openai_client.chat(messages=openai_messages, system_prompt=openai_system, trace_id=trace_id)
        if result["reply"]:
            llm_reply = result["reply"]
            action_type = "general_chat"
            model_used = result["model"]
            executed = True
            log_info(trace_id, f"openai_fallback tokens={result['tokens_used']} model={model_used}")

    if not llm_reply:
        llm_reply = "I'm not sure what to do with that. Try: add chore, list chores, add birthday, list birthdays."

    db.add(ChatMessage(
        convo_key=convo_key, conversation_id=conversation_id,
        role="assistant", content=llm_reply, trace_id=trace_id,
        action_type=action_type, model_used=model_used,
    ))
    db.commit()

    if conversation_id:
        convo_obj = db.query(Conversation).filter(Conversation.id == conversation_id).first()
        if convo_obj and convo_obj.title == "New Chat":
            convo_obj.title = message[:40].strip() + ("..." if len(message) > 40 else "")
            db.commit()

    db.add(ChatLog(client_ip=client_ip, trace_id=trace_id, message=message, action_type=action_type))
    db.commit()

    try_personality_update(user_id, is_guest, db, trace_id)

    ms = int((time.time() - t0) * 1000)
    record_latency(ms)
    log_info(trace_id, f"latency_ms={ms} action={action_type} executed={executed}")

    return {
        "trace_id": trace_id,
        "user": user_name,
        "is_guest": is_guest,
        "action_type": action_type,
        "executed": executed,
        "reply": llm_reply,
        "model_used": model_used,
        "conversation_id": conversation_id,
    }


@router.post("/api/chat/stream")
def chat_stream_api(
    request: Request,
    message: str = Form(...),
    conversation_id: Optional[int] = Form(None),
    db: Session = Depends(get_db),
):
    trace_id = new_trace_id()
    t0 = time.time()

    client_ip = request.client.host
    device_token = request.cookies.get(DEVICE_COOKIE_NAME)
    identity = get_identity_record(db, device_token)
    convo_key = get_convo_key(identity, device_token, client_ip)
    user_name = identity["user_name"]
    is_guest = identity["is_guest"]
    user_id = identity.get("user_id")

    chat_init = init_chat(db, convo_key, conversation_id, message, identity, trace_id)
    conversation_id = chat_init["conversation_id"]
    recent = chat_init["recent"]

    try:
        ctx = plan_and_enrich(db, message, identity, recent, trace_id)
    except Exception:
        reply = "CHILI's brain is offline. Start Ollama to use chat: ollama serve"

        def offline_gen():
            yield sse_event({"token": reply, "done": False})
            yield sse_event({"token": "", "done": True, "action_type": "llm_offline", "model_used": "offline", "conversation_id": conversation_id})
            store_and_title(convo_key, conversation_id, reply, trace_id, "llm_offline", "offline", client_ip, message)
        return StreamingResponse(offline_gen(), media_type="text/event-stream")

    planned = ctx["planned"]

    action_type = planned.get("type", "unknown")
    action_data = planned.get("data", {})
    llm_reply = planned.get("reply") or ""

    llm_reply, executed, action_type = execute_tool(db, action_type, action_data, llm_reply, is_guest)

    if action_type != "unknown" or not openai_client.is_configured():
        if not llm_reply:
            llm_reply = "I'm not sure what to do with that. Try: add chore, list chores, add birthday, list birthdays."
        model_used = "llama3"

        def tool_gen():
            yield sse_event({"token": llm_reply, "done": False})
            yield sse_event({"token": "", "done": True, "action_type": action_type, "model_used": model_used, "conversation_id": conversation_id})
            store_and_title(convo_key, conversation_id, llm_reply, trace_id, action_type, model_used, client_ip, message)
        return StreamingResponse(tool_gen(), media_type="text/event-stream")

    openai_messages = [{"role": m.role, "content": m.content} for m in recent]
    openai_system = build_openai_prompt(user_name, ctx["personality_context"], ctx["rag_context"], openai_client.SYSTEM_PROMPT)

    def stream_gen():
        full_reply = []
        for token in openai_client.chat_stream(messages=openai_messages, system_prompt=openai_system, trace_id=trace_id):
            full_reply.append(token)
            yield sse_event({"token": token, "done": False})

        complete = "".join(full_reply)
        if not complete:
            complete = "I'm not sure what to do with that."
            yield sse_event({"token": complete, "done": False})

        yield sse_event({"token": "", "done": True, "action_type": "general_chat", "model_used": openai_client.OPENAI_MODEL, "conversation_id": conversation_id})
        store_and_title(convo_key, conversation_id, complete, trace_id, "general_chat", openai_client.OPENAI_MODEL, client_ip, message)

    return StreamingResponse(stream_gen(), media_type="text/event-stream")
