from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.autopilot_frontier_source_evidence_recorder import (  # noqa: E402
    DEFAULT_SOURCE_ROOT,
    FrontierSourceEvidenceRecorderError,
    record_frontier_source_evidence,
)


DEFAULT_PROMPT_PACK_ROOT = (
    REPO_ROOT / "project_ws" / "AgentOps" / "frontier_model_prompt_packs"
)
DEFAULT_WORK_DIR = (
    REPO_ROOT / "project_ws" / "AgentOps" / "frontier_model_evidence_intake" / "source_runs"
)
DEFAULT_OUTPUT = REPO_ROOT / "project_ws" / "AgentOps" / "FRONTIER_SOURCE_RUN.md"
FRONTIER_SOURCE_RUNNER_SCHEMA_VERSION = "chili.frontier-source-runner.v1"
SUPPORTED_SOURCE_KINDS = ("codex", "claude", "local_model")
SUPPORTED_SOURCE_AUTH_MODES = ("auto", "account", "subscription", "api_key", "local")
DEFAULT_MODEL_NAMES = {
    "codex": "gpt-5.6-sol",
    "claude": "claude-fable-5",
    "local_model": "qwen2.5-coder:7b",
}
LOCAL_MODEL_NUM_CTX = 32768
LOCAL_MODEL_NUM_PREDICT = 8192


CommandRunner = Callable[
    [Sequence[str], int, str | None],
    subprocess.CompletedProcess[str],
]


class FrontierSourceRunnerError(ValueError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_name(value: object, *, fallback: str) -> str:
    import re

    raw = str(value or fallback).strip().lower()
    safe = re.sub(r"[^a-z0-9._-]+", "-", raw).strip(".-")
    return safe or fallback


def _command_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def _subprocess_args(args: Sequence[str]) -> list[str]:
    command = [str(part) for part in args]
    if not command:
        return command
    if sys.platform == "win32":
        resolved = shutil.which(command[0]) or command[0]
        suffix = Path(resolved).suffix.lower()
        if suffix in {".bat", ".cmd"}:
            return ["cmd.exe", "/d", "/c", resolved, *command[1:]]
        command[0] = resolved
    return command


def _run_command(
    args: Sequence[str],
    timeout_seconds: int,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    command = _subprocess_args(args)
    try:
        return subprocess.run(
            command,
            input=input_text,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = (
            exc.stdout.decode("utf-8", errors="replace")
            if isinstance(exc.stdout, bytes)
            else (exc.stdout or "")
        )
        stderr = (
            exc.stderr.decode("utf-8", errors="replace")
            if isinstance(exc.stderr, bytes)
            else (exc.stderr or "")
        )
        return subprocess.CompletedProcess(
            command,
            124,
            stdout=stdout,
            stderr=(stderr + f"\nTimeoutExpired: command exceeded {timeout_seconds}s").strip(),
        )
    except OSError as exc:
        return subprocess.CompletedProcess(
            command,
            127,
            stdout="",
            stderr=f"{exc.__class__.__name__}: {exc}",
        )


def _ollama_base_url(environment: Mapping[str, str] | None = None) -> str:
    env = os.environ if environment is None else environment
    raw = str(env.get("OLLAMA_HOST") or "http://127.0.0.1:11434").strip().rstrip("/")
    if not raw.startswith(("http://", "https://")):
        raw = "http://" + raw
    return raw


def _run_local_model_http(
    model_name: str,
    prompt_text: str,
    timeout_seconds: int,
    *,
    environment: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    endpoint = _ollama_base_url(environment) + "/api/generate"
    payload = json.dumps(
        {
            "model": model_name,
            "prompt": prompt_text,
            "stream": False,
            "keep_alive": "10m",
            "options": {
                "temperature": 0.05,
                "num_ctx": LOCAL_MODEL_NUM_CTX,
                "num_predict": LOCAL_MODEL_NUM_PREDICT,
            },
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    command = ("ollama-http", endpoint, model_name)
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            response_payload = json.loads(response.read().decode("utf-8", errors="replace"))
        text_response = str(response_payload.get("response") or "")
        if not text_response.strip():
            return subprocess.CompletedProcess(
                command,
                1,
                stdout="",
                stderr="Ollama returned an empty non-streaming response",
            )
        return subprocess.CompletedProcess(command, 0, stdout=text_response, stderr="")
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        return subprocess.CompletedProcess(
            command,
            1,
            stdout="",
            stderr=f"{exc.__class__.__name__}: {exc}",
        )


def _prompt_pack_path(source_kind: str) -> Path:
    return DEFAULT_PROMPT_PACK_ROOT / source_kind / "prompt_pack.md"


def _response_staging_file(source_root: Path, source_kind: str) -> Path:
    return source_root.parent / "collection_packets" / f"{source_kind}_all_cases_response.txt"


def _source_model_name(source_kind: str, model_name: str | None) -> str:
    if source_kind not in SUPPORTED_SOURCE_KINDS:
        raise FrontierSourceRunnerError(
            "source_kind must be one of " + ", ".join(SUPPORTED_SOURCE_KINDS)
        )
    return model_name or DEFAULT_MODEL_NAMES[source_kind]


def _env_has_anthropic_api_key(environment: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environment is None else environment
    return bool(str(env.get("ANTHROPIC_API_KEY", "")).strip())


def _resolve_source_auth_mode(
    source_kind: str,
    requested_auth_mode: str,
    environment: Mapping[str, str] | None = None,
) -> str:
    clean_auth_mode = str(requested_auth_mode or "auto").strip()
    if clean_auth_mode not in SUPPORTED_SOURCE_AUTH_MODES:
        raise FrontierSourceRunnerError(
            "source_auth_mode must be one of " + ", ".join(SUPPORTED_SOURCE_AUTH_MODES)
        )
    if source_kind == "codex":
        if clean_auth_mode not in {"auto", "account"}:
            raise FrontierSourceRunnerError(
                "Codex source_auth_mode must be auto or account"
            )
        return "account"
    if source_kind == "local_model":
        if clean_auth_mode not in {"auto", "local"}:
            raise FrontierSourceRunnerError(
                "Local-model source_auth_mode must be auto or local"
            )
        return "local"
    if source_kind != "claude":
        return clean_auth_mode
    if clean_auth_mode == "auto":
        return "api_key" if _env_has_anthropic_api_key(environment) else "subscription"
    return clean_auth_mode


def _claude_command(
    model_name: str,
    max_budget_usd: float,
    *,
    source_auth_mode: str,
) -> tuple[str, ...]:
    command = [
        "claude",
    ]
    if source_auth_mode == "api_key":
        command.append("--bare")
    command.extend(
        [
        "--print",
        "--model",
        model_name,
        "--output-format",
        "text",
        "--permission-mode",
        "dontAsk",
        "--no-session-persistence",
        "--effort",
        "max",
        "--max-budget-usd",
        str(max_budget_usd),
        ]
    )
    return tuple(command)


def _codex_command(model_name: str) -> tuple[str, ...]:
    return (
        "codex",
        "exec",
        "--ignore-user-config",
        "--ignore-rules",
        "--model",
        model_name,
        "--ephemeral",
        "--sandbox",
        "read-only",
        "--skip-git-repo-check",
        "-c",
        'model_reasoning_effort="xhigh"',
        "-",
    )


def codex_source_command(model_name: str = DEFAULT_MODEL_NAMES["codex"]) -> tuple[str, ...]:
    """Return the canonical read-only command used for exact Codex source evidence."""
    return _codex_command(model_name)


def _local_model_command(model_name: str) -> tuple[str, ...]:
    return ("ollama", "run", model_name)


def _command_text(args: Sequence[str]) -> str:
    return " ".join(str(part) for part in args)


def _frontier_source_command(
    source_kind: str,
    model_name: str,
    max_budget_usd: float,
    *,
    source_auth_mode: str,
) -> tuple[str, ...]:
    if source_kind == "codex":
        return _codex_command(model_name)
    if source_kind == "local_model":
        return _local_model_command(model_name)
    if source_kind == "claude":
        return _claude_command(
            model_name,
            max_budget_usd,
            source_auth_mode=source_auth_mode,
        )
    raise FrontierSourceRunnerError(f"unsupported source_kind: {source_kind}")


def _auth_recovery_action(source_kind: str, output: str) -> str:
    lowered = output.lower()
    if source_kind == "codex" and (
        "auth" in lowered
        or "login" in lowered
        or "requires a newer version" in lowered
        or "unknown model" in lowered
    ):
        return (
            "Run `codex login`, update the Codex CLI to the latest release, and rerun the "
            "exact gpt-5.6-sol source probe before collecting evidence."
        )
    if source_kind == "local_model":
        return (
            "Start or repair Ollama, verify the requested local model is installed, and rerun "
            "the local-model source collection."
        )
    if source_kind == "claude" and (
        "401" in lowered or "auth" in lowered or "invalid authentication" in lowered
    ):
        return (
            "Run `claude setup-token` in a trusted interactive terminal, then rerun "
            "source availability diagnostics with --source-kind claude --probe-live. "
            "If print-mode auth still fails, run `claude auth logout` then "
            "`claude auth login --claudeai`, or provide a valid `ANTHROPIC_API_KEY`."
        )
    if source_kind == "claude" and (
        "selected model" in lowered
        or "model not found" in lowered
        or "may not exist" in lowered
        or "404" in lowered
    ):
        return (
            "Update Claude Code to the latest release and verify that `claude-fable-5` is "
            "available to the authenticated account, then rerun the exact Fable 5 probe."
        )
    return "Inspect runner stdout/stderr, repair the source command, then rerun this source runner."


def _api_key_missing_action() -> str:
    return (
        "Provide a valid `ANTHROPIC_API_KEY`, or rerun with "
        "`--source-auth-mode subscription` after repairing Claude subscription auth with "
        "`claude setup-token`."
    )


def _failure_summary(
    *,
    source_kind: str,
    model_name: str,
    run_id: str,
    run_dir: Path,
    response_path: Path,
    source_command: str,
    source_auth_mode: str,
    failure_stage: str,
    failure_reason: str,
    write: bool,
    next_action: str,
    measured_run_duration_seconds: float | None = None,
) -> dict[str, object]:
    return {
        "schema": FRONTIER_SOURCE_RUNNER_SCHEMA_VERSION,
        "generated_utc": _utc_now(),
        "status": "failed",
        "write": bool(write),
        "source_kind": source_kind,
        "model_name": model_name,
        "run_id": run_id,
        "run_dir": str(run_dir),
        "response": str(response_path),
        "source_command": source_command,
        "source_auth_mode": source_auth_mode,
        "failure_stage": failure_stage,
        "failure_reason": failure_reason,
        "recorder": {},
        "promotion_ready": False,
        "next_action": next_action,
        "measured_run_duration_seconds": measured_run_duration_seconds,
        "permission_boundary": (
            "frontier source response collection only; no source/test edits, git/PR action, "
            "runtime restart, deployment, database migration, broker call, or live trading"
        ),
    }


def run_frontier_source(
    *,
    source_kind: str = "claude",
    source_root: Path = DEFAULT_SOURCE_ROOT,
    work_dir: Path = DEFAULT_WORK_DIR,
    prompt_pack: Path | None = None,
    response_output: Path | None = None,
    response_file: Path | None = None,
    model_name: str | None = None,
    run_id: str | None = None,
    source_command: str | None = None,
    source_auth_mode: str = "auto",
    max_budget_usd: float = 1.0,
    timeout_seconds: int = 900,
    runner: CommandRunner = _run_command,
    environment: Mapping[str, str] | None = None,
    clock: Callable[[], float] = time.perf_counter,
    write: bool = True,
    overwrite: bool = False,
) -> dict[str, object]:
    clean_source_kind = str(source_kind).strip()
    resolved_model_name = _source_model_name(clean_source_kind, model_name)
    resolved_auth_mode = _resolve_source_auth_mode(
        clean_source_kind,
        source_auth_mode,
        environment,
    )
    clean_run_id = run_id or (
        f"{clean_source_kind}-source-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    )
    prompt_pack = prompt_pack or _prompt_pack_path(clean_source_kind)
    if not prompt_pack.is_file():
        raise FrontierSourceRunnerError(f"prompt pack does not exist: {prompt_pack}")
    source_root = source_root.resolve()
    response_output = response_output or _response_staging_file(source_root, clean_source_kind)
    prompt_text = prompt_pack.read_text(encoding="utf-8", errors="replace")

    if write:
        run_dir = work_dir / _safe_name(clean_run_id, fallback="frontier-source-run")
        if run_dir.exists() and not overwrite:
            raise FrontierSourceRunnerError(
                f"run directory already exists: {run_dir}; rerun with --overwrite after reviewing it"
            )
        if overwrite and run_dir.exists():
            shutil.rmtree(run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        cleanup = None
    else:
        cleanup = tempfile.TemporaryDirectory(prefix="chili_frontier_source_run_")
        run_dir = Path(cleanup.name)
    try:
        run_prompt = run_dir / "prompt_pack.md"
        run_response = run_dir / "model_response.txt"
        run_prompt.write_text(prompt_text, encoding="utf-8")
        if response_file is not None:
            if not response_file.is_file():
                raise FrontierSourceRunnerError(f"response file does not exist: {response_file}")
            response_text = response_file.read_text(encoding="utf-8", errors="replace")
            command = source_command or f"response imported from {response_file}"
            command_auth_mode = "imported_response"
            exit_code = 0
            stderr = ""
            measured_run_duration_seconds = None
        elif (
            clean_source_kind == "claude"
            and resolved_auth_mode == "api_key"
            and not _env_has_anthropic_api_key(environment)
        ):
            return _failure_summary(
                source_kind=clean_source_kind,
                model_name=resolved_model_name,
                run_id=clean_run_id,
                run_dir=run_dir if write else work_dir / _safe_name(clean_run_id, fallback="frontier-source-run"),
                response_path=run_response if write else response_output,
                source_command=source_command or "claude --bare --print",
                source_auth_mode=resolved_auth_mode,
                failure_stage="auth_preflight",
                failure_reason="ANTHROPIC_API_KEY is required for source_auth_mode=api_key",
                write=write,
                next_action=_api_key_missing_action(),
            )
        else:
            command_args = _frontier_source_command(
                clean_source_kind,
                resolved_model_name,
                max_budget_usd,
                source_auth_mode=resolved_auth_mode,
            )
            command = source_command or (
                f"ollama api generate model={resolved_model_name} "
                f"num_ctx={LOCAL_MODEL_NUM_CTX} num_predict={LOCAL_MODEL_NUM_PREDICT}"
                if clean_source_kind == "local_model" and runner is _run_command
                else _command_text(command_args)
            )
            command_auth_mode = resolved_auth_mode
            started = clock()
            result = (
                _run_local_model_http(
                    resolved_model_name,
                    prompt_text,
                    timeout_seconds,
                    environment=environment,
                )
                if clean_source_kind == "local_model" and runner is _run_command
                else runner(command_args, timeout_seconds, prompt_text)
            )
            measured_run_duration_seconds = max(0.0, float(clock() - started))
            response_text = result.stdout or ""
            stderr = result.stderr or ""
            exit_code = int(result.returncode)
        run_response.write_text(response_text, encoding="utf-8")
        if exit_code != 0:
            return _failure_summary(
                source_kind=clean_source_kind,
                model_name=resolved_model_name,
                run_id=clean_run_id,
                run_dir=run_dir if write else work_dir / _safe_name(clean_run_id, fallback="frontier-source-run"),
                response_path=run_response if write else response_output,
                source_command=command,
                source_auth_mode=command_auth_mode,
                failure_stage="model",
                failure_reason=(response_text or stderr or f"source command exited {exit_code}").strip(),
                write=write,
                next_action=_auth_recovery_action(clean_source_kind, f"{response_text}\n{stderr}"),
                measured_run_duration_seconds=measured_run_duration_seconds,
            )
        if not response_text.strip():
            return _failure_summary(
                source_kind=clean_source_kind,
                model_name=resolved_model_name,
                run_id=clean_run_id,
                run_dir=run_dir if write else work_dir / _safe_name(clean_run_id, fallback="frontier-source-run"),
                response_path=run_response if write else response_output,
                source_command=command,
                source_auth_mode=command_auth_mode,
                failure_stage="model",
                failure_reason="source command produced an empty response",
                write=write,
                next_action="Rerun the source command and verify it returns one JSON object per case.",
                measured_run_duration_seconds=measured_run_duration_seconds,
            )
        recorder_response = run_response
        if write:
            response_output.parent.mkdir(parents=True, exist_ok=True)
            response_output.write_text(response_text, encoding="utf-8")
            recorder_response = response_output
        try:
            recorder = record_frontier_source_evidence(
                source_kind=clean_source_kind,
                source_root=source_root,
                prompt_pack_path=run_prompt,
                response_path=recorder_response,
                model_name=resolved_model_name,
                all_cases=True,
                run_id=clean_run_id,
                source_command=command,
                measured_run_duration_seconds=measured_run_duration_seconds,
                write=write,
                overwrite=overwrite,
            )
        except FrontierSourceEvidenceRecorderError as exc:
            return _failure_summary(
                source_kind=clean_source_kind,
                model_name=resolved_model_name,
                run_id=clean_run_id,
                run_dir=run_dir if write else work_dir / _safe_name(clean_run_id, fallback="frontier-source-run"),
                response_path=recorder_response if write else response_output,
                source_command=command,
                source_auth_mode=command_auth_mode,
                failure_stage="record",
                failure_reason=str(exc),
                write=write,
                next_action=(
                    "Inspect the staged response, repair the all-cases JSON output, then rerun the "
                    "frontier source evidence recorder dry run before writing evidence."
                ),
                measured_run_duration_seconds=measured_run_duration_seconds,
            )
        return {
            "schema": FRONTIER_SOURCE_RUNNER_SCHEMA_VERSION,
            "generated_utc": _utc_now(),
            "status": "passed",
            "write": bool(write),
            "source_kind": clean_source_kind,
            "model_name": resolved_model_name,
            "run_id": clean_run_id,
            "run_dir": str(run_dir if write else work_dir / _safe_name(clean_run_id, fallback="frontier-source-run")),
            "prompt_pack": str(run_prompt if write else prompt_pack),
            "response": str(recorder_response if write else response_output),
            "source_command": command,
            "source_auth_mode": command_auth_mode,
            "measured_run_duration_seconds": measured_run_duration_seconds,
            "duration_attribution": recorder.get("duration_attribution"),
            "cases": recorder.get("cases"),
            "recorder": recorder,
            "promotion_ready": False,
            "next_action": recorder.get("next_action"),
            "permission_boundary": (
                "frontier source response collection only; no source/test edits, git/PR action, "
                "runtime restart, deployment, database migration, broker call, or live trading"
            ),
        }
    finally:
        if cleanup is not None:
            cleanup.cleanup()


def render_summary(summary: Mapping[str, object]) -> str:
    lines = [
        "# CHILI Frontier Source Runner",
        "",
        f"- Schema: {summary.get('schema')}",
        f"- Generated UTC: {summary.get('generated_utc')}",
        f"- Status: {summary.get('status')}",
        f"- Write mode: {summary.get('write')}",
        f"- Source kind: {summary.get('source_kind')}",
        f"- Model: {summary.get('model_name')}",
        f"- Source auth mode: {summary.get('source_auth_mode') or 'none'}",
        f"- Run id: {summary.get('run_id')}",
        f"- Cases: {summary.get('cases') or 0}",
        f"- Measured run duration seconds: {summary.get('measured_run_duration_seconds') if summary.get('measured_run_duration_seconds') is not None else 'unmeasured'}",
        f"- Duration attribution: {summary.get('duration_attribution') or 'none'}",
        f"- Promotion ready: {summary.get('promotion_ready')}",
        f"- Failure stage: {summary.get('failure_stage') or 'none'}",
        f"- Failure reason: {summary.get('failure_reason') or 'none'}",
        f"- Next action: {summary.get('next_action')}",
        f"- Permission boundary: {summary.get('permission_boundary')}",
        "",
        "| Artifact | Path |",
        "| --- | --- |",
        f"| run_dir | {summary.get('run_dir') or ''} |",
        f"| response | {summary.get('response') or ''} |",
    ]
    recorder = summary.get("recorder")
    if isinstance(recorder, Mapping):
        for label, key in (
            ("metadata", "metadata"),
            ("transcript", "transcript"),
            ("raw_dir", "raw_dir"),
        ):
            if recorder.get(key):
                lines.append(f"| {label} | {recorder.get(key)} |")
    lines.append("")
    return "\n".join(lines)


def write_summary(markdown: str, output_path: Path = DEFAULT_OUTPUT) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(markdown, encoding="utf-8")
    return output_path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run a real frontier model source and record its all-cases evidence response."
    )
    parser.add_argument("--source-kind", default="claude", choices=SUPPORTED_SOURCE_KINDS)
    parser.add_argument("--source-root", type=Path, default=DEFAULT_SOURCE_ROOT)
    parser.add_argument("--work-dir", type=Path, default=DEFAULT_WORK_DIR)
    parser.add_argument("--prompt-pack", type=Path)
    parser.add_argument("--response-output", type=Path)
    parser.add_argument("--response-file", type=Path)
    parser.add_argument("--model-name")
    parser.add_argument("--run-id")
    parser.add_argument("--source-command")
    parser.add_argument(
        "--source-auth-mode",
        choices=SUPPORTED_SOURCE_AUTH_MODES,
        default="auto",
        help=(
            "Live source auth lane: Codex auto uses the signed-in account; Claude auto "
            "uses ANTHROPIC_API_KEY with --bare when present, otherwise subscription "
            "OAuth/keychain print mode."
        ),
    )
    parser.add_argument("--max-budget-usd", type=float, default=1.0)
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-write", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)

    try:
        summary = run_frontier_source(
            source_kind=args.source_kind,
            source_root=args.source_root,
            work_dir=args.work_dir,
            prompt_pack=args.prompt_pack,
            response_output=args.response_output,
            response_file=args.response_file,
            model_name=args.model_name,
            run_id=args.run_id,
            source_command=args.source_command,
            source_auth_mode=args.source_auth_mode,
            max_budget_usd=args.max_budget_usd,
            timeout_seconds=args.timeout_seconds,
            write=not args.no_write,
            overwrite=args.overwrite,
        )
    except FrontierSourceRunnerError as exc:
        summary = {
            "schema": FRONTIER_SOURCE_RUNNER_SCHEMA_VERSION,
            "generated_utc": _utc_now(),
            "status": "failed",
            "error": str(exc),
        }
    markdown = render_summary(summary)
    if not args.no_write:
        write_summary(markdown, args.output)
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(markdown)
        if not args.no_write:
            print(f"Wrote {args.output}")
    return 0 if summary.get("status") == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
