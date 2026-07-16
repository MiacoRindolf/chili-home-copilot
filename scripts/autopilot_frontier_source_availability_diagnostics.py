from __future__ import annotations

import argparse
import dataclasses
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.autopilot_frontier_source_runner import (  # noqa: E402
    DEFAULT_MODEL_NAMES as SOURCE_RUNNER_MODEL_NAMES,
    codex_source_command,
)


DEFAULT_RAW_SOURCE_ROOT = (
    REPO_ROOT
    / "project_ws"
    / "AgentOps"
    / "frontier_model_evidence_intake"
    / "raw_sources"
)
DEFAULT_OUTPUT = REPO_ROOT / "project_ws" / "AgentOps" / "FRONTIER_SOURCE_AVAILABILITY_DIAGNOSTICS.md"
FRONTIER_SOURCE_AVAILABILITY_SCHEMA_VERSION = "chili.frontier-source-availability-diagnostics.v1"
SOURCE_KINDS = ("codex", "claude", "local_model")
REQUIRED_SOURCE_FILES = ("metadata.json", "prompt_pack.md", "transcript.jsonl")
CLAUDE_MODEL = SOURCE_RUNNER_MODEL_NAMES["claude"]
CODEX_MODEL = SOURCE_RUNNER_MODEL_NAMES["codex"]
FRONTIER_COLLECTION_COMMAND = (
    "python scripts/autopilot_frontier_source_collection_packet.py --source-kind all --json"
)
FRONTIER_INTAKE_COMMAND = (
    "python scripts/autopilot_frontier_model_evidence_intake.py "
    "--input-root project_ws/AgentOps/frontier_model_evidence_intake/raw_sources "
    "--publish-scorecards --json"
)


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


@dataclasses.dataclass(frozen=True)
class SourceAvailability:
    source_kind: str
    source_status: str
    raw_drop_count: int
    missing_files: tuple[str, ...]
    probe_status: str
    blocker: str
    next_action: str
    probe_command: str = ""
    probe_exit_code: int | None = None
    stdout_preview: str = ""
    stderr_preview: str = ""
    credential_status: str = ""
    credential_detail: str = ""
    source_auth_mode: str = ""
    api_key_probe_status: str = ""
    source_runner_command: str = ""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _escape_cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").strip()


def _preview(value: str, limit: int = 260) -> str:
    clean = " ".join(str(value or "").split())
    if len(clean) <= limit:
        return clean
    return clean[: limit - 3].rstrip() + "..."


def _command_text(args: Sequence[str]) -> str:
    return " ".join(str(part) for part in args)


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
        stdout = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
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


def _source_bundle_state(raw_source_root: Path, source_kind: str) -> tuple[str, int, tuple[str, ...]]:
    source_dir = raw_source_root / source_kind
    missing: list[str] = []
    for filename in REQUIRED_SOURCE_FILES:
        path = source_dir / filename
        if not path.is_file():
            missing.append(str(path))
    raw_dir = source_dir / "raw"
    raw_drop_count = 0
    if raw_dir.is_dir():
        raw_drop_count = sum(1 for path in raw_dir.glob("*.json") if path.is_file())
    if raw_drop_count <= 0:
        missing.append(str(raw_dir / "*.json"))
    if not source_dir.exists():
        return "missing", raw_drop_count, tuple(missing)
    if missing:
        return "partial", raw_drop_count, tuple(missing)
    return "ready", raw_drop_count, tuple()


def _collection_next_action(source_kind: str) -> str:
    return (
        f"Collect {source_kind} evidence with {FRONTIER_COLLECTION_COMMAND}; "
        f"then publish source intake with {FRONTIER_INTAKE_COMMAND}."
    )


def _probe_static_source(
    *,
    source_kind: str,
    source_status: str,
    raw_drop_count: int,
    missing_files: tuple[str, ...],
) -> SourceAvailability:
    if source_status == "ready":
        return SourceAvailability(
            source_kind=source_kind,
            source_status=source_status,
            raw_drop_count=raw_drop_count,
            missing_files=missing_files,
            probe_status="source_bundle_ready",
            blocker="none",
            next_action="none",
        )
    return SourceAvailability(
        source_kind=source_kind,
        source_status=source_status,
        raw_drop_count=raw_drop_count,
        missing_files=missing_files,
        probe_status="source_bundle_missing",
        blocker="missing_source_bundle",
        next_action=_collection_next_action(source_kind),
    )


def _claude_live_probe_command(max_budget_usd: float) -> tuple[str, ...]:
    return (
        "claude",
        "--print",
        "--model",
        CLAUDE_MODEL,
        "--output-format",
        "text",
        "--permission-mode",
        "dontAsk",
        "--no-session-persistence",
        "--effort",
        "max",
        "--max-budget-usd",
        str(max_budget_usd),
    )


def _codex_runner_command() -> str:
    return (
        "python scripts/autopilot_frontier_source_runner.py "
        "--source-kind codex --source-auth-mode account --json"
    )


def _classify_codex_failure(combined_output: str) -> tuple[str, str, str]:
    lowered = combined_output.lower()
    if (
        "usage limit" in lowered
        or "purchase more credits" in lowered
        or "rate limit" in lowered
    ):
        return (
            "quota_exhausted",
            "codex_quota_exhausted",
            "Wait for the reported Codex usage window to reset or add credits, then rerun the exact gpt-5.6-sol live probe.",
        )
    if "requires a newer version" in lowered or "unknown model" in lowered:
        return (
            "cli_outdated",
            "codex_cli_outdated",
            "Update the Codex CLI to the latest release, then rerun the exact gpt-5.6-sol live probe.",
        )
    if "auth" in lowered or "login" in lowered or "401" in lowered:
        return (
            "auth_failed",
            "codex_auth_failed",
            "Run `codex login`, then rerun the exact gpt-5.6-sol live probe.",
        )
    return (
        "live_probe_failed",
        "codex_live_probe_failed",
        "Inspect the Codex probe output, repair the CLI invocation, and rerun the live probe.",
    )


def _probe_codex(
    *,
    source_status: str,
    raw_drop_count: int,
    missing_files: tuple[str, ...],
    runner: CommandRunner,
    timeout_seconds: int,
    probe_live: bool,
) -> SourceAvailability:
    version_args = ("codex", "--version")
    if runner is _run_command and shutil.which("codex") is None:
        version = subprocess.CompletedProcess(version_args, 127, stdout="", stderr="codex not found")
    else:
        version = runner(version_args, timeout_seconds, None)
    if version.returncode != 0:
        return SourceAvailability(
            source_kind="codex",
            source_status=source_status,
            raw_drop_count=raw_drop_count,
            missing_files=missing_files,
            probe_status="cli_missing" if version.returncode == 127 else "cli_version_failed",
            blocker="codex_cli_missing" if version.returncode == 127 else "codex_cli_version_failed",
            next_action="Install or repair the Codex CLI, then rerun the exact gpt-5.6-sol probe.",
            probe_command=_command_text(version_args),
            probe_exit_code=version.returncode,
            stdout_preview=_preview(version.stdout),
            stderr_preview=_preview(version.stderr),
            credential_status="account_unverified",
            source_auth_mode="account",
            source_runner_command=_codex_runner_command(),
        )
    if not probe_live:
        return SourceAvailability(
            source_kind="codex",
            source_status=source_status,
            raw_drop_count=raw_drop_count,
            missing_files=missing_files,
            probe_status="cli_available",
            blocker="none" if source_status == "ready" else "missing_source_bundle",
            next_action="none" if source_status == "ready" else _collection_next_action("codex"),
            probe_command=_command_text(version_args),
            probe_exit_code=version.returncode,
            stdout_preview=_preview(version.stdout),
            stderr_preview=_preview(version.stderr),
            credential_status="account_unverified",
            source_auth_mode="account",
            source_runner_command=_codex_runner_command(),
        )

    live_args = codex_source_command(CODEX_MODEL)
    live = runner(live_args, timeout_seconds, "Return exactly: frontier-probe-ok\n")
    stdout = _preview(live.stdout)
    stderr = _preview(live.stderr)
    if live.returncode == 0 and "frontier-probe-ok" in (live.stdout or ""):
        return SourceAvailability(
            source_kind="codex",
            source_status=source_status,
            raw_drop_count=raw_drop_count,
            missing_files=missing_files,
            probe_status="live_probe_passed",
            blocker="none" if source_status == "ready" else "missing_source_bundle",
            next_action="none" if source_status == "ready" else _collection_next_action("codex"),
            probe_command=_command_text(live_args),
            probe_exit_code=live.returncode,
            stdout_preview=stdout,
            stderr_preview=stderr,
            credential_status="account_probe_passed",
            source_auth_mode="account",
            source_runner_command=_codex_runner_command(),
        )
    probe_status, blocker, next_action = _classify_codex_failure(
        f"{live.stdout}\n{live.stderr}"
    )
    credential_status = (
        "account_quota_limited"
        if probe_status == "quota_exhausted"
        else "account_probe_failed"
    )
    return SourceAvailability(
        source_kind="codex",
        source_status=source_status,
        raw_drop_count=raw_drop_count,
        missing_files=missing_files,
        probe_status=probe_status,
        blocker=blocker,
        next_action=next_action,
        probe_command=_command_text(live_args),
        probe_exit_code=live.returncode,
        stdout_preview=stdout,
        stderr_preview=stderr,
        credential_status=credential_status,
        source_auth_mode="account",
        source_runner_command=_codex_runner_command(),
    )


def _claude_api_key_probe_command(max_budget_usd: float) -> tuple[str, ...]:
    return (
        "claude",
        "--bare",
        "--print",
        "--model",
        CLAUDE_MODEL,
        "--output-format",
        "text",
        "--permission-mode",
        "dontAsk",
        "--no-session-persistence",
        "--effort",
        "max",
        "--max-budget-usd",
        str(max_budget_usd),
    )


def _claude_runner_command() -> str:
    return (
        "python scripts/autopilot_frontier_source_runner.py "
        "--source-kind claude --source-auth-mode auto --json"
    )


def _claude_env_credential_status() -> str:
    credential_names = (
        "ANTHROPIC_API_KEY",
        "CLAUDE_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "ANTHROPIC_AUTH_TOKEN",
    )
    present = [name for name in credential_names if os.environ.get(name)]
    if present:
        return "env_credentials_present:" + ",".join(present)
    return "env_credentials_absent"


def _claude_has_anthropic_api_key() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _claude_auto_auth_mode() -> str:
    return "api_key" if _claude_has_anthropic_api_key() else "subscription"


def _claude_auth_status_detail(auth_status: subprocess.CompletedProcess[str]) -> tuple[str, str]:
    if auth_status.returncode != 0:
        detail = _preview(f"{auth_status.stdout}\n{auth_status.stderr}")
        return "auth_status_failed", detail or "none"
    try:
        payload = json.loads(auth_status.stdout or "{}")
    except json.JSONDecodeError:
        return "auth_status_unparseable", _preview(auth_status.stdout)
    logged_in = bool(payload.get("loggedIn"))
    method = str(payload.get("authMethod") or "unknown")
    provider = str(payload.get("apiProvider") or "unknown")
    subscription = str(payload.get("subscriptionType") or "unknown")
    status = "logged_in" if logged_in else "not_logged_in"
    return (
        status,
        f"auth_method={method}; provider={provider}; subscription={subscription}",
    )


def _claude_setup_token_status(
    *,
    runner: CommandRunner,
    timeout_seconds: int,
) -> tuple[str, str]:
    args = ("claude", "setup-token", "--help")
    result = runner(args, timeout_seconds, None)
    if result.returncode == 0:
        return "setup_token_available", f"setup_token_command={_command_text(args)}"
    detail = _preview(f"{result.stdout}\n{result.stderr}") or "none"
    return "setup_token_unavailable", f"{_command_text(args)} -> {detail}"


def _classify_claude_failure(combined_output: str) -> tuple[str, str]:
    lowered = combined_output.lower()
    if "401" in lowered or "auth" in lowered or "invalid authentication" in lowered:
        return "auth_failed", "claude_auth_failed"
    if (
        "selected model" in lowered
        or "model not found" in lowered
        or "may not exist" in lowered
        or "404" in lowered
    ):
        return "model_unavailable", "claude_fable5_unavailable"
    return "live_probe_failed", "claude_live_probe_failed"


def _claude_auth_recovery_action(
    credential_status: str,
    auth_detail: str,
    setup_token_status: str,
    *,
    source_auth_mode: str,
    api_key_probe_status: str,
) -> str:
    setup_token_guidance = (
        "Run `claude setup-token` in a trusted interactive terminal to refresh the "
        "long-lived subscription token"
        if setup_token_status == "setup_token_available"
        else "Refresh the Claude subscription session"
    )
    return (
        f"{setup_token_guidance}; if print-mode auth still fails, run "
        "`claude auth logout` then `claude auth login --claudeai`, or provide a valid "
        "`ANTHROPIC_API_KEY` and rerun the probe in API-key mode; "
        f"current credential status is {credential_status} ({auth_detail}; "
        f"{setup_token_status}); current source runner auth mode is {source_auth_mode} "
        f"({api_key_probe_status}); rerun source availability diagnostics with "
        "--source-kind claude --probe-live; then collect/import a real all-cases "
        f"Claude response with `{_claude_runner_command()}`."
    )


def _claude_probe_recovery_action(
    *,
    probe_status: str,
    credential_status: str,
    auth_detail: str,
    setup_token_status: str,
    source_auth_mode: str,
    api_key_probe_status: str,
) -> str:
    if probe_status == "model_unavailable":
        return (
            "Update Claude Code to the latest release, verify `claude-fable-5` access for "
            "the authenticated account, and rerun the exact Fable 5 live probe."
        )
    return _claude_auth_recovery_action(
        credential_status,
        auth_detail,
        setup_token_status,
        source_auth_mode=source_auth_mode,
        api_key_probe_status=api_key_probe_status,
    )


def _probe_claude(
    *,
    source_status: str,
    raw_drop_count: int,
    missing_files: tuple[str, ...],
    runner: CommandRunner,
    timeout_seconds: int,
    probe_live: bool,
    max_budget_usd: float,
) -> SourceAvailability:
    env_credential_status = _claude_env_credential_status()
    source_auth_mode = _claude_auto_auth_mode()
    api_key_probe_status = (
        "api_key_available" if source_auth_mode == "api_key" else "api_key_missing"
    )
    if runner is _run_command and shutil.which("claude") is None:
        return SourceAvailability(
            source_kind="claude",
            source_status=source_status,
            raw_drop_count=raw_drop_count,
            missing_files=missing_files,
            probe_status="cli_missing",
            blocker="claude_cli_missing",
            next_action=(
                "Install or expose the Claude CLI, then rerun source availability diagnostics "
                "with --source-kind claude --probe-live."
            ),
            credential_status=env_credential_status,
            source_auth_mode=source_auth_mode,
            api_key_probe_status=api_key_probe_status,
            source_runner_command=_claude_runner_command(),
        )

    version_args = ("claude", "--version")
    version = runner(version_args, timeout_seconds, None)
    if version.returncode != 0:
        stdout = _preview(version.stdout)
        stderr = _preview(version.stderr)
        if version.returncode == 127:
            return SourceAvailability(
                source_kind="claude",
                source_status=source_status,
                raw_drop_count=raw_drop_count,
                missing_files=missing_files,
                probe_status="cli_missing",
                blocker="claude_cli_missing",
                next_action=(
                    "Install or expose the Claude CLI, then rerun source availability diagnostics "
                    "with --source-kind claude --probe-live."
                ),
                probe_command=_command_text(version_args),
                probe_exit_code=version.returncode,
                stdout_preview=stdout,
                stderr_preview=stderr,
                credential_status=env_credential_status,
                source_auth_mode=source_auth_mode,
                api_key_probe_status=api_key_probe_status,
                source_runner_command=_claude_runner_command(),
            )
        return SourceAvailability(
            source_kind="claude",
            source_status=source_status,
            raw_drop_count=raw_drop_count,
            missing_files=missing_files,
            probe_status="cli_version_failed",
            blocker="claude_cli_version_failed",
            next_action=(
                "Repair Claude CLI invocation, then rerun source availability diagnostics "
                "with --source-kind claude --probe-live."
            ),
            probe_command=_command_text(version_args),
            probe_exit_code=version.returncode,
            stdout_preview=stdout,
            stderr_preview=stderr,
            credential_status=env_credential_status,
            source_auth_mode=source_auth_mode,
            api_key_probe_status=api_key_probe_status,
            source_runner_command=_claude_runner_command(),
        )

    auth_args = ("claude", "auth", "status", "--json")
    auth_status = runner(auth_args, timeout_seconds, None)
    auth_status_value, auth_detail = _claude_auth_status_detail(auth_status)
    credential_status = f"{env_credential_status}; {auth_status_value}"

    if not probe_live:
        return SourceAvailability(
            source_kind="claude",
            source_status=source_status,
            raw_drop_count=raw_drop_count,
            missing_files=missing_files,
            probe_status="cli_available",
            blocker="none" if source_status == "ready" else "missing_source_bundle",
            next_action="none" if source_status == "ready" else _collection_next_action("claude"),
            probe_command=_command_text(version_args),
            probe_exit_code=version.returncode,
            stdout_preview=_preview(version.stdout),
            stderr_preview=_preview(version.stderr),
            credential_status=credential_status,
            credential_detail=auth_detail,
            source_auth_mode=source_auth_mode,
            api_key_probe_status=api_key_probe_status,
            source_runner_command=_claude_runner_command(),
        )

    live_args = (
        _claude_api_key_probe_command(max_budget_usd)
        if source_auth_mode == "api_key"
        else _claude_live_probe_command(max_budget_usd)
    )
    live = runner(live_args, timeout_seconds, "Reply with exactly: ok\n")
    stdout = _preview(live.stdout)
    stderr = _preview(live.stderr)
    if live.returncode == 0:
        return SourceAvailability(
            source_kind="claude",
            source_status=source_status,
            raw_drop_count=raw_drop_count,
            missing_files=missing_files,
            probe_status="live_probe_passed",
            blocker="none" if source_status == "ready" else "missing_source_bundle",
            next_action="none" if source_status == "ready" else _collection_next_action("claude"),
            probe_command=_command_text(live_args),
            probe_exit_code=live.returncode,
            stdout_preview=stdout,
            stderr_preview=stderr,
            credential_status=credential_status,
            credential_detail=auth_detail,
            source_auth_mode=source_auth_mode,
            api_key_probe_status=api_key_probe_status,
            source_runner_command=_claude_runner_command(),
        )

    probe_status, blocker = _classify_claude_failure(f"{live.stdout}\n{live.stderr}")
    setup_token_status, setup_token_detail = _claude_setup_token_status(
        runner=runner,
        timeout_seconds=timeout_seconds,
    )
    auth_detail = f"{auth_detail}; {setup_token_detail}"
    return SourceAvailability(
        source_kind="claude",
        source_status=source_status,
        raw_drop_count=raw_drop_count,
        missing_files=missing_files,
        probe_status=probe_status,
        blocker=blocker,
        next_action=_claude_probe_recovery_action(
            probe_status=probe_status,
            credential_status=credential_status,
            auth_detail=auth_detail,
            setup_token_status=setup_token_status,
            source_auth_mode=source_auth_mode,
            api_key_probe_status=api_key_probe_status,
        ),
        probe_command=_command_text(live_args),
        probe_exit_code=live.returncode,
        stdout_preview=stdout,
        stderr_preview=stderr,
        credential_status=credential_status,
        credential_detail=auth_detail,
        source_auth_mode=source_auth_mode,
        api_key_probe_status=api_key_probe_status,
        source_runner_command=_claude_runner_command(),
    )


def _select_source_kinds(raw_source_kinds: Sequence[str]) -> tuple[str, ...]:
    selected: list[str] = []
    for raw in raw_source_kinds or ("all",):
        for part in str(raw).split(","):
            source_kind = part.strip()
            if not source_kind:
                continue
            if source_kind == "all":
                selected.extend(SOURCE_KINDS)
                continue
            if source_kind not in SOURCE_KINDS:
                raise ValueError("source-kind must be one of all, " + ", ".join(SOURCE_KINDS))
            selected.append(source_kind)
    unique: list[str] = []
    for source_kind in selected:
        if source_kind not in unique:
            unique.append(source_kind)
    return tuple(unique)


def run_diagnostics(
    *,
    source_kinds: Sequence[str] = ("all",),
    raw_source_root: Path = DEFAULT_RAW_SOURCE_ROOT,
    runner: CommandRunner = _run_command,
    timeout_seconds: int = 30,
    probe_live: bool = False,
    max_budget_usd: float = 0.01,
) -> dict[str, object]:
    raw_source_root = raw_source_root.resolve()
    results: list[SourceAvailability] = []
    for source_kind in _select_source_kinds(source_kinds):
        source_status, raw_drop_count, missing_files = _source_bundle_state(
            raw_source_root,
            source_kind,
        )
        if source_kind == "codex":
            results.append(
                _probe_codex(
                    source_status=source_status,
                    raw_drop_count=raw_drop_count,
                    missing_files=missing_files,
                    runner=runner,
                    timeout_seconds=timeout_seconds,
                    probe_live=probe_live,
                )
            )
        elif source_kind == "claude":
            results.append(
                _probe_claude(
                    source_status=source_status,
                    raw_drop_count=raw_drop_count,
                    missing_files=missing_files,
                    runner=runner,
                    timeout_seconds=timeout_seconds,
                    probe_live=probe_live,
                    max_budget_usd=max_budget_usd,
                )
            )
        else:
            results.append(
                _probe_static_source(
                    source_kind=source_kind,
                    source_status=source_status,
                    raw_drop_count=raw_drop_count,
                    missing_files=missing_files,
                )
            )
    blockers = [item for item in results if item.blocker != "none"]
    status = "passed" if not blockers else "warning"
    return {
        "schema": FRONTIER_SOURCE_AVAILABILITY_SCHEMA_VERSION,
        "generated_utc": _utc_now(),
        "status": status,
        "promotion_impact": "clear" if status == "passed" else "blocked",
        "raw_source_root": str(raw_source_root),
        "source_count": len(results),
        "blockers": len(blockers),
        "sources": [dataclasses.asdict(item) for item in results],
    }


def render_report(summary: dict[str, object]) -> str:
    sources = [
        SourceAvailability(**item)
        for item in summary.get("sources", [])
        if isinstance(item, dict)
    ]
    lines = [
        "# CHILI Frontier Source Availability Diagnostics",
        "",
        f"- Schema: {FRONTIER_SOURCE_AVAILABILITY_SCHEMA_VERSION}",
        f"- Generated UTC: {summary.get('generated_utc', '')}",
        f"- Status: {summary.get('status', 'missing')}",
        f"- Promotion impact: {summary.get('promotion_impact', 'blocked')}",
        f"- Raw source root: {summary.get('raw_source_root', '')}",
        f"- Source count: {summary.get('source_count', len(sources))}",
        f"- Blockers: {summary.get('blockers', 0)}",
    ]
    for item in sources:
        label = item.source_kind.replace("_", " ").title()
        lines.extend(
            [
                f"- {label} source status: {item.source_status}",
                f"- {label} probe status: {item.probe_status}",
                f"- {label} blocker: {item.blocker}",
                f"- {label} credential status: {item.credential_status or 'none'}",
                f"- {label} source auth mode: {item.source_auth_mode or 'none'}",
                f"- {label} API-key probe status: {item.api_key_probe_status or 'none'}",
                f"- {label} source runner command: {item.source_runner_command or 'none'}",
                f"- {label} next action: {item.next_action}",
            ]
        )
    lines.extend(
        [
            "- Safety: read-only diagnostics only; no source/test edit, git, PR, deploy, runtime, database, broker, or live-trading action.",
            "",
            "| Source | Source status | Raw drops | Probe status | Blocker | Credential status | Credential detail | Source auth mode | API-key probe | Source runner command | Missing files | Probe command | Exit | Stdout | Stderr | Next action |",
            "| --- | --- | ---: | --- | --- | --- | --- | --- | --- | --- | --- | --- | ---: | --- | --- | --- |",
        ]
    )
    for item in sources:
        lines.append(
            "| "
            + " | ".join(
                [
                    _escape_cell(item.source_kind),
                    _escape_cell(item.source_status),
                    str(item.raw_drop_count),
                    _escape_cell(item.probe_status),
                    _escape_cell(item.blocker),
                    _escape_cell(item.credential_status or "none"),
                    _escape_cell(item.credential_detail or "none"),
                    _escape_cell(item.source_auth_mode or "none"),
                    _escape_cell(item.api_key_probe_status or "none"),
                    _escape_cell(item.source_runner_command or "none"),
                    _escape_cell(", ".join(item.missing_files) or "none"),
                    _escape_cell(item.probe_command or "none"),
                    "" if item.probe_exit_code is None else str(item.probe_exit_code),
                    _escape_cell(item.stdout_preview or "none"),
                    _escape_cell(item.stderr_preview or "none"),
                    _escape_cell(item.next_action),
                ]
            )
            + " |"
        )
    lines.append("")
    return "\n".join(lines)


def write_report(markdown: str, output_path: Path = DEFAULT_OUTPUT) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(markdown, encoding="utf-8")
    return output_path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Diagnose frontier model source lane availability."
    )
    parser.add_argument("--source-kind", action="append")
    parser.add_argument("--raw-source-root", type=Path, default=DEFAULT_RAW_SOURCE_ROOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--probe-live", action="store_true")
    parser.add_argument("--timeout-seconds", type=int, default=30)
    parser.add_argument("--max-budget-usd", type=float, default=0.01)
    parser.add_argument("--no-write", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    summary = run_diagnostics(
        source_kinds=args.source_kind or ("all",),
        raw_source_root=args.raw_source_root,
        timeout_seconds=args.timeout_seconds,
        probe_live=args.probe_live,
        max_budget_usd=args.max_budget_usd,
    )
    markdown = render_report(summary)
    if not args.no_write:
        write_report(markdown, args.output)
    if args.json:
        payload = dict(summary)
        payload["path"] = str(args.output)
        payload["written"] = not args.no_write
        print(json.dumps(payload, indent=2, sort_keys=True))
    elif args.no_write:
        print(markdown)
    else:
        print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
