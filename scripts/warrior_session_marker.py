from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


DEFAULT_MARKER_PATH = Path(r"D:\CHILI-Docker\chili-data\ross_stream\warrior_session_ok.json")


def _utc_iso(now: datetime | None = None) -> str:
    dt = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return dt.isoformat()


def _log_messages(logs: Sequence[dict[str, Any]] | None) -> list[str]:
    out: list[str] = []
    for row in logs or []:
        msg = str(row.get("message") or "")
        if msg:
            out.append(msg)
    return out


def _has_playing_video_evidence(page_state: dict[str, Any], video_count: int) -> bool:
    videos = page_state.get("videos")
    if isinstance(videos, list):
        for row in videos:
            if not isinstance(row, dict):
                continue
            visible = bool(row.get("visible"))
            paused = bool(row.get("paused"))
            ended = bool(row.get("ended"))
            try:
                ready_state = int(row.get("readyState") or row.get("ready_state") or 0)
            except (TypeError, ValueError):
                ready_state = 0
            if visible and not paused and not ended and ready_state >= 2:
                return True
        return False
    return video_count > 0


def build_warrior_session_marker(
    page_state: dict[str, Any],
    *,
    logs: Sequence[dict[str, Any]] | None = None,
    checked_at: datetime | None = None,
) -> dict[str, Any]:
    """Build the browser-session marker consumed by Ross transcript ingestion."""
    url = str(page_state.get("url") or "")
    title = str(page_state.get("title") or "")
    video_count = int(page_state.get("videoCount") or page_state.get("video_count") or 0)
    iframe_count = int(page_state.get("iframeCount") or page_state.get("iframe_count") or 0)
    canvas_count = int(page_state.get("canvasCount") or page_state.get("canvas_count") or 0)
    body_text_length = int(page_state.get("bodyTextLength") or page_state.get("body_text_length") or 0)
    body_text = str(
        page_state.get("bodyText")
        or page_state.get("body_text")
        or page_state.get("bodyTextSample")
        or page_state.get("body_text_sample")
        or page_state.get("bodyExcerpt")
        or page_state.get("body_excerpt")
        or ""
    )
    explicit_stream_off = bool(page_state.get("hasStreamOff") or page_state.get("has_stream_off"))
    explicit_small_cap_stream_off = bool(
        page_state.get("hasSmallCapStreamOff") or page_state.get("has_small_cap_stream_off")
    )
    stream_off = explicit_stream_off or explicit_small_cap_stream_off
    if body_text:
        lowered_body = body_text.lower()
        small_cap_stream_open = bool(
            page_state.get("hasSmallCapStreamOpen") or page_state.get("has_small_cap_stream_open")
        ) or any(
            marker in lowered_body
            for marker in (
                "small cap stream open",
                "screencast - small cap stream open",
            )
        )
        small_cap_stream_off = explicit_small_cap_stream_off or any(
            marker in lowered_body
            for marker in (
                "small cap stream off",
                "small cap - offline",
                "this room is currently closed",
                "screencast - small cap\noffline",
            )
        )
        stream_off = False if small_cap_stream_open and not explicit_small_cap_stream_off else stream_off
        stream_off = stream_off or small_cap_stream_off
    else:
        lowered_body = ""
    disclaimer_blocking = bool(
        page_state.get("disclaimerBlocking") or page_state.get("disclaimer_blocking")
    ) or all(
        marker in lowered_body
        for marker in (
            "disclaimer",
            "accept",
            "decline",
        )
    )
    stream_context = bool(page_state.get("streamContext") or page_state.get("stream_context"))
    lowered_url = url.lower()
    lowered_title = title.lower()
    stream_context = stream_context or any(
        marker in lowered_url
        for marker in (
            "page=screencast",
            "roomid=",
            "screencast",
        )
    )
    stream_context = stream_context or any(
        marker in lowered_title or marker in lowered_body
        for marker in (
            "screencast",
            "small cap",
            "news room",
            "stream",
        )
    )
    playing_video = _has_playing_video_evidence(page_state, video_count)
    stream_visible = playing_video

    messages = _log_messages(logs)
    reason = "warrior_session_ok"
    ok = True
    if "chatroom.warriortrading.com" not in url:
        ok = False
        reason = "not_warrior_chatroom"
    elif any("Session not exists" in msg or "session not exists" in msg.lower() for msg in messages):
        ok = False
        reason = "warrior_session_not_exists"
    elif disclaimer_blocking:
        ok = False
        reason = "warrior_disclaimer_blocking"
    elif not stream_context:
        ok = False
        reason = "warrior_stream_context_missing"
    elif stream_off:
        ok = False
        reason = "warrior_stream_offline"
    elif not stream_visible:
        ok = False
        reason = "warrior_stream_not_visible"

    return {
        "ok": ok,
        "reason": reason,
        "ts": _utc_iso(checked_at),
        "url": url,
        "title": title,
        "body_text_length": body_text_length,
        "video_count": video_count,
        "iframe_count": iframe_count,
        "canvas_count": canvas_count,
        "stream_visible": stream_visible,
        "playing_video": playing_video,
        "stream_context": stream_context,
        "stream_off": stream_off,
        "disclaimer_blocking": disclaimer_blocking,
        "error_messages": messages[-5:],
    }


def write_warrior_session_marker(marker: dict[str, Any], path: str | Path = DEFAULT_MARKER_PATH) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(json.dumps(marker, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(out)
    return out


def refresh_marker_from_state_file(
    state_path: str | Path,
    *,
    out_path: str | Path = DEFAULT_MARKER_PATH,
    logs: Sequence[dict[str, Any]] | None = None,
    max_state_age_seconds: float | None = None,
    now: datetime | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Promote a fresh browser page-state snapshot into the session marker."""
    state = Path(state_path)
    now_dt = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    raw = state.read_text(encoding="utf-8-sig")
    page_state = json.loads(raw)
    marker = build_warrior_session_marker(page_state, logs=logs, checked_at=now_dt)
    try:
        mtime = datetime.fromtimestamp(state.stat().st_mtime, tz=timezone.utc)
        age_s = max(0.0, (now_dt - mtime).total_seconds())
    except OSError:
        age_s = None
    marker["source_state_path"] = str(state)
    marker["source_state_age_s"] = age_s
    if max_state_age_seconds is not None and age_s is not None:
        try:
            max_age = max(1.0, float(max_state_age_seconds))
        except (TypeError, ValueError):
            max_age = 30.0
        if age_s > max_age:
            marker["ok"] = False
            marker["reason"] = "warrior_browser_state_stale"
    path = write_warrior_session_marker(marker, out_path)
    return path, marker


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write Warrior browser session marker for Ross transcript gates.")
    parser.add_argument("--state-json", default="")
    parser.add_argument("--state-json-file", default="")
    parser.add_argument("--state-json-file-max-age-seconds", type=float, default=None)
    parser.add_argument("--logs-json", default="[]")
    parser.add_argument("--logs-json-file", default="")
    parser.add_argument("--out", default=str(DEFAULT_MARKER_PATH))
    args = parser.parse_args(argv)

    if args.state_json_file:
        page_state_raw = Path(args.state_json_file).read_text(encoding="utf-8-sig")
    else:
        page_state_raw = args.state_json
    if not page_state_raw:
        parser.error("--state-json or --state-json-file is required")
    if args.logs_json_file:
        logs_raw = Path(args.logs_json_file).read_text(encoding="utf-8-sig")
    else:
        logs_raw = args.logs_json

    logs = json.loads(logs_raw)
    if args.state_json_file and args.state_json_file_max_age_seconds is not None:
        path, marker = refresh_marker_from_state_file(
            args.state_json_file,
            out_path=args.out,
            logs=logs,
            max_state_age_seconds=args.state_json_file_max_age_seconds,
        )
    else:
        page_state = json.loads(page_state_raw)
        marker = build_warrior_session_marker(page_state, logs=logs)
        path = write_warrior_session_marker(marker, args.out)
    print(str(path))
    print(json.dumps(marker, sort_keys=True))
    return 0 if marker.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
