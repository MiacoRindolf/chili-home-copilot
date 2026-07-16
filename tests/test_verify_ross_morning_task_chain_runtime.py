from scripts.verify_ross_morning_task_chain_runtime import evaluate_ross_morning_task_chain


def _task(name: str, script: str, minute: str, *, state: str = "Ready", action_ok: bool = True) -> dict:
    action = (
        rf'wscript.exe "D:\dev\chili-home-copilot\scripts\run-hidden.vbs" powershell.exe -File "D:\dev\chili-home-copilot\scripts\{script}"'
        if action_ok
        else "python other.py"
    )
    return {
        "TaskName": name,
        "State": state,
        "Actions": action,
        "Triggers": f"MSFT_TaskDailyTrigger:2026-07-02T{minute}:00",
        "NextRunTime": f"2026-07-02T{minute}:00-07:00",
    }


def _good_tasks() -> list[dict]:
    return [
        _task("CHILI-Ross-Prestream-Report-345", "start-ross-prestream-report.ps1", "03:45"),
        _task("CHILI-Ross-Audio-Transcribe-350", "start-ross-audio-transcribe.ps1", "03:50"),
        _task("CHILI-IQFeed-Trade-Bridge-Daily", "start-iqfeed-trade-bridge.ps1", "03:56"),
        _task("CHILI-Ross-Live-Monitor-358", "start-ross-live-monitor.ps1", "03:58"),
    ]


def test_ross_morning_task_chain_accepts_expected_order() -> None:
    ok, reason, details = evaluate_ross_morning_task_chain(_good_tasks())

    assert ok is True
    assert reason == "ross_morning_task_chain_ok"
    assert [row["minute"] for row in details["tasks"]] == [225, 230, 236, 238]


def test_ross_morning_task_chain_rejects_missing_task() -> None:
    ok, reason, details = evaluate_ross_morning_task_chain(_good_tasks()[:-1])

    assert ok is False
    assert reason == "ross_morning_task_chain_missing_task"
    assert details["missing_tasks"] == ["CHILI-Ross-Live-Monitor-358"]


def test_ross_morning_task_chain_rejects_bad_time() -> None:
    tasks = _good_tasks()
    tasks[2] = _task("CHILI-IQFeed-Trade-Bridge-Daily", "start-iqfeed-trade-bridge.ps1", "04:05")

    ok, reason, details = evaluate_ross_morning_task_chain(tasks)

    assert ok is False
    assert reason == "ross_morning_task_chain_bad_time"
    assert details["bad_time_tasks"][0]["task_name"] == "CHILI-IQFeed-Trade-Bridge-Daily"


def test_ross_morning_task_chain_rejects_bad_action_and_disabled() -> None:
    tasks = _good_tasks()
    tasks[0] = _task("CHILI-Ross-Prestream-Report-345", "start-ross-prestream-report.ps1", "03:45", action_ok=False)

    ok, reason, details = evaluate_ross_morning_task_chain(tasks)

    assert ok is False
    assert reason == "ross_morning_task_chain_bad_action"
    assert details["bad_action_tasks"] == ["CHILI-Ross-Prestream-Report-345"]

    tasks = _good_tasks()
    tasks[1] = _task("CHILI-Ross-Audio-Transcribe-350", "start-ross-audio-transcribe.ps1", "03:50", state="Disabled")
    ok, reason, details = evaluate_ross_morning_task_chain(tasks)
    assert ok is False
    assert reason == "ross_morning_task_chain_disabled_task"
    assert details["disabled_tasks"] == ["CHILI-Ross-Audio-Transcribe-350"]
