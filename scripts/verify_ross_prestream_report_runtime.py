from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


ROSS_PRESTREAM_TASK_PREFIX = "CHILI-Ross-Prestream-Report"
DEFAULT_START_SCRIPT_PATH = Path(__file__).resolve().with_name("start-ross-prestream-report.ps1")
DEFAULT_REPORT_DIR = Path(r"D:\CHILI-Docker\chili-data\ross_stream")


def evaluate_ross_prestream_start_script(path: str | Path = DEFAULT_START_SCRIPT_PATH) -> tuple[bool, str, dict[str, Any]]:
    p = Path(path)
    details: dict[str, Any] = {"path": str(p)}
    if not p.exists():
        return False, "ross_prestream_start_script_missing", details
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        details["error"] = str(exc)[:200]
        return False, "ross_prestream_start_script_unreadable", details
    normalized = text.replace("\\", "/").lower()
    required = (
        "ross_prestream_report.py",
        "ross_prestream_report.daemon.log",
        "ross_prestream_report.daemon.err.log",
    )
    missing = [token for token in required if token not in normalized]
    if not any(pattern in normalized for pattern in ("--profile', 'prestream", '--profile", "prestream', "--profile prestream")):
        missing.extend(["--profile", "prestream"])
    details["missing_tokens"] = missing
    if missing:
        return False, "ross_prestream_start_script_bad_args", details
    return True, "ross_prestream_start_script_ok", details


def evaluate_ross_prestream_startup_tasks(tasks: Sequence[dict[str, Any]]) -> tuple[bool, str, dict[str, Any]]:
    matching = [
        row
        for row in tasks
        if str(row.get("TaskName") or row.get("task_name") or "").startswith(ROSS_PRESTREAM_TASK_PREFIX)
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
        return False, "ross_prestream_startup_task_missing", details
    enabled = [
        row
        for row in matching
        if str(row.get("State") or row.get("state") or "").strip().lower() != "disabled"
    ]
    if not enabled:
        return False, "ross_prestream_startup_task_disabled", details
    valid: list[dict[str, Any]] = []
    for row in enabled:
        action = str(row.get("Actions") or row.get("actions") or "").replace("\\", "/").lower()
        trigger = str(row.get("Triggers") or row.get("triggers") or "").lower()
        if (
            "run-hidden.vbs" in action
            and "start-ross-prestream-report.ps1" in action
            and "daily" in trigger
            and any(token in trigger for token in ("03:40", "03:41", "03:42", "03:43", "03:44", "03:45", "03:46", "03:47", "03:48", "03:49"))
        ):
            valid.append(row)
    if not valid:
        return False, "ross_prestream_startup_task_bad_action_or_time", details
    return True, "ross_prestream_startup_tasks_ok", details


def evaluate_ross_prestream_report_artifacts(
    report_dir: str | Path = DEFAULT_REPORT_DIR,
    *,
    max_age_seconds: float | None = None,
    now_utc: datetime | None = None,
) -> tuple[bool, str, dict[str, Any]]:
    root = Path(report_dir)
    json_path = root / "ross_prestream_report.json"
    text_path = root / "ross_prestream_report.txt"
    now = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    details: dict[str, Any] = {
        "json_path": str(json_path),
        "text_path": str(text_path),
    }
    for label, path in (("json", json_path), ("text", text_path)):
        if not path.exists():
            return False, f"ross_prestream_report_{label}_missing", details
        stat = path.stat()
        mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
        details[f"{label}_size"] = stat.st_size
        details[f"{label}_mtime_utc"] = mtime.isoformat()
        details[f"{label}_age_s"] = max(0.0, (now - mtime).total_seconds())
        if stat.st_size <= 0:
            return False, f"ross_prestream_report_{label}_empty", details
        if max_age_seconds is not None and details[f"{label}_age_s"] > max(1.0, float(max_age_seconds)):
            return False, f"ross_prestream_report_{label}_stale", details
    try:
        payload = json.loads(json_path.read_text(encoding="utf-8", errors="replace") or "{}")
    except json.JSONDecodeError as exc:
        details["json_error"] = str(exc)[:200]
        return False, "ross_prestream_report_json_invalid", details
    if not isinstance(payload, dict):
        return False, "ross_prestream_report_json_wrong_shape", details
    required = ("ok", "profile", "blockers", "next_actions", "tomorrow_live_command")
    missing = [key for key in required if key not in payload]
    details["missing_keys"] = missing
    if missing:
        return False, "ross_prestream_report_json_missing_keys", details
    text = text_path.read_text(encoding="utf-8", errors="replace")
    if "live_command=" not in text or "summary_command=" not in text:
        return False, "ross_prestream_report_text_missing_commands", details
    return True, "ross_prestream_report_artifacts_ok", details


def _host_startup_tasks() -> list[dict[str, Any]]:
    ps = r"""
$tasks=Get-ScheduledTask | Where-Object { $_.TaskName -like 'CHILI-Ross-Prestream-Report*' }
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
    parser = argparse.ArgumentParser(description="Verify Ross prestream report startup coverage.")
    parser.add_argument("--skip-startup-tasks", action="store_true")
    parser.add_argument("--skip-artifacts", action="store_true")
    parser.add_argument("--report-dir", default=str(DEFAULT_REPORT_DIR))
    parser.add_argument("--max-artifact-age-seconds", type=float, default=None)
    args = parser.parse_args(argv)

    ok, reason, script_details = evaluate_ross_prestream_start_script()
    task_details: dict[str, Any] = {}
    artifact_details: dict[str, Any] = {}
    if ok and not args.skip_startup_tasks:
        task_ok, task_reason, task_details = evaluate_ross_prestream_startup_tasks(_host_startup_tasks())
        if not task_ok:
            ok = False
            reason = task_reason
        else:
            reason = task_reason
    if ok and not args.skip_artifacts:
        artifact_ok, artifact_reason, artifact_details = evaluate_ross_prestream_report_artifacts(
            args.report_dir,
            max_age_seconds=args.max_artifact_age_seconds,
        )
        if not artifact_ok:
            ok = False
            reason = artifact_reason
    print(reason)
    print(f"start_script={script_details.get('path')}")
    if script_details.get("missing_tokens"):
        print(f"start_script_missing_tokens={','.join(script_details['missing_tokens'])}")
    if task_details:
        print(f"startup_tasks={len(task_details.get('tasks') or [])}")
        for task in task_details.get("tasks") or []:
            print(f"task={task.get('task_name')} state={task.get('state')} next={task.get('next_run_time')}")
    if artifact_details:
        print(f"report_json={artifact_details.get('json_path')} age_s={artifact_details.get('json_age_s')}")
        print(f"report_text={artifact_details.get('text_path')} age_s={artifact_details.get('text_age_s')}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
