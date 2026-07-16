from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
from typing import Any, Sequence


IQFEED_TRADE_BRIDGE_DAEMON = "iqfeed_trade_bridge.py"
TRADE_BRIDGE_DAILY_TASK = "CHILI-IQFeed-Trade-Bridge-Daily"
TRADE_BRIDGE_LOGON_TASK = "CHILI-IQFeed-Trade-Bridge-Logon"
ROOT = Path(__file__).resolve().parents[1]


def _command_line(proc: dict[str, Any]) -> str:
    return " ".join(
        str(proc.get(key) or "")
        for key in ("CommandLine", "command_line", "cmdline", "Name", "name")
    )


def _is_trade_bridge_command(line: str) -> bool:
    normalized = str(line or "").replace("\\", "/").lower()
    escaped = re.escape(IQFEED_TRADE_BRIDGE_DAEMON.lower())
    return bool(re.search(rf"(?<![a-z0-9_]){escaped}(?![a-z0-9_])", normalized))


def _notify_disabled_in_command(line: str) -> bool:
    normalized = str(line or "").strip().lower()
    return bool(
        re.search(
            r"(?:^|[\s;\"'])(?:\$env:)?iqfeed_notify_enabled\s*[:=]\s*['\"]?(?:0|false|no|off)['\"]?",
            normalized,
        )
    )


def _parse_process_creation_date(value: Any) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    match = re.search(r"/Date\((\d+)\)/", text)
    if match:
        return datetime.fromtimestamp(int(match.group(1)) / 1000.0, tz=timezone.utc)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def find_iqfeed_trade_bridge_daemons(processes: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for proc in processes:
        if _is_trade_bridge_command(_command_line(proc)):
            found.append(proc)
    return found


def evaluate_iqfeed_trade_bridge_runtime(
    processes: Sequence[dict[str, Any]],
    *,
    source_path: str | Path | None = None,
) -> tuple[bool, str, dict[str, Any]]:
    running = find_iqfeed_trade_bridge_daemons(processes)
    source_mtime: datetime | None = None
    source_text = ""
    if source_path is not None:
        path = Path(source_path)
        try:
            source_mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            source_text = path.read_text(encoding="utf-8")
        except OSError:
            source_mtime = None
    details: dict[str, Any] = {
        "running_daemons": [
            {
                "pid": proc.get("ProcessId") or proc.get("pid"),
                "name": proc.get("Name") or proc.get("name"),
                "command": _command_line(proc),
                "started_at_utc": (
                    _parse_process_creation_date(
                        proc.get("CreationDate")
                        or proc.get("creation_date")
                        or proc.get("started_at")
                    ).isoformat()
                    if _parse_process_creation_date(
                        proc.get("CreationDate")
                        or proc.get("creation_date")
                        or proc.get("started_at")
                    )
                    else None
                ),
            }
            for proc in running
        ],
        "source_mtime_utc": source_mtime.isoformat() if source_mtime else None,
    }
    if not running:
        return False, "iqfeed_trade_bridge_not_running", details
    disabled = [
        row for row in details["running_daemons"] if _notify_disabled_in_command(str(row.get("command") or ""))
    ]
    if disabled:
        details["disabled_daemons"] = disabled
        return False, "iqfeed_trade_bridge_notify_disabled", details
    if source_path is not None:
        required_markers = {
            "ross_universe_helper": "def _ross_universe_symbols",
            "ross_universe_builder": "build_equity_universe(EQUITY_ROSS_SMALLCAP)",
            "target_symbols_ross_fill": "_ross_universe_symbols(room)",
        }
        missing = [label for label, marker in required_markers.items() if marker not in source_text]
        if missing:
            details["missing_source_markers"] = missing
            return False, "iqfeed_trade_bridge_ross_watchlist_source_missing", details
        stale = []
        for proc in running:
            started = _parse_process_creation_date(
                proc.get("CreationDate")
                or proc.get("creation_date")
                or proc.get("started_at")
            )
            if started is not None and source_mtime is not None and source_mtime > started:
                stale.append(
                    {
                        "pid": proc.get("ProcessId") or proc.get("pid"),
                        "started_at_utc": started.isoformat(),
                    }
                )
        if stale:
            details["stale_daemons"] = stale
            return False, "iqfeed_trade_bridge_source_newer_than_process", details
    return True, "iqfeed_trade_bridge_runtime_ok", details


def evaluate_iqfeed_trade_bridge_startup_tasks(tasks: Sequence[dict[str, Any]]) -> tuple[bool, str, dict[str, Any]]:
    by_name = {str(row.get("TaskName") or row.get("task_name") or ""): row for row in tasks}
    details: dict[str, Any] = {
        "tasks": [
            {
                "task_name": row.get("TaskName") or row.get("task_name"),
                "state": row.get("State") or row.get("state"),
                "actions": row.get("Actions") or row.get("actions"),
                "triggers": row.get("Triggers") or row.get("triggers"),
                "next_run_time": row.get("NextRunTime") or row.get("next_run_time"),
            }
            for row in tasks
        ],
    }
    missing = [name for name in (TRADE_BRIDGE_DAILY_TASK, TRADE_BRIDGE_LOGON_TASK) if name not in by_name]
    if missing:
        details["missing_tasks"] = missing
        return False, "iqfeed_trade_bridge_startup_task_missing", details
    disabled = [
        name
        for name, row in by_name.items()
        if name in {TRADE_BRIDGE_DAILY_TASK, TRADE_BRIDGE_LOGON_TASK}
        and str(row.get("State") or row.get("state") or "").strip().lower() == "disabled"
    ]
    if disabled:
        details["disabled_tasks"] = disabled
        return False, "iqfeed_trade_bridge_startup_task_disabled", details
    bad_action = []
    for name in (TRADE_BRIDGE_DAILY_TASK, TRADE_BRIDGE_LOGON_TASK):
        action = str(by_name[name].get("Actions") or by_name[name].get("actions") or "").replace("\\", "/").lower()
        if "run-hidden.vbs" not in action or "start-iqfeed-trade-bridge.ps1" not in action:
            bad_action.append(name)
    if bad_action:
        details["bad_action_tasks"] = bad_action
        return False, "iqfeed_trade_bridge_startup_task_bad_action", details
    daily_trigger = str(
        by_name[TRADE_BRIDGE_DAILY_TASK].get("Triggers")
        or by_name[TRADE_BRIDGE_DAILY_TASK].get("triggers")
        or ""
    ).lower()
    if "daily" not in daily_trigger or "03:56" not in daily_trigger:
        details["daily_trigger"] = daily_trigger
        return False, "iqfeed_trade_bridge_daily_task_not_0356", details
    return True, "iqfeed_trade_bridge_startup_tasks_ok", details


def _host_processes() -> list[dict[str, Any]]:
    ps = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.CommandLine -match 'iqfeed_trade_bridge\\.py' -or $_.Name -match 'python|powershell' } | "
        "Select-Object ProcessId,Name,CreationDate,CommandLine | ConvertTo-Json -Compress"
    )
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if completed.returncode != 0 or not completed.stdout.strip():
        return []
    try:
        rows = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return []
    if isinstance(rows, dict):
        return [rows]
    if isinstance(rows, list):
        return [row for row in rows if isinstance(row, dict)]
    return []


def _host_startup_tasks() -> list[dict[str, Any]]:
    ps = r"""
$names=@('CHILI-IQFeed-Trade-Bridge-Daily','CHILI-IQFeed-Trade-Bridge-Logon')
$rows=@()
foreach($n in $names){
  $t=Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue
  if($null -ne $t){
    $info=Get-ScheduledTaskInfo -TaskName $n -ErrorAction SilentlyContinue
    $next=''
    if($null -ne $info -and $null -ne $info.NextRunTime){
      try { $next=$info.NextRunTime.ToString('o') } catch { $next='' }
    }
    $rows += [pscustomobject]@{
      TaskName=$t.TaskName
      State=$t.State.ToString()
      Actions=(($t.Actions | ForEach-Object { ($_.Execute + ' ' + $_.Arguments).Trim() }) -join ' | ')
      Triggers=(($t.Triggers | ForEach-Object { $_.CimClass.CimClassName + ':' + $_.StartBoundary }) -join ' | ')
      NextRunTime=$next
    }
  }
}
$rows | ConvertTo-Json -Compress
"""
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if completed.returncode != 0 or not completed.stdout.strip():
        return []
    try:
        rows = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return []
    if isinstance(rows, dict):
        return [rows]
    if isinstance(rows, list):
        return [row for row in rows if isinstance(row, dict)]
    return []


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify the host IQFeed trade bridge daemon is running.")
    parser.add_argument(
        "--source-path",
        default=str(ROOT / "scripts" / IQFEED_TRADE_BRIDGE_DAEMON),
        help="Bridge source file to compare against the running process.",
    )
    parser.add_argument("--skip-startup-tasks", action="store_true")
    args = parser.parse_args(argv)

    ok, reason, details = evaluate_iqfeed_trade_bridge_runtime(
        _host_processes(),
        source_path=args.source_path,
    )
    task_details: dict[str, Any] = {}
    if ok and not args.skip_startup_tasks:
        ok, reason, task_details = evaluate_iqfeed_trade_bridge_startup_tasks(_host_startup_tasks())
    print(reason)
    print(f"running_daemons={len(details['running_daemons'])}")
    print(f"source_mtime_utc={details.get('source_mtime_utc')}")
    for proc in details["running_daemons"]:
        print(f"daemon={proc.get('pid')} {proc.get('command')}")
    if task_details:
        print(f"startup_tasks={len(task_details.get('tasks') or [])}")
        for task in task_details.get("tasks") or []:
            print(f"task={task.get('task_name')} state={task.get('state')} next={task.get('next_run_time')}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
