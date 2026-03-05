"""Intercom routes: WebSocket PTT hub, REST APIs for status/messages/consent."""
import json
import asyncio
from collections import defaultdict
from fastapi import APIRouter, Depends, Request, WebSocket, WebSocketDisconnect, Form, Query
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from ..deps import get_db
from ..models import User
from ..pairing import DEVICE_COOKIE_NAME, get_identity_record
from ..services import intercom_service as svc

router = APIRouter()


# ---------------------------------------------------------------------------
# In-memory WebSocket connection pool: {user_id: set[WebSocket]}
# ---------------------------------------------------------------------------
_pool: dict[int, set[WebSocket]] = defaultdict(set)
_user_names: dict[int, str] = {}


def _online_user_ids() -> set[int]:
    return {uid for uid, conns in _pool.items() if conns}


# ---------------------------------------------------------------------------
# Intercom page
# ---------------------------------------------------------------------------
@router.get("/intercom")
def intercom_page(request: Request):
    return request.app.state.templates.TemplateResponse(
        request, "intercom.html",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


# ---------------------------------------------------------------------------
# REST API: Status
# ---------------------------------------------------------------------------
@router.get("/api/intercom/status", response_class=JSONResponse)
def get_statuses(request: Request, db: Session = Depends(get_db)):
    device_token = request.cookies.get(DEVICE_COOKIE_NAME)
    identity = get_identity_record(db, device_token)
    if identity["is_guest"]:
        return JSONResponse({"error": "Paired housemates only"}, status_code=403)

    online = _online_user_ids()
    all_statuses = svc.get_all_statuses(db)
    for s in all_statuses:
        s["online"] = s["user_id"] in online
    my_status = svc.get_user_status(identity["user_id"], db)
    return {"my_status": my_status, "housemates": all_statuses, "user_id": identity["user_id"]}


@router.post("/api/intercom/status", response_class=JSONResponse)
def update_status(
    request: Request,
    status: str = Form("available"),
    dnd_minutes: int | None = Form(None),
    db: Session = Depends(get_db),
):
    device_token = request.cookies.get(DEVICE_COOKIE_NAME)
    identity = get_identity_record(db, device_token)
    if identity["is_guest"]:
        return JSONResponse({"error": "Paired housemates only"}, status_code=403)
    if status not in ("available", "dnd"):
        return JSONResponse({"error": "Status must be 'available' or 'dnd'"}, status_code=400)
    result = svc.set_user_status(identity["user_id"], status, dnd_minutes, db)
    return result


# ---------------------------------------------------------------------------
# REST API: Voice messages
# ---------------------------------------------------------------------------
@router.get("/api/intercom/messages", response_class=JSONResponse)
def list_messages(request: Request, unread_only: bool = Query(False), db: Session = Depends(get_db)):
    device_token = request.cookies.get(DEVICE_COOKIE_NAME)
    identity = get_identity_record(db, device_token)
    if identity["is_guest"]:
        return JSONResponse({"error": "Paired housemates only"}, status_code=403)
    if unread_only:
        msgs = svc.get_unread_messages(identity["user_id"], db)
    else:
        msgs = svc.get_all_messages(identity["user_id"], db)
    return {"messages": msgs}


@router.post("/api/intercom/messages/{message_id}/read", response_class=JSONResponse)
def mark_message_read(message_id: int, request: Request, db: Session = Depends(get_db)):
    device_token = request.cookies.get(DEVICE_COOKIE_NAME)
    identity = get_identity_record(db, device_token)
    if identity["is_guest"]:
        return JSONResponse({"error": "Paired housemates only"}, status_code=403)
    ok = svc.mark_read(message_id, identity["user_id"], db)
    return {"ok": ok}


@router.delete("/api/intercom/messages/{message_id}", response_class=JSONResponse)
def delete_message(message_id: int, request: Request, db: Session = Depends(get_db)):
    device_token = request.cookies.get(DEVICE_COOKIE_NAME)
    identity = get_identity_record(db, device_token)
    if identity["is_guest"]:
        return JSONResponse({"error": "Paired housemates only"}, status_code=403)
    ok = svc.delete_message(message_id, identity["user_id"], db)
    if not ok:
        return JSONResponse({"error": "Message not found or access denied"}, status_code=404)
    return {"ok": True}


# ---------------------------------------------------------------------------
# REST API: Consent
# ---------------------------------------------------------------------------
@router.get("/api/intercom/consent", response_class=JSONResponse)
def check_consent(request: Request, db: Session = Depends(get_db)):
    device_token = request.cookies.get(DEVICE_COOKIE_NAME)
    identity = get_identity_record(db, device_token)
    if identity["is_guest"]:
        return JSONResponse({"error": "Paired housemates only"}, status_code=403)
    return {"consented": svc.has_consent(identity["user_id"], db)}


@router.post("/api/intercom/consent", response_class=JSONResponse)
def grant_consent(request: Request, db: Session = Depends(get_db)):
    device_token = request.cookies.get(DEVICE_COOKIE_NAME)
    identity = get_identity_record(db, device_token)
    if identity["is_guest"]:
        return JSONResponse({"error": "Paired housemates only"}, status_code=403)
    svc.grant_consent(identity["user_id"], db)
    return {"ok": True}


@router.delete("/api/intercom/consent", response_class=JSONResponse)
def revoke_consent_api(request: Request, db: Session = Depends(get_db)):
    device_token = request.cookies.get(DEVICE_COOKIE_NAME)
    identity = get_identity_record(db, device_token)
    if identity["is_guest"]:
        return JSONResponse({"error": "Paired housemates only"}, status_code=403)
    svc.revoke_consent(identity["user_id"], db)
    return {"ok": True}


# ---------------------------------------------------------------------------
# WebSocket: Push-to-Talk hub
# ---------------------------------------------------------------------------
@router.websocket("/ws/intercom")
async def intercom_ws(ws: WebSocket, db: Session = Depends(get_db)):
    await ws.accept()

    cookie_header = ws.headers.get("cookie", "")
    token = None
    for part in cookie_header.split(";"):
        part = part.strip()
        if part.startswith(f"{DEVICE_COOKIE_NAME}="):
            token = part.split("=", 1)[1]
            break

    identity = get_identity_record(db, token)
    if identity["is_guest"] or not identity["user_id"]:
        await ws.send_json({"type": "ERROR", "message": "Authentication required. Pair your device first."})
        await ws.close(code=4001)
        return

    user_id = identity["user_id"]
    user_name = identity["user_name"]

    if not svc.has_consent(user_id, db):
        await ws.send_json({"type": "NEED_CONSENT"})
        await ws.close(code=4002)
        return

    _pool[user_id].add(ws)
    _user_names[user_id] = user_name

    await _broadcast_presence(db)

    audio_buffer: list[bytes] = []
    ptt_target: int | str | None = None
    ptt_targets_dnd: set[int] = set()
    ptt_targets_live: set[int] = set()

    try:
        while True:
            raw = await ws.receive()

            if raw["type"] == "websocket.disconnect":
                break

            if "bytes" in raw and raw["bytes"]:
                if ptt_target is not None:
                    chunk = raw["bytes"]
                    audio_buffer.append(chunk)
                    for tid in ptt_targets_live:
                        for peer_ws in list(_pool.get(tid, set())):
                            try:
                                await peer_ws.send_bytes(chunk)
                            except Exception:
                                _pool.get(tid, set()).discard(peer_ws)
                continue

            if "text" in raw and raw["text"]:
                try:
                    msg = json.loads(raw["text"])
                except json.JSONDecodeError:
                    continue

                msg_type = msg.get("type")

                if msg_type == "PTT_START":
                    target = msg.get("target")
                    sender_mime = msg.get("mime_type", "audio/webm")
                    audio_buffer = []
                    ptt_targets_dnd = set()
                    ptt_targets_live = set()
                    online_ids = _online_user_ids()

                    if target == "broadcast":
                        ptt_target = "broadcast"
                        for uid in online_ids:
                            if svc.is_dnd(uid, db):
                                ptt_targets_dnd.add(uid)
                            else:
                                ptt_targets_live.add(uid)
                    else:
                        target_id = int(target)
                        ptt_target = target_id
                        if svc.is_dnd(target_id, db):
                            ptt_targets_dnd.add(target_id)
                        else:
                            ptt_targets_live.add(target_id)

                    if ptt_targets_dnd and not ptt_targets_live and ptt_target != "broadcast":
                        await ws.send_json({
                            "type": "PTT_DND",
                            "message": "They're on Do Not Disturb — not receiving.",
                        })

                    for tid in ptt_targets_live:
                        for peer_ws in list(_pool.get(tid, set())):
                            try:
                                await peer_ws.send_json({
                                    "type": "PTT_INCOMING",
                                    "from_user_id": user_id,
                                    "from_name": user_name,
                                    "is_broadcast": ptt_target == "broadcast",
                                    "mime_type": sender_mime,
                                })
                            except Exception:
                                _pool.get(tid, set()).discard(peer_ws)

                elif msg_type == "PTT_END":
                    if ptt_target is None:
                        continue
                    live_set = set(ptt_targets_live)
                    audio_buffer = []
                    ptt_target = None
                    ptt_targets_dnd = set()
                    ptt_targets_live = set()

                    for tid in live_set:
                        for peer_ws in list(_pool.get(tid, set())):
                            try:
                                await peer_ws.send_json({"type": "PTT_END", "from_user_id": user_id})
                            except Exception:
                                _pool.get(tid, set()).discard(peer_ws)

                elif msg_type == "STATUS_UPDATE":
                    status = msg.get("status", "available")
                    dnd_minutes = msg.get("dnd_minutes")
                    svc.set_user_status(user_id, status, dnd_minutes, db)
                    await _broadcast_presence(db)

    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        _pool[user_id].discard(ws)
        if not _pool[user_id]:
            _pool.pop(user_id, None)
        await _broadcast_presence(db)


async def _broadcast_presence(db: Session | None = None):
    """Notify all connected clients: who is online and full status (if db provided)."""
    online_ids = _online_user_ids()
    online_names = {uid: _user_names.get(uid, "?") for uid in online_ids}
    payload = {"type": "PRESENCE", "online": online_names}
    if db is not None:
        housemates = svc.get_all_statuses(db)
        for h in housemates:
            h["online"] = h["user_id"] in online_ids
        payload["housemates"] = housemates
    msg = json.dumps(payload)
    for uid, conns in list(_pool.items()):
        for ws in list(conns):
            try:
                await ws.send_text(msg)
            except Exception:
                conns.discard(ws)


async def broadcast_chili_audio(audio_bytes: bytes, duration_ms: int, db: Session):
    """Live-only: broadcast CHILI TTS to connected users who are not DND. No persistence."""
    for uid, conns in list(_pool.items()):
        if svc.is_dnd(uid, db):
            continue
        for peer_ws in list(conns):
            try:
                await peer_ws.send_json({
                    "type": "PTT_INCOMING",
                    "from_user_id": None,
                    "from_name": "CHILI",
                    "is_broadcast": True,
                })
                await peer_ws.send_bytes(audio_bytes)
                await peer_ws.send_json({"type": "PTT_END", "from_user_id": None})
            except Exception:
                conns.discard(peer_ws)
