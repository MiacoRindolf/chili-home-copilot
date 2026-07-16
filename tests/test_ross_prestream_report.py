from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone

from scripts.ross_prestream_report import (
    _run_check,
    build_report,
    evaluate_warrior_browser_state_file,
    runtime_checks,
    write_report,
)


def _snapshot(*, ok: bool = True, warrior: str = "warrior_session_ok", feed_severity: str = "ok") -> dict:
    return {
        "ok": ok,
        "read_only": True,
        "readiness": {
            "reason": "ross_live_window_ready" if ok else "ross_live_window_warrior_session_not_ready",
            "feed_reason": "ross_lane_feed_runtime_ok",
            "feed_severity": feed_severity,
            "warrior_session_reason": warrior,
        },
        "incidents": [],
    }


def test_prestream_report_surfaces_warrior_and_runtime_blockers() -> None:
    report = build_report(
        snapshot=_snapshot(ok=False, warrior="warrior_stream_offline"),
        checks={
            "momentum_worker_runtime": {
                "ok": False,
                "stderr": ["compose_image_alignment_failed:running=good:compose=stale"],
                "stdout": [],
            }
        },
        profile="prestream",
    )

    assert report["ok"] is False
    assert "warrior:warrior_stream_offline" in report["blockers"]
    assert any(row.startswith("momentum_worker_runtime:compose_image_alignment_failed") for row in report["blockers"])
    assert any("keep warrior_session_ok.json fresh every <=30s" in action for action in report["next_actions"])
    assert any("canonical momentum worker" in action for action in report["next_actions"])


def test_prestream_report_surfaces_disclaimer_action() -> None:
    report = build_report(
        snapshot=_snapshot(ok=False, warrior="warrior_disclaimer_blocking"),
        checks={},
        profile="prestream",
    )

    assert "warrior:warrior_disclaimer_blocking" in report["blockers"]
    assert any("Clear the Warrior disclaimer" in action for action in report["next_actions"])


def test_prestream_report_surfaces_stale_browser_state_probe() -> None:
    report = build_report(
        snapshot=_snapshot(),
        checks={},
        profile="prestream",
        browser_state={"ok": False, "reason": "warrior_browser_state_stale"},
    )

    assert report["ok"] is False
    assert "browser_state:warrior_browser_state_stale" in report["blockers"]
    assert any("warrior_browser_state_latest.json updates every <=30s" in row for row in report["next_actions"])


def test_warrior_browser_state_file_accepts_fresh_visible_stream(tmp_path) -> None:
    now = datetime.now(timezone.utc)
    path = tmp_path / "warrior_browser_state_latest.json"
    path.write_text(
        json.dumps(
            {
                "url": "https://chatroom.warriortrading.com/dashboard?hash=abc",
                "streamVisible": True,
                "videoCount": 1,
                "iframeCount": 1,
            }
        ),
        encoding="utf-8",
    )

    details = evaluate_warrior_browser_state_file(path, now_utc=now)

    assert details["ok"] is True
    assert details["reason"] == "warrior_browser_state_ok"


def test_warrior_browser_state_file_rejects_stale_visible_stream(tmp_path) -> None:
    now = datetime.now(timezone.utc)
    path = tmp_path / "warrior_browser_state_latest.json"
    path.write_text(
        json.dumps(
            {
                "url": "https://chatroom.warriortrading.com/dashboard?hash=abc",
                "streamVisible": True,
                "videoCount": 1,
            }
        ),
        encoding="utf-8",
    )
    stale = (now - timedelta(seconds=120)).timestamp()
    os.utime(path, (stale, stale))

    details = evaluate_warrior_browser_state_file(path, max_age_seconds=30, now_utc=now)

    assert details["ok"] is False
    assert details["reason"] == "warrior_browser_state_stale"


def test_warrior_browser_state_file_preserves_disclaimer_blocker(tmp_path) -> None:
    now = datetime.now(timezone.utc)
    path = tmp_path / "warrior_browser_state_latest.json"
    path.write_text(
        json.dumps(
            {
                "url": "https://chatroom.warriortrading.com/dashboard?hash=abc",
                "bodyExcerpt": "Disclaimer Ross results are NOT typical ACCEPT DECLINE",
                "bodyTextLength": 550,
                "streamVisible": False,
                "videoCount": 0,
            }
        ),
        encoding="utf-8",
    )

    details = evaluate_warrior_browser_state_file(path, now_utc=now)

    assert details["ok"] is False
    assert details["reason"] == "warrior_disclaimer_blocking"


def test_prestream_report_clean_path_includes_live_monitor_command(tmp_path) -> None:
    report = build_report(snapshot=_snapshot(), checks={}, profile="prestream")

    json_path, text_path = write_report(report, out_dir=tmp_path)

    assert report["ok"] is True
    assert report["blockers"] == []
    assert "ross_live_monitor_snapshot.py --profile live" in report["tomorrow_live_command"]
    assert "--watch --interval-seconds 2 --seconds 18000" in report["tomorrow_live_command"]
    assert "ross_live_monitor_{date}.jsonl" in report["tomorrow_live_command"]
    assert "summarize_ross_live_monitor.py" in report["poststream_summary_command"]
    assert "mark_ross_trade_event.py SYMBOL" in report["ross_trade_marker_command_template"]
    assert "--ts UTC_ISO_TIMESTAMP" in report["ross_trade_marker_with_timestamp_template"]
    assert json.loads(json_path.read_text(encoding="utf-8"))["ok"] is True
    assert json.loads(json_path.read_text(encoding="utf-8"))["post_write_artifact_check"]["ok"] is True
    text = text_path.read_text(encoding="utf-8")
    assert "blockers=none" in text
    assert "post_write_artifact_check=ross_prestream_report_artifacts_ok" in text
    assert "summary_command=python scripts\\summarize_ross_live_monitor.py" in text
    assert "mark_trade_command=python scripts\\mark_ross_trade_event.py SYMBOL" in text
    assert "mark_trade_with_ts_command=python scripts\\mark_ross_trade_event.py SYMBOL" in text


def test_runtime_checks_include_iqfeed_trade_bridge_and_monitor_runtime() -> None:
    commands: list[list[str]] = []

    def runner(cmd, **kwargs):  # noqa: ANN001, ANN202
        commands.append(list(cmd))

        class Completed:
            returncode = 0
            stdout = ""
            stderr = ""

        return Completed()

    checks = runtime_checks(runner=runner)

    assert "iqfeed_trade_bridge_runtime" in checks
    assert "ross_live_monitor_runtime" in checks
    assert "ross_prestream_report_runtime" in checks
    assert "ross_morning_task_chain_runtime" in checks
    assert any("scripts/verify_iqfeed_trade_bridge_runtime.py" in " ".join(cmd) for cmd in commands)
    assert any("scripts/verify_ross_live_monitor_runtime.py" in " ".join(cmd) for cmd in commands)
    assert any("scripts/verify_ross_prestream_report_runtime.py" in " ".join(cmd) for cmd in commands)
    assert any("--skip-artifacts" in " ".join(cmd) for cmd in commands)
    assert any("scripts/verify_ross_morning_task_chain_runtime.py" in " ".join(cmd) for cmd in commands)


def test_run_check_returns_blocker_row_on_timeout() -> None:
    def runner(cmd, **kwargs):  # noqa: ANN001, ANN202
        raise subprocess.TimeoutExpired(cmd, timeout=240)

    row = _run_check(["python", "slow.py"], runner=runner)

    assert row["ok"] is False
    assert row["returncode"] is None
    assert row["stderr"] == ["timeout_after_seconds:240"]
