from scripts.verify_ross_live_monitor_runtime import (
    evaluate_ross_live_monitor_start_script,
    evaluate_ross_live_monitor_startup_tasks,
    find_ross_live_monitor_daemons,
)


def _task(
    *,
    name: str = "CHILI-Ross-Live-Monitor-358",
    state: str = "Ready",
    actions: str = r'wscript.exe "D:\dev\chili-home-copilot\scripts\run-hidden.vbs" powershell.exe -File "D:\dev\chili-home-copilot\scripts\start-ross-live-monitor.ps1"',
    triggers: str = "MSFT_TaskDailyTrigger:2026-07-02T03:58:00",
) -> dict:
    return {
        "TaskName": name,
        "State": state,
        "Actions": actions,
        "Triggers": triggers,
        "NextRunTime": "2026-07-02T03:58:00-07:00",
    }


def test_find_ross_live_monitor_daemons_requires_live_watch_profile() -> None:
    rows = [
        {
            "ProcessId": 1,
            "Name": "python.exe",
            "CommandLine": "python scripts/ross_live_monitor_snapshot.py --profile live --watch --seconds 18000",
        },
        {
            "ProcessId": 2,
            "Name": "python.exe",
            "CommandLine": "python scripts/ross_live_monitor_snapshot.py --profile quiet",
        },
    ]

    found = find_ross_live_monitor_daemons(rows)

    assert [row["ProcessId"] for row in found] == [1]


def test_ross_live_monitor_startup_task_accepts_daily_premarket_task() -> None:
    ok, reason, details = evaluate_ross_live_monitor_startup_tasks([_task()])

    assert ok is True
    assert reason == "ross_live_monitor_startup_tasks_ok"
    assert details["tasks"][0]["task_name"] == "CHILI-Ross-Live-Monitor-358"


def test_ross_live_monitor_startup_task_rejects_missing_or_bad_action() -> None:
    ok, reason, _details = evaluate_ross_live_monitor_startup_tasks([])
    assert ok is False
    assert reason == "ross_live_monitor_startup_task_missing"

    ok, reason, _details = evaluate_ross_live_monitor_startup_tasks([_task(actions="python other.py")])
    assert ok is False
    assert reason == "ross_live_monitor_startup_task_bad_action_or_time"


def test_ross_live_monitor_startup_task_rejects_disabled_task() -> None:
    ok, reason, _details = evaluate_ross_live_monitor_startup_tasks([_task(state="Disabled")])

    assert ok is False
    assert reason == "ross_live_monitor_startup_task_disabled"


def test_ross_live_monitor_start_script_requires_bounded_jsonl_recorder(tmp_path) -> None:
    script = tmp_path / "start-ross-live-monitor.ps1"
    script.write_text(
        """
python.exe D:\\dev\\chili-home-copilot\\scripts\\ross_live_monitor_snapshot.py `
  --profile live --watch --interval-seconds 2 --seconds 18000 `
  --out D:\\CHILI-Docker\\chili-data\\ross_stream\\ross_live_monitor_{date}.jsonl
""",
        encoding="utf-8",
    )

    ok, reason, details = evaluate_ross_live_monitor_start_script(script)

    assert ok is True
    assert reason == "ross_live_monitor_start_script_ok"
    assert details["missing_tokens"] == []


def test_ross_live_monitor_start_script_rejects_one_shot_watch(tmp_path) -> None:
    script = tmp_path / "start-ross-live-monitor.ps1"
    script.write_text(
        "python.exe scripts\\ross_live_monitor_snapshot.py --profile live --watch --out ross_live_monitor.jsonl",
        encoding="utf-8",
    )

    ok, reason, details = evaluate_ross_live_monitor_start_script(script)

    assert ok is False
    assert reason == "ross_live_monitor_start_script_bad_args"
    assert "--seconds" in details["missing_tokens"]
    assert "18000" in details["missing_tokens"]
