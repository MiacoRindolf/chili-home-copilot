from __future__ import annotations

from datetime import datetime, timezone

from scripts.verify_iqfeed_trade_bridge_runtime import (
    TRADE_BRIDGE_DAILY_TASK,
    TRADE_BRIDGE_LOGON_TASK,
    evaluate_iqfeed_trade_bridge_runtime,
    evaluate_iqfeed_trade_bridge_startup_tasks,
    find_iqfeed_trade_bridge_daemons,
)


def test_iqfeed_trade_bridge_runtime_rejects_missing_daemon() -> None:
    ok, reason, details = evaluate_iqfeed_trade_bridge_runtime(
        [
            {"ProcessId": 1, "Name": "python.exe", "CommandLine": "python scripts/iqfeed_depth_bridge.py"},
            {"ProcessId": 2, "Name": "python.exe", "CommandLine": "python scripts/ross_audio_transcribe.py"},
        ]
    )

    assert ok is False
    assert reason == "iqfeed_trade_bridge_not_running"
    assert details["running_daemons"] == []


def test_iqfeed_trade_bridge_runtime_accepts_running_daemon() -> None:
    ok, reason, details = evaluate_iqfeed_trade_bridge_runtime(
        [
            {
                "ProcessId": 7,
                "Name": "python.exe",
                "CommandLine": r"C:\Python\python.exe D:\dev\chili-home-copilot\scripts\iqfeed_trade_bridge.py",
            }
        ]
    )

    assert ok is True
    assert reason == "iqfeed_trade_bridge_runtime_ok"
    assert details["running_daemons"][0]["pid"] == 7


def test_iqfeed_trade_bridge_runtime_accepts_loaded_ross_watchlist_source(tmp_path) -> None:
    source = tmp_path / "iqfeed_trade_bridge.py"
    source.write_text(
        "def _ross_universe_symbols(limit): pass\n"
        "symbols = build_equity_universe(EQUITY_ROSS_SMALLCAP)\n"
        "ross = _ross_universe_symbols(room)\n",
        encoding="utf-8",
    )
    source_mtime = datetime.fromtimestamp(source.stat().st_mtime, tz=timezone.utc)
    started_ms = int((source_mtime.timestamp() + 10.0) * 1000)

    ok, reason, details = evaluate_iqfeed_trade_bridge_runtime(
        [
            {
                "ProcessId": 7,
                "Name": "python.exe",
                "CreationDate": f"/Date({started_ms})/",
                "CommandLine": "python scripts/iqfeed_trade_bridge.py",
            }
        ],
        source_path=source,
    )

    assert ok is True
    assert reason == "iqfeed_trade_bridge_runtime_ok"
    assert details["source_mtime_utc"] is not None


def test_iqfeed_trade_bridge_runtime_rejects_stale_process_after_source_change(tmp_path) -> None:
    source = tmp_path / "iqfeed_trade_bridge.py"
    source.write_text(
        "def _ross_universe_symbols(limit): pass\n"
        "symbols = build_equity_universe(EQUITY_ROSS_SMALLCAP)\n"
        "ross = _ross_universe_symbols(room)\n",
        encoding="utf-8",
    )
    source_mtime = datetime.fromtimestamp(source.stat().st_mtime, tz=timezone.utc)
    started_ms = int((source_mtime.timestamp() - 10.0) * 1000)

    ok, reason, details = evaluate_iqfeed_trade_bridge_runtime(
        [
            {
                "ProcessId": 8,
                "Name": "python.exe",
                "CreationDate": f"/Date({started_ms})/",
                "CommandLine": "python scripts/iqfeed_trade_bridge.py",
            }
        ],
        source_path=source,
    )

    assert ok is False
    assert reason == "iqfeed_trade_bridge_source_newer_than_process"
    assert details["stale_daemons"][0]["pid"] == 8


def test_iqfeed_trade_bridge_runtime_rejects_missing_ross_watchlist_source(tmp_path) -> None:
    source = tmp_path / "iqfeed_trade_bridge.py"
    source.write_text("def _target_symbols(): return set()\n", encoding="utf-8")

    ok, reason, details = evaluate_iqfeed_trade_bridge_runtime(
        [
            {
                "ProcessId": 10,
                "Name": "python.exe",
                "CreationDate": "/Date(1782937425978)/",
                "CommandLine": "python scripts/iqfeed_trade_bridge.py",
            }
        ],
        source_path=source,
    )

    assert ok is False
    assert reason == "iqfeed_trade_bridge_ross_watchlist_source_missing"
    assert "ross_universe_helper" in details["missing_source_markers"]


def test_iqfeed_trade_bridge_runtime_rejects_notify_disabled_command() -> None:
    ok, reason, details = evaluate_iqfeed_trade_bridge_runtime(
        [
            {
                "ProcessId": 9,
                "Name": "powershell.exe",
                "CommandLine": (
                    "powershell -Command \"$env:IQFEED_NOTIFY_ENABLED='0'; "
                    "python scripts/iqfeed_trade_bridge.py\""
                ),
            }
        ]
    )

    assert ok is False
    assert reason == "iqfeed_trade_bridge_notify_disabled"
    assert details["disabled_daemons"][0]["pid"] == 9


def test_iqfeed_trade_bridge_finder_ignores_adjacent_daemons() -> None:
    found = find_iqfeed_trade_bridge_daemons(
        [
            {"ProcessId": 1, "CommandLine": "python scripts/iqfeed_depth_bridge.py"},
            {"ProcessId": 2, "CommandLine": "python scripts/ross_transcript_viability_bridge.py"},
            {"ProcessId": 3, "CommandLine": "python scripts/iqfeed_trade_bridge.py"},
        ]
    )

    assert [row["ProcessId"] for row in found] == [3]


def _task(name: str, *, state: str = "Ready", action: str | None = None, trigger: str | None = None) -> dict:
    return {
        "TaskName": name,
        "State": state,
        "Actions": action
        or r'wscript.exe "D:\dev\chili-home-copilot\scripts\run-hidden.vbs" powershell.exe -File "D:\dev\chili-home-copilot\scripts\start-iqfeed-trade-bridge.ps1"',
        "Triggers": trigger or "MSFT_TaskDailyTrigger:2026-06-23T03:56:00-07:00",
        "NextRunTime": "2026-07-02T03:56:00-07:00",
    }


def test_iqfeed_trade_bridge_startup_tasks_accept_daily_and_logon() -> None:
    ok, reason, details = evaluate_iqfeed_trade_bridge_startup_tasks(
        [
            _task(TRADE_BRIDGE_DAILY_TASK),
            _task(TRADE_BRIDGE_LOGON_TASK, trigger="MSFT_TaskLogonTrigger:"),
        ]
    )

    assert ok is True
    assert reason == "iqfeed_trade_bridge_startup_tasks_ok"
    assert len(details["tasks"]) == 2


def test_iqfeed_trade_bridge_startup_tasks_reject_missing_logon() -> None:
    ok, reason, details = evaluate_iqfeed_trade_bridge_startup_tasks([_task(TRADE_BRIDGE_DAILY_TASK)])

    assert ok is False
    assert reason == "iqfeed_trade_bridge_startup_task_missing"
    assert details["missing_tasks"] == [TRADE_BRIDGE_LOGON_TASK]


def test_iqfeed_trade_bridge_startup_tasks_reject_disabled_task() -> None:
    ok, reason, details = evaluate_iqfeed_trade_bridge_startup_tasks(
        [
            _task(TRADE_BRIDGE_DAILY_TASK, state="Disabled"),
            _task(TRADE_BRIDGE_LOGON_TASK, trigger="MSFT_TaskLogonTrigger:"),
        ]
    )

    assert ok is False
    assert reason == "iqfeed_trade_bridge_startup_task_disabled"
    assert details["disabled_tasks"] == [TRADE_BRIDGE_DAILY_TASK]


def test_iqfeed_trade_bridge_startup_tasks_reject_bad_action() -> None:
    ok, reason, details = evaluate_iqfeed_trade_bridge_startup_tasks(
        [
            _task(TRADE_BRIDGE_DAILY_TASK, action="powershell.exe -File other.ps1"),
            _task(TRADE_BRIDGE_LOGON_TASK, trigger="MSFT_TaskLogonTrigger:"),
        ]
    )

    assert ok is False
    assert reason == "iqfeed_trade_bridge_startup_task_bad_action"
    assert details["bad_action_tasks"] == [TRADE_BRIDGE_DAILY_TASK]


def test_iqfeed_trade_bridge_startup_tasks_reject_wrong_daily_time() -> None:
    ok, reason, details = evaluate_iqfeed_trade_bridge_startup_tasks(
        [
            _task(TRADE_BRIDGE_DAILY_TASK, trigger="MSFT_TaskDailyTrigger:2026-06-23T04:30:00-07:00"),
            _task(TRADE_BRIDGE_LOGON_TASK, trigger="MSFT_TaskLogonTrigger:"),
        ]
    )

    assert ok is False
    assert reason == "iqfeed_trade_bridge_daily_task_not_0356"
    assert "04:30" in details["daily_trigger"]
