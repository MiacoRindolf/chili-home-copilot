from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.trading.momentum_neural.ross_transcript_bridge import DEFAULT_TRANSCRIPT_PATH
from scripts.ross_live_monitor_snapshot import collect_monitor_snapshot
from scripts.verify_ross_prestream_report_runtime import evaluate_ross_prestream_report_artifacts


DEFAULT_REPORT_DIR = Path(r"D:\CHILI-Docker\chili-data\ross_stream")
DEFAULT_WARRIOR_BROWSER_STATE_PATH = DEFAULT_REPORT_DIR / "warrior_browser_state_latest.json"
ROSS_TRADE_MARK_COMMAND_TEMPLATE = (
    "python scripts\\mark_ross_trade_event.py SYMBOL --action buy --price PRICE --note \"Ross trade\""
)
ROSS_TRADE_MARK_WITH_TS_COMMAND_TEMPLATE = (
    "python scripts\\mark_ross_trade_event.py SYMBOL --action buy --price PRICE "
    "--ts UTC_ISO_TIMESTAMP --note \"Ross trade\""
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run_check(cmd: Sequence[str], *, runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run) -> dict[str, Any]:
    try:
        completed = runner(
            list(cmd),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=240,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "cmd": list(cmd),
            "ok": False,
            "returncode": None,
            "stdout": [],
            "stderr": [f"timeout_after_seconds:{exc.timeout}"],
        }
    return {
        "cmd": list(cmd),
        "ok": completed.returncode == 0,
        "returncode": completed.returncode,
        "stdout": completed.stdout.strip().splitlines()[:12],
        "stderr": completed.stderr.strip().splitlines()[:12],
    }


def runtime_checks(*, runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run) -> dict[str, dict[str, Any]]:
    py = sys.executable or "python"
    return {
        "momentum_worker_runtime": _run_check([py, "scripts/verify_momentum_worker_runtime.py"], runner=runner),
        "ross_live_eligible_hygiene": _run_check(
            [py, "scripts/verify_ross_live_eligible_hygiene.py", "--max-rows", "10000"],
            runner=runner,
        ),
        "ross_transcript_runtime": _run_check([py, "scripts/verify_ross_transcript_runtime.py"], runner=runner),
        "ross_live_monitor_runtime": _run_check([py, "scripts/verify_ross_live_monitor_runtime.py"], runner=runner),
        "ross_prestream_report_runtime": _run_check(
            [py, "scripts/verify_ross_prestream_report_runtime.py", "--skip-artifacts"],
            runner=runner,
        ),
        "ross_morning_task_chain_runtime": _run_check([py, "scripts/verify_ross_morning_task_chain_runtime.py"], runner=runner),
        "iqfeed_trade_bridge_runtime": _run_check(
            [py, "scripts/verify_iqfeed_trade_bridge_runtime.py"],
            runner=runner,
        ),
    }


def evaluate_warrior_browser_state_file(
    path: str | Path = DEFAULT_WARRIOR_BROWSER_STATE_PATH,
    *,
    max_age_seconds: float = 30.0,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    p = Path(path)
    details: dict[str, Any] = {"path": str(p), "ok": False}
    if not p.exists():
        details["reason"] = "warrior_browser_state_missing"
        return details
    now = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    try:
        stat = p.stat()
        mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
        details["age_s"] = max(0.0, (now - mtime).total_seconds())
        details["mtime_utc"] = mtime.isoformat()
        payload = json.loads(p.read_text(encoding="utf-8-sig") or "{}")
    except Exception as exc:
        details["reason"] = "warrior_browser_state_invalid"
        details["error"] = str(exc)[:160]
        return details
    if details["age_s"] > max(1.0, float(max_age_seconds or 30.0)):
        details["reason"] = "warrior_browser_state_stale"
        return details
    details["url"] = str(payload.get("url") or "")
    body_text = str(
        payload.get("bodyExcerpt")
        or payload.get("body_excerpt")
        or payload.get("bodyTextSample")
        or payload.get("body_text_sample")
        or payload.get("bodyText")
        or payload.get("body_text")
        or ""
    ).lower()
    details["stream_visible"] = bool(payload.get("streamVisible") or payload.get("stream_visible"))
    details["video_count"] = int(payload.get("videoCount") or payload.get("video_count") or 0)
    details["iframe_count"] = int(payload.get("iframeCount") or payload.get("iframe_count") or 0)
    if "chatroom.warriortrading.com" not in details["url"]:
        details["reason"] = "warrior_browser_state_not_chatroom"
        return details
    if bool(payload.get("disclaimerBlocking") or payload.get("disclaimer_blocking")) or all(
        token in body_text for token in ("disclaimer", "accept", "decline")
    ):
        details["reason"] = "warrior_disclaimer_blocking"
        return details
    if details["video_count"] <= 0 and not details["stream_visible"]:
        details["reason"] = "warrior_browser_state_stream_not_visible"
        return details
    details["ok"] = True
    details["reason"] = "warrior_browser_state_ok"
    return details


def _blockers(snapshot: dict[str, Any], checks: dict[str, dict[str, Any]]) -> list[str]:
    blockers: list[str] = []
    readiness = snapshot.get("readiness") if isinstance(snapshot.get("readiness"), dict) else {}
    if not snapshot.get("ok"):
        blockers.append(f"readiness:{readiness.get('reason') or 'not_ready'}")
    if readiness.get("feed_severity") == "error":
        blockers.append(f"feed:{readiness.get('feed_reason') or 'error'}")
    warrior_reason = str(readiness.get("warrior_session_reason") or "")
    if warrior_reason and warrior_reason != "warrior_session_ok":
        blockers.append(f"warrior:{warrior_reason}")
    for name, row in checks.items():
        if not row.get("ok"):
            first = (row.get("stderr") or row.get("stdout") or ["failed"])[0]
            blockers.append(f"{name}:{first}")
    return blockers


def _next_actions(blockers: Sequence[str], *, profile: str) -> list[str]:
    actions: list[str] = []
    joined = "\n".join(blockers)
    if "warrior_disclaimer_blocking" in joined:
        actions.append("Clear the Warrior disclaimer in the browser, then refresh the screencast room marker.")
    if "warrior:" in joined:
        actions.append(
            "Refresh the Warrior dashboard and keep warrior_session_ok.json fresh every <=30s only while the stream is visibly live."
        )
    if "browser_state:" in joined:
        actions.append("Refresh the Codex Warrior browser-state probe so warrior_browser_state_latest.json updates every <=30s.")
    if "feed:" in joined or "iqfeed" in joined.lower():
        actions.append("Start or repair the IQFeed trade bridge before expecting sub-second Ross entries.")
    if "momentum_worker_runtime:" in joined:
        actions.append("Fix/restart only the canonical momentum worker until verify_momentum_worker_runtime passes.")
    if "ross_live_eligible_hygiene:" in joined:
        actions.append("Clear non-Ross live_eligible equity rows before the live window.")
    if "ross_live_monitor_runtime:" in joined:
        actions.append("Register/fix the read-only Ross live monitor startup task before the live window.")
    if "ross_prestream_report_runtime:" in joined:
        actions.append("Register/fix the Ross prestream readiness report task before the live window.")
    if "ross_morning_task_chain_runtime:" in joined:
        actions.append("Fix the Ross morning task order: prestream report, audio, IQFeed bridge, then live monitor.")
    if profile == "prestream":
        actions.append("When Ross starts, confirm the live monitor JSONL is appending snapshots.")
    if not actions:
        actions.append("Prestream checks are clean; keep the live monitor running once Ross starts.")
    return actions


def build_report(
    *,
    snapshot: dict[str, Any],
    checks: dict[str, dict[str, Any]],
    profile: str,
    browser_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    blockers = _blockers(snapshot, checks)
    if browser_state is not None and not browser_state.get("ok"):
        blockers.append(f"browser_state:{browser_state.get('reason') or 'not_ready'}")
    return {
        "ok": not blockers,
        "read_only": True,
        "as_of_utc": _utc_now_iso(),
        "profile": profile,
        "blockers": blockers,
        "next_actions": _next_actions(blockers, profile=profile),
        "snapshot": snapshot,
        "checks": checks,
        "browser_state": browser_state or {},
        "tomorrow_live_command": (
            "python scripts\\ross_live_monitor_snapshot.py --profile live --watch "
            "--interval-seconds 2 --seconds 18000 "
            "--out D:\\CHILI-Docker\\chili-data\\ross_stream\\ross_live_monitor_{date}.jsonl"
        ),
        "poststream_summary_command": (
            "python scripts\\summarize_ross_live_monitor.py "
            "--path D:\\CHILI-Docker\\chili-data\\ross_stream\\ross_live_monitor_{date}.jsonl"
        ),
        "ross_trade_marker_command_template": ROSS_TRADE_MARK_COMMAND_TEMPLATE,
        "ross_trade_marker_with_timestamp_template": ROSS_TRADE_MARK_WITH_TS_COMMAND_TEMPLATE,
    }


def write_report(report: dict[str, Any], *, out_dir: str | Path = DEFAULT_REPORT_DIR) -> tuple[Path, Path]:
    root = Path(out_dir)
    root.mkdir(parents=True, exist_ok=True)
    json_path = root / "ross_prestream_report.json"
    text_path = root / "ross_prestream_report.txt"
    artifact_check = {"ok": None, "reason": "not_checked"}
    report["post_write_artifact_check"] = artifact_check
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    lines = [
        f"ok={report['ok']}",
        f"profile={report['profile']}",
        "blockers=" + (", ".join(report["blockers"]) if report["blockers"] else "none"),
        "next_actions:",
    ]
    lines.extend(f"- {action}" for action in report["next_actions"])
    lines.append(f"live_command={report['tomorrow_live_command']}")
    lines.append(f"summary_command={report['poststream_summary_command']}")
    lines.append(f"mark_trade_command={report['ross_trade_marker_command_template']}")
    lines.append(f"mark_trade_with_ts_command={report['ross_trade_marker_with_timestamp_template']}")
    text_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    artifact_ok, artifact_reason, artifact_details = evaluate_ross_prestream_report_artifacts(root)
    artifact_check = {
        "ok": artifact_ok,
        "reason": artifact_reason,
        "json_age_s": artifact_details.get("json_age_s"),
        "text_age_s": artifact_details.get("text_age_s"),
    }
    report["post_write_artifact_check"] = artifact_check
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    lines.insert(3, f"post_write_artifact_check={artifact_reason}")
    text_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, text_path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write a read-only Ross prestream readiness report.")
    parser.add_argument("--profile", choices=("quiet", "prestream", "live"), default="prestream")
    parser.add_argument("--since-minutes", type=float, default=720.0)
    parser.add_argument("--readiness-since-minutes", type=float, default=30.0)
    parser.add_argument("--transcript-path", default=DEFAULT_TRANSCRIPT_PATH)
    parser.add_argument("--out-dir", default=str(DEFAULT_REPORT_DIR))
    parser.add_argument("--skip-runtime-checks", action="store_true")
    args = parser.parse_args(argv)

    snapshot = collect_monitor_snapshot(
        symbols_arg=[],
        since_minutes=args.since_minutes,
        readiness_since_minutes=args.readiness_since_minutes,
        mode="live",
        transcript_path=args.transcript_path,
        profile=args.profile,
    )
    checks = {} if args.skip_runtime_checks else runtime_checks()
    browser_state = evaluate_warrior_browser_state_file()
    report = build_report(snapshot=snapshot, checks=checks, profile=args.profile, browser_state=browser_state)
    json_path, text_path = write_report(report, out_dir=args.out_dir)
    print(str(json_path))
    print(str(text_path))
    print(f"ok={report['ok']}")
    for blocker in report["blockers"]:
        print(f"blocker={blocker}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
