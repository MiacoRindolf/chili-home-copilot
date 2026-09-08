"""Claude Code bridge — transcript tailing and status derivation.

DB-free.  The transcript is an append-only JSONL that reaches 100 MB+ in a long
session, so the reader must tail by byte offset and must never return a record
twice; both properties are asserted here against synthetic transcripts.
"""
from __future__ import annotations

import json
import time

import pytest

from app.routers import claude_bridge as cb


def _line(rec: dict) -> str:
    return json.dumps(rec, ensure_ascii=False) + "\n"


def _assistant(text: str = "", tools: list[tuple[str, str]] | None = None, ts: str = "2026-09-08T05:00:00Z") -> str:
    content: list[dict] = []
    if text:
        content.append({"type": "text", "text": text})
    for name, desc in (tools or []):
        content.append({"type": "tool_use", "name": name, "input": {"description": desc}})
    return _line({"type": "assistant", "timestamp": ts, "message": {"role": "assistant", "content": content}})


def _user(text: str, ts: str = "2026-09-08T05:00:00Z") -> str:
    return _line({"type": "user", "timestamp": ts, "message": {"role": "user", "content": text}})


def _tool_result(ts: str = "2026-09-08T05:00:01Z") -> str:
    return _line({"type": "user", "timestamp": ts, "message": {
        "role": "user", "content": [{"type": "tool_result", "content": "ok"}]}})


@pytest.fixture()
def transcript(tmp_path):
    p = tmp_path / "sess.jsonl"
    p.write_text(
        _user("kamusta?")
        + _assistant("Tumatakbo.", [("Bash", "check the lane")])
        + _tool_result()
        + _assistant("Tapos na."),
        encoding="utf-8",
    )
    return p


def test_tail_splits_text_from_tool_activity(transcript):
    rows, offset, raw = cb._parse_tail(transcript, 0, 512 * 1024)
    kinds = [r["kind"] for r in rows]
    assert kinds == ["text", "text", "tool", "text"]
    assert rows[0]["role"] == "user" and rows[0]["text"] == "kamusta?"
    assert rows[2]["tool"] == "Bash" and rows[2]["text"] == "check the lane"
    assert offset == transcript.stat().st_size
    assert raw["last_kind"] == "text_assistant"


def test_tool_result_records_are_not_shown_as_user_speech(transcript):
    rows, _, _ = cb._parse_tail(transcript, 0, 512 * 1024)
    # The harness feeding a tool result back is not the operator talking.
    assert all(not (r["role"] == "user" and r["text"] == "ok") for r in rows)


def test_incremental_tail_never_repeats_a_record(transcript):
    first, offset, _ = cb._parse_tail(transcript, 0, 512 * 1024)
    with transcript.open("a", encoding="utf-8") as fh:
        fh.write(_assistant("Bagong linya."))
    second, offset2, _ = cb._parse_tail(transcript, offset, 512 * 1024)
    assert [r["text"] for r in second] == ["Bagong linya."]
    assert offset2 > offset
    assert not any(r in first for r in second)


def test_partial_trailing_line_is_rewound_not_parsed(tmp_path):
    """A writer mid-append must not cost us the record."""
    p = tmp_path / "s.jsonl"
    p.write_text(_assistant("kumpleto"), encoding="utf-8")
    full = p.stat().st_size
    with p.open("a", encoding="utf-8") as fh:
        fh.write('{"type":"assistant","message":{"role":"assis')  # torn write
    rows, offset, _ = cb._parse_tail(p, 0, 512 * 1024)
    assert [r["text"] for r in rows] == ["kumpleto"]
    assert offset == full          # rewound to the last complete record
    # Once the writer finishes the line, the next poll picks it up whole.
    with p.open("a", encoding="utf-8") as fh:
        fh.write('tant","content":[{"type":"text","text":"buo na"}]}}\n')
    rows2, _, _ = cb._parse_tail(p, offset, 512 * 1024)
    assert [r["text"] for r in rows2] == ["buo na"]


def test_window_start_mid_line_drops_the_fragment(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text(_assistant("una") + _assistant("pangalawa"), encoding="utf-8")
    # A tail smaller than the file starts mid-record; that fragment is junk.
    rows, _, _ = cb._parse_tail(p, 0, 40)
    assert all(isinstance(r.get("text"), str) for r in rows)
    assert "una" not in [r.get("text") for r in rows]


def test_queued_operator_message_is_shown_but_task_noise_is_not(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text(
        _line({"type": "queue-operation", "operation": "enqueue",
               "timestamp": "2026-09-08T05:00:00Z", "content": "ayusin mo yung bailout"})
        + _line({"type": "queue-operation", "operation": "enqueue",
                 "timestamp": "2026-09-08T05:00:01Z",
                 "content": "<task-notification>\n<task-id>abc</task-id>"})
        + _line({"type": "queue-operation", "operation": "remove",
                 "timestamp": "2026-09-08T05:00:02Z", "content": "ayusin mo yung bailout"}),
        encoding="utf-8")
    rows, _, _ = cb._parse_tail(p, 0, 512 * 1024)
    assert [r["text"] for r in rows] == ["ayusin mo yung bailout"]


@pytest.mark.parametrize(
    "last_kind,silence,expected",
    [
        ("tool_use", 3, "working"),        # tools still landing
        ("text_assistant", 5, "working"),  # just spoke, turn may continue
        ("text_assistant", 400, "waiting"),
        ("tool_use", 400, "waiting"),      # silent long enough to have stopped
        ("text_assistant", 4000, "idle"),
    ],
)
def test_status_states(last_kind, silence, expected):
    st = cb._status_from({"last_kind": last_kind, "last_ts": ""}, time.time() - silence)
    assert st["state"] == expected
    assert st["label"]


def test_session_path_refuses_traversal_and_unknown_ids(tmp_path):
    (tmp_path / "good.jsonl").write_text("", encoding="utf-8")
    assert cb._safe_session_path(tmp_path, "../../secrets") is None
    assert cb._safe_session_path(tmp_path, "not a session id") is None
    assert cb._safe_session_path(tmp_path, "missing-0000-0000") is None


def test_slug_dir_refuses_traversal(tmp_path):
    (tmp_path / "D--dev-chili-home-copilot").mkdir()
    assert cb._slug_dir(tmp_path, "../..") is None
    assert cb._slug_dir(tmp_path, ".hidden") is None
    assert cb._slug_dir(tmp_path, "D--dev-chili-home-copilot") is not None


def test_slug_dir_prefers_this_repo_over_a_newer_unrelated_project(tmp_path):
    other = tmp_path / "C--dev-something-else"
    mine = tmp_path / "D--dev-chili-home-copilot"
    mine.mkdir()
    other.mkdir()
    # Make the unrelated project the most recently touched one.
    import os
    os.utime(other, (time.time() + 60, time.time() + 60))
    assert cb._slug_dir(tmp_path, None) == mine
