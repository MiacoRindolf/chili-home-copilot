"""Claude Code bridge — read a live Claude Code session from the CHILI web UI
and keep talking to it.

Why it is shaped this way
-------------------------
The web app runs in a container with no mounts and no ``claude`` binary, so it
can neither read ``~/.claude`` nor execute a turn by itself.  The two halves
therefore travel different roads:

READ   the session JSONL is bind-mounted read-only at ``CHILI_CLAUDE_PROJECTS_DIR``.
       It is append-only, so the reader seeks to a byte offset and tails — a
       115 MB transcript is never parsed whole.
WRITE  a message is dropped in ``<bridge>/inbox`` and a host-side worker
       (``scripts/claude_bridge_worker.ps1``) runs the real CLI and answers in
       ``<bridge>/outbox``.  The worker forks the session
       (``--resume <sid> --fork-session``) deliberately: the terminal session
       owns its own transcript and must never have a second writer.

Nothing here writes to a transcript.  A reply lands in its own forked session
file, which this reader picks up like any other.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..deps import get_db, get_identity_ctx

logger = logging.getLogger(__name__)

router = APIRouter(tags=["claude_bridge"])

# Tail window.  Big enough to cover a long turn, small enough that a cold read
# of a 100 MB transcript stays sub-100 ms.
_TAIL_BYTES = 512 * 1024
_MAX_TAIL_BYTES = 4 * 1024 * 1024

# Listing costs one file read per preview, and the transcripts sit on a Windows
# bind mount where that is expensive: 40 previews measured 22.8 s through the
# origin.  Only the sessions a person would plausibly pick get one.
_SESSION_LIST_MAX = 25
_PREVIEW_SESSIONS = 6
_PREVIEW_TAIL_BYTES = 16 * 1024

# A turn is "working" while tool traffic keeps arriving; past this it is either
# waiting on the operator or idle.  Derived from observed cadence, not taste:
# tool results land every few seconds, and the longest quiet stretch inside an
# active turn is a build or a bench step.
_WORKING_SILENCE_S = 180
_IDLE_SILENCE_S = 15 * 60

_SESSION_ID_RE = re.compile(r"^[0-9a-fA-F-]{8,64}$")

# The set of sessions changes when someone starts a new one — minutes apart at
# worst.  The live transcript is tailed separately and is never cached, so this
# only spares the directory walk, never freshness of the conversation.
_LISTING_TTL_S = 45.0
_listing_cache: dict[str, tuple[float, list[dict]]] = {}


def _cached_listing(sdir: Path, previews: bool) -> list[dict] | None:
    hit = _listing_cache.get(f"{sdir}|{previews}")
    if hit and (time.time() - hit[0]) < _LISTING_TTL_S:
        now = time.time()
        # Ages are recomputed on the way out; a cached "3s ago" would be a lie.
        return [dict(r, age_s=round(max(0.0, now - r["mtime"]), 1)) for r in hit[1]]
    return None


def _store_listing(sdir: Path, previews: bool, rows: list[dict]) -> None:
    _listing_cache[f"{sdir}|{previews}"] = (time.time(), rows)
    if len(_listing_cache) > 8:
        oldest = min(_listing_cache, key=lambda k: _listing_cache[k][0])
        _listing_cache.pop(oldest, None)


def _projects_dir() -> Path | None:
    raw = os.environ.get("CHILI_CLAUDE_PROJECTS_DIR", "").strip()
    if not raw:
        return None
    p = Path(raw)
    return p if p.is_dir() else None


def _bridge_dir() -> Path | None:
    raw = os.environ.get("CHILI_CLAUDE_BRIDGE_DIR", "").strip()
    if not raw:
        return None
    p = Path(raw)
    return p if p.is_dir() else None


def _slug_dir(projects: Path, slug: str | None) -> Path | None:
    """Resolve one project slug under the projects dir, refusing traversal."""
    if slug:
        if "/" in slug or "\\" in slug or slug.startswith("."):
            return None
        cand = projects / slug
        return cand if cand.is_dir() else None
    # Default: this repo.  Claude Code slugs a path by replacing separators
    # and the drive colon with dashes -> "D--dev-chili-home-copilot".
    preferred = os.environ.get("CHILI_CLAUDE_PROJECT_SLUG", "").strip()
    if preferred:
        cand = projects / preferred
        if cand.is_dir():
            return cand
    dirs = [d for d in projects.iterdir() if d.is_dir()]
    if not dirs:
        return None
    chili = [d for d in dirs if "chili-home-copilot" in d.name]
    pool = chili or dirs
    return max(pool, key=lambda d: d.stat().st_mtime)


def _safe_session_path(slug_dir: Path, session_id: str) -> Path | None:
    if not _SESSION_ID_RE.match(session_id or ""):
        return None
    p = slug_dir / f"{session_id}.jsonl"
    return p if p.is_file() else None


# ── transcript parsing ───────────────────────────────────────────


def _text_of(content: Any) -> str:
    """Flatten an Anthropic content block list to display text."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    out: list[str] = []
    for blk in content:
        if not isinstance(blk, dict):
            continue
        if blk.get("type") == "text":
            out.append(str(blk.get("text") or ""))
    return "\n".join(t for t in out if t)


def _tool_activity(content: Any) -> list[dict]:
    """Compact tool-call summaries — what the agent is DOING, not the payload."""
    acts: list[dict] = []
    if not isinstance(content, list):
        return acts
    for blk in content:
        if not isinstance(blk, dict) or blk.get("type") != "tool_use":
            continue
        inp = blk.get("input") or {}
        label = ""
        if isinstance(inp, dict):
            for key in ("description", "file_path", "pattern", "command", "prompt"):
                v = inp.get(key)
                if isinstance(v, str) and v.strip():
                    label = v.strip().splitlines()[0][:160]
                    break
        acts.append({"tool": str(blk.get("name") or "tool"), "label": label})
    return acts


def _parse_tail(path: Path, offset: int, tail_bytes: int) -> tuple[list[dict], int, dict]:
    """Read from ``offset`` (or the last ``tail_bytes``) and return display rows.

    Returns (rows, new_offset, raw_status).  ``new_offset`` is a byte position
    the caller passes back to poll incrementally.
    """
    size = path.stat().st_size
    start = offset if offset and 0 < offset <= size else max(0, size - tail_bytes)
    partial_first = start > 0 and start != offset

    with path.open("rb") as fh:
        fh.seek(start)
        blob = fh.read()
    new_offset = start + len(blob)

    lines = blob.split(b"\n")
    # A tail that did not begin at a record boundary starts mid-line: drop it.
    if partial_first and lines:
        lines = lines[1:]
    # A trailing fragment means the writer is mid-append; rewind past it so the
    # next poll re-reads that record whole.
    if lines and lines[-1] and not blob.endswith(b"\n"):
        new_offset -= len(lines[-1])
        lines = lines[:-1]

    rows: list[dict] = []
    last_ts = ""
    last_kind = ""
    for raw in lines:
        if not raw.strip():
            continue
        try:
            rec = json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            continue
        if not isinstance(rec, dict):
            continue
        rtype = rec.get("type")
        ts = str(rec.get("timestamp") or "")
        if ts:
            last_ts = ts

        if rtype in ("user", "assistant"):
            msg = rec.get("message") or {}
            role = msg.get("role") or rtype
            content = msg.get("content")
            text = _text_of(content)
            acts = _tool_activity(content)
            if role == "user" and isinstance(content, list):
                # A "user" record carrying only tool_result is the harness
                # feeding results back, not a person speaking.
                if not text:
                    last_kind = "tool_result"
                    continue
            if text:
                rows.append({"role": role, "kind": "text", "text": text, "ts": ts})
                last_kind = "text_" + str(role)
            for a in acts:
                rows.append({"role": "assistant", "kind": "tool",
                             "tool": a["tool"], "text": a["label"], "ts": ts})
                last_kind = "tool_use"
        elif rtype == "queue-operation" and rec.get("operation") == "enqueue":
            c = rec.get("content")
            if isinstance(c, str) and c and not c.lstrip().startswith("<task-notification>"):
                rows.append({"role": "user", "kind": "queued", "text": c, "ts": ts})
                last_kind = "queued"

    return rows, new_offset, {"last_ts": last_ts, "last_kind": last_kind}


def _status_from(raw: dict, mtime: float) -> dict:
    """working / waiting / idle, plus the seconds of silence behind the call."""
    silence = max(0.0, time.time() - mtime)
    kind = raw.get("last_kind") or ""
    if silence > _IDLE_SILENCE_S:
        state, label = "idle", "Idle"
    elif kind in ("tool_use", "tool_result") and silence <= _WORKING_SILENCE_S:
        state, label = "working", "Working"
    elif silence <= _WORKING_SILENCE_S:
        state, label = "working", "Thinking"
    else:
        state, label = "waiting", "Waiting for you"
    return {"state": state, "label": label,
            "silence_s": round(silence, 1), "last_ts": raw.get("last_ts") or ""}


# ── API ──────────────────────────────────────────────────────────


@router.get("/api/claude/sessions", response_class=JSONResponse)
def api_claude_sessions(request: Request, db: Session = Depends(get_db),
                        slug: str | None = Query(None),
                        previews: bool = Query(True)):
    """Claude Code sessions for this repo, newest first."""
    get_identity_ctx(request, db)
    projects = _projects_dir()
    if projects is None:
        return {"available": False,
                "reason": "CHILI_CLAUDE_PROJECTS_DIR is not mounted in this container.",
                "sessions": []}
    sdir = _slug_dir(projects, slug)
    if sdir is None:
        return {"available": False, "reason": "No Claude Code project directory found.",
                "sessions": []}

    cached = _cached_listing(sdir, previews)
    if cached is not None:
        return {"available": True, "slug": sdir.name, "sessions": cached,
                "can_send": _bridge_dir() is not None, "cached": True}

    # scandir, not glob+stat: this directory holds 1,595 transcripts and every
    # stat is a round trip over a Windows bind mount.  Measured in the running
    # container: 2,935 ms for glob+stat, 1,412 ms for scandir.
    out = []
    now = time.time()
    try:
        with os.scandir(sdir) as it:
            for e in it:
                if not e.name.endswith(".jsonl"):
                    continue
                try:
                    st = e.stat()
                except OSError:
                    continue
                out.append({
                    "session_id": e.name[:-6],
                    "bytes": st.st_size,
                    "mtime": st.st_mtime,
                    "age_s": round(max(0.0, now - st.st_mtime), 1),
                })
    except OSError as exc:
        logger.warning("[claude_bridge] could not list %s: %s", sdir, exc)
        return {"available": False, "reason": "Could not read the session directory.",
                "sessions": []}
    out.sort(key=lambda r: r["mtime"], reverse=True)
    out = out[:_SESSION_LIST_MAX]
    # Previews cost a file read each, and the transcripts live on a Windows bind
    # mount where that is expensive -- 40 of them took 22.8 s through the origin.
    # Only the sessions a person would actually pick get one.
    if previews:
        for r in out[:_PREVIEW_SESSIONS]:
            p = sdir / f"{r['session_id']}.jsonl"
            try:
                rows, _, _ = _parse_tail(p, 0, _PREVIEW_TAIL_BYTES)
                last_text = next((x["text"] for x in reversed(rows) if x["kind"] == "text"), "")
                r["preview"] = last_text[:120]
            except Exception:
                r["preview"] = ""
    _store_listing(sdir, previews, out)
    return {"available": True, "slug": sdir.name, "sessions": out,
            "can_send": _bridge_dir() is not None, "cached": False}


@router.get("/api/claude/transcript", response_class=JSONResponse)
def api_claude_transcript(request: Request, db: Session = Depends(get_db),
                          session_id: str = Query(...),
                          offset: int = Query(0, ge=0),
                          slug: str | None = Query(None),
                          tail_bytes: int = Query(_TAIL_BYTES, ge=4096, le=_MAX_TAIL_BYTES)):
    """Incremental tail of one session: display rows + a live status."""
    get_identity_ctx(request, db)
    projects = _projects_dir()
    if projects is None:
        return JSONResponse({"error": "Claude Code transcripts are not mounted."}, status_code=503)
    sdir = _slug_dir(projects, slug)
    if sdir is None:
        return JSONResponse({"error": "No Claude Code project directory."}, status_code=503)
    path = _safe_session_path(sdir, session_id)
    if path is None:
        return JSONResponse({"error": "Unknown session."}, status_code=404)

    try:
        rows, new_offset, raw = _parse_tail(path, offset, tail_bytes)
        mtime = path.stat().st_mtime
    except OSError as exc:
        logger.warning("[claude_bridge] tail failed for %s: %s", session_id, exc)
        return JSONResponse({"error": "Could not read the transcript."}, status_code=500)

    return {"session_id": session_id, "rows": rows, "offset": new_offset,
            "status": _status_from(raw, mtime)}


class _ClaudeSendRequest(BaseModel):
    model_config = {"extra": "forbid"}
    session_id: str = Field(min_length=8)
    message: str = Field(min_length=1)
    slug: str | None = None


@router.post("/api/claude/send", response_class=JSONResponse)
def api_claude_send(body: _ClaudeSendRequest, request: Request, db: Session = Depends(get_db)):
    """Queue a message for the host worker to run against a forked session."""
    ctx = get_identity_ctx(request, db)
    if ctx.get("user_id") is None:
        return JSONResponse({"error": "Sign in to talk to Claude Code."}, status_code=403)
    bridge = _bridge_dir()
    if bridge is None:
        return JSONResponse(
            {"error": "The Claude Code bridge directory is not mounted, so sending is off. "
                      "Reading still works."}, status_code=503)
    if not _SESSION_ID_RE.match(body.session_id):
        return JSONResponse({"error": "Bad session id."}, status_code=400)

    msg_id = uuid.uuid4().hex
    inbox = bridge / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    payload = {
        "id": msg_id,
        "session_id": body.session_id,
        "slug": body.slug or "",
        "message": body.message,
        "user_id": ctx.get("user_id"),
        "queued_at": time.time(),
    }
    # Write beside the target then rename: the worker must never observe a
    # half-written job.
    tmp = inbox / f".{msg_id}.tmp"
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(inbox / f"{msg_id}.json")
    logger.info("[claude_bridge] queued %s for session %s", msg_id, body.session_id)
    return {"queued": True, "id": msg_id}


@router.get("/api/claude/reply/{msg_id}", response_class=JSONResponse)
def api_claude_reply(msg_id: str, request: Request, db: Session = Depends(get_db)):
    """Poll for the host worker's answer to a queued message."""
    get_identity_ctx(request, db)
    if not re.match(r"^[0-9a-f]{32}$", msg_id or ""):
        return JSONResponse({"error": "Bad id."}, status_code=400)
    bridge = _bridge_dir()
    if bridge is None:
        return JSONResponse({"error": "Bridge directory not mounted."}, status_code=503)

    # utf-8-sig throughout: the host worker is PowerShell, which stamps a BOM on
    # everything it writes, and json.loads rejects it.
    done = bridge / "outbox" / f"{msg_id}.json"
    if done.is_file():
        try:
            data = json.loads(done.read_text(encoding="utf-8-sig"))
        except Exception:
            return {"state": "error", "error": "The reply file was unreadable."}
        return {"state": data.get("state") or "done",
                "reply": data.get("reply") or "",
                "error": data.get("error") or "",
                "forked_session_id": data.get("forked_session_id") or "",
                "duration_s": data.get("duration_s")}
    if (bridge / "inbox" / f"{msg_id}.json").is_file():
        return {"state": "queued"}
    if (bridge / "working" / f"{msg_id}.json").is_file():
        return {"state": "running"}
    return {"state": "unknown"}


@router.get("/api/claude/health", response_class=JSONResponse)
def api_claude_health(request: Request, db: Session = Depends(get_db)):
    """What the bridge can actually do right now — no guessing in the UI."""
    get_identity_ctx(request, db)
    projects = _projects_dir()
    bridge = _bridge_dir()
    hb = None
    if bridge is not None:
        hbf = bridge / "worker_heartbeat.json"
        if hbf.is_file():
            try:
                hb = json.loads(hbf.read_text(encoding="utf-8-sig"))
            except Exception:
                hb = None
    worker_age = None
    if isinstance(hb, dict) and isinstance(hb.get("at"), (int, float)):
        worker_age = round(max(0.0, time.time() - float(hb["at"])), 1)
    return {
        "can_read": projects is not None,
        "can_send": bridge is not None,
        "worker_alive": worker_age is not None and worker_age < 120,
        "worker_age_s": worker_age,
        "projects_dir": str(projects) if projects else "",
        "bridge_dir": str(bridge) if bridge else "",
    }
