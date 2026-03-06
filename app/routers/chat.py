"""Chat routes: chat page, API chat, streaming, history, conversations."""
from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request, Query, File, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from typing import Optional
from sqlalchemy.orm import Session
import json as json_stdlib
import time

from ..deps import get_db, get_convo_key
from ..models import ChatMessage, ChatLog, Conversation
from ..logger import new_trace_id, log_info
from .. import openai_client
from .. import vision as vision_module
from .. import wellness
from ..metrics import record_latency
from ..pairing import DEVICE_COOKIE_NAME, get_identity_record
from ..schemas.chat import MobileChatRequest, MobileChatResponse
from ..services.chat_service import (
    execute_tool,
    init_chat,
    nlu_fallback,
    plan_and_enrich,
    build_openai_prompt,
    resolve_response,
    run_personality_and_memory_in_background,
    sse_event,
    store_and_title,
    store_and_title_with_memory,
    try_personality_update,
    try_memory_extraction,
)

router = APIRouter()


@router.get("/chat")
def chat_page(request: Request):
    return request.app.state.templates.TemplateResponse(
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
            {"id": c.id, "title": c.title, "project_id": c.project_id, "created_at": str(c.created_at)}
            for c in convos
        ],
    }


@router.post("/api/conversations", response_class=JSONResponse)
async def create_conversation(request: Request, db: Session = Depends(get_db)):
    client_ip = request.client.host
    device_token = request.cookies.get(DEVICE_COOKIE_NAME)
    identity = get_identity_record(db, device_token)
    convo_key = get_convo_key(identity, device_token, client_ip)

    if identity["is_guest"]:
        return JSONResponse({"error": "Guests cannot create conversations"}, status_code=403)

    project_id = None
    try:
        body = await request.json()
        project_id = body.get("project_id")
    except Exception:
        pass

    convo = Conversation(convo_key=convo_key, title="New Chat", project_id=project_id)
    db.add(convo)
    db.commit()
    db.refresh(convo)
    return {"id": convo.id, "title": convo.title, "project_id": convo.project_id, "created_at": str(convo.created_at)}


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
        "conversation_id": conversation_id,
        "messages": [
            {
                "role": m.role, "content": m.content, "created_at": str(m.created_at),
                "model_used": m.model_used, "trace_id": m.trace_id, "action_type": m.action_type,
                "image_paths": _parse_image_paths(m.image_path),
            }
            for m in msgs
        ],
    }


async def _handle_image_uploads(images: list[UploadFile], trace_id: str) -> list[str]:
    """Read and save multiple uploaded images. Returns list of saved filenames."""
    saved_names: list[str] = []
    for image in images:
        if not image or not image.filename:
            continue
        file_bytes = await image.read()
        if not file_bytes:
            continue
        saved = vision_module.save_upload(file_bytes, image.filename, image.content_type or "")
        if not saved:
            log_info(trace_id, f"image_upload_rejected type={image.content_type} size={len(file_bytes)}")
        else:
            log_info(trace_id, f"image_uploaded name={saved} size={len(file_bytes)}")
            saved_names.append(saved)
    return saved_names


def _image_path_json(saved_images: list[str]) -> str | None:
    """Serialize saved image names to JSON for storage, or None if empty."""
    if not saved_images:
        return None
    return json_stdlib.dumps(saved_images)


def _parse_image_paths(image_path_raw: str | None) -> list[str]:
    """Parse the image_path column back into a list. Handles JSON array and legacy single-name."""
    if not image_path_raw:
        return []
    try:
        parsed = json_stdlib.loads(image_path_raw)
        if isinstance(parsed, list):
            return parsed
    except (json_stdlib.JSONDecodeError, TypeError):
        pass
    return [image_path_raw]


@router.post("/api/chat", response_class=JSONResponse)
async def chat_api(
    request: Request,
    message: str = Form(""),
    conversation_id: Optional[int] = Form(None),
    images: list[UploadFile] = File([]),
    planner_project_id: Optional[int] = Form(None),
    planner_project_name: Optional[str] = Form(None),
    from_planner_page: Optional[str] = Form(None),
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

    saved_images = await _handle_image_uploads(images, trace_id)
    if not message.strip() and not saved_images:
        return JSONResponse({"error": "Message or image required"}, status_code=400)

    display_message = message.strip() or "(image)"
    log_info(trace_id, f"client_ip={client_ip} user={user_name} guest={is_guest} convo={convo_key} conversation_id={conversation_id} images={len(saved_images)}")
    log_info(trace_id, f"chat_message={display_message!r}")

    image_path_val = _image_path_json(saved_images)
    chat_init = init_chat(db, convo_key, conversation_id, display_message, identity, trace_id, image_path=image_path_val)
    conversation_id = chat_init["conversation_id"]
    recent = chat_init["recent"]

    if saved_images:
        system = build_openai_prompt(user_name, None, None, vision_module.VISION_SYSTEM_PROMPT)
        llm_reply, model_used = vision_module.describe_image(saved_images, message.strip(), system, trace_id)
        action_type = "vision"
        executed = True

        db.add(ChatMessage(convo_key=convo_key, conversation_id=conversation_id, role="assistant", content=llm_reply, trace_id=trace_id, action_type=action_type, model_used=model_used))
        db.commit()
        db.add(ChatLog(client_ip=client_ip, trace_id=trace_id, message=display_message, action_type=action_type))
        db.commit()

        if conversation_id:
            convo_obj = db.query(Conversation).filter(Conversation.id == conversation_id).first()
            if convo_obj and convo_obj.title == "New Chat":
                title_text = message.strip()[:40] if message.strip() else "Image chat"
                convo_obj.title = title_text + ("..." if len(title_text) > 39 else "")
                db.commit()

        ms = int((time.time() - t0) * 1000)
        record_latency(ms)
        return {"trace_id": trace_id, "user": user_name, "is_guest": is_guest, "action_type": action_type, "executed": executed, "reply": llm_reply, "model_used": model_used, "conversation_id": conversation_id, "rag_sources": [], "personality_used": False}

    # --- Wellness detection (fires before planner) ---
    if wellness.detect_crisis(message):
        log_info(trace_id, "crisis_detected")
        llm_reply = wellness.CRISIS_RESPONSE
        action_type = "crisis_support"
        model_used = "crisis-detector"
        db.add(ChatMessage(convo_key=convo_key, conversation_id=conversation_id, role="assistant", content=llm_reply, trace_id=trace_id, action_type=action_type, model_used=model_used))
        db.commit()
        db.add(ChatLog(client_ip=client_ip, trace_id=trace_id, message=message, action_type=action_type))
        db.commit()
        ms = int((time.time() - t0) * 1000)
        record_latency(ms)
        return {"trace_id": trace_id, "user": user_name, "is_guest": is_guest, "action_type": action_type, "executed": True, "reply": llm_reply, "model_used": model_used, "conversation_id": conversation_id, "rag_sources": [], "personality_used": False}

    if wellness.detect_wellness_topic(message):
        log_info(trace_id, "wellness_topic_detected")
        wellness_msgs = [{"role": m.role, "content": m.content} for m in recent]
        result = wellness.wellness_chat(messages=wellness_msgs, user_name=user_name, trace_id=trace_id)
        llm_reply = result["reply"]
        action_type = "wellness_support"
        model_used = result["model"]
        db.add(ChatMessage(convo_key=convo_key, conversation_id=conversation_id, role="assistant", content=llm_reply, trace_id=trace_id, action_type=action_type, model_used=model_used))
        db.commit()
        db.add(ChatLog(client_ip=client_ip, trace_id=trace_id, message=message, action_type=action_type))
        db.commit()
        ms = int((time.time() - t0) * 1000)
        record_latency(ms)
        return {"trace_id": trace_id, "user": user_name, "is_guest": is_guest, "action_type": action_type, "executed": True, "reply": llm_reply, "model_used": model_used, "conversation_id": conversation_id, "rag_sources": [], "personality_used": False}

    rag_sources = []
    personality_used = False

    # Resolve project_id from the conversation for project-scoped RAG
    _project_id = None
    if conversation_id:
        _convo_obj = db.query(Conversation).filter(Conversation.id == conversation_id).first()
        if _convo_obj:
            _project_id = _convo_obj.project_id

    planner_current = None
    if planner_project_id is not None and planner_project_name:
        planner_current = {"id": planner_project_id, "name": planner_project_name.strip()}
    on_planner_page = from_planner_page in ("1", "true", "yes") or planner_current is not None

    try:
        ctx = plan_and_enrich(db, message, identity, recent, trace_id, project_id=_project_id, planner_current_project=planner_current)
    except Exception as e:
        log_info(trace_id, f"llm_error={e}, trying NLU fallback")
        ctx = None

    result = resolve_response(db, message, recent, identity, ctx, on_planner_page, trace_id, stream=False)

    llm_reply = result["reply"]
    action_type = result["action_type"]
    executed = result["executed"]
    model_used = result["model_used"]
    rag_sources = result["rag_sources"]
    personality_used = result["personality_used"]

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
    try_memory_extraction(user_id, is_guest, message, llm_reply, action_type, db, trace_id)

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
        "rag_sources": rag_sources,
        "personality_used": personality_used,
    }


def _get_token_from_request(request: Request) -> str | None:
    """Extract a device/auth token from Authorization header or cookie.

    Mobile clients can send `Authorization: Bearer <token>` while web clients
    continue to rely on the chili_device_token cookie.
    """
    auth = request.headers.get("authorization") or request.headers.get("Authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth.split(" ", 1)[1].strip() or None
    return request.cookies.get(DEVICE_COOKIE_NAME)


@router.post("/api/mobile/chat", response_model=MobileChatResponse)
async def mobile_chat_api(
    body: MobileChatRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """JSON-based chat endpoint for native/mobile clients.

    This mirrors the behavior of /api/chat but:
    - accepts a JSON body instead of form data
    - does not handle file uploads (text-only for now)
    - allows auth via Bearer token in the Authorization header
    """
    trace_id = new_trace_id()
    t0 = time.time()

    client_ip = request.client.host
    device_token = _get_token_from_request(request)
    identity = get_identity_record(db, device_token)
    convo_key = get_convo_key(identity, device_token, client_ip)
    user_name = identity["user_name"]
    is_guest = identity["is_guest"]
    user_id = identity.get("user_id")

    message = (body.message or "").strip()
    if not message:
        return JSONResponse({"error": "Message is required"}, status_code=400)

    log_info(
        trace_id,
        f"mobile_chat client_ip={client_ip} user={user_name} guest={is_guest} "
        f"convo={convo_key} conversation_id={body.conversation_id}",
    )
    log_info(trace_id, f"chat_message={message!r}")

    chat_init = init_chat(
        db,
        convo_key,
        body.conversation_id,
        message,
        identity,
        trace_id,
        image_path=None,
    )
    conversation_id = chat_init["conversation_id"]
    recent = chat_init["recent"]

    # --- Wellness detection (same as HTML/chat endpoint) ---
    if wellness.detect_crisis(message):
        log_info(trace_id, "crisis_detected (mobile)")
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
        db.add(
            ChatLog(
                client_ip=client_ip,
                trace_id=trace_id,
                message=message,
                action_type=action_type,
            )
        )
        db.commit()
        ms = int((time.time() - t0) * 1000)
        record_latency(ms)
        return MobileChatResponse(
            trace_id=trace_id,
            user=user_name,
            is_guest=is_guest,
            action_type=action_type,
            executed=True,
            reply=llm_reply,
            model_used=model_used,
            conversation_id=conversation_id,
            rag_sources=[],
            personality_used=False,
        )

    if wellness.detect_wellness_topic(message):
        log_info(trace_id, "wellness_topic_detected (mobile)")
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
        db.add(
            ChatLog(
                client_ip=client_ip,
                trace_id=trace_id,
                message=message,
                action_type=action_type,
            )
        )
        db.commit()
        ms = int((time.time() - t0) * 1000)
        record_latency(ms)
        return MobileChatResponse(
            trace_id=trace_id,
            user=user_name,
            is_guest=is_guest,
            action_type=action_type,
            executed=True,
            reply=llm_reply,
            model_used=model_used,
            conversation_id=conversation_id,
            rag_sources=[],
            personality_used=False,
        )

    # Resolve project_id from the conversation for project-scoped RAG
    _project_id = None
    if conversation_id:
        _convo_obj = db.query(Conversation).filter(Conversation.id == conversation_id).first()
        if _convo_obj:
            _project_id = _convo_obj.project_id

    planner_current = None
    if body.planner_project_id is not None and body.planner_project_name:
        planner_current = {
            "id": body.planner_project_id,
            "name": body.planner_project_name.strip(),
        }
    on_planner_page = planner_current is not None

    # NLU fast-path: skip expensive Ollama planner for known desktop/chore/birthday patterns
    nlu_result = nlu_fallback(message)
    if nlu_result is not None:
        log_info(trace_id, f"mobile_nlu_fast_path type={nlu_result['type']}")
        ctx = {
            "planned": nlu_result,
            "rag_context": None,
            "rag_hits": [],
            "personality_context": None,
        }
    else:
        try:
            ctx = plan_and_enrich(
                db,
                message,
                identity,
                recent,
                trace_id,
                project_id=_project_id,
                planner_current_project=planner_current,
            )
        except Exception as e:
            log_info(trace_id, f"llm_error={e}, trying NLU fallback (mobile)")
            ctx = None

    result = resolve_response(
        db,
        message,
        recent,
        identity,
        ctx,
        on_planner_page,
        trace_id,
        stream=False,
    )

    llm_reply = result["reply"]
    action_type = result["action_type"]
    executed = result["executed"]
    model_used = result["model_used"]
    rag_sources = result["rag_sources"]
    personality_used = result["personality_used"]
    client_action = result.get("client_action")

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

    db.add(
        ChatLog(
            client_ip=client_ip,
            trace_id=trace_id,
            message=message,
            action_type=action_type,
        )
    )
    db.commit()

    background_tasks.add_task(
        run_personality_and_memory_in_background,
        user_id,
        is_guest,
        message,
        llm_reply,
        action_type,
        trace_id,
    )

    ms = int((time.time() - t0) * 1000)
    record_latency(ms)
    log_info(trace_id, f"latency_ms={ms} action={action_type} executed={executed} client_type=mobile")

    return MobileChatResponse(
        trace_id=trace_id,
        user=user_name,
        is_guest=is_guest,
        action_type=action_type,
        executed=executed,
        reply=llm_reply,
        model_used=model_used,
        conversation_id=conversation_id,
        rag_sources=rag_sources,
        personality_used=personality_used,
        client_action=client_action,
    )


@router.post("/api/mobile/chat/stream")
async def mobile_chat_stream_api(
    body: MobileChatRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """SSE streaming chat for mobile/desktop clients. JSON body, SSE response."""
    trace_id = new_trace_id()
    t0 = time.time()

    client_ip = request.client.host
    device_token = _get_token_from_request(request)
    identity = get_identity_record(db, device_token)
    convo_key = get_convo_key(identity, device_token, client_ip)
    user_name = identity["user_name"]
    is_guest = identity["is_guest"]
    user_id = identity.get("user_id")

    message = (body.message or "").strip()
    if not message:
        return JSONResponse({"error": "Message is required"}, status_code=400)

    log_info(trace_id, f"mobile_chat_stream client_ip={client_ip} user={user_name}")

    chat_init = init_chat(
        db, convo_key, body.conversation_id, message, identity, trace_id, image_path=None
    )
    conversation_id = chat_init["conversation_id"]
    recent = chat_init["recent"]

    if wellness.detect_crisis(message):
        log_info(trace_id, "crisis_detected (mobile stream)")

        def crisis_gen():
            yield sse_event({"token": wellness.CRISIS_RESPONSE, "done": False})
            yield sse_event({
                "token": "", "done": True,
                "action_type": "crisis_support", "model_used": "crisis-detector",
                "conversation_id": conversation_id, "trace_id": trace_id,
                "rag_sources": [], "personality_used": False, "client_action": None,
            })
            store_and_title_with_memory(
                convo_key, conversation_id, wellness.CRISIS_RESPONSE,
                trace_id, "crisis_support", "crisis-detector", client_ip, message,
                user_id=user_id, is_guest=is_guest,
            )
        return StreamingResponse(crisis_gen(), media_type="text/event-stream")

    if wellness.detect_wellness_topic(message):
        log_info(trace_id, "wellness_topic_detected (mobile stream)")
        wellness_msgs = [{"role": m.role, "content": m.content} for m in recent]

        def wellness_gen():
            full = []
            used_model = "llama3-wellness"
            for tok, model in wellness.wellness_chat_stream(
                messages=wellness_msgs, user_name=user_name, trace_id=trace_id
            ):
                full.append(tok)
                used_model = model
                yield sse_event({"token": tok, "done": False})
            complete = "".join(full) or "I'm here for you. Tell me more about what you're feeling."
            if not full:
                yield sse_event({"token": complete, "done": False})
            yield sse_event({
                "token": "", "done": True,
                "action_type": "wellness_support", "model_used": used_model,
                "conversation_id": conversation_id, "trace_id": trace_id,
                "rag_sources": [], "personality_used": False, "client_action": None,
            })
            store_and_title_with_memory(
                convo_key, conversation_id, complete, trace_id,
                "wellness_support", used_model, client_ip, message,
                user_id=user_id, is_guest=is_guest,
            )
        return StreamingResponse(wellness_gen(), media_type="text/event-stream")

    _project_id = None
    if conversation_id:
        _convo_obj = db.query(Conversation).filter(Conversation.id == conversation_id).first()
        if _convo_obj:
            _project_id = _convo_obj.project_id

    planner_current = None
    if body.planner_project_id is not None and body.planner_project_name:
        planner_current = {
            "id": body.planner_project_id,
            "name": body.planner_project_name.strip(),
        }
    on_planner_page = planner_current is not None

    nlu_result = nlu_fallback(message)
    if nlu_result is not None:
        log_info(trace_id, f"mobile_stream_nlu_fast_path type={nlu_result['type']}")
        ctx = {
            "planned": nlu_result,
            "rag_context": None,
            "rag_hits": [],
            "personality_context": None,
        }
    else:
        try:
            ctx = plan_and_enrich(
                db, message, identity, recent, trace_id,
                project_id=_project_id, planner_current_project=planner_current,
            )
        except Exception as e:
            log_info(trace_id, f"llm_error={e}, trying NLU fallback (mobile stream)")
            ctx = None

    result = resolve_response(
        db, message, recent, identity, ctx, on_planner_page, trace_id, stream=True
    )

    if result.get("continue_stream"):
        messages = result["messages"]
        system_prompt = result["system_prompt"]
        action_type = result["action_type"]
        model_used = result.get("model_used", openai_client.LLM_MODEL)
        rag_sources = result.get("rag_sources", [])
        personality_used = result.get("personality_used", False)
        fallback_reply = result.get("fallback_reply", "I'm not sure what to do with that.")
        client_action = result.get("client_action")

        def stream_openai_gen():
            full = []
            used_model = model_used
            for tok, model in openai_client.chat_stream(
                messages=messages, system_prompt=system_prompt,
                trace_id=trace_id, user_message=message,
            ):
                full.append(tok)
                used_model = model
                yield sse_event({"token": tok, "done": False})
            complete = "".join(full) or fallback_reply
            if not full:
                yield sse_event({"token": complete, "done": False})
            yield sse_event({
                "token": "", "done": True,
                "action_type": action_type, "model_used": used_model,
                "conversation_id": conversation_id, "trace_id": trace_id,
                "rag_sources": rag_sources, "personality_used": personality_used,
                "client_action": client_action,
            })
            store_and_title_with_memory(
                convo_key, conversation_id, complete, trace_id,
                action_type, used_model, client_ip, message,
                user_id=user_id, is_guest=is_guest,
            )
        return StreamingResponse(stream_openai_gen(), media_type="text/event-stream")

    llm_reply = result["reply"]
    action_type = result["action_type"]
    model_used = result["model_used"]
    rag_sources = result.get("rag_sources", [])
    personality_used = result.get("personality_used", False)
    client_action = result.get("client_action")

    def tool_result_gen():
        yield sse_event({"token": llm_reply, "done": False})
        yield sse_event({
            "token": "", "done": True,
            "action_type": action_type, "model_used": model_used,
            "conversation_id": conversation_id, "trace_id": trace_id,
            "rag_sources": rag_sources, "personality_used": personality_used,
            "client_action": client_action,
        })
        store_and_title_with_memory(
            convo_key, conversation_id, llm_reply, trace_id,
            action_type, model_used, client_ip, message,
            user_id=user_id, is_guest=is_guest,
        )

    ms = int((time.time() - t0) * 1000)
    record_latency(ms)
    log_info(trace_id, f"mobile_stream_latency_ms={ms} action={action_type}")

    return StreamingResponse(tool_result_gen(), media_type="text/event-stream")


@router.post("/api/chat/stream")
async def chat_stream_api(
    request: Request,
    message: str = Form(""),
    conversation_id: Optional[int] = Form(None),
    images: list[UploadFile] = File([]),
    planner_project_id: Optional[int] = Form(None),
    planner_project_name: Optional[str] = Form(None),
    from_planner_page: Optional[str] = Form(None),
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

    saved_images = await _handle_image_uploads(images, trace_id)
    if not message.strip() and not saved_images:
        return JSONResponse({"error": "Message or image required"}, status_code=400)

    display_message = message.strip() or "(image)"

    image_path_val = _image_path_json(saved_images)
    chat_init = init_chat(db, convo_key, conversation_id, display_message, identity, trace_id, image_path=image_path_val)
    conversation_id = chat_init["conversation_id"]
    recent = chat_init["recent"]

    if saved_images:
        system = build_openai_prompt(user_name, None, None, vision_module.VISION_SYSTEM_PROMPT)

        def vision_gen():
            full = []
            model_used = "none"
            for tok, model in vision_module.describe_image_stream(saved_images, message.strip(), system, trace_id):
                model_used = model
                if tok:
                    full.append(tok)
                    yield sse_event({"token": tok, "done": False})
            complete = "".join(full) or "Could not analyze the image."
            if not full:
                yield sse_event({"token": complete, "done": False})
            yield sse_event({"token": "", "done": True, "action_type": "vision", "model_used": model_used, "conversation_id": conversation_id, "trace_id": trace_id, "rag_sources": [], "personality_used": False})
            store_and_title_with_memory(convo_key, conversation_id, complete, trace_id, "vision", model_used, client_ip, display_message, user_id=user_id, is_guest=is_guest)

        return StreamingResponse(vision_gen(), media_type="text/event-stream")

    # --- Wellness detection (stream) ---
    if wellness.detect_crisis(message):
        log_info(trace_id, "crisis_detected (stream)")
        def crisis_gen():
            yield sse_event({"token": wellness.CRISIS_RESPONSE, "done": False})
            yield sse_event({"token": "", "done": True, "action_type": "crisis_support", "model_used": "crisis-detector", "conversation_id": conversation_id, "trace_id": trace_id, "rag_sources": [], "personality_used": False})
            store_and_title_with_memory(convo_key, conversation_id, wellness.CRISIS_RESPONSE, trace_id, "crisis_support", "crisis-detector", client_ip, message, user_id=user_id, is_guest=is_guest)
        return StreamingResponse(crisis_gen(), media_type="text/event-stream")

    if wellness.detect_wellness_topic(message):
        log_info(trace_id, "wellness_topic_detected (stream)")
        wellness_msgs = [{"role": m.role, "content": m.content} for m in recent]
        def wellness_gen():
            full = []
            used_model = "llama3-wellness"
            for tok, model in wellness.wellness_chat_stream(messages=wellness_msgs, user_name=user_name, trace_id=trace_id):
                full.append(tok)
                used_model = model
                yield sse_event({"token": tok, "done": False})
            complete = "".join(full) or "I'm here for you. Tell me more about what you're feeling."
            if not full:
                yield sse_event({"token": complete, "done": False})
            yield sse_event({"token": "", "done": True, "action_type": "wellness_support", "model_used": used_model, "conversation_id": conversation_id, "trace_id": trace_id, "rag_sources": [], "personality_used": False})
            store_and_title_with_memory(convo_key, conversation_id, complete, trace_id, "wellness_support", used_model, client_ip, message, user_id=user_id, is_guest=is_guest)
        return StreamingResponse(wellness_gen(), media_type="text/event-stream")

    rag_sources = []
    personality_used = False

    _project_id_stream = None
    if conversation_id:
        _convo_obj_stream = db.query(Conversation).filter(Conversation.id == conversation_id).first()
        if _convo_obj_stream:
            _project_id_stream = _convo_obj_stream.project_id

    planner_current_stream = None
    if planner_project_id is not None and planner_project_name:
        planner_current_stream = {"id": planner_project_id, "name": planner_project_name.strip()}
    on_planner_page_stream = from_planner_page in ("1", "true", "yes") or planner_current_stream is not None

    try:
        ctx = plan_and_enrich(db, message, identity, recent, trace_id, project_id=_project_id_stream, planner_current_project=planner_current_stream)
    except Exception as e:
        log_info(trace_id, f"llm_error={e}, trying NLU fallback (stream)")
        ctx = None

    result = resolve_response(db, message, recent, identity, ctx, on_planner_page_stream, trace_id, stream=True)

    if result.get("continue_stream"):
        messages = result["messages"]
        system_prompt = result["system_prompt"]
        action_type = result["action_type"]
        model_used = result.get("model_used", openai_client.LLM_MODEL)
        rag_sources = result.get("rag_sources", [])
        personality_used = result.get("personality_used", False)
        fallback_reply = result.get("fallback_reply", "I'm not sure what to do with that.")

        def stream_openai_gen():
            full = []
            used_model = model_used
            for tok, model in openai_client.chat_stream(messages=messages, system_prompt=system_prompt, trace_id=trace_id, user_message=message):
                full.append(tok)
                used_model = model
                yield sse_event({"token": tok, "done": False})
            complete = "".join(full) or fallback_reply
            if not full:
                yield sse_event({"token": complete, "done": False})
            yield sse_event({"token": "", "done": True, "action_type": action_type, "model_used": used_model, "conversation_id": conversation_id, "trace_id": trace_id, "rag_sources": rag_sources, "personality_used": personality_used})
            store_and_title_with_memory(convo_key, conversation_id, complete, trace_id, action_type, used_model, client_ip, message, user_id=user_id, is_guest=is_guest)

        return StreamingResponse(stream_openai_gen(), media_type="text/event-stream")

    llm_reply = result["reply"]
    action_type = result["action_type"]
    model_used = result["model_used"]
    rag_sources = result["rag_sources"]
    personality_used = result["personality_used"]

    def tool_result_gen():
        yield sse_event({"token": llm_reply, "done": False})
        yield sse_event({"token": "", "done": True, "action_type": action_type, "model_used": model_used, "conversation_id": conversation_id, "trace_id": trace_id, "rag_sources": rag_sources, "personality_used": personality_used})
        store_and_title_with_memory(convo_key, conversation_id, llm_reply, trace_id, action_type, model_used, client_ip, message, user_id=user_id, is_guest=is_guest)

    return StreamingResponse(tool_result_gen(), media_type="text/event-stream")


# --- Conversation export ---

@router.get("/api/conversations/{convo_id}/export", response_class=JSONResponse)
def export_conversation(
    convo_id: int,
    request: Request,
    fmt: str = Query("json"),
    db: Session = Depends(get_db),
):
    client_ip = request.client.host
    device_token = request.cookies.get(DEVICE_COOKIE_NAME)
    identity = get_identity_record(db, device_token)
    convo_key = get_convo_key(identity, device_token, client_ip)

    convo = db.query(Conversation).filter(Conversation.id == convo_id).first()
    if not convo:
        return JSONResponse({"error": "Not found"}, status_code=404)

    is_owner = convo.convo_key == convo_key
    is_housemate_viewing_guest = (not identity["is_guest"] and convo.convo_key.startswith("guest:"))
    if not is_owner and not is_housemate_viewing_guest:
        return JSONResponse({"error": "Forbidden"}, status_code=403)

    msgs = (
        db.query(ChatMessage)
        .filter(ChatMessage.conversation_id == convo_id, ChatMessage.content != "")
        .order_by(ChatMessage.id.asc())
        .all()
    )

    if fmt == "md":
        lines = [f"# {convo.title}\n"]
        for m in msgs:
            label = "**You**" if m.role == "user" else "**CHILI**"
            ts = str(m.created_at) if m.created_at else ""
            lines.append(f"{label} ({ts}):\n{m.content}\n")
        content = "\n".join(lines)
        return StreamingResponse(
            iter([content]),
            media_type="text/markdown",
            headers={"Content-Disposition": f"attachment; filename=chili-chat-{convo_id}.md"},
        )

    data = {
        "conversation": {"id": convo.id, "title": convo.title, "created_at": str(convo.created_at)},
        "messages": [
            {"role": m.role, "content": m.content, "created_at": str(m.created_at), "model_used": m.model_used, "trace_id": m.trace_id}
            for m in msgs
        ],
    }
    content = json_stdlib.dumps(data, indent=2)
    return StreamingResponse(
        iter([content]),
        media_type="application/json",
        headers={"Content-Disposition": f"attachment; filename=chili-chat-{convo_id}.json"},
    )


# --- Guest chat visibility for housemates ---

@router.get("/api/conversations/guests", response_class=JSONResponse)
def list_guest_conversations(request: Request, db: Session = Depends(get_db)):
    """Return guest conversations visible to paired housemates only."""
    device_token = request.cookies.get(DEVICE_COOKIE_NAME)
    identity = get_identity_record(db, device_token)

    if identity["is_guest"]:
        return JSONResponse({"error": "Guests cannot view other guest chats"}, status_code=403)

    from sqlalchemy import func
    guest_convos = (
        db.query(
            ChatMessage.convo_key,
            func.count(ChatMessage.id).label("msg_count"),
            func.max(ChatMessage.created_at).label("last_active"),
        )
        .filter(ChatMessage.convo_key.like("guest:%"))
        .group_by(ChatMessage.convo_key)
        .order_by(func.max(ChatMessage.created_at).desc())
        .all()
    )

    results = []
    for convo_key_val, msg_count, last_active in guest_convos:
        first_msg = (
            db.query(ChatMessage)
            .filter(ChatMessage.convo_key == convo_key_val, ChatMessage.role == "user")
            .order_by(ChatMessage.id.asc())
            .first()
        )
        title = (first_msg.content[:40] + "...") if first_msg and len(first_msg.content) > 40 else (first_msg.content if first_msg else "Guest Chat")
        results.append({
            "convo_key": convo_key_val,
            "title": title,
            "msg_count": msg_count,
            "last_active": str(last_active),
        })

    return {"guest_conversations": results}


@router.get("/api/chat/guest-history", response_class=JSONResponse)
def guest_chat_history(
    request: Request,
    guest_convo_key: str = Query(...),
    db: Session = Depends(get_db),
):
    """Let a housemate view a guest's chat history."""
    device_token = request.cookies.get(DEVICE_COOKIE_NAME)
    identity = get_identity_record(db, device_token)

    if identity["is_guest"]:
        return JSONResponse({"error": "Forbidden"}, status_code=403)

    if not guest_convo_key.startswith("guest:"):
        return JSONResponse({"error": "Invalid convo key"}, status_code=400)

    msgs = (
        db.query(ChatMessage)
        .filter(ChatMessage.convo_key == guest_convo_key, ChatMessage.content != "")
        .order_by(ChatMessage.id.asc())
        .limit(50)
        .all()
    )

    return {
        "convo_key": guest_convo_key,
        "user": "Guest",
        "is_guest": True,
        "messages": [
            {
                "role": m.role, "content": m.content, "created_at": str(m.created_at),
                "model_used": m.model_used, "trace_id": m.trace_id, "action_type": m.action_type,
                "image_paths": _parse_image_paths(m.image_path),
            }
            for m in msgs
        ],
    }


@router.post("/api/chat/guest-reply", response_class=JSONResponse)
def reply_to_guest(
    request: Request,
    guest_convo_key: str = Form(...),
    message: str = Form(...),
    db: Session = Depends(get_db),
):
    """Let a housemate post a reply visible in a guest's conversation."""
    device_token = request.cookies.get(DEVICE_COOKIE_NAME)
    identity = get_identity_record(db, device_token)

    if identity["is_guest"]:
        return JSONResponse({"error": "Forbidden"}, status_code=403)

    if not guest_convo_key.startswith("guest:"):
        return JSONResponse({"error": "Invalid convo key"}, status_code=400)

    user_name = identity["user_name"]
    trace_id = new_trace_id()
    content = f"[{user_name}]: {message}"

    db.add(ChatMessage(
        convo_key=guest_convo_key,
        conversation_id=None,
        role="assistant",
        content=content,
        trace_id=trace_id,
        action_type="housemate_reply",
        model_used="human",
    ))
    db.commit()

    return {"ok": True, "trace_id": trace_id}
