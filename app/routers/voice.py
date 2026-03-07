"""Voice assistant API routes: transcription, command parsing, TTS, and wake-word stream.

Voice UI is integrated directly into the chat page (chat.html).
Desktop companion uses /ws/voice/stream for continuous openWakeWord + Whisper flow.
"""
import asyncio
import json

from fastapi import APIRouter, Depends, Query, Request, UploadFile, File, Form, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response, StreamingResponse
from sqlalchemy.orm import Session

from ..deps import get_db, get_convo_key
from ..logger import new_trace_id, log_info
from ..pairing import get_identity_record
from ..services import voice_service
from ..services.voice_stream import (
    VoiceStreamProcessor,
    process_utterance_pcm,
    SAMPLE_RATE,
    CHUNK_BYTES,
)
from ..services.chat_service import process_message_get_reply

router = APIRouter()


@router.post("/api/voice/transcribe")
async def voice_transcribe(
    audio: UploadFile = File(...),
    mime_type: str = Form("audio/webm"),
):
    """Transcribe uploaded audio using available STT backend."""
    audio_bytes = await audio.read()
    if not audio_bytes:
        return JSONResponse({"ok": False, "error": "Empty audio"}, status_code=400)

    result = voice_service.transcribe_audio(audio_bytes, mime_type)
    return JSONResponse(result)


@router.post("/api/voice/command")
async def voice_command(
    text: str = Form(...),
    db: Session = Depends(get_db),
):
    """Parse transcribed text for voice commands or route to chat."""
    parsed = voice_service.parse_voice_command(text)
    return JSONResponse(parsed)


@router.post("/api/voice/tts")
async def voice_tts(
    text: str = Form(...),
    voice: str = Form("aria"),
):
    """Generate natural-sounding speech audio from text (Qwen3-TTS primary, Edge fallback)."""
    if not text or not text.strip():
        return JSONResponse({"ok": False, "error": "No text provided"}, status_code=400)

    audio_bytes = await voice_service.text_to_speech(text.strip(), voice)
    if not audio_bytes:
        return JSONResponse({"ok": False, "error": "TTS generation failed"}, status_code=500)

    return Response(
        content=audio_bytes,
        media_type="audio/mpeg",
        headers={"Content-Disposition": "inline; filename=speech.mp3"},
    )


@router.get("/api/voice/tts/stream")
async def voice_tts_stream(text: str = Query(...)):
    """Stream MP3 audio as Edge TTS generates it — much lower time-to-first-byte
    than the buffered /api/voice/tts endpoint."""
    if not text or not text.strip():
        return JSONResponse({"ok": False, "error": "No text provided"}, status_code=400)

    return StreamingResponse(
        voice_service.text_to_speech_stream(text.strip()),
        media_type="audio/mpeg",
        headers={
            "Content-Disposition": "inline; filename=speech.mp3",
            "Cache-Control": "no-cache",
        },
    )


@router.get("/api/voice/capabilities")
def voice_capabilities():
    """Check available STT/TTS backends."""
    return JSONResponse(voice_service.get_voice_capabilities())


@router.websocket("/ws/voice/stream")
async def voice_stream_ws(websocket: WebSocket, db: Session = Depends(get_db)):
    """WebSocket: stream raw PCM 16 kHz 16-bit mono; openWakeWord wake + VAD + Whisper + chat.

    First message must be JSON: {"token": "optional_device_token", "wake_word": "Chili"}.
    Then send binary PCM chunks. Server sends JSON: {"type": "status", "message": "..."},
    {"type": "reply", "reply": "..."}, {"type": "error", "message": "..."}.
    """
    await websocket.accept()
    trace_id = new_trace_id()
    client_ip = websocket.client.host if websocket.client else "unknown"
    token = None
    wake_word = None
    processor = None

    try:
        # First message: config (JSON text)
        msg = await websocket.receive()
        raw = msg.get("text") or msg.get("bytes") or b"{}"
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="ignore")
        try:
            data = json.loads(raw) if isinstance(raw, str) and raw.strip().startswith("{") else {}
        except (json.JSONDecodeError, TypeError):
            data = {}
        token = data.get("token") or None
        wake_word = (data.get("wake_word") or "").strip() or None
        identity = get_identity_record(db, token)
        convo_key = get_convo_key(identity, token, client_ip)
        processor = VoiceStreamProcessor(client_wake_word=wake_word)
        await websocket.send_json({
            "type": "ready",
            "message": "Say 'Hey Mycroft' then your command.",
            "wake_word": wake_word or "Chili",
        })
    except Exception as e:
        log_info(trace_id, f"[voice_stream] config error: {e}")
        try:
            await websocket.send_json({"type": "error", "message": str(e)})
        except Exception:
            pass
        await websocket.close()
        return

    async def send_status(msg: str):
        try:
            await websocket.send_json({"type": "status", "message": msg})
        except Exception:
            pass

    async def send_reply(reply: str):
        try:
            await websocket.send_json({"type": "reply", "reply": reply})
        except Exception:
            pass

    try:
        await send_status("Listening for 'Hey Mycroft'...")
        buffer = bytearray()
        while True:
            try:
                message = await asyncio.wait_for(websocket.receive(), timeout=300.0)
            except asyncio.TimeoutError:
                await send_status("Listening...")
                continue
            if "bytes" in message:
                buffer.extend(message["bytes"])
            elif "text" in message:
                try:
                    data = json.loads(message["text"])
                    if data.get("type") == "ping":
                        await websocket.send_json({"type": "pong"})
                    continue
                except (json.JSONDecodeError, KeyError):
                    pass

            while len(buffer) >= CHUNK_BYTES:
                chunk = bytes(buffer[:CHUNK_BYTES])
                del buffer[:CHUNK_BYTES]
                segment = processor.feed(chunk)
                if segment is None:
                    continue
                await send_status("Transcribing...")
                command, raw_transcript = await asyncio.to_thread(
                    process_utterance_pcm, segment, wake_word
                )
                if not command:
                    await send_status("Listening for 'Hey Mycroft'...")
                    continue
                log_info(trace_id, f"[voice_stream] command={command!r}")
                await send_status(f"Processing: {command[:50]}...")
                try:
                    reply, _ = await asyncio.to_thread(
                        process_message_get_reply,
                        db, convo_key, identity, client_ip, command, trace_id,
                    )
                    await send_reply(reply or "No reply.")
                except Exception as e:
                    log_info(trace_id, f"[voice_stream] chat error: {e}")
                    await send_reply(f"Error: {e}")
                await send_status("Listening for 'Hey Mycroft'...")
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log_info(trace_id, f"[voice_stream] error: {e}")
        try:
            await websocket.send_json({"type": "error", "message": str(e)})
        except Exception:
            pass
    finally:
        if processor:
            processor.reset()
