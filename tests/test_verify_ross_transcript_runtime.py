from __future__ import annotations

from scripts.verify_ross_transcript_runtime import (
    evaluate_ross_audio_daemon_source,
    evaluate_ross_audio_start_script,
    evaluate_ross_audio_startup_tasks,
    evaluate_transcript_runtime,
    find_ross_transcript_daemons,
)


def test_missing_warrior_marker_passes_when_transcript_daemons_are_off() -> None:
    ok, reason, details = evaluate_transcript_runtime(
        marker_ok=False,
        marker_reason="warrior_session_marker_missing",
        marker_detail={"path": "D:/CHILI-Docker/chili-data/ross_stream/warrior_session_ok.json"},
        processes=[],
    )

    assert ok is True
    assert reason == "ross_transcript_ingestion_guarded"
    assert details["running_daemons"] == []


def test_missing_warrior_marker_fails_when_viability_bridge_is_running() -> None:
    ok, reason, details = evaluate_transcript_runtime(
        marker_ok=False,
        marker_reason="warrior_session_marker_missing",
        marker_detail={},
        processes=[
            {
                "ProcessId": 222,
                "Name": "python.exe",
                "CommandLine": "python scripts/ross_transcript_viability_bridge.py",
            }
        ],
    )

    assert ok is False
    assert reason == "ross_transcript_daemon_running_without_warrior_session"
    assert details["running_daemons"][0]["pid"] == 222


def test_stale_warrior_marker_fails_when_audio_daemon_is_running() -> None:
    ok, reason, details = evaluate_transcript_runtime(
        marker_ok=False,
        marker_reason="warrior_session_marker_stale",
        marker_detail={"age_s": 300.0},
        processes=[
            {
                "pid": 333,
                "name": "python.exe",
                "cmdline": "python -u scripts/ross_audio_transcribe.py",
            }
        ],
    )

    assert ok is False
    assert reason == "ross_transcript_daemon_running_without_warrior_session"
    assert details["running_daemons"][0]["pid"] == 333


def test_fresh_warrior_marker_allows_transcript_daemon() -> None:
    ok, reason, details = evaluate_transcript_runtime(
        marker_ok=True,
        marker_reason="warrior_session_ok",
        marker_detail={"video_count": 1, "stream_visible": True},
        processes=[
            {
                "ProcessId": 444,
                "Name": "python.exe",
                "CommandLine": "python scripts/ross_transcript_admission_audit.py",
            }
        ],
    )

    assert ok is True
    assert reason == "ross_transcript_runtime_ok"
    assert details["running_daemons"][0]["pid"] == 444


def test_finder_ignores_unrelated_python_processes() -> None:
    found = find_ross_transcript_daemons(
        [
            {"ProcessId": 1, "Name": "python.exe", "CommandLine": "python scripts/iqfeed_trade_bridge.py"},
            {"ProcessId": 2, "Name": "python.exe", "CommandLine": "python scripts/ross_audio_transcribe.py"},
        ]
    )

    assert len(found) == 1
    assert found[0]["ProcessId"] == 2


def _audio_task(
    *,
    name: str = "CHILI-Ross-Audio-Transcribe-350",
    state: str = "Ready",
    action: str | None = None,
    trigger: str = "MSFT_TaskDailyTrigger:2026-06-24T03:50:00-07:00",
) -> dict:
    return {
        "TaskName": name,
        "State": state,
        "Actions": action
        or r'wscript.exe "D:\dev\chili-home-copilot\scripts\run-hidden.vbs" powershell.exe -File "D:\dev\chili-home-copilot\scripts\start-ross-audio-transcribe.ps1"',
        "Triggers": trigger,
        "NextRunTime": "2026-07-02T03:50:00-07:00",
    }


def test_ross_audio_startup_task_accepts_350_daily_launcher() -> None:
    ok, reason, details = evaluate_ross_audio_startup_tasks([_audio_task()])

    assert ok is True
    assert reason == "ross_audio_startup_tasks_ok"
    assert details["tasks"][0]["task_name"] == "CHILI-Ross-Audio-Transcribe-350"


def test_ross_audio_startup_task_rejects_missing() -> None:
    ok, reason, details = evaluate_ross_audio_startup_tasks([])

    assert ok is False
    assert reason == "ross_audio_startup_task_missing"
    assert details["tasks"] == []


def test_ross_audio_startup_task_rejects_disabled() -> None:
    ok, reason, details = evaluate_ross_audio_startup_tasks([_audio_task(state="Disabled")])

    assert ok is False
    assert reason == "ross_audio_startup_task_disabled"
    assert details["tasks"][0]["state"] == "Disabled"


def test_ross_audio_startup_task_rejects_wrong_launcher() -> None:
    ok, reason, details = evaluate_ross_audio_startup_tasks(
        [_audio_task(action="powershell.exe -File other.ps1")]
    )

    assert ok is False
    assert reason == "ross_audio_startup_task_bad_action_or_time"
    assert "other.ps1" in details["tasks"][0]["actions"]


def test_ross_audio_startup_task_rejects_late_daily_time() -> None:
    ok, reason, details = evaluate_ross_audio_startup_tasks(
        [_audio_task(trigger="MSFT_TaskDailyTrigger:2026-06-24T05:00:00-07:00")]
    )

    assert ok is False
    assert reason == "ross_audio_startup_task_bad_action_or_time"
    assert "05:00" in details["tasks"][0]["triggers"]


def test_ross_audio_start_script_requires_marker_wait_guard(tmp_path) -> None:
    script = tmp_path / "start-ross-audio-transcribe.ps1"
    script.write_text(
        """
$marker = 'D:\\CHILI-Docker\\chili-data\\ross_stream\\warrior_session_ok.json'
waiting_for_warrior_session_marker
$m.video_count
$m.stream_visible
if ($existing) { exit 0 }
python.exe D:\\dev\\chili-home-copilot\\scripts\\ross_audio_transcribe.py
""",
        encoding="utf-8",
    )

    ok, reason, details = evaluate_ross_audio_start_script(script)

    assert ok is True
    assert reason == "ross_audio_start_script_ok"
    assert details["missing_tokens"] == []


def test_ross_audio_start_script_rejects_direct_daemon_launch_without_marker_wait(tmp_path) -> None:
    script = tmp_path / "start-ross-audio-transcribe.ps1"
    script.write_text(
        "python.exe D:\\dev\\chili-home-copilot\\scripts\\ross_audio_transcribe.py",
        encoding="utf-8",
    )

    ok, reason, details = evaluate_ross_audio_start_script(script)

    assert ok is False
    assert reason == "ross_audio_start_script_missing_marker_wait"
    assert "warrior_session_ok.json" in details["missing_tokens"]
    assert "waiting_for_warrior_session_marker" in details["missing_tokens"]


def test_ross_audio_daemon_source_requires_capture_stop_gate(tmp_path) -> None:
    source = tmp_path / "ross_audio_transcribe.py"
    source.write_text(
        """
def audio_capture_acceptance():
    return True, "capture_marker_disabled"

def _transcribe_loop():
    return "warrior marker invalid -> stop capture"
""",
        encoding="utf-8",
    )

    ok, reason, details = evaluate_ross_audio_daemon_source(source)

    assert ok is True
    assert reason == "ross_audio_daemon_source_ok"
    assert details["missing_tokens"] == []


def test_ross_audio_daemon_source_rejects_missing_capture_stop_gate(tmp_path) -> None:
    source = tmp_path / "ross_audio_transcribe.py"
    source.write_text("def transcript_feed_acceptance(): pass", encoding="utf-8")

    ok, reason, details = evaluate_ross_audio_daemon_source(source)

    assert ok is False
    assert reason == "ross_audio_daemon_source_missing_capture_gate"
    assert "def audio_capture_acceptance" in details["missing_tokens"]
