"""Voice assistant routes: transcription, command execution, page."""
from fastapi import APIRouter, Depends, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..deps import get_db, get_identity_ctx
from ..services import voice_service

router = APIRouter()
templates = None


def init_templates(t: Jinja2Templates):
    global templates
    templates = t


@router.get("/voice", response_class=HTMLResponse)
def voice_page(request: Request, db: Session = Depends(get_db)):
    from ..pairing import DEVICE_COOKIE_NAME, get_identity
    token = request.cookies.get(DEVICE_COOKIE_NAME)
    user_name, _ = get_identity(db, token)
    return templates.TemplateResponse(request, "voice.html", {
        "user_name": user_name,
    })


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


@router.get("/api/voice/capabilities")
def voice_capabilities():
    """Check available STT/TTS backends."""
    return JSONResponse(voice_service.get_voice_capabilities())
