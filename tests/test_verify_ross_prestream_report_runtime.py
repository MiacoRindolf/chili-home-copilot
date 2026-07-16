import json
import os
from datetime import datetime, timedelta, timezone

from scripts.verify_ross_prestream_report_runtime import (
    evaluate_ross_prestream_report_artifacts,
    evaluate_ross_prestream_start_script,
    evaluate_ross_prestream_startup_tasks,
)


def _task(
    *,
    name: str = "CHILI-Ross-Prestream-Report-345",
    state: str = "Ready",
    actions: str = r'wscript.exe "D:\dev\chili-home-copilot\scripts\run-hidden.vbs" powershell.exe -File "D:\dev\chili-home-copilot\scripts\start-ross-prestream-report.ps1"',
    triggers: str = "MSFT_TaskDailyTrigger:2026-07-02T03:45:00",
) -> dict:
    return {
        "TaskName": name,
        "State": state,
        "Actions": actions,
        "Triggers": triggers,
        "NextRunTime": "2026-07-02T03:45:00-07:00",
    }


def test_ross_prestream_start_script_requires_prestream_report_args(tmp_path) -> None:
    script = tmp_path / "start-ross-prestream-report.ps1"
    script.write_text(
        """
python.exe D:\\dev\\chili-home-copilot\\scripts\\ross_prestream_report.py --profile prestream
ross_prestream_report.daemon.log
ross_prestream_report.daemon.err.log
""",
        encoding="utf-8",
    )

    ok, reason, details = evaluate_ross_prestream_start_script(script)

    assert ok is True
    assert reason == "ross_prestream_start_script_ok"
    assert details["missing_tokens"] == []


def test_ross_prestream_start_script_rejects_missing_profile(tmp_path) -> None:
    script = tmp_path / "start-ross-prestream-report.ps1"
    script.write_text("python.exe scripts\\ross_prestream_report.py", encoding="utf-8")

    ok, reason, details = evaluate_ross_prestream_start_script(script)

    assert ok is False
    assert reason == "ross_prestream_start_script_bad_args"
    assert "--profile" in details["missing_tokens"]
    assert "prestream" in details["missing_tokens"]


def test_ross_prestream_startup_task_accepts_daily_premarket_task() -> None:
    ok, reason, details = evaluate_ross_prestream_startup_tasks([_task()])

    assert ok is True
    assert reason == "ross_prestream_startup_tasks_ok"
    assert details["tasks"][0]["task_name"] == "CHILI-Ross-Prestream-Report-345"


def test_ross_prestream_startup_task_rejects_missing_bad_or_disabled_task() -> None:
    ok, reason, _details = evaluate_ross_prestream_startup_tasks([])
    assert ok is False
    assert reason == "ross_prestream_startup_task_missing"

    ok, reason, _details = evaluate_ross_prestream_startup_tasks([_task(actions="python other.py")])
    assert ok is False
    assert reason == "ross_prestream_startup_task_bad_action_or_time"

    ok, reason, _details = evaluate_ross_prestream_startup_tasks([_task(state="Disabled")])
    assert ok is False
    assert reason == "ross_prestream_startup_task_disabled"


def _write_report_artifacts(root, *, ok: bool = False) -> None:  # noqa: ANN001
    root.mkdir(parents=True, exist_ok=True)
    (root / "ross_prestream_report.json").write_text(
        json.dumps(
            {
                "ok": ok,
                "profile": "prestream",
                "blockers": ["warrior:warrior_disclaimer_blocking"],
                "next_actions": ["Clear disclaimer"],
                "tomorrow_live_command": "python scripts\\ross_live_monitor_snapshot.py --profile live",
            }
        ),
        encoding="utf-8",
    )
    (root / "ross_prestream_report.txt").write_text(
        "ok=False\nlive_command=python scripts\\ross_live_monitor_snapshot.py\nsummary_command=python scripts\\summarize_ross_live_monitor.py\n",
        encoding="utf-8",
    )


def test_ross_prestream_report_artifacts_accept_valid_report(tmp_path) -> None:
    _write_report_artifacts(tmp_path)

    ok, reason, details = evaluate_ross_prestream_report_artifacts(tmp_path)

    assert ok is True
    assert reason == "ross_prestream_report_artifacts_ok"
    assert details["missing_keys"] == []


def test_ross_prestream_report_artifacts_reject_missing_and_bad_shape(tmp_path) -> None:
    ok, reason, _details = evaluate_ross_prestream_report_artifacts(tmp_path)
    assert ok is False
    assert reason == "ross_prestream_report_json_missing"

    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "ross_prestream_report.json").write_text("[]", encoding="utf-8")
    (tmp_path / "ross_prestream_report.txt").write_text("live_command=x\nsummary_command=y\n", encoding="utf-8")
    ok, reason, _details = evaluate_ross_prestream_report_artifacts(tmp_path)
    assert ok is False
    assert reason == "ross_prestream_report_json_wrong_shape"


def test_ross_prestream_report_artifacts_reject_stale_report(tmp_path) -> None:
    _write_report_artifacts(tmp_path)
    now = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    stale = now - timedelta(hours=2)
    stale_ts = stale.timestamp()
    for name in ("ross_prestream_report.json", "ross_prestream_report.txt"):
        path = tmp_path / name
        path.touch()
        os.utime(path, (stale_ts, stale_ts))

    ok, reason, details = evaluate_ross_prestream_report_artifacts(
        tmp_path,
        max_age_seconds=60,
        now_utc=now,
    )

    assert ok is False
    assert reason in {"ross_prestream_report_json_stale", "ross_prestream_report_text_stale"}
    assert details["json_age_s"] >= 7200 or details["text_age_s"] >= 7200
