"""Ross-stream AUDIO -> transcript (WASAPI loopback + faster-whisper).

Captures SYSTEM AUDIO (whatever is playing on the default output — Ross Cameron's Warrior live
stream), transcribes rolling chunks with faster-whisper, appends {ts, text} to transcript.jsonl so
the watch loop can read Ross's verbal commentary (his REASONING — the richest "how he thinks" signal)
and cross-reference it with CHILI in real time. Personal real-time analysis use (operator's own
subscription + machine; not redistributed). Mirrors the IQFeed bridge daemon pattern.

Usage: python scripts/ross_audio_transcribe.py [--seconds N] [--model base.en] [--device-substr ASUS]
  --seconds N    : bounded run for testing (default: run forever)
  --model        : faster-whisper model (base.en fast / small.en better)
  --device-substr: pick the loopback whose name contains this (skips the level probe)

HANG-PROOF DESIGN (root-cause fix): WASAPI loopback `stream.read()` BLOCKS INDEFINITELY when the
output device is SILENT (no frames are delivered). The old probe looped a blocking read on each
device and hung on the first silent one, so it never found the active device. This version uses a
CALLBACK-based stream (`stream_callback=`) that PyAudio drives on its own thread and pushes frames
into a bounded queue; the main loop only drains the queue (never blocks). The device probe opens a
callback stream per device and measures the level over a bounded wall-clock window, so a silent
device yields level~=0 and the probe always completes within a few seconds.
"""
from __future__ import annotations

import datetime
import json
import os
import queue
import re
import sys
import time
from math import gcd

import numpy as np
from scipy.signal import resample_poly

pa = None

OUT_DIR = r"D:\CHILI-Docker\chili-data\ross_stream"
OUT = os.path.join(OUT_DIR, "transcript.jsonl")
RAW_OUT = os.environ.get("ROSS_RAW_TRANSCRIPT_PATH", os.path.join(OUT_DIR, "raw_transcript.jsonl"))
WARRIOR_SESSION_OK_PATH = os.environ.get(
    "ROSS_WARRIOR_SESSION_OK_PATH",
    os.path.join(OUT_DIR, "warrior_session_ok.json"),
)
WARRIOR_SESSION_OK_MAX_AGE_SECONDS = float(os.environ.get("ROSS_WARRIOR_SESSION_OK_MAX_AGE_SECONDS", "30"))
WARRIOR_BROWSER_STATE_PATH = os.environ.get(
    "ROSS_WARRIOR_BROWSER_STATE_PATH",
    os.path.join(OUT_DIR, "warrior_browser_state_latest.json"),
)
WARRIOR_BROWSER_STATE_MAX_AGE_SECONDS = float(os.environ.get("ROSS_WARRIOR_BROWSER_STATE_MAX_AGE_SECONDS", "30"))
REQUIRE_WARRIOR_SESSION_OK = os.environ.get("ROSS_TRANSCRIPT_REQUIRE_WARRIOR_SESSION_OK", "1").strip().lower() not in (
    "0",
    "false",
    "no",
)
RAW_TRANSCRIPT_ENABLED = os.environ.get("ROSS_RAW_TRANSCRIPT_ENABLED", "1").strip().lower() not in (
    "0",
    "false",
    "no",
)
CHUNK_SECONDS = float(os.environ.get("ROSS_CHUNK_SECONDS", "12"))
MODEL = os.environ.get("ROSS_WHISPER_MODEL", "base.en")
WHISPER_SR = 16000
FRAMES_PER_BUFFER = 4096
# Per-device probe window: bounded wall clock so a silent device returns quickly instead of hanging.
PROBE_SECONDS = float(os.environ.get("ROSS_PROBE_SECONDS", "1.5"))
# Minimum mean-amplitude to consider a loopback "active" (playing the stream).
ACTIVE_LEVEL = float(os.environ.get("ROSS_ACTIVE_LEVEL", "0.0015"))
# Cap the frame queue so a stalled consumer can never grow memory without bound. Each item is one
# callback buffer (~4096 frames). 400 buffers @ 48k/2ch ~= 34s of audio — plenty of headroom.
QUEUE_MAX = int(os.environ.get("ROSS_QUEUE_MAX", "400"))
# After this many seconds of continuous SILENCE, exit the capture loop so the outer loop RE-PROBES and
# re-picks the loudest loopback. This makes the daemon ADAPTIVE: start it anytime, and when the operator
# begins playing a video on whatever output device, capture migrates to that device within one cycle.
REPROBE_SILENCE_SEC = float(os.environ.get("ROSS_REPROBE_SILENCE_SEC", "45"))
MARKER_INVALID_BACKOFF_SECONDS = float(os.environ.get("ROSS_MARKER_INVALID_BACKOFF_SECONDS", "5"))
REQUIRE_TRADING_CONTEXT = os.environ.get("ROSS_TRANSCRIPT_REQUIRE_TRADING_CONTEXT", "1").strip().lower() not in (
    "0",
    "false",
    "no",
)
_WORD_SYMBOL_RE = re.compile(r"\b[A-Z]{2,5}\b")
_DASHED_SYMBOL_RE = re.compile(r"\b(?:[A-Z]\s*-\s*){1,4}[A-Z]\b")
_STRONG_TRADING_CONTEXT_RE = re.compile(
    r"\b("
    r"add|ask|bid|break(?:ing|out)?|candle|chart|curl|daily|entry|exit|"
    r"float|gap(?:per)?|halt|high(?:\s+of\s+day|\s+day)?|hod|level|"
    r"low|lod|momentum|offer|premarket|price|pullback|"
    r"reclaim|red|resistance|risk|runner|scalp|scanner|share[s]?|"
    r"starter|stock|stop|support|ticker|trade|trading|volume|vwap"
    r")\b",
    re.IGNORECASE,
)
_HIGH_CONFIDENCE_MARKET_RE = re.compile(
    r"\b("
    r"ask|bid|break(?:ing|out)?|candle|curl|entry|exit|float|gap(?:per)?|halt|"
    r"high(?:\s+of\s+day|\s+day)?|hod|lod|momentum|premarket|pullback|reclaim|"
    r"runner|scalp|scanner|share[s]?|ticker|trade|trading|volume|vwap"
    r")\b",
    re.IGNORECASE,
)
_SOFT_TRADING_CONTEXT_RE = re.compile(
    r"\b(long|watch(?:ing)?|interested(?:\s+in)?|interesting(?:\s+to\s+me)?)\b",
    re.IGNORECASE,
)
_OVER_MARKET_CONTEXT_RE = re.compile(
    r"\bover\s+(?:\$?\d+(?:\.\d+)?|vwap|high|hod|pre[-\s]?market\s+high)\b",
    re.IGNORECASE,
)
_NON_TRADING_CONTEXT_RE = re.compile(
    r"\b("
    r"long\s+conversation|watch(?:ing)?\s+(?:our\s+)?(?:show|video|episode|movie|tv|clip|tiktok)|"
    r"qr\s+code"
    r")\b",
    re.IGNORECASE,
)
_RECAP_CONTEXT_RE = re.compile(
    r"\b("
    r"(?:this|that)\s+was\s+(?:the\s+)?(?:first|second|third|stock|trade)|"
    r"(?:i|we)\s+(?:already\s+)?traded\b|"
    r"(?:i|we)\s+was\s+trading\b|"
    r"\bi\s+thought\s+(?:it\s+)?was\b|"
    r"\bwhich\s+i\s+thought\s+was\b|"
    r"\bwas\s+pretty\s+good\b|"
    r"\bhits?\s+(?:the\s+)?(?:running\s+up\s+)?scanner\s+at\s+\d{1,2}(?::?\d{2})?\b|"
    r"(?:we(?:'ll| will)|i(?:'ll| will))\s+break\s+(?:this|that|it|one)\s+down|"
    r"break\s+(?:this|that|it|one)\s+down\s+in\s+a\s+second|"
    r"glad\s+to\s+see\s+it\s+moving\s+higher"
    r")\b",
    re.IGNORECASE,
)
_STOP_SYMBOLS = frozenset(
    {
        "AI",
        "AM",
        "API",
        "ASK",
        "BID",
        "CEO",
        "CFO",
        "CTO",
        "DMA",
        "ECN",
        "EDT",
        "EMA",
        "EST",
        "ET",
        "FDA",
        "GDP",
        "HOD",
        "IPO",
        "LOD",
        "LULD",
        "MACD",
        "NASDAQ",
        "NBBO",
        "NYSE",
        "ORB",
        "OTC",
        "PDT",
        "PM",
        "PST",
        "QR",
        "RTH",
        "SEC",
        "SIM",
        "SMA",
        "SSR",
        "US",
        "UTC",
        "VWAP",
    }
)


# ----------------------------------------------------------------------------------------------
# Deterministic helpers (unit-tested in tests/test_ross_audio_transcribe.py — no live audio needed)
# ----------------------------------------------------------------------------------------------

def chunk_byte_target(rate: int, channels: int, chunk_seconds: float = CHUNK_SECONDS) -> int:
    """Bytes of int16 PCM that make up one `chunk_seconds` transcription chunk at rate*channels."""
    return int(rate * chunk_seconds) * channels * 2  # int16 = 2 bytes/sample


def downmix_to_mono(audio: "np.ndarray", channels: int) -> "np.ndarray":
    """Interleaved float32 -> mono float32 by averaging channels."""
    if channels > 1:
        return audio.reshape(-1, channels).mean(axis=1).astype(np.float32)
    return audio.astype(np.float32)


def resample_to_whisper(audio: "np.ndarray", rate: int, target: int = WHISPER_SR) -> "np.ndarray":
    """Resample mono float32 from `rate` to `target` (16k) for Whisper. Uses polyphase resampling
    with the reduced up/down ratio. Output length ~= len(audio) * target / rate."""
    if rate == target:
        return audio.astype(np.float32)
    g = gcd(target, rate)
    return resample_poly(audio, target // g, rate // g).astype(np.float32)


def transcript_row(text: str, ts: "datetime.datetime | None" = None) -> dict:
    """The JSONL row shape appended to transcript.jsonl: {"ts": iso8601-utc, "text": str}."""
    if ts is None:
        ts = datetime.datetime.now(datetime.timezone.utc)
    return {"ts": ts.isoformat(), "text": text}


def append_transcript(path: str, row: dict) -> None:
    """Append one JSONL row (newline-terminated, utf-8)."""
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


def _extract_uppercase_tickers(text: str) -> "list[str]":
    out: list[str] = []
    seen: set[str] = set()
    for match in _DASHED_SYMBOL_RE.finditer(str(text or "")):
        sym = re.sub(r"[^A-Z]", "", match.group(0).upper())
        if 2 <= len(sym) <= 5 and sym not in _STOP_SYMBOLS and sym not in seen:
            seen.add(sym)
            out.append(sym)
    for match in _WORD_SYMBOL_RE.finditer(str(text or "")):
        sym = match.group(0).upper()
        if 2 <= len(sym) <= 5 and sym not in _STOP_SYMBOLS and sym not in seen:
            seen.add(sym)
            out.append(sym)
    return out


def has_trading_context(text: str) -> bool:
    text_s = str(text or "")
    if not text_s:
        return False
    if _NON_TRADING_CONTEXT_RE.search(text_s):
        return False
    if _RECAP_CONTEXT_RE.search(text_s):
        return False
    if _DASHED_SYMBOL_RE.search(text_s):
        return True
    tickers = _extract_uppercase_tickers(text_s)
    if tickers and (_STRONG_TRADING_CONTEXT_RE.search(text_s) or _SOFT_TRADING_CONTEXT_RE.search(text_s)):
        return True
    if tickers and _OVER_MARKET_CONTEXT_RE.search(text_s):
        return True
    if _HIGH_CONFIDENCE_MARKET_RE.search(text_s):
        return True
    strong_terms = _STRONG_TRADING_CONTEXT_RE.findall(text_s)
    return len(strong_terms) >= 2


def should_append_transcript(text: str, *, require_trading_context: bool = REQUIRE_TRADING_CONTEXT) -> bool:
    accepted, _reason = transcript_acceptance(text, require_trading_context=require_trading_context)
    return accepted


def _parse_marker_ts(value) -> "datetime.datetime | None":
    if isinstance(value, datetime.datetime):
        dt = value
    elif isinstance(value, str) and value.strip():
        raw = value.strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            dt = datetime.datetime.fromisoformat(raw)
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc)


def refresh_warrior_session_marker_from_state(
    *,
    state_path: str | None = None,
    marker_path: str | None = None,
    max_state_age_seconds: float = WARRIOR_BROWSER_STATE_MAX_AGE_SECONDS,
    now: "datetime.datetime | None" = None,
) -> "tuple[bool, str]":
    """Refresh marker from a fresh browser-state probe before rejecting audio capture."""
    state_path = state_path or WARRIOR_BROWSER_STATE_PATH
    marker_path = marker_path or WARRIOR_SESSION_OK_PATH
    if not state_path or not os.path.isfile(state_path):
        return False, "warrior_browser_state_missing"
    try:
        now_dt = (now or datetime.datetime.now(datetime.timezone.utc)).astimezone(datetime.timezone.utc)
        mtime = datetime.datetime.fromtimestamp(os.path.getmtime(state_path), tz=datetime.timezone.utc)
        age_s = max(0.0, (now_dt - mtime).total_seconds())
        if age_s > max(1.0, float(max_state_age_seconds or 30.0)):
            return False, "warrior_browser_state_stale"
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if root not in sys.path:
            sys.path.insert(0, root)
        from scripts.warrior_session_marker import refresh_marker_from_state_file

        _path, marker = refresh_marker_from_state_file(
            state_path,
            out_path=marker_path,
            max_state_age_seconds=max_state_age_seconds,
            now=now_dt,
        )
        return bool(marker.get("ok")), str(marker.get("reason") or "warrior_session_marker_not_ok")
    except Exception as exc:
        return False, f"warrior_browser_state_refresh_failed:{type(exc).__name__}"


def warrior_session_marker_acceptance(
    path: str = WARRIOR_SESSION_OK_PATH,
    *,
    now: "datetime.datetime | None" = None,
    max_age_seconds: float = WARRIOR_SESSION_OK_MAX_AGE_SECONDS,
) -> "tuple[bool, str]":
    """Return whether the audio transcript may feed trading rows right now."""
    refresh_warrior_session_marker_from_state(marker_path=path, now=now)
    if not os.path.isfile(path):
        return False, "warrior_session_marker_missing"
    try:
        with open(path, "r", encoding="utf-8") as fh:
            row = json.loads(fh.read() or "{}")
    except Exception:
        return False, "warrior_session_marker_invalid"
    if not isinstance(row, dict) or row.get("ok") is not True:
        reason = str(row.get("reason") or "").strip() if isinstance(row, dict) else ""
        if not reason or reason == "warrior_session_ok":
            reason = "warrior_session_marker_not_ok"
        return False, reason
    ts = _parse_marker_ts(row.get("ts") or row.get("checked_at") or row.get("updated_at"))
    if ts is None:
        return False, "warrior_session_marker_missing_ts"
    now_dt = (now or datetime.datetime.now(datetime.timezone.utc)).astimezone(datetime.timezone.utc)
    age_s = max(0.0, (now_dt - ts).total_seconds())
    if age_s > max(1.0, float(max_age_seconds or 30.0)):
        return False, "warrior_session_marker_stale"
    if int(row.get("video_count") or 0) <= 0 and not bool(row.get("stream_visible")):
        return False, "warrior_session_marker_no_stream"
    return True, "warrior_session_ok"


def transcript_feed_acceptance(
    text: str,
    *,
    require_trading_context: bool = REQUIRE_TRADING_CONTEXT,
    require_warrior_session_ok: bool = REQUIRE_WARRIOR_SESSION_OK,
    marker_checker=warrior_session_marker_acceptance,
) -> "tuple[bool, str]":
    """Return whether text may be appended to the trading transcript feed."""
    accepted, reason = transcript_acceptance(text, require_trading_context=require_trading_context)
    if not accepted:
        return accepted, reason
    if require_warrior_session_ok:
        marker_ok, marker_reason = marker_checker()
        if not marker_ok:
            return False, marker_reason
    return True, reason


def audio_capture_acceptance(
    *,
    require_warrior_session_ok: bool = REQUIRE_WARRIOR_SESSION_OK,
    marker_checker=warrior_session_marker_acceptance,
) -> "tuple[bool, str]":
    """Return whether the daemon may keep capturing system audio right now."""
    if not require_warrior_session_ok:
        return True, "capture_marker_disabled"
    marker_ok, marker_reason = marker_checker()
    if not marker_ok:
        return False, marker_reason
    return True, marker_reason


def marker_invalid_backoff_seconds(
    reason: str,
    *,
    default_seconds: float = MARKER_INVALID_BACKOFF_SECONDS,
) -> float:
    """Backoff before retrying audio setup when the Warrior stream marker is invalid."""
    if str(reason or "") in {"warrior_session_ok", "capture_marker_disabled"}:
        return 0.0
    try:
        return max(1.0, float(default_seconds))
    except (TypeError, ValueError):
        return 5.0


def transcript_acceptance(
    text: str,
    *,
    require_trading_context: bool = REQUIRE_TRADING_CONTEXT,
) -> "tuple[bool, str]":
    """Return whether text may feed CHILI's trading transcript and the audit reason."""
    if len(str(text or "").strip()) < 3:
        return False, "too_short"
    if not require_trading_context:
        return True, "context_filter_disabled"
    if has_trading_context(text):
        return True, "trading_context"
    return False, "non_trading_audio"


def raw_transcript_row(
    text: str,
    *,
    accepted: bool,
    reason: str,
    ts: "datetime.datetime | None" = None,
) -> dict:
    row = transcript_row(text, ts=ts)
    row.update({"accepted": accepted, "reason": reason})
    return row


def pick_loudest(levels: "list[tuple]", threshold: float = ACTIVE_LEVEL):
    """Given [(device, level), ...] return the device with the highest level if it clears
    `threshold`, else None. Deterministic tie-break: first-seen wins (stable order)."""
    best, bestlvl = None, threshold
    for dev, lvl in levels:
        if lvl > bestlvl:
            best, bestlvl = dev, lvl
    return best


# ----------------------------------------------------------------------------------------------
# Audio capture (callback-based -> queue; never blocks the main loop)
# ----------------------------------------------------------------------------------------------

def _open_callback_stream(p: "pa.PyAudio", dev, q: "queue.Queue"):
    """Open a non-blocking input stream on `dev` that pushes raw int16 PCM bytes into `q`."""
    rate = int(dev["defaultSampleRate"])
    ch = int(dev["maxInputChannels"]) or 2

    def _cb(in_data, frame_count, time_info, status):
        try:
            q.put_nowait(in_data)
        except queue.Full:
            try:
                q.get_nowait()  # drop the oldest buffer; keep the freshest audio
                q.put_nowait(in_data)
            except queue.Empty:
                pass
        return (None, pa.paContinue)

    audio_mod = _audio_module()
    stream = p.open(format=audio_mod.paInt16, channels=ch, rate=rate, input=True,
                    input_device_index=dev["index"], frames_per_buffer=FRAMES_PER_BUFFER,
                    stream_callback=_cb)
    return stream, rate, ch


def _device_level(p: "pa.PyAudio", dev, secs: float = PROBE_SECONDS) -> float:
    """Mean |amplitude| captured from `dev` over a bounded wall-clock window. A SILENT loopback
    delivers no callbacks, so the queue stays empty and this returns ~0 once the timer expires —
    it NEVER blocks (the old blocking-read probe hung here on silent devices)."""
    q: "queue.Queue[bytes]" = queue.Queue(maxsize=QUEUE_MAX)
    stream = None
    try:
        stream, _rate, _ch = _open_callback_stream(p, dev, q)
        stream.start_stream()
        deadline = time.monotonic() + secs
        chunks = []
        while time.monotonic() < deadline:
            try:
                chunks.append(q.get(timeout=0.1))
            except queue.Empty:
                continue
        if not chunks:
            return 0.0
        a = np.frombuffer(b"".join(chunks), dtype=np.int16).astype(np.float32) / 32768.0
        if a.size == 0:
            return 0.0
        return float(np.abs(a).mean())
    except Exception:
        return 0.0
    finally:
        if stream is not None:
            try:
                stream.stop_stream(); stream.close()
            except Exception:
                pass


def _pick_loopback(p: "pa.PyAudio", substr: "str | None"):
    """Pick the WASAPI loopback device. With --device-substr, match by name (skips probing).
    Otherwise PROBE every loopback for live audio level (bounded, never hangs) and pick the LOUDEST
    active one — falling back to the default-output loopback when all are silent (e.g. Ross paused)."""
    devs = list(p.get_loopback_device_info_generator())
    if not devs:
        return None
    if substr:
        for d in devs:
            if substr.lower() in d["name"].lower():
                print("  -> --device-substr match [%d] %s" % (d["index"], d["name"][:48]), flush=True)
                return d
        print("  -> --device-substr '%s' matched nothing; falling back to probe" % substr, flush=True)
    # bounded level-probe: the loudest active device is where the audio plays
    levels = []
    for d in devs:
        lvl = _device_level(p, d)
        levels.append((d, lvl))
        print("  probe [%d] %-48s level=%.5f" % (d["index"], d["name"][:48], lvl), flush=True)
    best = pick_loudest(levels)
    if best is not None:
        blvl = next(l for dd, l in levels if dd is best)
        print("  -> picked LOUDEST [%d] %s (level=%.5f)" % (best["index"], best["name"][:40], blvl), flush=True)
        return best
    # all silent -> fall back to the default-output loopback so capture is ready when audio resumes
    try:
        wi = p.get_host_api_info_by_type(_audio_module().paWASAPI)
        tgt = p.get_device_info_by_index(wi["defaultOutputDevice"])["name"].split(" (")[0].lower()
        for d in devs:
            if tgt in d["name"].lower():
                print("  -> all silent; default-output loopback [%d] %s" % (d["index"], d["name"][:40]), flush=True)
                return d
    except Exception:
        pass
    print("  -> all silent; falling back to first loopback [%d] %s" % (devs[0]["index"], devs[0]["name"][:40]), flush=True)
    return devs[0]


def _transcribe_loop(model, p, dev, deadline) -> str:
    """Continuous capture via callback queue + rolling-chunk transcription. Returns a reason string
    when it exits (deadline / stream-error) so the outer loop can decide whether to reconnect."""
    q: "queue.Queue[bytes]" = queue.Queue(maxsize=QUEUE_MAX)
    stream, rate, ch = _open_callback_stream(p, dev, q)
    stream.start_stream()
    raw_msg = f", raw -> {RAW_OUT}" if RAW_TRANSCRIPT_ENABLED else ""
    print(f"capturing loopback [{dev['index']}] {dev['name']} {rate}Hz {ch}ch -> {OUT}{raw_msg}", flush=True)
    need = chunk_byte_target(rate, ch)
    buf = bytearray()
    last_frame = time.monotonic()
    last_audio = time.monotonic()
    while deadline is None or time.monotonic() < deadline:
        capture_ok, capture_reason = audio_capture_acceptance()
        if not capture_ok:
            return f"warrior marker invalid -> stop capture ({capture_reason})"
        # Detect a dead stream: callback delivers nothing for a long stretch AND PyAudio reports
        # the stream inactive -> break so the outer loop can reconnect / re-probe.
        try:
            data = q.get(timeout=0.5)
            buf += data
            last_frame = time.monotonic()
        except queue.Empty:
            if not stream.is_active() and (time.monotonic() - last_frame) > 5.0:
                return "stream inactive"
            continue
        if len(buf) < need:
            continue
        audio = np.frombuffer(bytes(buf), dtype=np.int16).astype(np.float32) / 32768.0
        buf = bytearray()
        audio = downmix_to_mono(audio, ch)
        audio = resample_to_whisper(audio, rate)
        if float(np.abs(audio).mean()) < 1e-4:  # silence -> skip transcription
            if (time.monotonic() - last_audio) > REPROBE_SILENCE_SEC:
                return "prolonged silence -> re-probe device"
            continue
        last_audio = time.monotonic()
        try:
            segs, _ = model.transcribe(audio, language="en", vad_filter=True,
                                       beam_size=1, condition_on_previous_text=False)
            text = " ".join(s.text for s in segs).strip()
        except Exception as e:
            print("transcribe error:", str(e)[:80], flush=True)
            continue
        accepted, reason = transcript_feed_acceptance(text)
        if RAW_TRANSCRIPT_ENABLED:
            try:
                append_transcript(RAW_OUT, raw_transcript_row(text, accepted=accepted, reason=reason))
            except Exception as e:
                print("raw write error:", str(e)[:80], flush=True)
        if not accepted:
            print(f"[skip transcript audio:{reason}] {text[:120]}", flush=True)
            continue
        row = transcript_row(text)
        try:
            append_transcript(OUT, row)
        except Exception as e:
            print("write error:", str(e)[:80], flush=True)
        print(f"[{row['ts'][11:19]}] {text}", flush=True)
    try:
        stream.stop_stream(); stream.close()
    except Exception:
        pass
    return "deadline"


def _load_model():
    from faster_whisper import WhisperModel
    # CUDA-Whisper + WASAPI loopback in one process can crash natively (silent exit 255); CPU int8 is
    # the safe default for this audio-capture daemon. Set ROSS_WHISPER_DEVICE=cuda to force GPU.
    force = os.environ.get("ROSS_WHISPER_DEVICE", "cpu").lower()
    if force == "cuda":
        try:
            model = WhisperModel(MODEL, device="cuda", compute_type="float16")
            print(f"whisper {MODEL} on CUDA", flush=True)
            return model
        except Exception as e:
            print(f"CUDA unavailable ({str(e)[:50]}) -> CPU int8", flush=True)
    model = WhisperModel(MODEL, device="cpu", compute_type="int8")
    print(f"whisper {MODEL} on CPU int8", flush=True)
    return model


def _audio_module():
    global pa
    if pa is None:
        import pyaudiowpatch as _pa

        pa = _pa
    return pa


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    deadline = None
    if "--seconds" in sys.argv:
        deadline = time.monotonic() + float(sys.argv[sys.argv.index("--seconds") + 1])
    substr = None
    if "--device-substr" in sys.argv:
        substr = sys.argv[sys.argv.index("--device-substr") + 1]
    global MODEL
    if "--model" in sys.argv:
        MODEL = sys.argv[sys.argv.index("--model") + 1]

    model = _load_model()

    while deadline is None or time.monotonic() < deadline:
        capture_ok, capture_reason = audio_capture_acceptance()
        if not capture_ok:
            wait_s = marker_invalid_backoff_seconds(capture_reason)
            print(
                f"waiting for valid Warrior stream marker ({capture_reason}); retry in {wait_s:.1f}s",
                flush=True,
            )
            if deadline is not None:
                remaining = max(0.0, deadline - time.monotonic())
                if remaining <= 0:
                    break
                wait_s = min(wait_s, remaining)
            time.sleep(wait_s)
            continue
        p = _audio_module().PyAudio()
        try:
            dev = _pick_loopback(p, substr)
            if dev is None:
                print("no loopback device found", flush=True)
                return
            reason = _transcribe_loop(model, p, dev, deadline)
            print(f"capture loop ended ({reason})", flush=True)
        except Exception as e:
            print("loop error:", str(e)[:100], flush=True)
        finally:
            try:
                p.terminate()
            except Exception:
                pass
        if deadline is not None and time.monotonic() >= deadline:
            break
        time.sleep(3)  # brief backoff before reconnect / re-probe
    print("transcriber stopped", flush=True)


if __name__ == "__main__":
    main()
