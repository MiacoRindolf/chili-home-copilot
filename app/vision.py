"""Vision module: image upload handling and multi-model image understanding.

Supports local Ollama llava-llama3 (primary) and OpenAI GPT-4o-mini (fallback).
"""
import base64
import os
import uuid
from pathlib import Path

import requests
from openai import OpenAI

from .config import settings
from .logger import log_info
from .prompts import load_prompt

UPLOAD_DIR = Path(__file__).resolve().parent.parent / "data" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

ALLOWED_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
MAX_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB

OLLAMA_VISION_URL = f"{settings.ollama_host}/api/chat"
OLLAMA_VISION_MODEL = settings.ollama_vision_model
OPENAI_VISION_MODEL = settings.openai_vision_model

VISION_SYSTEM_PROMPT = load_prompt("vision_system")


def save_upload(file_bytes: bytes, filename: str, content_type: str) -> str | None:
    """Validate and save an uploaded image. Returns the filename on success, None on failure."""
    if not file_bytes or content_type not in ALLOWED_TYPES:
        return None

    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        ext = ".png"

    if len(file_bytes) > MAX_SIZE_BYTES:
        return None

    safe_name = f"{uuid.uuid4().hex}{ext}"
    dest = UPLOAD_DIR / safe_name
    dest.write_bytes(file_bytes)
    return safe_name


def _encode_image(image_path: str) -> tuple[str, str]:
    """Read an image file and return (base64_data, mime_type)."""
    full_path = UPLOAD_DIR / image_path
    data = full_path.read_bytes()
    b64 = base64.b64encode(data).decode("utf-8")
    ext = Path(image_path).suffix.lower()
    mime_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".gif": "image/gif", ".webp": "image/webp"}
    mime = mime_map.get(ext, "image/png")
    return b64, mime


def _build_ollama_images(image_paths: list[str]) -> list[str]:
    """Encode a list of image paths to base64 strings for the Ollama API."""
    return [_encode_image(p)[0] for p in image_paths]


def _build_openai_image_parts(image_paths: list[str]) -> list[dict]:
    """Build OpenAI content parts for multiple images."""
    parts = []
    for p in image_paths:
        b64, mime = _encode_image(p)
        parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
    return parts


_DEFAULT_VISION_PROMPT = (
    "Analyze this image thoroughly. Describe what you see in detail: "
    "identify the subject, key objects, text, colors, and context. "
    "Provide any useful observations or information."
)


def call_ollama_vision(image_paths: list[str], user_text: str, system_prompt: str = "", trace_id: str = "vision") -> str | None:
    """Call Ollama llava model with one or more images. Returns response text or None on failure."""
    try:
        b64_list = _build_ollama_images(image_paths)
        prompt = user_text or _DEFAULT_VISION_PROMPT

        payload = {
            "model": OLLAMA_VISION_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt or VISION_SYSTEM_PROMPT},
                {"role": "user", "content": prompt, "images": b64_list},
            ],
            "stream": False,
            "options": {"temperature": 0.6, "num_predict": 1024},
        }

        r = requests.post(OLLAMA_VISION_URL, json=payload, timeout=120)
        r.raise_for_status()
        content = r.json()["message"]["content"]
        log_info(trace_id, f"ollama_vision_ok model={OLLAMA_VISION_MODEL} images={len(image_paths)}")
        return content.strip()
    except Exception as e:
        log_info(trace_id, f"ollama_vision_error={e}")
        return None


def call_ollama_vision_stream(image_paths: list[str], user_text: str, system_prompt: str = "", trace_id: str = "vision"):
    """Stream response from Ollama llava model. Yields token strings."""
    try:
        b64_list = _build_ollama_images(image_paths)
        prompt = user_text or _DEFAULT_VISION_PROMPT

        payload = {
            "model": OLLAMA_VISION_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt or VISION_SYSTEM_PROMPT},
                {"role": "user", "content": prompt, "images": b64_list},
            ],
            "stream": True,
            "options": {"temperature": 0.6, "num_predict": 1024},
        }

        r = requests.post(OLLAMA_VISION_URL, json=payload, timeout=120, stream=True)
        r.raise_for_status()

        import json
        for line in r.iter_lines():
            if not line:
                continue
            chunk = json.loads(line)
            token = chunk.get("message", {}).get("content", "")
            if token:
                yield token
            if chunk.get("done"):
                break

        log_info(trace_id, f"ollama_vision_stream_ok model={OLLAMA_VISION_MODEL} images={len(image_paths)}")
    except Exception as e:
        log_info(trace_id, f"ollama_vision_stream_error={e}")


def call_openai_vision(image_paths: list[str], user_text: str, system_prompt: str = "", trace_id: str = "vision") -> str | None:
    """Call OpenAI with one or more images. Returns response text or None."""
    if not settings.premium_api_key_resolved():
        return None

    try:
        prompt = user_text or _DEFAULT_VISION_PROMPT
        user_content: list[dict] = [{"type": "text", "text": prompt}]
        user_content.extend(_build_openai_image_parts(image_paths))

        client = OpenAI(api_key=settings.premium_api_key_resolved())
        response = client.chat.completions.create(
            model=OPENAI_VISION_MODEL,
            messages=[
                {"role": "system", "content": system_prompt or VISION_SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            temperature=0.7,
            max_completion_tokens=1024,
        )

        reply = response.choices[0].message.content.strip()
        tokens = response.usage.total_tokens if response.usage else 0
        log_info(trace_id, f"openai_vision_ok model={OPENAI_VISION_MODEL} tokens={tokens} images={len(image_paths)}")
        return reply
    except Exception as e:
        log_info(trace_id, f"openai_vision_error={e}")
        return None


def call_openai_vision_stream(image_paths: list[str], user_text: str, system_prompt: str = "", trace_id: str = "vision"):
    """Stream response from OpenAI vision. Yields token strings."""
    if not settings.premium_api_key_resolved():
        return

    try:
        prompt = user_text or _DEFAULT_VISION_PROMPT
        user_content: list[dict] = [{"type": "text", "text": prompt}]
        user_content.extend(_build_openai_image_parts(image_paths))

        client = OpenAI(api_key=settings.premium_api_key_resolved())
        stream = client.chat.completions.create(
            model=OPENAI_VISION_MODEL,
            messages=[
                {"role": "system", "content": system_prompt or VISION_SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            temperature=0.7,
            max_completion_tokens=1024,
            stream=True,
        )

        for chunk in stream:
            delta = chunk.choices[0].delta if chunk.choices else None
            if delta and delta.content:
                yield delta.content

        log_info(trace_id, f"openai_vision_stream_ok model={OPENAI_VISION_MODEL} images={len(image_paths)}")
    except Exception as e:
        log_info(trace_id, f"openai_vision_stream_error={e}")


def describe_image(image_paths: list[str], user_text: str = "", system_prompt: str = "", trace_id: str = "vision") -> tuple[str, str]:
    """Try local vision first, fall back to OpenAI. Returns (reply, model_used)."""
    reply = call_ollama_vision(image_paths, user_text, system_prompt, trace_id)
    if reply:
        return reply, OLLAMA_VISION_MODEL

    reply = call_openai_vision(image_paths, user_text, system_prompt, trace_id)
    if reply:
        return reply, OPENAI_VISION_MODEL

    return "I couldn't analyze this image right now. Make sure Ollama is running with llava-llama3, or configure an OpenAI API key.", "none"


def describe_image_stream(image_paths: list[str], user_text: str = "", system_prompt: str = "", trace_id: str = "vision"):
    """Stream vision response. Yields (token, model_used) tuples; final yield has empty token."""
    tokens = []
    for tok in call_ollama_vision_stream(image_paths, user_text, system_prompt, trace_id):
        tokens.append(tok)
        yield tok, OLLAMA_VISION_MODEL

    if tokens:
        yield "", OLLAMA_VISION_MODEL
        return

    for tok in call_openai_vision_stream(image_paths, user_text, system_prompt, trace_id):
        tokens.append(tok)
        yield tok, OPENAI_VISION_MODEL

    if tokens:
        yield "", OPENAI_VISION_MODEL
        return

    yield "I couldn't analyze this image right now. Make sure Ollama is running with llava-llama3, or configure an OpenAI API key.", "none"
    yield "", "none"
