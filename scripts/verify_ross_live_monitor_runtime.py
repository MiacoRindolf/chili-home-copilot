from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from typing import Any, Sequence


ROSS_LIVE_MONITOR_TASK_PREFIX = "CHILI-Ross-Live-Monitor"
DEFAULT_START_SCRIPT_PATH = Path(__file__).resolve().with_name("start-ross-live-monitor.ps1")


def _command_line(proc: dict[str, Any]) -> str:
    return " ".join(
        str(proc.get(key) or "")
        for key in ("CommandLine", "command_line", "cmdline", "Name", "name")
    )


def find_ross_live_monitor_daemons(processes: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for proc in processes:
        line = _command_line(proc).replace("\\", "/").lower()
        if (
            re.search(r"(?<![a-z0-9_])ross_live_monitor_snapshot\.py(?![a-z0-9_])", line)
            and "--profile live" in line
            and "--watch" in line
        ):
            found.append(proc)
    return found


def evaluate_ross_live_monitor_startup_tasks(tasks: Sequence[dict[str, Any]]) -> tuple[bool, str, dict[str, Any]]:
    matching = [
        row
        for row in tasks
        if str(row.get("TaskName") or row.get("task_name") or "").startswith(ROSS_LIVE_MONITOR_TASK_PREFIX)
    ]
    details: dict[str, Any] = {
        "tasks": [
            {
                "task_name": row.get("TaskName") or row.get("task_name"),
                "state": row.get("State") or row.get("state"),
                "actions": row.get("Actions") or row.get("actions"),
                "triggers": row.get("Triggers") or row.get("triggers"),
                "next_run_time": row.get("NextRunTime") or row.get("next_run_time"),
            }
            for row in matching
        ],
    }
    if not matching:
        return False, "ross_live_monitor_startup_task_missing", details
    enabled = [
        row
        for row in matching
        if str(row.get("State") or row.get("state") or "").strip().lower() != "disabled"
    ]
    if not enabled:
        return False, "ross_live_monitor_startup_task_disabled", details
    valid: list[dict[str, Any]] = []
    for row in enabled:
        action = str(row.get("Actions") or row.get("actions") or "").replace("\\", "/").lower()
        trigger = str(row.get("Triggers") or row.get("triggers") or "").lower()
        if (
            "run-hidden.vbs" in action
            and "start-ross-live-monitor.ps1" in action
            and "daily" in trigger
            and any(token in trigger for token in ("03:55", "03:56", "03:57", "03:58", "03:59", "04:00"))
        ):
            valid.append(row)
    if not valid:
        return False, "ross_live_monitor_startup_task_bad_action_or_time", details
    return True, "ross_live_monitor_startup_tasks_ok", details


def evaluate_ross_live_monitor_start_script(path: str | Path = DEFAULT_START_SCRIPT_PATH) -> tuple[bool, str, dict[str, Any]]:
    p = Path(path)
    details: dict[str, Any] = {"path": str(p)}
    if not p.exists():
        return False, "ross_live_monitor_start_script_missing", details
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        details["error"] = str(exc)[:200]
        return False, "ross_live_monitor_start_script_unreadable", details
    normalized = text.replace("\\", "/").lower()
    required = (
        "ross_live_monitor_snapshot.py",
        "--profile",
        "live",
        "--watch",
        "--interval-seconds",
        "2",
        "--seconds",
        "18000",
        "--out",
        "ross_live_monitor_{date}.jsonl",
    )
    missing = [token for token in required if token not in normalized]
    details["missing_tokens"] = missing
    if missing:
        return False, "ross_live_monitor_start_script_bad_args", details
    return True, "ross_live_monitor_start_script_ok", details


def _host_processes() -> list[dict[str, Any]]:
    ps = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.CommandLine -match 'ross_live_monitor_snapshot\\.py' -or $_.Name -match 'python|powershell' } | "
        "Select-Object ProcessId,Name,CommandLine | ConvertTo-Json -Compress"
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
$tasks=Get-ScheduledTask | Where-Object { $_.TaskName -like 'CHILI-Ross-Live-Monitor*' }
$rows=@()
foreach($t in $tasks){
  $info=Get-ScheduledTaskInfo -TaskName $t.TaskName -TaskPath $t.TaskPath -ErrorAction SilentlyContinue
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
    parser = argparse.ArgumentParser(description="Verify read-only Ross live monitor startup/runtime coverage.")
    parser.add_argument("--skip-startup-tasks", action="store_true")
    args = parser.parse_args(argv)

    daemons = find_ross_live_monitor_daemons(_host_processes())
    ok = True
    reason = "ross_live_monitor_runtime_ok"
    task_details: dict[str, Any] = {}
    script_ok, script_reason, script_details = evaluate_ross_live_monitor_start_script()
    if not script_ok:
        ok = False
        reason = script_reason
    if not args.skip_startup_tasks:
        task_ok, task_reason, task_details = evaluate_ross_live_monitor_startup_tasks(_host_startup_tasks())
        if ok and not task_ok:
            ok = False
            reason = task_reason
        elif ok:
            reason = task_reason
    print(reason)
    print(f"start_script={script_reason} path={script_details.get('path')}")
    if script_details.get("missing_tokens"):
        print(f"start_script_missing_tokens={','.join(script_details['missing_tokens'])}")
    print(f"running_daemons={len(daemons)}")
    for proc in daemons:
        print(f"daemon={proc.get('ProcessId') or proc.get('pid')} {_command_line(proc)}")
    if task_details:
        print(f"startup_tasks={len(task_details.get('tasks') or [])}")
        for task in task_details.get("tasks") or []:
            print(f"task={task.get('task_name')} state={task.get('state')} next={task.get('next_run_time')}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
