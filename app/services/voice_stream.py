"""Voice stream: openWakeWord-based wake detection + VAD + Whisper for desktop wake-word flow.

Expects raw PCM 16-bit mono 16 kHz. Uses openWakeWord (built-in model, e.g. hey_mycroft)
to detect wake; then buffers until end-of-utterance (silence); transcribes with Whisper;
strips wake phrase and optional client wake_word; returns command for chat.
"""
from __future__ import annotations

import asyncio
import io
import struct
import tempfile
from pathlib import Path
from typing import Optional

from ..logger import log_info, new_trace_id

# Sample rate and format expected by openWakeWord and this pipeline
SAMPLE_RATE = 16000
BYTES_PER_SAMPLE = 2
CHUNK_SAMPLES = 1280  # 80 ms at 16 kHz
CHUNK_BYTES = CHUNK_SAMPLES * BYTES_PER_SAMPLE

# VAD: min RMS (0–32767) to count as speech; silence gap (seconds) to end utterance
VAD_RMS_THRESHOLD = 200.0
SILENCE_DURATION_SEC = 0.7
# Min speech duration after wake to avoid false triggers
MIN_SPEECH_AFTER_WAKE_SEC = 0.3

_oww_model: Optional["Model"] = None


def _get_oww_model():
    """Lazy-load openWakeWord model (built-in e.g. hey_mycroft)."""
    global _oww_model
    if _oww_model is not None and _oww_model is not False:
        return _oww_model
    if _oww_model is False:
        return None
    try:
        from openwakeword.model import Model
        _oww_model = Model(inference_framework="onnx")
        return _oww_model
    except Exception as e:
        log_info(new_trace_id(), f"[voice_stream] openWakeWord load failed: {e}")
        _oww_model = False
        return None


def _rms_from_pcm(pcm_bytes: bytes) -> float:
    """Compute RMS of 16-bit PCM (signed)."""
    if len(pcm_bytes) < 2:
        return 0.0
    n = len(pcm_bytes) // 2
    fmt = f"<{n}h"
    try:
        samples = struct.unpack(fmt, pcm_bytes[: n * 2])
    except struct.error:
        return 0.0
    if not samples:
        return 0.0
    sum_sq = sum(s * s for s in samples)
    return (sum_sq / len(samples)) ** 0.5


def _strip_wake_phrase(transcript: str, client_wake_word: Optional[str] = None) -> str:
    """Remove leading 'hey mycroft' and optional client wake word (e.g. 'chili') from transcript."""
    t = transcript.strip().lower()
    # Strip "hey mycroft" (openWakeWord trigger)
    for prefix in ("hey mycroft", "hey mycroft,", "hey mycroft "):
        if t.startswith(prefix):
            t = t[len(prefix) :].strip()
            break
    if not client_wake_word:
        return t.strip()
    c = client_wake_word.strip().lower()
    if not c:
        return t.strip()
    for prefix in (f"{c},", f"{c} ", f"hey {c},", f"hey {c} "):
        if t.startswith(prefix):
            t = t[len(prefix) :].strip()
            break
    if t.startswith(c) and (len(t) == len(c) or t[len(c) : len(c) + 1] in (" ", ",")):
        t = t[len(c) :].lstrip(" ,")
    return t.strip()


def _pcm_to_wav_bytes(pcm: bytes) -> bytes:
    """Wrap 16-bit mono PCM (16 kHz) in a minimal WAV header."""
    n = len(pcm)
    buf = io.BytesIO()
    buf.write(b"RIFF")
    buf.write(struct.pack("<I", 36 + n))
    buf.write(b"WAVEfmt ")
    buf.write(struct.pack("<IHHIIHH", 16, 1, 1, SAMPLE_RATE, SAMPLE_RATE * 2, 2, 16))
    buf.write(b"data")
    buf.write(struct.pack("<I", n))
    buf.write(pcm)
    return buf.getvalue()


class VoiceStreamProcessor:
    """Processes a stream of PCM chunks: openWakeWord wake + VAD end-of-utterance + transcript."""

    def __init__(self, client_wake_word: Optional[str] = None):
        self.client_wake_word = (client_wake_word or "").strip() or None
        self._model = _get_oww_model()
        self._pcm_buffer = bytearray()
        self._state = "listening"  # listening | capturing
        self._speech_start_byte: Optional[int] = None
        self._silence_start_byte: Optional[int] = None
        self._wake_byte_offset: Optional[int] = None

    def feed(self, pcm_chunk: bytes) -> Optional[bytes]:
        """Feed a PCM chunk. Returns None or the PCM segment to transcribe (from wake to end of speech)."""
        if not pcm_chunk:
            return None

        self._pcm_buffer.extend(pcm_chunk)

        if self._model is None:
            return None

        # Run openWakeWord on full chunks (only when listening)
        if self._state == "listening":
            while len(self._pcm_buffer) >= CHUNK_BYTES:
                frame = bytes(self._pcm_buffer[:CHUNK_BYTES])
                del self._pcm_buffer[:CHUNK_BYTES]

                import numpy as np
                audio = np.frombuffer(frame, dtype=np.int16)
                self._model.predict(audio)
                for name, scores in (self._model.prediction_buffer or {}).items():
                    if scores and scores[-1] > 0.5:
                        self._state = "capturing"
                        self._wake_byte_offset = 0
                        self._speech_start_byte = None
                        self._silence_start_byte = None
                        break
                else:
                    continue
                break

        if self._state != "capturing":
            # Keep buffer bounded while listening
            if len(self._pcm_buffer) > CHUNK_BYTES * 20:
                del self._pcm_buffer[: CHUNK_BYTES * 10]
            return None

        # In capturing: find speech then silence in the buffer
        silence_required_bytes = int(SAMPLE_RATE * SILENCE_DURATION_SEC * BYTES_PER_SAMPLE)
        min_speech_bytes = int(SAMPLE_RATE * MIN_SPEECH_AFTER_WAKE_SEC * BYTES_PER_SAMPLE)
        pos = 0
        while pos + CHUNK_BYTES <= len(self._pcm_buffer):
            chunk = bytes(self._pcm_buffer[pos : pos + CHUNK_BYTES])
            rms = _rms_from_pcm(chunk)
            is_speech = rms >= VAD_RMS_THRESHOLD

            if is_speech:
                self._silence_start_byte = None
                if self._speech_start_byte is None:
                    self._speech_start_byte = pos
            else:
                if self._speech_start_byte is not None:
                    if self._silence_start_byte is None:
                        self._silence_start_byte = pos
                    elif pos - self._silence_start_byte >= silence_required_bytes:
                        end_byte = self._silence_start_byte
                        start_byte = self._wake_byte_offset or 0
                        if end_byte - start_byte >= min_speech_bytes:
                            segment = bytes(self._pcm_buffer[start_byte:end_byte])
                            del self._pcm_buffer[: end_byte + silence_required_bytes]
                            self._state = "listening"
                            self._speech_start_byte = None
                            self._silence_start_byte = None
                            self._wake_byte_offset = None
                            return segment
                else:
                    self._silence_start_byte = None
            pos += CHUNK_BYTES

        return None

    def reset(self):
        """Reset to listening state."""
        self._state = "listening"
        self._pcm_buffer.clear()
        self._speech_start_byte = None
        self._silence_start_byte = None
        self._wake_byte_offset = None


def transcribe_pcm_to_text(pcm_bytes: bytes) -> Optional[str]:
    """Transcribe 16-bit 16 kHz mono PCM using Whisper (via existing voice_service)."""
    if len(pcm_bytes) < SAMPLE_RATE * BYTES_PER_SAMPLE * 2:  # at least ~2 s
        return None
    wav = _pcm_to_wav_bytes(pcm_bytes)
    from . import voice_service
    result = voice_service.transcribe_audio(wav, "audio/wav")
    if result.get("ok") and result.get("text"):
        return result["text"].strip()
    return None


def _transcript_starts_with_wake(transcript: str, client_wake_word: Optional[str]) -> bool:
    """True if transcript starts with client wake word or 'hey <wake_word>'."""
    if not transcript or not client_wake_word:
        return False
    t = transcript.strip().lower()
    c = client_wake_word.strip().lower()
    if t.startswith(c) and (len(t) == len(c) or t[len(c) : len(c) + 1] in (" ", ",")):
        return True
    if t.startswith("hey ") and t[4:].strip().startswith(c):
        return True
    return False


def process_utterance_pcm(
    pcm_bytes: bytes,
    client_wake_word: Optional[str] = None,
    require_wake_in_transcript: bool = False,
) -> tuple[Optional[str], Optional[str]]:
    """
    Transcribe PCM and strip wake phrase.
    Returns (command, raw_transcript). command is None if transcript is empty after stripping,
    or if require_wake_in_transcript and transcript doesn't start with wake phrase.
    """
    raw = transcribe_pcm_to_text(pcm_bytes)
    if not raw:
        return None, raw
    if require_wake_in_transcript and not _transcript_starts_with_wake(raw, client_wake_word):
        return None, raw
    command = _strip_wake_phrase(raw, client_wake_word)
    return (command if command else None), raw
