import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.trading.momentum_neural.ross_transcript_bridge import warrior_session_marker_ok


ROSS_TRANSCRIPT_DAEMONS = (
    "ross_audio_transcribe.py",
    "ross_transcript_viability_bridge.py",
    "ross_transcript_admission_audit.py",
)
ROSS_AUDIO_TASK_PREFIX = "CHILI-Ross-Audio-Transcribe"
DEFAULT_AUDIO_START_SCRIPT_PATH = ROOT / "scripts" / "start-ross-audio-transcribe.ps1"
DEFAULT_AUDIO_DAEMON_SOURCE_PATH = ROOT / "scripts" / "ross_audio_transcribe.py"


def _command_line(proc: dict[str, Any]) -> str:
    return " ".join(
        str(proc.get(key) or "")
        for key in ("CommandLine", "command_line", "cmdline", "Name", "name")
    )


def _is_daemon_command(line: str) -> bool:
    normalized = str(line or "").replace("\\", "/").lower()
    for name in ROSS_TRANSCRIPT_DAEMONS:
        escaped = re.escape(name.lower())
        if re.search(rf"(?<![a-z0-9_]){escaped}(?![a-z0-9_])", normalized):
            return True
    return False


def find_ross_transcript_daemons(processes: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for proc in processes:
        if _is_daemon_command(_command_line(proc)):
            found.append(proc)
    return found


def evaluate_transcript_runtime(
    *,
    marker_ok: bool,
    marker_reason: str,
    marker_detail: dict[str, Any] | None,
    processes: Sequence[dict[str, Any]],
) -> tuple[bool, str, dict[str, Any]]:
    running = find_ross_transcript_daemons(processes)
    details: dict[str, Any] = {
        "warrior_session_reason": marker_reason,
        "warrior_session": marker_detail or {},
        "running_daemons": [
            {
                "pid": proc.get("ProcessId") or proc.get("pid"),
                "name": proc.get("Name") or proc.get("name"),
                "command": _command_line(proc),
            }
            for proc in running
        ],
    }
    if not marker_ok and running:
        return False, "ross_transcript_daemon_running_without_warrior_session", details
    if not marker_ok:
        return True, "ross_transcript_ingestion_guarded", details
    return True, "ross_transcript_runtime_ok", details


def evaluate_ross_audio_startup_tasks(tasks: Sequence[dict[str, Any]]) -> tuple[bool, str, dict[str, Any]]:
    matching = [
        row
        for row in tasks
        if str(row.get("TaskName") or row.get("task_name") or "").startswith(ROSS_AUDIO_TASK_PREFIX)
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
        return False, "ross_audio_startup_task_missing", details
    enabled = [
        row
        for row in matching
        if str(row.get("State") or row.get("state") or "").strip().lower() != "disabled"
    ]
    if not enabled:
        return False, "ross_audio_startup_task_disabled", details
    valid: list[dict[str, Any]] = []
    for row in enabled:
        action = str(row.get("Actions") or row.get("actions") or "").replace("\\", "/").lower()
        trigger = str(row.get("Triggers") or row.get("triggers") or "").lower()
        if (
            "run-hidden.vbs" in action
            and "start-ross-audio-transcribe.ps1" in action
            and "daily" in trigger
            and any(token in trigger for token in ("03:50", "03:5", "04:00"))
        ):
            valid.append(row)
    if not valid:
        return False, "ross_audio_startup_task_bad_action_or_time", details
    return True, "ross_audio_startup_tasks_ok", details


def evaluate_ross_audio_start_script(path: str | Path = DEFAULT_AUDIO_START_SCRIPT_PATH) -> tuple[bool, str, dict[str, Any]]:
    p = Path(path)
    details: dict[str, Any] = {"path": str(p)}
    if not p.exists():
        return False, "ross_audio_start_script_missing", details
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        details["error"] = str(exc)[:200]
        return False, "ross_audio_start_script_unreadable", details
    normalized = text.replace("\\", "/").lower()
    required = (
        "warrior_session_ok.json",
        "waiting_for_warrior_session_marker",
        "video_count",
        "stream_visible",
        "if ($existing) { exit 0 }",
        "ross_audio_transcribe.py",
    )
    missing = [token for token in required if token not in normalized]
    details["missing_tokens"] = missing
    if missing:
        return False, "ross_audio_start_script_missing_marker_wait", details
    return True, "ross_audio_start_script_ok", details


def evaluate_ross_audio_daemon_source(path: str | Path = DEFAULT_AUDIO_DAEMON_SOURCE_PATH) -> tuple[bool, str, dict[str, Any]]:
    p = Path(path)
    details: dict[str, Any] = {"path": str(p)}
    if not p.exists():
        return False, "ross_audio_daemon_source_missing", details
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        details["error"] = str(exc)[:200]
        return False, "ross_audio_daemon_source_unreadable", details
    required = (
        "def audio_capture_acceptance",
        "warrior marker invalid -> stop capture",
        "capture_marker_disabled",
    )
    missing = [token for token in required if token not in text]
    details["missing_tokens"] = missing
    if missing:
        return False, "ross_audio_daemon_source_missing_capture_gate", details
    return True, "ross_audio_daemon_source_ok", details


def _host_processes() -> list[dict[str, Any]]:
    ps = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.CommandLine -match 'ross_(audio_transcribe|transcript_(viability_bridge|admission_audit))\\.py' -or $_.Name -match 'python|powershell' } | "
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
$tasks=Get-ScheduledTask | Where-Object { $_.TaskName -like 'CHILI-Ross-Audio-Transcribe*' }
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
    parser = argparse.ArgumentParser(description="Verify Ross transcript ingestion is safe for current Warrior session state.")
    parser.add_argument("--marker-path", default=None)
    parser.add_argument("--max-age-seconds", type=float, default=None)
    parser.add_argument("--skip-startup-tasks", action="store_true")
    args = parser.parse_args(argv)

    marker_ok, marker_reason, marker_detail = warrior_session_marker_ok(
        args.marker_path,
        max_age_seconds=args.max_age_seconds,
    )
    ok, reason, details = evaluate_transcript_runtime(
        marker_ok=marker_ok,
        marker_reason=marker_reason,
        marker_detail=marker_detail,
        processes=_host_processes(),
    )
    script_ok, script_reason, script_details = evaluate_ross_audio_start_script()
    if ok and not script_ok:
        ok = False
        reason = script_reason
    source_ok, source_reason, source_details = evaluate_ross_audio_daemon_source()
    if ok and not source_ok:
        ok = False
        reason = source_reason
    task_details: dict[str, Any] = {}
    if ok and not args.skip_startup_tasks:
        task_ok, task_reason, task_details = evaluate_ross_audio_startup_tasks(_host_startup_tasks())
        if not task_ok:
            ok = False
            reason = task_reason
    print(reason)
    print(f"warrior_session_reason={details['warrior_session_reason']}")
    print(f"start_script={script_reason} path={script_details.get('path')}")
    if script_details.get("missing_tokens"):
        print(f"start_script_missing_tokens={','.join(script_details['missing_tokens'])}")
    print(f"daemon_source={source_reason} path={source_details.get('path')}")
    if source_details.get("missing_tokens"):
        print(f"daemon_source_missing_tokens={','.join(source_details['missing_tokens'])}")
    print(f"running_daemons={len(details['running_daemons'])}")
    for proc in details["running_daemons"]:
        print(f"daemon={proc.get('pid')} {proc.get('command')}")
    if task_details:
        print(f"startup_tasks={len(task_details.get('tasks') or [])}")
        for task in task_details.get("tasks") or []:
            print(f"task={task.get('task_name')} state={task.get('state')} next={task.get('next_run_time')}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
