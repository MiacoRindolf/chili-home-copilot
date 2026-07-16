from __future__ import annotations

import datetime
import json
import importlib.util
import os
from pathlib import Path


def _load_module():
    path = Path("scripts/ross_audio_transcribe.py")
    spec = importlib.util.spec_from_file_location("ross_audio_transcribe_under_test", path)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_should_append_transcript_requires_trading_context_by_default() -> None:
    mod = _load_module()

    assert mod.should_append_transcript("Watching CANF over VWAP for first pullback") is True
    assert mod.should_append_transcript("JEM still watching") is True
    assert mod.should_append_transcript("Ross is interested in CWD") is True
    assert mod.should_append_transcript("CWD is interesting to me over 1.55") is True
    assert mod.should_append_transcript("CANF over 80") is True
    assert mod.should_append_transcript("C-A-N-F is curling at high of day") is True
    assert mod.should_append_transcript("VWAP pullback is starting to curl") is True
    assert mod.should_append_transcript("The trip to Cuba was complicated and the clinic was open") is False
    assert mod.should_append_transcript("QR code and the US audience is watching our show") is False
    assert mod.should_append_transcript("I had a long conversation with ABC earlier") is False
    assert mod.should_append_transcript("The US support and response throughout the year was disappointing") is False
    assert mod.should_append_transcript("The daily lives of people were affected") is False
    assert mod.should_append_transcript("The GDP of Venezuela was cut after Maduro took over") is False


def test_should_append_transcript_can_keep_raw_audio_when_disabled() -> None:
    mod = _load_module()

    assert (
        mod.should_append_transcript(
            "The trip to Cuba was complicated and the clinic was open",
            require_trading_context=False,
        )
        is True
    )


def test_transcript_acceptance_explains_filtered_audio_for_raw_audit() -> None:
    mod = _load_module()

    accepted, reason = mod.transcript_acceptance("The trip to Cuba was complicated and the clinic was open")
    assert accepted is False
    assert reason == "non_trading_audio"

    row = mod.raw_transcript_row("The trip to Cuba was complicated and the clinic was open", accepted=accepted, reason=reason)
    assert row["accepted"] is False
    assert row["reason"] == "non_trading_audio"
    assert row["text"] == "The trip to Cuba was complicated and the clinic was open"


def test_transcript_feed_acceptance_requires_warrior_session_marker() -> None:
    mod = _load_module()

    accepted, reason = mod.transcript_feed_acceptance(
        "Watching CANF over VWAP for first pullback",
        marker_checker=lambda: (False, "warrior_session_marker_missing"),
    )

    assert accepted is False
    assert reason == "warrior_session_marker_missing"


def test_audio_capture_acceptance_requires_fresh_warrior_marker() -> None:
    mod = _load_module()

    accepted, reason = mod.audio_capture_acceptance(
        marker_checker=lambda: (False, "warrior_session_marker_stale"),
    )

    assert accepted is False
    assert reason == "warrior_session_marker_stale"


def test_audio_capture_acceptance_can_disable_marker_for_offline_tests() -> None:
    mod = _load_module()

    accepted, reason = mod.audio_capture_acceptance(
        require_warrior_session_ok=False,
        marker_checker=lambda: (False, "warrior_session_marker_stale"),
    )

    assert accepted is True
    assert reason == "capture_marker_disabled"


def test_marker_invalid_backoff_waits_only_for_invalid_marker_reasons() -> None:
    mod = _load_module()

    assert mod.marker_invalid_backoff_seconds("warrior_session_marker_stale", default_seconds=3) == 3
    assert mod.marker_invalid_backoff_seconds("not_warrior_chatroom", default_seconds=0) == 1
    assert mod.marker_invalid_backoff_seconds("warrior_session_ok", default_seconds=3) == 0
    assert mod.marker_invalid_backoff_seconds("capture_marker_disabled", default_seconds=3) == 0


def test_transcript_feed_acceptance_allows_fresh_warrior_session_marker() -> None:
    mod = _load_module()

    accepted, reason = mod.transcript_feed_acceptance(
        "Watching CANF over VWAP for first pullback",
        marker_checker=lambda: (True, "warrior_session_ok"),
    )

    assert accepted is True
    assert reason == "trading_context"


def test_transcript_feed_acceptance_can_disable_marker_for_offline_tests() -> None:
    mod = _load_module()

    accepted, reason = mod.transcript_feed_acceptance(
        "Watching CANF over VWAP for first pullback",
        require_warrior_session_ok=False,
        marker_checker=lambda: (False, "warrior_session_marker_missing"),
    )

    assert accepted is True
    assert reason == "trading_context"


def test_warrior_session_marker_acceptance_rejects_missing_and_stale(tmp_path, monkeypatch) -> None:
    mod = _load_module()
    monkeypatch.setattr(mod, "WARRIOR_BROWSER_STATE_PATH", str(tmp_path / "no_state.json"))
    now = datetime.datetime(2026, 7, 1, 12, 0, tzinfo=datetime.timezone.utc)
    missing = tmp_path / "missing.json"

    ok, reason = mod.warrior_session_marker_acceptance(str(missing), now=now)
    assert ok is False
    assert reason == "warrior_session_marker_missing"

    stale = tmp_path / "stale.json"
    stale.write_text(
        json.dumps(
            {
                "ok": True,
                "ts": (now - datetime.timedelta(seconds=120)).isoformat(),
                "video_count": 1,
            }
        ),
        encoding="utf-8",
    )
    ok, reason = mod.warrior_session_marker_acceptance(str(stale), now=now, max_age_seconds=30)
    assert ok is False
    assert reason == "warrior_session_marker_stale"


def test_warrior_session_marker_acceptance_refreshes_from_fresh_browser_state(tmp_path, monkeypatch) -> None:
    mod = _load_module()
    now = datetime.datetime.now(datetime.timezone.utc)
    marker = tmp_path / "warrior_session_ok.json"
    state = tmp_path / "warrior_browser_state_latest.json"
    state.write_text(
        json.dumps(
            {
                "url": "https://chatroom.warriortrading.com/dashboard?hash=abc",
                "title": "WarriorTrading Chatroom",
                "bodyTextLength": 17943,
                "bodyTextSample": "Small Cap STREAM OPEN Screencast - Small Cap STREAM OPEN",
                "videoCount": 1,
                "iframeCount": 1,
                "canvasCount": 0,
            }
        ),
        encoding="utf-8",
    )
    fresh_epoch = now.timestamp()
    os.utime(state, (fresh_epoch, fresh_epoch))
    monkeypatch.setattr(mod, "WARRIOR_BROWSER_STATE_PATH", str(state))

    ok, reason = mod.warrior_session_marker_acceptance(str(marker), now=now, max_age_seconds=30)

    assert ok is True
    assert reason == "warrior_session_ok"
    written = json.loads(marker.read_text(encoding="utf-8"))
    assert written["ok"] is True
    assert written["source_state_path"] == str(state)


def test_warrior_session_marker_acceptance_requires_visible_stream(tmp_path, monkeypatch) -> None:
    mod = _load_module()
    monkeypatch.setattr(mod, "WARRIOR_BROWSER_STATE_PATH", str(tmp_path / "no_state.json"))
    now = datetime.datetime(2026, 7, 1, 12, 0, tzinfo=datetime.timezone.utc)
    marker = tmp_path / "marker.json"
    marker.write_text(
        json.dumps(
            {
                "ok": True,
                "ts": now.isoformat(),
                "video_count": 0,
                "stream_visible": False,
            }
        ),
        encoding="utf-8",
    )

    ok, reason = mod.warrior_session_marker_acceptance(str(marker), now=now, max_age_seconds=30)

    assert ok is False
    assert reason == "warrior_session_marker_no_stream"


def test_warrior_session_marker_acceptance_preserves_explicit_negative_reason(tmp_path, monkeypatch) -> None:
    mod = _load_module()
    monkeypatch.setattr(mod, "WARRIOR_BROWSER_STATE_PATH", str(tmp_path / "no_state.json"))
    now = datetime.datetime(2026, 7, 1, 12, 0, tzinfo=datetime.timezone.utc)
    marker = tmp_path / "marker.json"
    marker.write_text(
        json.dumps(
            {
                "ok": False,
                "reason": "warrior_disclaimer_blocking",
                "ts": now.isoformat(),
            }
        ),
        encoding="utf-8",
    )

    ok, reason = mod.warrior_session_marker_acceptance(str(marker), now=now, max_age_seconds=30)

    assert ok is False
    assert reason == "warrior_disclaimer_blocking"
