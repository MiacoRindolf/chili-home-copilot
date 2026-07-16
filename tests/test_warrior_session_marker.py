from __future__ import annotations

import json
from datetime import datetime, timezone

from scripts.warrior_session_marker import (
    build_warrior_session_marker,
    main,
    refresh_marker_from_state_file,
    write_warrior_session_marker,
)


def test_marker_accepts_visible_warrior_stream() -> None:
    marker = build_warrior_session_marker(
        {
            "url": "https://chatroom.warriortrading.com/dashboard?page=Screencast",
            "title": "WarriorTrading Chatroom",
            "bodyTextLength": 120,
            "videoCount": 1,
            "videos": [{"visible": True, "paused": False, "ended": False, "readyState": 4}],
            "iframeCount": 0,
            "canvasCount": 0,
        },
        checked_at=datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc),
    )

    assert marker["ok"] is True
    assert marker["reason"] == "warrior_session_ok"
    assert marker["stream_visible"] is True
    assert marker["stream_context"] is True


def test_marker_rejects_session_not_exists_log() -> None:
    marker = build_warrior_session_marker(
        {
            "url": "https://chatroom.warriortrading.com/dashboard?page=Screencast",
            "title": "WarriorTrading Chatroom",
            "bodyTextLength": 0,
            "videoCount": 0,
            "iframeCount": 0,
        },
        logs=[{"message": 'Request data failed, details: Error: {"message":"Session not exists"}'}],
    )

    assert marker["ok"] is False
    assert marker["reason"] == "warrior_session_not_exists"


def test_marker_rejects_blank_warrior_chatroom_without_stream() -> None:
    marker = build_warrior_session_marker(
        {
            "url": "https://chatroom.warriortrading.com/dashboard?page=Screencast",
            "title": "WarriorTrading Chatroom",
            "bodyTextLength": 0,
            "videoCount": 0,
            "iframeCount": 0,
            "canvasCount": 0,
        }
    )

    assert marker["ok"] is False
    assert marker["reason"] == "warrior_stream_not_visible"


def test_marker_reports_disclaimer_blocking_before_stream_checks() -> None:
    marker = build_warrior_session_marker(
        {
            "url": "https://chatroom.warriortrading.com/dashboard?hash=abc",
            "title": "WarriorTrading Chatroom",
            "bodyTextLength": 550,
            "bodyTextSample": "Disclaimer Ross results are NOT typical ACCEPT DECLINE",
            "videoCount": 0,
            "iframeCount": 0,
            "canvasCount": 0,
        }
    )

    assert marker["ok"] is False
    assert marker["reason"] == "warrior_disclaimer_blocking"
    assert marker["disclaimer_blocking"] is True


def test_marker_preserves_browser_disclaimer_state_aliases() -> None:
    marker = build_warrior_session_marker(
        {
            "url": "https://chatroom.warriortrading.com/dashboard?hash=abc",
            "title": "WarriorTrading Chatroom",
            "body_text_length": 550,
            "body_excerpt": "Disclaimer Ross results are NOT typical ACCEPT DECLINE",
            "disclaimer_blocking": True,
            "video_count": 0,
            "iframe_count": 0,
            "canvas_count": 0,
            "stream_context": True,
            "stream_visible": False,
        }
    )

    assert marker["ok"] is False
    assert marker["reason"] == "warrior_disclaimer_blocking"
    assert marker["disclaimer_blocking"] is True


def test_marker_rejects_generic_dashboard_iframe_without_stream_context() -> None:
    marker = build_warrior_session_marker(
        {
            "url": "https://chatroom.warriortrading.com/dashboard?hash=abc",
            "title": "WarriorTrading Chatroom",
            "bodyTextLength": 500,
            "bodyTextSample": "Settings Account Tools Watchlist",
            "videoCount": 0,
            "iframeCount": 1,
            "canvasCount": 0,
        }
    )

    assert marker["ok"] is False
    assert marker["reason"] == "warrior_stream_context_missing"


def test_marker_rejects_dashboard_iframe_with_screencast_context_but_no_playing_video() -> None:
    marker = build_warrior_session_marker(
        {
            "url": "https://chatroom.warriortrading.com/dashboard?hash=abc",
            "title": "WarriorTrading Chatroom",
            "bodyTextLength": 500,
            "bodyTextSample": "Small Cap Screencast News Room Live",
            "videoCount": 0,
            "iframeCount": 1,
            "canvasCount": 0,
        }
    )

    assert marker["ok"] is False
    assert marker["reason"] == "warrior_stream_not_visible"
    assert marker["stream_context"] is True
    assert marker["stream_visible"] is False


def test_marker_rejects_stream_off_dashboard_even_with_iframe() -> None:
    marker = build_warrior_session_marker(
        {
            "url": "https://chatroom.warriortrading.com/dashboard?hash=abc",
            "title": "WarriorTrading Chatroom",
            "bodyTextLength": 500,
            "bodyTextSample": "Small Cap - offline\nScreencast - News Room\nOFFLINE\nSTREAM Off",
            "videoCount": 0,
            "iframeCount": 1,
            "canvasCount": 0,
        }
    )

    assert marker["ok"] is False
    assert marker["reason"] == "warrior_stream_offline"
    assert marker["stream_off"] is True


def test_marker_accepts_small_cap_open_despite_other_rooms_stream_off() -> None:
    marker = build_warrior_session_marker(
        {
            "url": "https://chatroom.warriortrading.com/dashboard?hash=abc",
            "title": "WarriorTrading Chatroom",
            "bodyTextLength": 500,
            "bodyTextSample": (
                "Small Cap STREAM OPEN Pro Mentor STREAM Off Live Classes STREAM Off "
                "Screencast - Small Cap STREAM OPEN Ross's 5 Pillars Scan (Online)"
            ),
            "videoCount": 1,
            "videos": [{"visible": True, "paused": False, "ended": False, "readyState": 4}],
            "iframeCount": 1,
            "canvasCount": 0,
        }
    )

    assert marker["ok"] is True
    assert marker["reason"] == "warrior_session_ok"
    assert marker["stream_off"] is False


def test_marker_rejects_non_warrior_page() -> None:
    marker = build_warrior_session_marker(
        {
            "url": "about:blank",
            "title": "",
            "bodyTextLength": 0,
            "videoCount": 0,
            "iframeCount": 0,
        }
    )

    assert marker["ok"] is False
    assert marker["reason"] == "not_warrior_chatroom"


def test_write_marker_is_atomic_json(tmp_path) -> None:
    path = tmp_path / "warrior_session_ok.json"
    marker = build_warrior_session_marker(
        {
            "url": "https://chatroom.warriortrading.com/dashboard?page=Screencast",
            "title": "WarriorTrading Chatroom",
            "bodyTextLength": 120,
            "videoCount": 1,
        }
    )

    written = write_warrior_session_marker(marker, path)

    assert written == path
    assert json.loads(path.read_text(encoding="utf-8"))["ok"] is True


def test_marker_cli_accepts_state_json_file(tmp_path) -> None:
    state_path = tmp_path / "state.json"
    out_path = tmp_path / "warrior_session_ok.json"
    state_path.write_text(
        json.dumps(
            {
                "url": "https://chatroom.warriortrading.com/dashboard?page=Screencast",
                "title": "WarriorTrading Chatroom",
                "bodyTextLength": 120,
                "videoCount": 1,
                "videos": [{"visible": True, "paused": False, "ended": False, "readyState": 4}],
                "iframeCount": 0,
                "canvasCount": 0,
            }
        ),
        encoding="utf-8",
    )

    rc = main(["--state-json-file", str(state_path), "--out", str(out_path)])

    assert rc == 0
    assert json.loads(out_path.read_text(encoding="utf-8"))["reason"] == "warrior_session_ok"


def test_marker_cli_accepts_utf8_bom_state_json_file(tmp_path) -> None:
    state_path = tmp_path / "state.json"
    out_path = tmp_path / "warrior_session_ok.json"
    state_path.write_text(
        json.dumps(
            {
                "url": "https://chatroom.warriortrading.com/dashboard?page=Screencast",
                "title": "WarriorTrading Chatroom",
                "bodyTextLength": 120,
                "videoCount": 1,
                "videos": [{"visible": True, "paused": False, "ended": False, "readyState": 4}],
                "iframeCount": 0,
                "canvasCount": 0,
            }
        ),
        encoding="utf-8-sig",
    )

    rc = main(["--state-json-file", str(state_path), "--out", str(out_path)])

    assert rc == 0
    assert json.loads(out_path.read_text(encoding="utf-8"))["reason"] == "warrior_session_ok"


def test_refresh_marker_from_state_file_promotes_visible_stream_snapshot(tmp_path) -> None:
    state_path = tmp_path / "warrior_browser_state_latest.json"
    out_path = tmp_path / "warrior_session_ok.json"
    state_path.write_text(
        json.dumps(
            {
                "url": "https://chatroom.warriortrading.com/dashboard?hash=abc",
                "title": "WarriorTrading Chatroom",
                "bodyTextLength": 17943,
                "bodyTextSample": "Small Cap STREAM OPEN Screencast - Small Cap STREAM OPEN",
                "videoCount": 1,
                "videos": [{"visible": True, "paused": False, "ended": False, "readyState": 4}],
                "iframeCount": 1,
                "canvasCount": 0,
            }
        ),
        encoding="utf-8",
    )

    path, marker = refresh_marker_from_state_file(
        state_path,
        out_path=out_path,
        max_state_age_seconds=30,
        now=datetime.now(timezone.utc),
    )

    assert path == out_path
    assert marker["ok"] is True
    assert marker["reason"] == "warrior_session_ok"
    written = json.loads(out_path.read_text(encoding="utf-8"))
    assert written["ok"] is True
    assert written["source_state_path"] == str(state_path)


def test_refresh_marker_rejects_chatroom_iframe_with_stream_off_without_playing_video(tmp_path) -> None:
    state_path = tmp_path / "warrior_browser_state_latest.json"
    out_path = tmp_path / "warrior_session_ok.json"
    state_path.write_text(
        json.dumps(
            {
                "url": "https://chatroom.warriortrading.com/dashboard?hash=abc&page=Empty&roomId=DB04",
                "title": "WarriorTrading Chatroom",
                "bodyTextLength": 2000,
                "bodyTextSample": "Small Cap STREAM Off Screencast - Small Cap JOIN STREAM",
                "streamContext": True,
                "streamVisible": False,
                "disclaimerBlocking": False,
                "videoCount": 0,
                "videos": [],
                "iframeCount": 1,
                "canvasCount": 0,
            }
        ),
        encoding="utf-8",
    )

    _, marker = refresh_marker_from_state_file(
        state_path,
        out_path=out_path,
        max_state_age_seconds=30,
        now=datetime.now(timezone.utc),
    )

    assert marker["ok"] is False
    assert marker["reason"] == "warrior_stream_offline"
    assert marker["stream_visible"] is False
    written = json.loads(out_path.read_text(encoding="utf-8"))
    assert written["ok"] is False


def test_marker_rejects_explicit_stream_visible_without_playing_video() -> None:
    marker = build_warrior_session_marker(
        {
            "url": "https://chatroom.warriortrading.com/dashboard?hash=abc&page=Screencast&roomId=DB04",
            "title": "WarriorTrading Chatroom",
            "bodyTextLength": 2000,
            "bodyTextSample": "Small Cap Screencast JOIN STREAM",
            "streamContext": True,
            "streamVisible": True,
            "disclaimerBlocking": False,
            "videoCount": 0,
            "videos": [],
            "iframeCount": 1,
            "canvasCount": 0,
        }
    )

    assert marker["ok"] is False
    assert marker["reason"] == "warrior_stream_not_visible"
    assert marker["stream_visible"] is False
    assert marker["playing_video"] is False
