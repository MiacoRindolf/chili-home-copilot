from __future__ import annotations

import argparse
import json
import re
import subprocess
from typing import Any, Sequence


EXPECTED_CHAIN = (
    {
        "label": "prestream_report",
        "name": "CHILI-Ross-Prestream-Report-345",
        "script": "start-ross-prestream-report.ps1",
        "minute": 3 * 60 + 45,
    },
    {
        "label": "audio_transcript",
        "name": "CHILI-Ross-Audio-Transcribe-350",
        "script": "start-ross-audio-transcribe.ps1",
        "minute": 3 * 60 + 50,
    },
    {
        "label": "iqfeed_trade_bridge",
        "name": "CHILI-IQFeed-Trade-Bridge-Daily",
        "script": "start-iqfeed-trade-bridge.ps1",
        "minute": 3 * 60 + 56,
    },
    {
        "label": "live_monitor",
        "name": "CHILI-Ross-Live-Monitor-358",
        "script": "start-ross-live-monitor.ps1",
        "minute": 3 * 60 + 58,
    },
)


def _minute_from_trigger(value: Any) -> int | None:
    text = str(value or "")
    match = re.search(r"T(\d{2}):(\d{2})(?::\d{2})?", text)
    if not match:
        match = re.search(r"\b(\d{2}):(\d{2})\b", text)
    if not match:
        return None
    return int(match.group(1)) * 60 + int(match.group(2))


def evaluate_ross_morning_task_chain(tasks: Sequence[dict[str, Any]]) -> tuple[bool, str, dict[str, Any]]:
    by_name = {str(row.get("TaskName") or row.get("task_name") or ""): row for row in tasks}
    details: dict[str, Any] = {
        "tasks": [],
        "expected_order": [row["label"] for row in EXPECTED_CHAIN],
    }
    seen_minutes: list[int] = []
    for expected in EXPECTED_CHAIN:
        name = str(expected["name"])
        row = by_name.get(name)
        if row is None:
            details.setdefault("missing_tasks", []).append(name)
            continue
        action = str(row.get("Actions") or row.get("actions") or "").replace("\\", "/").lower()
        trigger = str(row.get("Triggers") or row.get("triggers") or "")
        minute = _minute_from_trigger(trigger)
        task_detail = {
            "label": expected["label"],
            "task_name": name,
            "state": row.get("State") or row.get("state"),
            "minute": minute,
            "expected_minute": expected["minute"],
            "next_run_time": row.get("NextRunTime") or row.get("next_run_time"),
        }
        details["tasks"].append(task_detail)
        if str(task_detail["state"] or "").strip().lower() == "disabled":
            details.setdefault("disabled_tasks", []).append(name)
        if "run-hidden.vbs" not in action or str(expected["script"]).lower() not in action:
            details.setdefault("bad_action_tasks", []).append(name)
        if minute != expected["minute"]:
            details.setdefault("bad_time_tasks", []).append(task_detail)
        if minute is not None:
            seen_minutes.append(minute)
    if details.get("missing_tasks"):
        return False, "ross_morning_task_chain_missing_task", details
    if details.get("disabled_tasks"):
        return False, "ross_morning_task_chain_disabled_task", details
    if details.get("bad_action_tasks"):
        return False, "ross_morning_task_chain_bad_action", details
    if details.get("bad_time_tasks"):
        return False, "ross_morning_task_chain_bad_time", details
    if seen_minutes != sorted(seen_minutes) or len(seen_minutes) != len(EXPECTED_CHAIN):
        details["seen_minutes"] = seen_minutes
        return False, "ross_morning_task_chain_order_invalid", details
    return True, "ross_morning_task_chain_ok", details


def _host_tasks() -> list[dict[str, Any]]:
    names = ",".join("'" + str(row["name"]) + "'" for row in EXPECTED_CHAIN)
    ps = f"""
$names=@({names})
$rows=@()
foreach($n in $names){{
  $t=Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue
  if($null -ne $t){{
    $info=Get-ScheduledTaskInfo -TaskName $n -ErrorAction SilentlyContinue
    $next=''
    if($null -ne $info -and $null -ne $info.NextRunTime){{
      try {{ $next=$info.NextRunTime.ToString('o') }} catch {{ $next='' }}
    }}
    $rows += [pscustomobject]@{{
      TaskName=$t.TaskName
      State=$t.State.ToString()
      Actions=(($t.Actions | ForEach-Object {{ ($_.Execute + ' ' + $_.Arguments).Trim() }}) -join ' | ')
      Triggers=(($t.Triggers | ForEach-Object {{ $_.CimClass.CimClassName + ':' + $_.StartBoundary }}) -join ' | ')
      NextRunTime=$next
    }}
  }}
}}
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
    parser = argparse.ArgumentParser(description="Verify Ross morning scheduled task chain order.")
    parser.parse_args(argv)
    ok, reason, details = evaluate_ross_morning_task_chain(_host_tasks())
    print(reason)
    for task in details.get("tasks") or []:
        print(
            f"task={task.get('task_name')} state={task.get('state')} "
            f"minute={task.get('minute')} next={task.get('next_run_time')}"
        )
    if details.get("missing_tasks"):
        print(f"missing_tasks={','.join(details['missing_tasks'])}")
    if details.get("bad_time_tasks"):
        print(f"bad_time_tasks={json.dumps(details['bad_time_tasks'], sort_keys=True)}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
