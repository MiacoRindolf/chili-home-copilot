from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml


CANONICAL_MOMENTUM_WORKER = "chili-clean-recovery-momentum-exec"
LEGACY_SPLIT_WORKER = "chili-ross-live-worker"
LEGACY_SCHEDULER_WORKER = "chili-clean-recovery-scheduler"
STALE_LIVE_CONTAINER_PREFIXES = (
    "chili-clean-recovery-momentum-exec-pre",
    "chili-clean-recovery-scheduler-pre",
)
ROSS_CRITICAL_SOURCE_PATHS = (
    "/app/app/services/trading/momentum_neural/live_runner.py",
    "/app/app/services/trading/momentum_neural/live_runner_loop.py",
    "/app/app/services/trading/momentum_neural/live_fsm.py",
    "/app/app/services/trading/momentum_neural/risk_evaluator.py",
    "/app/app/services/trading/momentum_neural/auto_arm.py",
    "/app/app/services/trading/momentum_neural/universe.py",
    "/app/app/services/trading/momentum_neural/ross_feed_health.py",
)
LOAD_BEARING_MOMENTUM_ENV_CONTROLS = (
    "CHILI_MOMENTUM_PULLBACK_ENTRY_INTERVAL",
    "CHILI_MOMENTUM_EARLY_PREMARKET_MIN_MOVERS",
    "CHILI_MOMENTUM_TICK_FIRST_PULLBACK_ENABLED",
    "CHILI_MOMENTUM_MIDDAY_DEWEIGHT_ENABLED",
    "CHILI_MOMENTUM_PYRAMID_ENABLED",
    "CHILI_MOMENTUM_PYRAMID_DISCRETE_ADD_ENABLED",
    "CHILI_MOMENTUM_PYRAMID_SKIP_VIABILITY_RECHECK",
    "CHILI_MOMENTUM_PYRAMID_ADD_SUBMIT_RETRY_MAX",
    "CHILI_MOMENTUM_SCALE_GRID_ENABLED",
    "CHILI_MOMENTUM_ADD_INTO_HALT_ENABLED",
    "CHILI_MOMENTUM_STOP_L2_CONFIRM_ENABLED",
    "CHILI_MOMENTUM_EXIT_OFI_LOCK_PARTIAL_ENABLED",
    "CHILI_MOMENTUM_EXIT_OFI_HIDDEN_SELLER_ENABLED",
    "CHILI_MOMENTUM_CATALYST_CONVICTION_ENABLED",
)
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PREMARKET_READINESS_SCRIPT = Path(r"D:\CHILI-Docker\premarket-readiness.ps1")


def _env_map(raw_env: Iterable[str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in raw_env or []:
        key, sep, value = str(item).partition("=")
        if sep:
            out[key] = value
    return out


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _container_name(container: dict[str, Any]) -> str:
    names = container.get("Names")
    if isinstance(names, list) and names:
        return str(names[0]).lstrip("/")
    name = container.get("Name")
    if name:
        return str(name).lstrip("/")
    return str(container.get("Id") or "")


def _command_text(container: dict[str, Any]) -> str:
    parts: list[str] = []
    command = container.get("Command")
    if command:
        parts.append(str(command))
    config = container.get("Config")
    if isinstance(config, dict):
        for key in ("Entrypoint", "Cmd"):
            raw = config.get(key)
            if isinstance(raw, list):
                parts.extend(str(x) for x in raw)
            elif raw:
                parts.append(str(raw))
    return " ".join(parts).lower()


def _is_running(container: dict[str, Any]) -> bool:
    state = container.get("State")
    if isinstance(state, dict):
        status = str(state.get("Status") or "").lower()
        return bool(state.get("Running")) or status == "running"
    status = str(container.get("Status") or "").lower()
    return status.startswith("up") or status == "running"


def _run_text(cmd: Sequence[str], *, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(cmd),
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def _docker_python_code(code: str, *, container_name: str | None = None, image: str | None = None, timeout: float = 30) -> str:
    if image:
        completed = _run_text(
            [
                "docker",
                "run",
                "--rm",
                "-e",
                "DATABASE_URL=postgresql://chili:chili@postgres:5432/chili",
                image,
                "python",
                "-c",
                code,
            ],
            timeout=timeout,
        )
        return completed.stdout
    completed = _run_text(
        ["docker", "exec", container_name or CANONICAL_MOMENTUM_WORKER, "python", "-c", code],
        timeout=timeout,
    )
    return completed.stdout


def _runtime_stage_timeout_text(exc: subprocess.TimeoutExpired) -> str:
    timeout = getattr(exc, "timeout", None)
    if timeout is None:
        return "unknown"
    try:
        return f"{float(timeout):g}"
    except (TypeError, ValueError):
        return str(timeout)


def _run_runtime_stage(
    label: str,
    errors: list[str],
    producer: Callable[[], tuple[bool, list[str]]],
) -> bool:
    try:
        ok, stage_errors = producer()
    except subprocess.TimeoutExpired as exc:
        errors.append(f"runtime_guard_stage_timeout:{label}:timeout={_runtime_stage_timeout_text(exc)}")
        return False
    except subprocess.CalledProcessError as exc:
        details: list[str] = []
        stdout = str(getattr(exc, "stdout", "") or "").strip()
        stderr = str(getattr(exc, "stderr", "") or "").strip()
        if stdout:
            details.append(f"stdout={stdout[-500:]}")
        if stderr:
            details.append(f"stderr={stderr[-500:]}")
        suffix = f":{';'.join(details)}" if details else ""
        errors.append(f"runtime_guard_stage_failed:{label}:returncode={exc.returncode}{suffix}")
        return False
    errors.extend(stage_errors)
    return ok


def _parse_utc(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _is_stale_live_container_name(name: str, *, canonical_name: str) -> bool:
    lowered = name.lower()
    if any(lowered.startswith(prefix) for prefix in STALE_LIVE_CONTAINER_PREFIXES):
        return True
    return lowered.startswith(canonical_name.lower()) and "placeholder" in lowered


def _is_relevant_docker_container_name(name: str) -> bool:
    lowered = str(name or "").lstrip("/").lower()
    if not lowered:
        return True
    if lowered in {
        CANONICAL_MOMENTUM_WORKER.lower(),
        LEGACY_SPLIT_WORKER.lower(),
        LEGACY_SCHEDULER_WORKER.lower(),
    }:
        return True
    if any(lowered.startswith(prefix) for prefix in STALE_LIVE_CONTAINER_PREFIXES):
        return True
    return lowered.startswith(CANONICAL_MOMENTUM_WORKER.lower()) and "placeholder" in lowered


def evaluate_container_set(
    containers: Sequence[dict[str, Any]],
    *,
    canonical_name: str = CANONICAL_MOMENTUM_WORKER,
    legacy_split_name: str = LEGACY_SPLIT_WORKER,
    allow_batch_fallback: bool = False,
) -> tuple[bool, list[str]]:
    """Validate that the canonical momentum worker is real, live, and unique."""
    errors: list[str] = []
    by_name = {_container_name(container): container for container in containers}
    canonical = by_name.get(canonical_name)
    if canonical is None:
        return False, [f"canonical_missing:{canonical_name}"]

    state = canonical.get("State")
    if isinstance(state, dict):
        status = str(state.get("Status") or "").lower()
    else:
        status = str(canonical.get("Status") or "").lower()
    if not _is_running(canonical):
        errors.append(f"canonical_not_running:{status or 'unknown'}")

    command = _command_text(canonical)
    for marker in ("placeholder", "live runner disabled", "sleep(86400)"):
        if marker in command:
            errors.append(f"canonical_placeholder_command:{marker}")

    config = canonical.get("Config")
    env = _env_map(config.get("Env") if isinstance(config, dict) else canonical.get("Env"))
    required_true = {
        "CHILI_MOMENTUM_LIVE_RUNNER_ENABLED": "live_runner",
        "CHILI_MOMENTUM_LIVE_RUNNER_LOOP_ENABLED": "event_loop",
        "CHILI_MOMENTUM_LIVE_RUNNER_LOOP_IQFEED_TAPE_ENABLED": "iqfeed_tape",
        "CHILI_MOMENTUM_LIVE_RUNNER_LOOP_IQFEED_NOTIFY_ENABLED": "iqfeed_notify",
        "CHILI_MOMENTUM_LIVE_RUNNER_LOOP_IQFEED_POLL_FALLBACK_ENABLED": "iqfeed_poll_fallback",
        "CHILI_MOMENTUM_ROSS_EVENT_ADMISSION_ENABLED": "ross_event_admission",
        "CHILI_MOMENTUM_AUTO_ARM_LIVE_ENABLED": "auto_arm",
        "CHILI_MOMENTUM_ROSS_EQUITY_UNIVERSE_REQUIRED": "ross_universe",
    }
    for key, label in required_true.items():
        if not _truthy(env.get(key)):
            errors.append(f"canonical_env_disabled:{label}:{key}={env.get(key, '')}")

    if _truthy(env.get("CHILI_MOMENTUM_LIVE_RUNNER_SCHEDULER_ENABLED")):
        errors.append("canonical_scheduler_live_runner_enabled")

    if _truthy(env.get("CHILI_MOMENTUM_LIVE_RUNNER_BATCH_FALLBACK_ENABLED")) and not allow_batch_fallback:
        errors.append("canonical_batch_fallback_enabled")

    if _truthy(env.get("CHILI_MOMENTUM_AUTO_ARM_LIVE_SCHEDULER_ENABLED")):
        errors.append("canonical_auto_arm_scheduler_enabled")

    if _truthy(env.get("CHILI_MOMENTUM_AUTO_ARM_LIVE_SCHEDULER_FALLBACK_ENABLED")):
        errors.append("canonical_auto_arm_scheduler_fallback_enabled")

    legacy = by_name.get(legacy_split_name)
    if legacy is not None:
        if _is_running(legacy):
            errors.append(f"duplicate_live_worker_running:{legacy_split_name}")

    legacy_scheduler = by_name.get(LEGACY_SCHEDULER_WORKER)
    if legacy_scheduler is not None and _is_running(legacy_scheduler):
        errors.append(f"stale_scheduler_container_running:{LEGACY_SCHEDULER_WORKER}")

    for name, container in by_name.items():
        if name in {canonical_name, legacy_split_name, LEGACY_SCHEDULER_WORKER}:
            continue
        if _is_stale_live_container_name(name, canonical_name=canonical_name) and _is_running(container):
            errors.append(f"stale_live_container_running:{name}")

    return not errors, errors


def evaluate_source_reload_freshness(config: dict[str, Any]) -> tuple[bool, list[str]]:
    errors: list[str] = []
    started = _parse_utc(config.get("started_at"))
    if started is None:
        return False, ["source_reload_freshness_failed:missing_started_at"]
    for path, raw_mtime in (config.get("source_mtimes") or {}).items():
        mtime = _parse_utc(raw_mtime)
        if mtime is None:
            errors.append(f"source_reload_freshness_failed:bad_mtime:{path}")
            continue
        if mtime > started:
            errors.append(
                "source_reload_freshness_failed:worker_older_than_source:"
                f"{path}:started={started.isoformat()}:mtime={mtime.isoformat()}"
            )
    return not errors, errors


def _ns_to_seconds(value: Any) -> float | None:
    try:
        raw = float(value)
    except (TypeError, ValueError):
        return None
    if raw <= 0:
        return None
    return raw / 1_000_000_000.0


def _health_stabilization_seconds(container: dict[str, Any]) -> float:
    config = container.get("Config") if isinstance(container.get("Config"), dict) else {}
    health = config.get("Healthcheck") if isinstance(config, dict) else None
    if not isinstance(health, dict):
        return 0.0
    interval_s = _ns_to_seconds(health.get("Interval"))
    start_period_s = _ns_to_seconds(health.get("StartPeriod")) or 0.0
    try:
        retries = int(health.get("Retries") or 0)
    except (TypeError, ValueError):
        retries = 0
    retry_window_s = (interval_s or 0.0) * max(retries, 1 if interval_s else 0)
    return max(start_period_s, retry_window_s)


def _event_time_utc(event: dict[str, Any]) -> datetime | None:
    raw_nano = event.get("timeNano") or event.get("TimeNano")
    if raw_nano not in (None, ""):
        try:
            return datetime.fromtimestamp(float(raw_nano) / 1_000_000_000.0, tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            pass
    raw_time = event.get("time") or event.get("Time")
    if raw_time not in (None, ""):
        try:
            return datetime.fromtimestamp(float(raw_time), tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            pass
    return _parse_utc(event.get("time_utc") or event.get("timestamp"))


def evaluate_worker_lifecycle_quiet(config: dict[str, Any]) -> tuple[bool, list[str]]:
    quiet_seconds = float(config.get("quiet_seconds") or 0.0)
    if quiet_seconds <= 0:
        return True, []
    now = _parse_utc(config.get("now")) or datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=quiet_seconds)
    disruptive_actions = {"kill", "die", "restart", "stop", "oom"}
    errors: list[str] = []
    for event in config.get("events") or []:
        if not isinstance(event, dict):
            continue
        action = str(event.get("Action") or event.get("action") or "").strip().lower()
        if action not in disruptive_actions:
            continue
        event_time = _event_time_utc(event)
        if event_time is None or event_time < cutoff:
            continue
        attrs = event.get("Actor", {}).get("Attributes", {}) if isinstance(event.get("Actor"), dict) else {}
        if not isinstance(attrs, dict):
            attrs = {}
        exit_code = str(attrs.get("exitCode") or event.get("exitCode") or "").strip()
        signal = str(attrs.get("signal") or event.get("signal") or "").strip()
        errors.append(
            "worker_lifecycle_quiet_failed:"
            f"action={action}:time={event_time.isoformat()}:quiet_seconds={quiet_seconds:g}:"
            f"exit={exit_code}:signal={signal}"
        )
    return not errors, errors


def evaluate_compose_image_alignment(config: dict[str, Any]) -> tuple[bool, list[str]]:
    running_image = str(config.get("running_image") or "").strip()
    compose_image = str(config.get("compose_image") or "").strip()
    if not running_image:
        return False, ["compose_image_alignment_failed:missing_running_image"]
    if not compose_image:
        return False, ["compose_image_alignment_failed:missing_compose_image"]
    if running_image != compose_image:
        return False, [f"compose_image_alignment_failed:running={running_image}:compose={compose_image}"]
    return True, []


def evaluate_expected_running_image(config: dict[str, Any], expected_image: str) -> tuple[bool, list[str]]:
    running_image = str(config.get("running_image") or "").strip()
    expected = str(expected_image or "").strip()
    if not expected:
        return True, []
    if not running_image:
        return False, ["expected_image_alignment_failed:missing_running_image"]
    if running_image != expected:
        return False, [f"expected_image_alignment_failed:running={running_image}:expected={expected}"]
    return True, []


def _list_text(value: Any) -> str:
    if isinstance(value, list):
        return " ".join(str(item) for item in value)
    if value is None:
        return ""
    return str(value)


def _compose_env_map(raw_env: Any) -> dict[str, str]:
    if isinstance(raw_env, dict):
        return {str(key): str(value) for key, value in raw_env.items()}
    if isinstance(raw_env, list):
        return _env_map(str(item) for item in raw_env)
    return {}


def evaluate_compose_momentum_exec_service(
    service: dict[str, Any],
    *,
    canonical_name: str = CANONICAL_MOMENTUM_WORKER,
) -> tuple[bool, list[str]]:
    """Validate the rendered Compose service cannot recreate a disabled or stale worker."""
    errors: list[str] = []
    if str(service.get("container_name") or "").strip() != canonical_name:
        errors.append(
            "compose_momentum_exec_service_failed:container_name="
            f"{service.get('container_name') or ''}:expected={canonical_name}"
        )

    profiles = service.get("profiles") or []
    if "live-momentum" not in {str(item) for item in profiles}:
        errors.append("compose_momentum_exec_service_failed:missing_live_momentum_profile")

    command = _list_text(service.get("entrypoint")) + " " + _list_text(service.get("command"))
    lowered_command = command.lower()
    for marker in ("placeholder", "live runner disabled", "sleep(86400)"):
        if marker in lowered_command:
            errors.append(f"compose_momentum_exec_service_failed:placeholder_command:{marker}")
    if "scripts/scheduler_worker.py" not in lowered_command:
        errors.append("compose_momentum_exec_service_failed:not_scheduler_worker")

    env = _compose_env_map(service.get("environment"))
    required_true = {
        "CHILI_MOMENTUM_LIVE_RUNNER_ENABLED": "live_runner",
        "CHILI_MOMENTUM_LIVE_RUNNER_LOOP_ENABLED": "event_loop",
        "CHILI_MOMENTUM_LIVE_RUNNER_LOOP_IQFEED_TAPE_ENABLED": "iqfeed_tape",
        "CHILI_MOMENTUM_LIVE_RUNNER_LOOP_IQFEED_NOTIFY_ENABLED": "iqfeed_notify",
        "CHILI_MOMENTUM_LIVE_RUNNER_LOOP_IQFEED_POLL_FALLBACK_ENABLED": "iqfeed_poll_fallback",
        "CHILI_MOMENTUM_ROSS_EVENT_ADMISSION_ENABLED": "ross_event_admission",
        "CHILI_MOMENTUM_AUTO_ARM_LIVE_ENABLED": "auto_arm",
        "CHILI_MOMENTUM_ROSS_EQUITY_UNIVERSE_REQUIRED": "ross_universe",
    }
    for key, label in required_true.items():
        if not _truthy(env.get(key)):
            errors.append(f"compose_momentum_exec_service_failed:env_disabled:{label}:{key}={env.get(key, '')}")
    if str(env.get("CHILI_SCHEDULER_ROLE") or "").strip() != "momentum_exec_only":
        errors.append(
            "compose_momentum_exec_service_failed:scheduler_role="
            f"{env.get('CHILI_SCHEDULER_ROLE', '')}:expected=momentum_exec_only"
        )
    if _truthy(env.get("CHILI_MOMENTUM_LIVE_RUNNER_SCHEDULER_ENABLED")):
        errors.append("compose_momentum_exec_service_failed:scheduled_live_runner_enabled")
    if _truthy(env.get("CHILI_MOMENTUM_AUTO_ARM_LIVE_SCHEDULER_ENABLED")):
        errors.append("compose_momentum_exec_service_failed:auto_arm_scheduler_enabled")
    if _truthy(env.get("CHILI_MOMENTUM_AUTO_ARM_LIVE_SCHEDULER_FALLBACK_ENABLED")):
        errors.append("compose_momentum_exec_service_failed:auto_arm_scheduler_fallback_enabled")
    if _truthy(env.get("CHILI_MOMENTUM_LIVE_RUNNER_BATCH_FALLBACK_ENABLED")):
        errors.append("compose_momentum_exec_service_failed:batch_fallback_enabled")
    for key in ("CHILI_AUTOTRADER_ENABLED", "CHILI_AUTOTRADER_CRYPTO_ENABLED"):
        if _truthy(env.get(key)):
            errors.append(f"compose_momentum_exec_service_failed:autotrader_enabled:{key}")
    for key in LOAD_BEARING_MOMENTUM_ENV_CONTROLS:
        if key not in env:
            errors.append(f"compose_momentum_exec_service_failed:missing_load_bearing_env:{key}")

    return not errors, errors


def evaluate_transcript_gate_config(config: dict[str, Any]) -> tuple[bool, list[str]]:
    errors: list[str] = []
    bridge_enabled = bool(config.get("bridge_enabled"))
    require_marker = bool(config.get("require_warrior_session_ok"))
    marker_path = str(config.get("warrior_session_ok_path") or "").strip()
    try:
        max_age_s = float(config.get("warrior_session_ok_max_age_seconds"))
    except (TypeError, ValueError):
        max_age_s = 0.0

    if bridge_enabled and not require_marker:
        errors.append("ross_transcript_marker_not_required")
    if bridge_enabled and not marker_path:
        errors.append("ross_transcript_marker_path_missing")
    if bridge_enabled and (max_age_s <= 0.0 or max_age_s > 60.0):
        errors.append(f"ross_transcript_marker_max_age_unsafe:{max_age_s:g}")
    return not errors, errors


def evaluate_ross_event_admission_config(config: dict[str, Any]) -> tuple[bool, list[str]]:
    errors: list[str] = []
    try:
        tick_count = int(config.get("tick_count"))
    except (TypeError, ValueError):
        tick_count = 0
    if tick_count < 1:
        errors.append(f"ross_event_admission_tick_count_too_low:{tick_count}")
    return not errors, errors


def evaluate_live_loop_timing_config(config: dict[str, Any]) -> tuple[bool, list[str]]:
    errors: list[str] = []
    notify_enabled = _truthy(str(config.get("iqfeed_notify_enabled")))
    tape_enabled = _truthy(str(config.get("iqfeed_tape_enabled")))
    fallback_enabled = _truthy(str(config.get("iqfeed_poll_fallback_enabled")))
    try:
        poll_s = float(config.get("iqfeed_poll_seconds"))
    except (TypeError, ValueError):
        poll_s = 999.0
    try:
        min_tick_ms = int(config.get("min_tick_interval_ms"))
    except (TypeError, ValueError):
        min_tick_ms = 999999
    if not notify_enabled:
        errors.append("live_loop_iqfeed_notify_disabled")
    if not tape_enabled:
        errors.append("live_loop_iqfeed_tape_disabled")
    if fallback_enabled and poll_s > 0.25:
        errors.append(f"live_loop_iqfeed_poll_seconds_too_slow:{poll_s:g}")
    if min_tick_ms > 250:
        errors.append(f"live_loop_min_tick_interval_too_slow_ms:{min_tick_ms}")
    markers = config.get("source_markers")
    if isinstance(markers, dict):
        required = {
            "has_notify_handler": "_handle_iqfeed_notify_payload",
            "has_notify_admission": "_admit_iqfeed_symbol",
            "has_refresh_viability": "refresh_viability=True",
            "has_immediate_notify_submit": 'cause="iqfeed_notify"',
            "has_iqfeed_listen_channel": "LISTEN momentum_iqfeed_l1",
        }
        for key, label in required.items():
            if markers.get(key) is not True:
                errors.append(f"live_loop_source_marker_missing:{label}")
    return not errors, errors


def evaluate_iqfeed_bridge_notify_source(results: dict[str, Any]) -> tuple[bool, list[str]]:
    markers = results.get("source_markers")
    if not isinstance(markers, dict):
        return False, [f"iqfeed_bridge_notify_source_failed:bad_payload:{results}"]
    errors: list[str] = []
    required = {
        "has_notify_enabled_flag": "IQFEED_NOTIFY_ENABLED",
        "has_notify_channel": "IQFEED_NOTIFY_CHANNEL",
        "has_pg_notify_statement": "SELECT pg_notify",
        "has_notify_payload_symbol": '"symbol": sym',
        "has_notify_payload_observed_at": '"observed_at"',
        "has_notify_payload_source": '"source": "iqfeed_l1"',
        "has_notify_after_nbbo_branch": "notify_by_symbol",
    }
    for key, label in required.items():
        if markers.get(key) is not True:
            errors.append(f"iqfeed_bridge_notify_source_marker_missing:{label}")
    return not errors, errors


def evaluate_ross_entry_shape_smoke(results: dict[str, Any]) -> tuple[bool, list[str]]:
    errors: list[str] = []
    markers = results.get("source_markers")
    if isinstance(markers, dict):
        required = {
            "has_entry_shape_block": "_ross_live_entry_shape_block",
            "has_pre_candidate_block": "_ross_live_pre_candidate_shape_block",
            "has_shape_reason": "ross_live_requires_tick_tape_revalidation",
            "has_pre_candidate_event": "live_entry_pre_candidate_ross_shape_block",
            "has_5m_block": 'frame_used in {"1m", "5m"}',
            "has_tick_label_not_enough": "if not tick_tape_revalidated",
            "has_scheduler_entry_wall": "ross_equity_scheduler_entry_wall",
        }
        for key, label in required.items():
            if markers.get(key) is not True:
                errors.append(f"ross_entry_shape_source_marker_missing:{label}")
        return not errors, errors
    expected = {
        "jem_1m_breakout_attempt_block": "ross_live_requires_tick_tape_revalidation",
        "lhai_5m_abcd_block": "ross_live_requires_tick_tape_revalidation",
        "lhai_tick_label_no_frame_block": "ross_live_requires_tick_tape_revalidation",
        "lhai_pre_candidate_micro_error_5m_block": "ross_live_requires_tick_tape_revalidation",
    }
    for key, reason in expected.items():
        row = results.get(key)
        if not isinstance(row, dict) or row.get("reason") != reason:
            errors.append(f"ross_entry_shape_smoke_failed:{key}:{row}")
    if results.get("canf_tick_first_pullback_allow") is not None:
        errors.append(f"ross_entry_shape_smoke_failed:canf_tick_first_pullback_allow:{results.get('canf_tick_first_pullback_allow')}")
    if results.get("jem_tick_breakout_allow") is not None:
        errors.append(f"ross_entry_shape_smoke_failed:jem_tick_breakout_allow:{results.get('jem_tick_breakout_allow')}")
    return not errors, errors


def evaluate_ross_reentry_smoke(results: dict[str, Any]) -> tuple[bool, list[str]]:
    errors: list[str] = []
    markers = results.get("source_markers")
    if isinstance(markers, dict):
        required = {
            "has_session_helper": "_live_same_session_reentry_allowed_for_session",
            "has_ross_family_check": "EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP",
            "has_stock_check": 'asset_class_for_symbol(symbol) == "stock"',
            "has_forced_false": "return False",
        }
        for key, label in required.items():
            if markers.get(key) is not True:
                errors.append(f"ross_reentry_source_marker_missing:{label}")
        return not errors, errors
    if results.get("ross_equity_reentry_allowed") is not False:
        errors.append(f"ross_reentry_smoke_failed:ross_equity_reentry_allowed:{results.get('ross_equity_reentry_allowed')}")
    if results.get("crypto_reentry_allowed") is not True:
        errors.append(f"ross_reentry_smoke_failed:crypto_reentry_allowed:{results.get('crypto_reentry_allowed')}")
    return not errors, errors


def evaluate_a_setup_size_floor_smoke(results: dict[str, Any]) -> tuple[bool, list[str]]:
    errors: list[str] = []
    markers = results.get("source_markers")
    if isinstance(markers, dict):
        if markers.get("has_hard_reducer_reason") is not True:
            errors.append("a_setup_size_floor_source_marker_missing:hard_reducer_respected")
        if markers.get("has_hard_blocker_label") is True:
            errors.append("a_setup_size_floor_source_marker_present:hard_blocker")
        return not errors, errors
    reason = results.get("reason")
    if reason != "hard_reducer_respected":
        errors.append(f"a_setup_size_floor_smoke_failed:reason:{reason}")
    if "hard_blockers" in results:
        errors.append("a_setup_size_floor_smoke_failed:hard_blockers_label_present")
    reducers = results.get("hard_reducers")
    if not isinstance(reducers, dict) or reducers.get("severe_liquidity") != 0.5:
        errors.append(f"a_setup_size_floor_smoke_failed:hard_reducers:{reducers}")
    return not errors, errors


def evaluate_ross_starter_alias_coverage_smoke(results: dict[str, Any]) -> tuple[bool, list[str]]:
    trace = results.get("setup_trace") if isinstance(results.get("setup_trace"), dict) else {}
    errors: list[str] = []
    if trace.get("setup_alias") != "ross_breakout_starter_tick":
        errors.append(f"ross_starter_alias_coverage_failed:setup_alias:{trace.get('setup_alias')}")
    if trace.get("setup_coverage") != "structural_a_setup":
        errors.append(f"ross_starter_alias_coverage_failed:setup_coverage:{trace.get('setup_coverage')}")
    if trace.get("structural_stop_covered") is not True:
        errors.append(
            "ross_starter_alias_coverage_failed:structural_stop_covered:"
            f"{trace.get('structural_stop_covered')}"
        )
    if trace.get("a_setup_floor_covered") is not True:
        errors.append(
            "ross_starter_alias_coverage_failed:a_setup_floor_covered:"
            f"{trace.get('a_setup_floor_covered')}"
        )
    if trace.get("source_wait_tick_armed") is not True:
        errors.append(
            "ross_starter_alias_coverage_failed:source_wait_tick_armed:"
            f"{trace.get('source_wait_tick_armed')}"
        )
    return not errors, errors


def evaluate_ross_exit_shape_smoke(results: dict[str, Any]) -> tuple[bool, list[str]]:
    markers = results.get("source_markers")
    if not isinstance(markers, dict):
        return False, [f"ross_exit_shape_smoke_failed:bad_payload:{results}"]
    errors: list[str] = []
    required = {
        "has_tick_tape_helper": "_is_ross_tick_tape_entry",
        "has_smart_hold_for_ross": "smart_hold_enabled or ross_tick_tape_entry",
        "legacy_bail_excludes_ross": "legacy_breakout_bailout_excludes_ross",
    }
    for key, label in required.items():
        if markers.get(key) is not True:
            errors.append(f"ross_exit_shape_source_marker_missing:{label}")
    return not errors, errors


def _docker_read_text(container_name: str, path: str) -> str:
    code = (
        "from pathlib import Path; "
        f"print(Path({path!r}).read_text(encoding='utf-8'), end='')"
    )
    completed = _run_text(["docker", "exec", container_name, "python", "-c", code], timeout=20)
    return completed.stdout


def evaluate_ross_symbol_resolution_smoke(results: dict[str, Any]) -> tuple[bool, list[str]]:
    errors: list[str] = []
    warnings = results.get("warnings")
    if not isinstance(warnings, list) or not warnings:
        errors.append(f"ross_symbol_resolution_smoke_failed:no_warning:{results}")
        return False, errors
    first = warnings[0] if isinstance(warnings[0], dict) else {}
    if first.get("mentioned_symbol") != "DXTS":
        errors.append(f"ross_symbol_resolution_smoke_failed:mentioned_symbol:{first}")
    if first.get("reason") != "mentioned_symbol_unresolved_near_market_symbol":
        errors.append(f"ross_symbol_resolution_smoke_failed:reason:{first}")
    near = first.get("near_symbols")
    if not isinstance(near, list) or not near or not isinstance(near[0], dict):
        errors.append(f"ross_symbol_resolution_smoke_failed:no_near_symbol:{first}")
    elif near[0].get("symbol") != "DXF":
        errors.append(f"ross_symbol_resolution_smoke_failed:near_symbol:{near[0]}")
    return not errors, errors


def evaluate_replay_scheduler_snapshot_smoke(results: dict[str, Any]) -> tuple[bool, list[str]]:
    errors: list[str] = []
    markers = results.get("source_markers")
    if not isinstance(markers, dict):
        markers = {}
    if "snapshot_enabled" in results and results.get("snapshot_enabled") is not True:
        errors.append(f"replay_snapshot_smoke_failed:snapshot_enabled:{results.get('snapshot_enabled')}")
    if "emitter_callable" in results and results.get("emitter_callable") is not True:
        errors.append(f"replay_snapshot_smoke_failed:emitter_callable:{results.get('emitter_callable')}")
    if "venue_state_callable" in results and results.get("venue_state_callable") is not True:
        errors.append(f"replay_snapshot_smoke_failed:venue_state_callable:{results.get('venue_state_callable')}")
    if "event_type" in results and results.get("event_type") != "live_replay_scheduler_snapshot":
        errors.append(f"replay_snapshot_smoke_failed:event_type:{results.get('event_type')}")
    audit_contract = results.get("audit_contract")
    if isinstance(audit_contract, dict):
        expected_audit_contract = {
            "has_evidence_status": True,
            "has_missing_evidence": True,
            "has_multi_snapshot_missing_key": True,
            "has_counterfactual_missing_key": True,
            "has_opportunity_label_missing_key": True,
            "pnl_minmax_claim_ready": False,
        }
        for key, expected_value in expected_audit_contract.items():
            if audit_contract.get(key) != expected_value:
                errors.append(f"replay_audit_contract_failed:{key}:{audit_contract.get(key)!r}")
    if not markers:
        return not errors, errors
    for key in (
        "has_first_class_flag",
        "has_snapshot_event_type",
        "has_emitter",
        "has_best_effort_comment",
        "has_plan_hook",
        "has_payload_selected_ids",
        "has_payload_prefilter_results",
        "has_payload_venue_states",
        "has_replay_evidence_status",
        "has_replay_missing_evidence",
        "has_opportunity_label_evidence",
        "has_opportunity_label_export",
        "has_pnl_minmax_label_gate",
        "has_event_snapshot_export",
        "has_event_loop_snapshot_emitter",
    ):
        if markers.get(key) is not True:
            errors.append(f"replay_snapshot_source_marker_missing:{key}")
    return not errors, errors


def evaluate_restored_helper_contract_smoke(results: dict[str, Any]) -> tuple[bool, list[str]]:
    errors: list[str] = []
    expected = {
        "is_real_entry_outcome_success": True,
        "is_real_entry_outcome_no_fill": False,
        "live_ofi_reader_callable": True,
        "ladder_distribution_callable": True,
        "target_prints_callable": True,
        "catalyst_grade_strong": 3,
        "catalyst_news_max_age_const": 120,
        "catalyst_news_max_age_setting": 120.0,
    }
    for key, expected_value in expected.items():
        if results.get(key) != expected_value:
            errors.append(f"restored_helper_contract_failed:{key}:{results.get(key)!r}")
    return not errors, errors


def evaluate_premarket_binding_config(results: dict[str, Any]) -> tuple[bool, list[str]]:
    errors: list[str] = []
    pullback_interval = str(results.get("pullback_entry_interval") or "").strip().lower()
    try:
        early_min_movers = int(results.get("early_premarket_min_movers"))
    except (TypeError, ValueError):
        early_min_movers = -1
    if pullback_interval != "1m":
        errors.append(f"premarket_binding_failed:pullback_entry_interval:{pullback_interval or 'missing'}")
    if early_min_movers != 1:
        errors.append(f"premarket_binding_failed:early_premarket_min_movers:{early_min_movers}")
    return not errors, errors


def evaluate_no_active_like_sessions(results: dict[str, Any]) -> tuple[bool, list[str]]:
    if "reload_blocking_count" in results:
        try:
            blocking_count = int(results.get("reload_blocking_count") or 0)
        except (TypeError, ValueError):
            blocking_count = -1
        if blocking_count == 0:
            return True, []
        rows = results.get("reload_blocking_rows")
        sample = rows[:5] if isinstance(rows, list) else []
        passive = results.get("passive_watch_count")
        suffix = f":passive_watch_count={passive}" if passive is not None else ""
        return False, [f"reload_blocking_live_risk_present:count={blocking_count}{suffix}:sample={sample}"]

    try:
        count = int(results.get("active_like_count") or 0)
    except (TypeError, ValueError):
        count = -1
    if count == 0:
        return True, []
    rows = results.get("rows")
    sample = rows[:5] if isinstance(rows, list) else []
    return False, [f"active_like_sessions_present:count={count}:sample={sample}"]


def evaluate_premarket_readiness_script_source(results: dict[str, Any]) -> tuple[bool, list[str]]:
    """Reject host premarket scripts that can resurrect stale momentum workers."""
    errors: list[str] = []
    if not bool(results.get("exists")):
        errors.append("premarket_readiness_script_missing")
        return False, errors
    text = str(results.get("text") or "")
    lowered = text.lower()
    if "docker start $c" in lowered and "chili-clean-recovery-momentum-exec" in lowered:
        errors.append("premarket_readiness_uses_plain_docker_start_for_momentum_exec")
    required_markers = {
        "expected_image_from_env": "chili_momentum_exec_image" in lowered,
        "inspect_image": ".config.image" in lowered,
        "inspect_service_label": "com.chili.service" in lowered,
        "quarantine_rename": "pre-premarket-stale" in lowered and "docker rename" in lowered,
        "compose_momentum_exec": (
            "docker compose --profile live-momentum up -d --no-deps momentum-exec-worker" in lowered
        ),
    }
    for label, ok in required_markers.items():
        if not ok:
            errors.append(f"premarket_readiness_missing_guard:{label}")
    return not errors, errors


def _docker_containers() -> list[dict[str, Any]]:
    cmd = [
        "docker",
        "ps",
        "-a",
        "--filter",
        f"name={CANONICAL_MOMENTUM_WORKER}",
        "--filter",
        f"name={LEGACY_SPLIT_WORKER}",
        "--filter",
        f"name={LEGACY_SCHEDULER_WORKER}",
        "--filter",
        "name=chili-clean-recovery-scheduler-pre",
        "--format",
        "{{json .}}",
    ]
    listing = _run_text(cmd, timeout=10)
    ids: list[str] = []
    for line in listing.stdout.splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        name = str(row.get("Names") or row.get("Name") or "")
        if not _is_relevant_docker_container_name(name):
            continue
        container_id = str(row.get("ID") or "").strip()
        if container_id and container_id not in ids:
            ids.append(container_id)
    if not ids:
        return []
    out: list[dict[str, Any]] = []
    for container_id in ids:
        try:
            inspected = _run_text(["docker", "inspect", container_id], timeout=10)
            rows = json.loads(inspected.stdout)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError):
            continue
        if isinstance(rows, list):
            out.extend(row for row in rows if isinstance(row, dict))
    return out


def _docker_worker_lifecycle_config(container_name: str = CANONICAL_MOMENTUM_WORKER) -> dict[str, Any]:
    containers = _docker_containers()
    by_name = {_container_name(container): container for container in containers}
    container = by_name.get(container_name) or {}
    quiet_seconds = _health_stabilization_seconds(container)
    if quiet_seconds <= 0:
        return {"quiet_seconds": 0.0, "events": []}

    since_seconds = max(1, int(quiet_seconds) + 1)
    completed = _run_text(
        [
            "docker",
            "events",
            "--since",
            f"{since_seconds}s",
            "--until",
            "0s",
            "--filter",
            f"container={container_name}",
            "--format",
            "{{json .}}",
        ],
        timeout=since_seconds + 10,
    )
    events: list[dict[str, Any]] = []
    for line in completed.stdout.splitlines():
        text = line.strip()
        if not text:
            continue
        try:
            row = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            events.append(row)
    return {"quiet_seconds": quiet_seconds, "now": datetime.now(timezone.utc).isoformat(), "events": events}


def _docker_transcript_gate_config(container_name: str = CANONICAL_MOMENTUM_WORKER) -> dict[str, Any]:
    code = (
        "import json; "
        "from app.config import settings; "
        "print(json.dumps({"
        "'bridge_enabled': bool(getattr(settings, 'chili_momentum_ross_transcript_bridge_enabled', True)), "
        "'require_warrior_session_ok': bool(getattr(settings, 'chili_momentum_ross_transcript_require_warrior_session_ok', True)), "
        "'warrior_session_ok_path': getattr(settings, 'chili_momentum_ross_transcript_warrior_session_ok_path', ''), "
        "'warrior_session_ok_max_age_seconds': float(getattr(settings, 'chili_momentum_ross_transcript_warrior_session_ok_max_age_seconds', 30.0) or 0.0)"
        "}))"
    )
    completed = _run_text(["docker", "exec", container_name, "python", "-c", code], timeout=20)
    return json.loads(completed.stdout.strip())


def _docker_premarket_binding_config(container_name: str = CANONICAL_MOMENTUM_WORKER) -> dict[str, Any]:
    code = (
        "import json; "
        "from app.config import settings; "
        "print(json.dumps({"
        "'pullback_entry_interval': getattr(settings, 'chili_momentum_pullback_entry_interval', ''), "
        "'early_premarket_min_movers': getattr(settings, 'chili_momentum_early_premarket_min_movers', None)"
        "}))"
    )
    completed = _run_text(["docker", "exec", container_name, "python", "-c", code], timeout=20)
    return json.loads(completed.stdout.strip())


def _docker_source_reload_freshness_config(container_name: str = CANONICAL_MOMENTUM_WORKER) -> dict[str, Any]:
    containers = _docker_containers()
    by_name = {_container_name(container): container for container in containers}
    container = by_name.get(container_name) or {}
    state = container.get("State") if isinstance(container.get("State"), dict) else {}
    started_at = state.get("StartedAt")
    code = (
        "import json, os; "
        f"paths = {list(ROSS_CRITICAL_SOURCE_PATHS)!r}; "
        "print(json.dumps({p: os.path.getmtime(p) for p in paths if os.path.exists(p)}))"
    )
    completed = _run_text(["docker", "exec", container_name, "python", "-c", code], timeout=20)
    return {"started_at": started_at, "source_mtimes": json.loads(completed.stdout.strip())}


def _docker_compose_image_alignment_config(container_name: str = CANONICAL_MOMENTUM_WORKER) -> dict[str, Any]:
    containers = _docker_containers()
    by_name = {_container_name(container): container for container in containers}
    container = by_name.get(container_name) or {}
    config = container.get("Config") if isinstance(container.get("Config"), dict) else {}
    running_image = str(config.get("Image") or "").strip()
    completed = _run_text(["docker", "compose", "--profile", "live-momentum", "config", "momentum-exec-worker"], timeout=60)
    compose_image = ""
    for line in completed.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("image:"):
            compose_image = stripped.partition(":")[2].strip().strip('"')
            break
    return {"running_image": running_image, "compose_image": compose_image}


def _docker_compose_momentum_exec_service_config() -> dict[str, Any]:
    completed = _run_text(["docker", "compose", "--profile", "live-momentum", "config"], timeout=60)
    rendered = yaml.safe_load(completed.stdout) or {}
    services = rendered.get("services") if isinstance(rendered, dict) else {}
    service = services.get("momentum-exec-worker") if isinstance(services, dict) else None
    return service if isinstance(service, dict) else {}


def _docker_ross_event_admission_config(container_name: str = CANONICAL_MOMENTUM_WORKER) -> dict[str, Any]:
    code = (
        "import json; "
        "from app.config import settings; "
        "from app.services.trading.momentum_neural.ross_event_admission import _tick_count; "
        "print(json.dumps({"
        "'setting': int(getattr(settings, 'chili_momentum_ross_event_admission_tick_count', 0) or 0), "
        "'tick_count': int(_tick_count())"
        "}))"
    )
    completed = _run_text(["docker", "exec", container_name, "python", "-c", code], timeout=20)
    return json.loads(completed.stdout.strip())


def _docker_live_loop_timing_config(container_name: str = CANONICAL_MOMENTUM_WORKER) -> dict[str, Any]:
    loop_text = _docker_read_text(container_name, "/app/app/services/trading/momentum_neural/live_runner_loop.py")
    code = (
        "import json; "
        "from app.config import settings; "
        "print(json.dumps({"
        "'iqfeed_notify_enabled': bool(getattr(settings, 'chili_momentum_live_runner_loop_iqfeed_notify_enabled', True)), "
        "'iqfeed_tape_enabled': bool(getattr(settings, 'chili_momentum_live_runner_loop_iqfeed_tape_enabled', True)), "
        "'iqfeed_poll_fallback_enabled': bool(getattr(settings, 'chili_momentum_live_runner_loop_iqfeed_poll_fallback_enabled', True)), "
        "'iqfeed_poll_seconds': float(getattr(settings, 'chili_momentum_live_runner_loop_iqfeed_poll_seconds', 0.25) or 0.25), "
        "'min_tick_interval_ms': int(getattr(settings, 'chili_momentum_live_runner_loop_min_tick_interval_ms', 250) or 250)"
        "}))"
    )
    completed = _run_text(["docker", "exec", container_name, "python", "-c", code], timeout=20)
    out = json.loads(completed.stdout.strip())
    out["source_markers"] = {
        "has_notify_handler": "def _handle_iqfeed_notify_payload" in loop_text,
        "has_notify_admission": "self._admit_iqfeed_symbol(sym, data)" in loop_text,
        "has_refresh_viability": "refresh_viability=True" in loop_text,
        "has_immediate_notify_submit": 'cause="iqfeed_notify"' in loop_text,
        "has_iqfeed_listen_channel": 'channel = "momentum_iqfeed_l1"' in loop_text and "LISTEN" in loop_text,
    }
    return out


def _docker_ross_entry_shape_smoke(container_name: str = CANONICAL_MOMENTUM_WORKER) -> dict[str, Any]:
    text = _docker_read_text(container_name, "/app/app/services/trading/momentum_neural/live_runner.py")
    return {
        "source_markers": {
            "has_entry_shape_block": "def _ross_live_entry_shape_block" in text,
            "has_pre_candidate_block": "def _ross_live_pre_candidate_shape_block" in text,
            "has_shape_reason": "ross_live_requires_tick_tape_revalidation" in text,
            "has_pre_candidate_event": "live_entry_pre_candidate_ross_shape_block" in text,
            "has_5m_block": 'frame_used in {"1m", "5m"}' in text,
            "has_tick_label_not_enough": "if not tick_tape_revalidated" in text,
            "has_scheduler_entry_wall": "ross_equity_scheduler_entry_wall" in text,
        }
    }


def _docker_ross_reentry_smoke(container_name: str = CANONICAL_MOMENTUM_WORKER) -> dict[str, Any]:
    text = _docker_read_text(container_name, "/app/app/services/trading/momentum_neural/live_runner.py")
    start = text.find("def _live_same_session_reentry_allowed_for_session")
    block = text[start : start + 900] if start >= 0 else ""
    return {
        "source_markers": {
            "has_session_helper": bool(block),
            "has_ross_family_check": "EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP" in block,
            "has_stock_check": 'asset_class_for_symbol(symbol) == "stock"' in block,
            "has_forced_false": "return False" in block,
        }
    }


def _docker_a_setup_size_floor_smoke(container_name: str = CANONICAL_MOMENTUM_WORKER) -> dict[str, Any]:
    text = _docker_read_text(container_name, "/app/app/services/trading/momentum_neural/risk_policy.py")
    return {
        "source_markers": {
            "has_hard_reducer_reason": '"reason": "hard_reducer_respected"' in text,
            "has_hard_blocker_label": "hard_blockers" in text,
        }
    }


def _docker_ross_starter_alias_coverage_smoke(
    container_name: str = CANONICAL_MOMENTUM_WORKER,
    *,
    smoke_image: str | None = None,
) -> dict[str, Any]:
    code = r"""
import json
from app.services.trading.momentum_neural.live_runner import _entry_trace_event_payload
payload = _entry_trace_event_payload({
    "entry_trigger_reason": "ross_breakout_starter_tick",
    "entry_source_wait_reason": "ross_breakout_starter_waiting_for_level",
    "entry_pullback_high": 4.25,
    "entry_pullback_low": 4.08,
    "structural_stop_price": 4.08,
})
print(json.dumps({"setup_trace": payload.get("setup_trace", {})}, sort_keys=True))
"""
    stdout = _docker_python_code(code, container_name=container_name, image=smoke_image, timeout=40)
    return json.loads(stdout.strip().splitlines()[-1])


def _docker_ross_exit_shape_smoke(container_name: str = CANONICAL_MOMENTUM_WORKER) -> dict[str, Any]:
    text = _docker_read_text(container_name, "/app/app/services/trading/momentum_neural/live_runner.py")
    return {
        "source_markers": {
            "has_tick_tape_helper": "def _is_ross_tick_tape_entry" in text,
            "has_smart_hold_for_ross": 'getattr(settings, "chili_momentum_smart_hold_enabled", False)) or _ross_tick_tape_entry' in text,
            "legacy_bail_excludes_ross": "and not _ross_tick_tape_entry" in text,
        }
    }


def _docker_ross_symbol_resolution_smoke(
    container_name: str = CANONICAL_MOMENTUM_WORKER,
    *,
    smoke_image: str | None = None,
) -> dict[str, Any]:
    code = r"""
import json
from app.services.trading.momentum_neural.ross_transcript_bridge import symbol_resolution_warnings
snapshot = [{
    "ticker": "DXF",
    "todaysChangePerc": 64.0,
    "lastTrade": {"p": 3.41},
    "day": {"c": 3.41, "h": 3.7, "l": 2.8, "v": 14000000},
    "prevDay": {"c": 2.08, "v": 900000},
}]
print(json.dumps({
    "warnings": symbol_resolution_warnings(["DXTS"], resolved_signals={}, snapshot=snapshot)
}, default=str))
"""
    stdout = _docker_python_code(code, container_name=container_name, image=smoke_image, timeout=30)
    return json.loads(stdout.strip().splitlines()[-1])


def _docker_replay_scheduler_snapshot_smoke(
    container_name: str = CANONICAL_MOMENTUM_WORKER,
    *,
    smoke_image: str | None = None,
) -> dict[str, Any]:
    if smoke_image:
        code = r"""
import json
from pathlib import Path
from app.config import settings
from app.services.trading.momentum_neural.live_runner import (
    _emit_live_runner_replay_snapshot,
    _replay_snapshot_venue_states,
)
from app.services.trading.momentum_neural.live_replay_audit import _certification_boundary
from app.services.trading.momentum_neural.replay_v3 import (
    ReplaySchedulerTimelineResult,
    ReplaySchedulerPnLAttribution,
)
boundary = _certification_boundary(
    timeline=ReplaySchedulerTimelineResult(
        steps=[],
        selected_session_ids=[],
        terminalized_session_ids=[],
        pending_session_ids=[],
        selected_expected_pnl_usd=0.0,
        selected_expected_pnl_by_session={},
        missed_expected_pnl_usd=0.0,
        open_expected_pnl_usd=0.0,
        skipped_expected_pnl_by_reason={},
        decision_trace={},
    ),
    attribution=ReplaySchedulerPnLAttribution(
        selected_session_ids=[],
        realized_session_ids=[],
        rejected_session_ids=[],
        no_fill_session_ids=[],
        selected_without_outcome_ids=[],
        realized_pnl_usd=0.0,
        selected_expected_pnl_usd=0.0,
        missed_expected_pnl_usd=0.0,
        rejected_expected_pnl_usd=0.0,
        no_fill_expected_pnl_usd=0.0,
        open_expected_pnl_usd=0.0,
        realized_vs_selected_expected_usd=0.0,
        outcome_trace={},
    ),
    broker_outcome_count=0,
    session_row_count=1,
    opportunity_labels={
        "has_market_path_counterfactual_opportunity_labels": False,
        "complete_missed_vs_taken_outcome_labels": False,
    },
)
live_text = Path("/app/app/services/trading/momentum_neural/live_runner.py").read_text(encoding="utf-8")
audit_text = Path("/app/app/services/trading/momentum_neural/live_replay_audit.py").read_text(encoding="utf-8")
export_text = Path("/app/app/services/trading/momentum_neural/live_replay_export.py").read_text(encoding="utf-8")
loop_text = Path("/app/app/services/trading/momentum_neural/live_runner_loop.py").read_text(encoding="utf-8")
config_text = Path("/app/app/config.py").read_text(encoding="utf-8")
print(json.dumps({
    "snapshot_enabled": bool(settings.chili_momentum_live_runner_replay_snapshot_enabled),
    "emitter_callable": callable(_emit_live_runner_replay_snapshot),
    "venue_state_callable": callable(_replay_snapshot_venue_states),
    "event_type": "live_replay_scheduler_snapshot",
    "audit_contract": {
        "has_evidence_status": isinstance(boundary.get("evidence_status"), dict),
        "has_missing_evidence": isinstance(boundary.get("missing_evidence"), list),
        "has_multi_snapshot_missing_key": "multi_snapshot_scheduler_timeline" in (boundary.get("missing_evidence") or []),
        "has_counterfactual_missing_key": "market_path_counterfactual_opportunity_labels" in (boundary.get("missing_evidence") or []),
        "has_opportunity_label_missing_key": "complete_missed_vs_taken_outcome_labels" in (boundary.get("missing_evidence") or []),
        "pnl_minmax_claim_ready": boundary.get("pnl_minmax_claim_ready") is True,
    },
    "source_markers": {
        "has_first_class_flag": "chili_momentum_live_runner_replay_snapshot_enabled" in config_text,
        "has_snapshot_event_type": "live_replay_scheduler_snapshot" in live_text,
        "has_emitter": "def _emit_live_runner_replay_snapshot" in live_text,
        "has_best_effort_comment": "must never block entries" in live_text,
        "has_plan_hook": "_emit_live_runner_replay_snapshot(" in live_text and "plan_live_runner_batch_sessions" in live_text,
        "has_payload_selected_ids": '"selected_session_ids"' in live_text,
        "has_payload_prefilter_results": '"prefilter_results"' in live_text,
        "has_payload_venue_states": '"venue_states"' in live_text,
        "has_replay_evidence_status": '"evidence_status"' in audit_text,
        "has_replay_missing_evidence": '"missing_evidence"' in audit_text,
        "has_opportunity_label_evidence": "def _opportunity_label_evidence" in audit_text,
        "has_opportunity_label_export": "def build_opportunity_label_rows" in export_text,
        "has_pnl_minmax_label_gate": "complete_missed_vs_taken_outcome_labels" in audit_text,
        "has_event_snapshot_export": "live_replay_event_snapshot" in export_text,
        "has_event_loop_snapshot_emitter": "live_replay_event_snapshot" in loop_text,
    },
}, sort_keys=True))
"""
        stdout = _docker_python_code(code, container_name=container_name, image=smoke_image, timeout=40)
        return json.loads(stdout.strip().splitlines()[-1])
    live_text = _docker_read_text(container_name, "/app/app/services/trading/momentum_neural/live_runner.py")
    audit_text = _docker_read_text(container_name, "/app/app/services/trading/momentum_neural/live_replay_audit.py")
    export_text = _docker_read_text(container_name, "/app/app/services/trading/momentum_neural/live_replay_export.py")
    loop_text = _docker_read_text(container_name, "/app/app/services/trading/momentum_neural/live_runner_loop.py")
    config_text = _docker_read_text(container_name, "/app/app/config.py")
    source_markers = {
        "has_first_class_flag": "chili_momentum_live_runner_replay_snapshot_enabled" in config_text,
        "has_snapshot_event_type": "live_replay_scheduler_snapshot" in live_text,
        "has_emitter": "def _emit_live_runner_replay_snapshot" in live_text,
        "has_best_effort_comment": "must never block entries" in live_text,
        "has_plan_hook": "_emit_live_runner_replay_snapshot(" in live_text and "plan_live_runner_batch_sessions" in live_text,
        "has_payload_selected_ids": '"selected_session_ids"' in live_text,
        "has_payload_prefilter_results": '"prefilter_results"' in live_text,
        "has_payload_venue_states": '"venue_states"' in live_text,
        "has_replay_evidence_status": '"evidence_status"' in audit_text,
        "has_replay_missing_evidence": '"missing_evidence"' in audit_text,
        "has_opportunity_label_evidence": "def _opportunity_label_evidence" in audit_text,
        "has_opportunity_label_export": "def build_opportunity_label_rows" in export_text,
        "has_pnl_minmax_label_gate": "complete_missed_vs_taken_outcome_labels" in audit_text,
        "has_event_snapshot_export": "live_replay_event_snapshot" in export_text,
        "has_event_loop_snapshot_emitter": "live_replay_event_snapshot" in loop_text,
    }
    return {"source_markers": source_markers}


def _docker_restored_helper_contract_smoke(
    container_name: str = CANONICAL_MOMENTUM_WORKER,
    *,
    smoke_image: str | None = None,
) -> dict[str, Any]:
    code = r"""
import json
from app.config import settings
from app.services.trading.momentum_neural.catalyst import (
    NEWS_CATALYST_MAX_AGE_MIN,
    catalyst_grade_rank,
)
from app.services.trading.momentum_neural.outcome_labels import is_real_entry_outcome
from app.services.trading.momentum_neural.pipeline import (
    _live_ofi_microprice,
    read_ladder_distribution,
    read_target_level_trade_prints,
)
print(json.dumps({
    "is_real_entry_outcome_success": is_real_entry_outcome("success"),
    "is_real_entry_outcome_no_fill": is_real_entry_outcome("no_fill"),
    "live_ofi_reader_callable": callable(_live_ofi_microprice),
    "ladder_distribution_callable": callable(read_ladder_distribution),
    "target_prints_callable": callable(read_target_level_trade_prints),
    "catalyst_grade_strong": catalyst_grade_rank("TEST", strong_symbols={"TEST"}),
    "catalyst_news_max_age_const": NEWS_CATALYST_MAX_AGE_MIN,
    "catalyst_news_max_age_setting": float(settings.chili_momentum_news_catalyst_max_age_min),
}, sort_keys=True))
"""
    stdout = _docker_python_code(code, container_name=container_name, image=smoke_image, timeout=40)
    return json.loads(stdout.strip().splitlines()[-1])


def _docker_active_like_sessions_config(container_name: str = CANONICAL_MOMENTUM_WORKER) -> dict[str, Any]:
    code = r"""
import json
from sqlalchemy import func
from app.db import SessionLocal
from app.models.trading import TradingAutomationSession
from app.services.trading.momentum_neural.live_fsm import (
    LIVE_POSITION_HOLDING_STATES,
    LIVE_RUNNER_RUNNABLE_STATES,
    STATE_LIVE_PENDING_ENTRY,
)
db = SessionLocal()
try:
    states = set(LIVE_RUNNER_RUNNABLE_STATES) | set(LIVE_POSITION_HOLDING_STATES)
    reload_blocking_states = set(LIVE_POSITION_HOLDING_STATES) | {STATE_LIVE_PENDING_ENTRY}
    total = (
        db.query(func.count(TradingAutomationSession.id))
        .filter(
            TradingAutomationSession.mode == "live",
            TradingAutomationSession.state.in_(states),
        )
        .scalar()
        or 0
    )
    rows = (
        db.query(
            TradingAutomationSession.id,
            TradingAutomationSession.symbol,
            TradingAutomationSession.state,
            TradingAutomationSession.updated_at,
        )
        .filter(
            TradingAutomationSession.mode == "live",
            TradingAutomationSession.state.in_(states),
        )
        .order_by(TradingAutomationSession.updated_at.desc())
        .limit(20)
        .all()
    )
    blocking_total = (
        db.query(func.count(TradingAutomationSession.id))
        .filter(
            TradingAutomationSession.mode == "live",
            TradingAutomationSession.state.in_(reload_blocking_states),
        )
        .scalar()
        or 0
    )
    blocking_rows = (
        db.query(
            TradingAutomationSession.id,
            TradingAutomationSession.symbol,
            TradingAutomationSession.state,
            TradingAutomationSession.updated_at,
        )
        .filter(
            TradingAutomationSession.mode == "live",
            TradingAutomationSession.state.in_(reload_blocking_states),
        )
        .order_by(TradingAutomationSession.updated_at.desc())
        .limit(20)
        .all()
    )
    print(json.dumps({
        "active_like_count": int(total),
        "reload_blocking_count": int(blocking_total),
        "passive_watch_count": int(max(0, int(total) - int(blocking_total))),
        "rows": [
            {
                "id": int(row[0]),
                "symbol": row[1],
                "state": row[2],
                "updated_at": str(row[3]),
            }
            for row in rows
        ],
        "reload_blocking_rows": [
            {
                "id": int(row[0]),
                "symbol": row[1],
                "state": row[2],
                "updated_at": str(row[3]),
            }
            for row in blocking_rows
        ],
    }, sort_keys=True))
finally:
    db.close()
"""
    stdout = _docker_python_code(code, container_name=container_name, timeout=40)
    return json.loads(stdout.strip().splitlines()[-1])


def _host_iqfeed_bridge_notify_source() -> dict[str, Any]:
    text = (ROOT / "scripts/iqfeed_trade_bridge.py").read_text(encoding="utf-8")
    return {
        "source_markers": {
            "has_notify_enabled_flag": "IQFEED_NOTIFY_ENABLED" in text,
            "has_notify_channel": "IQFEED_NOTIFY_CHANNEL" in text and "momentum_iqfeed_l1" in text,
            "has_pg_notify_statement": "SELECT pg_notify(:channel, :payload)" in text,
            "has_notify_payload_symbol": '"symbol": sym' in text,
            "has_notify_payload_observed_at": '"observed_at"' in text,
            "has_notify_payload_source": '"source": "iqfeed_l1"' in text,
            "has_notify_after_nbbo_branch": "notify_by_symbol" in text and "IQFEED_NOTIFY_ENABLED and notify_by_symbol" in text,
        }
    }


def _host_premarket_readiness_script_source(
    path: Path = DEFAULT_PREMARKET_READINESS_SCRIPT,
) -> dict[str, Any]:
    if not path.exists():
        return {"exists": False, "path": str(path)}
    return {"exists": True, "path": str(path), "text": path.read_text(encoding="utf-8", errors="replace")}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify CHILI canonical momentum worker runtime is real and live.")
    parser.add_argument("--allow-batch-fallback", action="store_true")
    parser.add_argument("--skip-worker-lifecycle-quiet", action="store_true")
    parser.add_argument("--skip-transcript-config", action="store_true")
    parser.add_argument("--skip-premarket-binding-config", action="store_true")
    parser.add_argument("--skip-source-reload-freshness", action="store_true")
    parser.add_argument("--skip-compose-image-alignment", action="store_true")
    parser.add_argument(
        "--expected-image",
        default="",
        help="Optional exact image tag expected on the running canonical momentum worker.",
    )
    parser.add_argument("--skip-compose-service-config", action="store_true")
    parser.add_argument("--skip-ross-event-admission-config", action="store_true")
    parser.add_argument("--skip-live-loop-timing-config", action="store_true")
    parser.add_argument("--skip-ross-entry-shape-smoke", action="store_true")
    parser.add_argument("--skip-ross-reentry-smoke", action="store_true")
    parser.add_argument("--skip-a-setup-size-floor-smoke", action="store_true")
    parser.add_argument("--skip-ross-starter-alias-coverage-smoke", action="store_true")
    parser.add_argument("--skip-ross-exit-shape-smoke", action="store_true")
    parser.add_argument("--skip-ross-symbol-resolution-smoke", action="store_true")
    parser.add_argument("--skip-replay-scheduler-snapshot-smoke", action="store_true")
    parser.add_argument("--skip-restored-helper-contract-smoke", action="store_true")
    parser.add_argument("--skip-iqfeed-bridge-notify-source", action="store_true")
    parser.add_argument("--skip-premarket-readiness-script-source", action="store_true")
    parser.add_argument(
        "--require-no-active-like-sessions",
        action="store_true",
        help=(
            "Fail if live runnable/holding sessions exist. Use before worker reloads; "
            "not part of ordinary runtime health because active watchers are normal."
        ),
    )
    parser.add_argument(
        "--active-session-preflight-only",
        action="store_true",
        help=(
            "Run only the cheap active live-session reload preflight and exit. "
            "Use near market open instead of the full runtime verifier, which runs "
            "heavier import/config stages."
        ),
    )
    parser.add_argument(
        "--reload-preflight-only",
        action="store_true",
        help=(
            "Run only cheap reload-safety checks: canonical worker uniqueness/placeholder, "
            "zero active live sessions, mounted-source freshness, rendered Compose live-lane "
            "env contract, lifecycle quiet window, and optional expected image alignment. "
            "Skips import-heavy config/smoke stages."
        ),
    )
    parser.add_argument(
        "--smoke-image",
        default="",
        help=(
            "Optional image used for import-heavy smoke tests. This keeps live "
            "preflight from running nonessential imports inside the live worker."
        ),
    )
    args = parser.parse_args(argv)
    errors: list[str] = []
    if args.active_session_preflight_only:
        ok = _run_runtime_stage(
            "no_active_like_sessions",
            errors,
            lambda: evaluate_no_active_like_sessions(_docker_active_like_sessions_config()),
        )
        if ok:
            print("momentum_worker_no_reload_blocking_live_risk")
            return 0
        for err in errors:
            print(err, file=sys.stderr)
        return 1
    if args.reload_preflight_only:
        ok = _run_runtime_stage(
            "container_set",
            errors,
            lambda: evaluate_container_set(_docker_containers(), allow_batch_fallback=args.allow_batch_fallback),
        )
        if ok:
            ok = _run_runtime_stage(
                "no_active_like_sessions",
                errors,
                lambda: evaluate_no_active_like_sessions(_docker_active_like_sessions_config()),
            )
        if ok and args.expected_image:
            ok = _run_runtime_stage(
                "expected_image_alignment",
                errors,
                lambda: evaluate_expected_running_image(
                    _docker_compose_image_alignment_config(),
                    args.expected_image,
                ),
            )
        if ok and not args.skip_source_reload_freshness:
            ok = _run_runtime_stage(
                "source_reload_freshness",
                errors,
                lambda: evaluate_source_reload_freshness(_docker_source_reload_freshness_config()),
            )
        if ok and not args.skip_compose_service_config:
            ok = _run_runtime_stage(
                "compose_service_config",
                errors,
                lambda: evaluate_compose_momentum_exec_service(_docker_compose_momentum_exec_service_config()),
            )
        if ok:
            ok = _run_runtime_stage(
                "worker_lifecycle_quiet",
                errors,
                lambda: evaluate_worker_lifecycle_quiet(_docker_worker_lifecycle_config()),
            )
        if ok:
            print("momentum_worker_reload_preflight_ok")
            return 0
        for err in errors:
            print(err, file=sys.stderr)
        return 1
    ok = _run_runtime_stage(
        "container_set",
        errors,
        lambda: evaluate_container_set(_docker_containers(), allow_batch_fallback=args.allow_batch_fallback),
    )
    if ok and not args.skip_worker_lifecycle_quiet:
        ok = _run_runtime_stage(
            "worker_lifecycle_quiet",
            errors,
            lambda: evaluate_worker_lifecycle_quiet(_docker_worker_lifecycle_config()),
        )
    if ok and not args.skip_transcript_config:
        ok = _run_runtime_stage(
            "transcript_config",
            errors,
            lambda: evaluate_transcript_gate_config(_docker_transcript_gate_config()),
        )
    if ok and not args.skip_premarket_binding_config:
        ok = _run_runtime_stage(
            "premarket_binding_config",
            errors,
            lambda: evaluate_premarket_binding_config(_docker_premarket_binding_config()),
        )
    if ok and not args.skip_source_reload_freshness:
        ok = _run_runtime_stage(
            "source_reload_freshness",
            errors,
            lambda: evaluate_source_reload_freshness(_docker_source_reload_freshness_config()),
        )
    if ok and not args.skip_compose_image_alignment:
        ok = _run_runtime_stage(
            "compose_image_alignment",
            errors,
            lambda: evaluate_compose_image_alignment(_docker_compose_image_alignment_config()),
        )
    if ok and args.expected_image:
        ok = _run_runtime_stage(
            "expected_image_alignment",
            errors,
            lambda: evaluate_expected_running_image(
                _docker_compose_image_alignment_config(),
                args.expected_image,
            ),
        )
    if ok and not args.skip_compose_service_config:
        ok = _run_runtime_stage(
            "compose_service_config",
            errors,
            lambda: evaluate_compose_momentum_exec_service(_docker_compose_momentum_exec_service_config()),
        )
    if ok and not args.skip_ross_event_admission_config:
        ok = _run_runtime_stage(
            "ross_event_admission_config",
            errors,
            lambda: evaluate_ross_event_admission_config(_docker_ross_event_admission_config()),
        )
    if ok and not args.skip_live_loop_timing_config:
        ok = _run_runtime_stage(
            "live_loop_timing_config",
            errors,
            lambda: evaluate_live_loop_timing_config(_docker_live_loop_timing_config()),
        )
    if ok and not args.skip_ross_entry_shape_smoke:
        ok = _run_runtime_stage(
            "ross_entry_shape_smoke",
            errors,
            lambda: evaluate_ross_entry_shape_smoke(_docker_ross_entry_shape_smoke()),
        )
    if ok and not args.skip_ross_reentry_smoke:
        ok = _run_runtime_stage(
            "ross_reentry_smoke",
            errors,
            lambda: evaluate_ross_reentry_smoke(_docker_ross_reentry_smoke()),
        )
    if ok and not args.skip_a_setup_size_floor_smoke:
        ok = _run_runtime_stage(
            "a_setup_size_floor_smoke",
            errors,
            lambda: evaluate_a_setup_size_floor_smoke(_docker_a_setup_size_floor_smoke()),
        )
    if ok and not args.skip_ross_starter_alias_coverage_smoke:
        ok = _run_runtime_stage(
            "ross_starter_alias_coverage_smoke",
            errors,
            lambda: evaluate_ross_starter_alias_coverage_smoke(
                _docker_ross_starter_alias_coverage_smoke(smoke_image=args.smoke_image or None)
            ),
        )
    if ok and not args.skip_ross_exit_shape_smoke:
        ok = _run_runtime_stage(
            "ross_exit_shape_smoke",
            errors,
            lambda: evaluate_ross_exit_shape_smoke(_docker_ross_exit_shape_smoke()),
        )
    if ok and not args.skip_ross_symbol_resolution_smoke:
        ok = _run_runtime_stage(
            "ross_symbol_resolution_smoke",
            errors,
            lambda: evaluate_ross_symbol_resolution_smoke(
                _docker_ross_symbol_resolution_smoke(smoke_image=args.smoke_image or None)
            ),
        )
    if ok and not args.skip_replay_scheduler_snapshot_smoke:
        ok = _run_runtime_stage(
            "replay_scheduler_snapshot_smoke",
            errors,
            lambda: evaluate_replay_scheduler_snapshot_smoke(
                _docker_replay_scheduler_snapshot_smoke(smoke_image=args.smoke_image or None)
            ),
        )
    if ok and not args.skip_restored_helper_contract_smoke:
        ok = _run_runtime_stage(
            "restored_helper_contract_smoke",
            errors,
            lambda: evaluate_restored_helper_contract_smoke(
                _docker_restored_helper_contract_smoke(smoke_image=args.smoke_image or None)
            ),
        )
    if ok and not args.skip_iqfeed_bridge_notify_source:
        ok = _run_runtime_stage(
            "iqfeed_bridge_notify_source",
            errors,
            lambda: evaluate_iqfeed_bridge_notify_source(_host_iqfeed_bridge_notify_source()),
        )
    if ok and not args.skip_premarket_readiness_script_source:
        ok = _run_runtime_stage(
            "premarket_readiness_script_source",
            errors,
            lambda: evaluate_premarket_readiness_script_source(_host_premarket_readiness_script_source()),
        )
    if ok and args.require_no_active_like_sessions:
        ok = _run_runtime_stage(
            "no_active_like_sessions",
            errors,
            lambda: evaluate_no_active_like_sessions(_docker_active_like_sessions_config()),
        )
    if ok:
        print("momentum_worker_runtime_ok")
        return 0
    for err in errors:
        print(err, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
