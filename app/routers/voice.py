"""Voice assistant API routes: transcription, command parsing, and TTS.

Voice UI is integrated directly into the chat page (chat.html).
"""
from fastapi import APIRouter, Depends, Request, UploadFile, File, Form
from fastapi.responses import JSONResponse, Response
from sqlalchemy.orm import Session

from ..deps import get_db
from ..services import voice_service

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


@router.get("/api/voice/capabilities")
def voice_capabilities():
    """Check available STT/TTS backends."""
    return JSONResponse(voice_service.get_voice_capabilities())
