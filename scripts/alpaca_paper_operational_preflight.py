"""Fail-closed, read-only readiness audit for the Alpaca paper execution lane.

This command never authorizes execution and never changes a feature flag.  Its
only broker operations are four public read surfaces: account, market clock,
all positions, and strict open orders.  Capture, adaptive-risk, build, and
topology evidence is checked before a staged fake-money soak can be proposed.

The result is one canonical, credential-free JSON object.  A readiness blocker
always produces a non-zero process exit status.  ``ready`` means only that the
lane is operationally staged; it is not a profitability certification and it
does not turn order execution on.
"""

from __future__ import annotations

import argparse
from dataclasses import fields
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import stat
import sys
from typing import Any, Iterable, Mapping, Sequence
import uuid

import psutil


UTC = timezone.utc
REPORT_SCHEMA_VERSION = "chili.alpaca-paper-operational-readiness.v1"
CAPTURE_BENCHMARK_SCHEMA_VERSION = "chili.replay-capture-benchmark.v4"
CAPTURE_BENCHMARK_MAX_AGE_SECONDS = 3_600.0
CAPTURE_BENCHMARK_CAPACITY_AUTHORITY = "diagnostic_only"
CAPTURE_SEAL_SCHEMA_VERSION = "chili-replay-capture-run-seal-v4"
LEGACY_CAPTURE_SEAL_SCHEMA_VERSION = "chili-replay-capture-run-seal-v3"
CAPTURE_RESOURCE_SCHEMA_VERSION = "chili-replay-capture-resource-binding-v1"
ADAPTIVE_READINESS_SCHEMA_VERSION = "chili.adaptive-risk-runtime-parity.v1"
ROSS_COVERAGE_SCHEMA_VERSION = "chili.ross-local-capture-audit.v1"
REPLAY_COVERAGE_REQUEST_SCHEMA_VERSION = "chili.replay-coverage-request.v1"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_JSON_BYTES = 64 * 1024 * 1024

REQUIRED_ADAPTIVE_SURFACES = frozenset(
    {"replay_v3", "db_paper", "alpaca_paper", "live"}
)

SETTING_NAMES = (
    "chili_alpaca_enabled",
    "chili_alpaca_paper",
    "chili_alpaca_expected_account_id",
    "chili_alpaca_data_feed",
    "chili_momentum_equity_execution_via_alpaca_paper",
    "chili_momentum_paper_runner_enabled",
    "chili_momentum_paper_runner_scheduler_enabled",
    "chili_momentum_paper_runner_dev_tick_enabled",
    "chili_momentum_live_runner_enabled",
    "chili_momentum_live_runner_scheduler_enabled",
    "chili_momentum_live_runner_loop_enabled",
    "chili_momentum_live_runner_dev_tick_enabled",
    "chili_momentum_auto_arm_live_enabled",
    "chili_momentum_auto_arm_live_scheduler_enabled",
)

FEATURE_FLAG_NAMES = tuple(
    name
    for name in SETTING_NAMES
    if name != "chili_alpaca_expected_account_id"
    and name != "chili_alpaca_data_feed"
)

MEASUREMENT_FIELDS = (
    "measured_at",
    "sample_seconds",
    "total_memory_bytes",
    "available_memory_bytes",
    "disk_free_bytes",
    "average_cpu_percent",
    "sustained_append_bytes_per_second",
    "fsync_p95_milliseconds",
    "logical_cpu_count",
    "host_fingerprint_sha256",
)


class EvidenceError(ValueError):
    """An evidence artifact is absent, malformed, stale, or not ready."""


def canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise EvidenceError("value is not canonical JSON") from exc


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _is_sha256(value: Any) -> bool:
    return bool(SHA256_RE.fullmatch(str(value or "")))


def _require_sha256(value: Any, label: str) -> str:
    digest = str(value or "")
    if not _is_sha256(digest):
        raise EvidenceError(f"{label}_invalid")
    return digest


def _read_json(path: Path | None, *, label: str) -> tuple[dict[str, Any], bytes]:
    if path is None:
        raise EvidenceError(f"{label}_missing")
    candidate = Path(path).expanduser()
    if candidate.is_symlink() or not candidate.is_file():
        raise EvidenceError(f"{label}_missing_or_unsafe")
    size = candidate.stat().st_size
    if size <= 0 or size > MAX_JSON_BYTES:
        raise EvidenceError(f"{label}_size_invalid")
    raw = candidate.read_bytes()
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"{label}_json_invalid") from exc
    if not isinstance(value, dict):
        raise EvidenceError(f"{label}_root_invalid")
    return value, raw


def _path_has_reparse_component(path: Path) -> bool:
    """Return true for a symlink, junction, or other Windows reparse hop."""

    candidate = path.absolute()
    while True:
        try:
            info = candidate.lstat()
        except OSError:
            return True
        if stat.S_ISLNK(info.st_mode):
            return True
        attributes = int(getattr(info, "st_file_attributes", 0) or 0)
        reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0) or 0)
        if reparse_flag and attributes & reparse_flag:
            return True
        parent = candidate.parent
        if parent == candidate:
            return False
        candidate = parent


def _path_is_local_fixed_storage(path: Path) -> bool:
    resolved = path.resolve(strict=True)
    if str(resolved).startswith("\\\\"):
        return False
    if os.name != "nt":
        return True
    try:
        import ctypes

        get_drive_type = ctypes.windll.kernel32.GetDriveTypeW
        get_drive_type.argtypes = [ctypes.c_wchar_p]
        get_drive_type.restype = ctypes.c_uint
        # DRIVE_FIXED is the only posture accepted for a readiness artifact.
        return int(get_drive_type(resolved.anchor)) == 3
    except Exception:
        return False


def _read_local_canonical_json(
    path: Path | None,
    *,
    label: str,
) -> tuple[dict[str, Any], bytes, Path]:
    if path is None:
        raise EvidenceError(f"{label}_missing")
    candidate = Path(path).expanduser().absolute()
    if _path_has_reparse_component(candidate):
        raise EvidenceError(f"{label}_missing_or_unsafe")
    if not _path_is_local_fixed_storage(candidate):
        raise EvidenceError(f"{label}_not_local_fixed_storage")
    payload, raw = _read_json(candidate, label=label)
    if canonical_json_bytes(payload) != raw:
        raise EvidenceError(f"{label}_not_canonical")
    return payload, raw, candidate.resolve(strict=True)


def _current_host_fingerprint(total_memory_bytes: int) -> str:
    material = {
        "logical_cpu_count": psutil.cpu_count(logical=True),
        "machine": platform.machine(),
        "node": platform.node(),
        "platform": platform.platform(),
        "processor": platform.processor(),
        "total_memory_bytes": int(total_memory_bytes),
    }
    return sha256_json(material)


def _strict_bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise EvidenceError(f"{label}_not_boolean")
    return value


def _finite_positive(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise EvidenceError(f"{label}_invalid")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise EvidenceError(f"{label}_invalid") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise EvidenceError(f"{label}_invalid")
    return parsed


def _parse_aware_datetime(value: Any, label: str) -> datetime:
    raw = str(value or "").strip()
    if not raw:
        raise EvidenceError(f"{label}_missing")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvidenceError(f"{label}_invalid") from exc
    if parsed.tzinfo is None:
        raise EvidenceError(f"{label}_timezone_missing")
    return parsed.astimezone(UTC)


def settings_snapshot(settings_object: Any) -> dict[str, Any]:
    """Read only the non-secret topology fields used by this audit."""

    return {name: getattr(settings_object, name, None) for name in SETTING_NAMES}


def _sanitized_settings_material(values: Mapping[str, Any]) -> dict[str, Any]:
    expected = str(values.get("chili_alpaca_expected_account_id") or "").strip()
    material: dict[str, Any] = {
        name: values.get(name)
        for name in SETTING_NAMES
        if name != "chili_alpaca_expected_account_id"
    }
    material["expected_account_id_sha256"] = (
        _sha256_bytes(expected.encode("utf-8")) if expected else None
    )
    return material


def calculate_code_build_sha256(repo_root: Path) -> str:
    """Hash the exact application source tree without exposing its contents."""

    root = Path(repo_root).resolve()
    app_root = root / "app"
    if not app_root.is_dir():
        raise EvidenceError("code_build_app_tree_missing")
    paths = sorted(path for path in app_root.rglob("*.py") if path.is_file())
    if not paths:
        raise EvidenceError("code_build_app_tree_empty")
    rows = [
        {
            "path": path.relative_to(root).as_posix(),
            "sha256": _sha256_bytes(path.read_bytes()),
        }
        for path in paths
    ]
    requirements = root / "requirements.txt"
    if requirements.is_file():
        rows.append(
            {
                "path": "requirements.txt",
                "sha256": _sha256_bytes(requirements.read_bytes()),
            }
        )
    return sha256_json(sorted(rows, key=lambda row: row["path"]))


def provenance_payload(
    *, repo_root: Path, settings_values: Mapping[str, Any]
) -> dict[str, Any]:
    sanitized = _sanitized_settings_material(settings_values)
    feature_flags = {name: settings_values.get(name) for name in FEATURE_FLAG_NAMES}
    return {
        "code_build_sha256": calculate_code_build_sha256(repo_root),
        "config_sha256": sha256_json(sanitized),
        "feature_flags_sha256": sha256_json(feature_flags),
        "expected_account_id_sha256": sanitized["expected_account_id_sha256"],
    }


def inspect_topology(
    settings_values: Mapping[str, Any], *, mode: str
) -> tuple[dict[str, Any], list[str]]:
    blockers: list[str] = []
    bool_names = tuple(
        name
        for name in SETTING_NAMES
        if name not in {"chili_alpaca_expected_account_id", "chili_alpaca_data_feed"}
    )
    parsed: dict[str, bool] = {}
    for name in bool_names:
        value = settings_values.get(name)
        if not isinstance(value, bool):
            blockers.append(f"topology_{name}_unreadable")
            continue
        parsed[name] = value

    expected = str(settings_values.get("chili_alpaca_expected_account_id") or "").strip()
    pin_valid = False
    if expected:
        try:
            pin_valid = str(uuid.UUID(expected)) == expected
        except ValueError:
            pin_valid = False
    if not pin_valid:
        blockers.append("alpaca_expected_account_uuid_missing_or_invalid")
    if parsed.get("chili_alpaca_enabled") is not True:
        blockers.append("alpaca_adapter_disabled")
    if parsed.get("chili_alpaca_paper") is not True:
        blockers.append("alpaca_paper_posture_required")
    if parsed.get("chili_momentum_equity_execution_via_alpaca_paper") is not True:
        blockers.append("alpaca_equity_route_not_staged")
    if parsed.get("chili_momentum_live_runner_enabled") is not False:
        blockers.append("broker_live_runner_must_remain_off")
    if parsed.get("chili_momentum_auto_arm_live_enabled") is not False:
        blockers.append("auto_arm_must_remain_off")
    if (
        mode == "staged-alpaca-soak"
        and parsed.get("chili_momentum_paper_runner_enabled") is not False
    ):
        blockers.append("legacy_db_paper_runner_conflict")

    return (
        {
            "mode": mode,
            "alpaca_enabled": parsed.get("chili_alpaca_enabled"),
            "alpaca_paper": parsed.get("chili_alpaca_paper"),
            "alpaca_equity_route_staged": parsed.get(
                "chili_momentum_equity_execution_via_alpaca_paper"
            ),
            "broker_live_runner_enabled": parsed.get(
                "chili_momentum_live_runner_enabled"
            ),
            "auto_arm_enabled": parsed.get("chili_momentum_auto_arm_live_enabled"),
            "legacy_db_paper_runner_enabled": parsed.get(
                "chili_momentum_paper_runner_enabled"
            ),
            "expected_account_uuid_pin_present": bool(pin_valid),
        },
        blockers,
    )


def probe_alpaca_read_only(
    adapter: Any,
    *,
    expected_account_id: str,
) -> tuple[dict[str, Any], list[str]]:
    """Use exactly four non-mutating adapter calls and sanitize their result."""

    blockers: list[str] = []
    account: Any = None
    clock: Any = None
    positions: Any = None
    orders: Any = None

    try:
        account = adapter.get_account_snapshot()
    except Exception:
        blockers.append("alpaca_account_read_failed")
    try:
        clock = adapter.get_market_clock_snapshot()
    except Exception:
        blockers.append("alpaca_clock_read_failed")
    try:
        positions, _positions_meta = adapter.list_positions()
    except Exception:
        blockers.append("alpaca_positions_read_failed")
    try:
        orders, _orders_meta = adapter.list_open_orders(strict=True)
    except Exception:
        blockers.append("alpaca_open_orders_read_failed")

    account_ok = False
    identity_match = False
    active_unblocked = False
    equity: float | None = None
    buying_power: float | None = None
    if not isinstance(account, Mapping) or account.get("ok") is not True:
        blockers.append("alpaca_account_unreadable")
    else:
        observed = str(account.get("account_id") or "").strip()
        identity_match = bool(
            expected_account_id and observed and observed == expected_account_id
        )
        if not identity_match:
            blockers.append("alpaca_account_identity_mismatch")
        if account.get("paper") is not True:
            blockers.append("alpaca_account_not_confirmed_paper")
        if str(account.get("status") or "").strip().upper() != "ACTIVE":
            blockers.append("alpaca_account_not_active")
        blocked_keys = (
            "account_blocked",
            "trading_blocked",
            "transfers_blocked",
            "trade_suspended_by_user",
        )
        blocked_values: list[bool] = []
        for key in blocked_keys:
            value = account.get(key)
            if not isinstance(value, bool):
                blockers.append(f"alpaca_{key}_unreadable")
            else:
                blocked_values.append(value)
                if value:
                    blockers.append(f"alpaca_{key}")
        active_unblocked = (
            len(blocked_values) == len(blocked_keys)
            and not any(blocked_values)
            and str(account.get("status") or "").strip().upper() == "ACTIVE"
        )
        try:
            equity = _finite_positive(account.get("equity"), "alpaca_equity")
        except EvidenceError:
            blockers.append("alpaca_equity_unreadable")
        try:
            buying_power = _finite_positive(
                account.get("buying_power"), "alpaca_buying_power"
            )
        except EvidenceError:
            blockers.append("alpaca_buying_power_unreadable")
        account_ok = (
            identity_match
            and account.get("paper") is True
            and active_unblocked
            and equity is not None
            and buying_power is not None
        )

    market_open: bool | None = None
    clock_ok = False
    if not isinstance(clock, Mapping) or clock.get("ok") is not True:
        blockers.append("alpaca_clock_unreadable")
    else:
        if not isinstance(clock.get("is_open"), bool):
            blockers.append("alpaca_clock_open_state_unreadable")
        else:
            market_open = bool(clock["is_open"])
        try:
            _parse_aware_datetime(clock.get("timestamp"), "alpaca_clock_timestamp")
            _parse_aware_datetime(clock.get("next_open"), "alpaca_clock_next_open")
            _parse_aware_datetime(clock.get("next_close"), "alpaca_clock_next_close")
            clock_ok = market_open is not None and clock.get("paper") is True
            if clock.get("paper") is not True:
                blockers.append("alpaca_clock_not_confirmed_paper")
        except EvidenceError:
            blockers.append("alpaca_clock_timestamps_unreadable")

    if positions is None:
        if "alpaca_positions_read_failed" not in blockers:
            blockers.append("alpaca_positions_unreadable")
        position_count: int | None = None
    elif not isinstance(positions, Sequence) or isinstance(positions, (str, bytes)):
        blockers.append("alpaca_positions_malformed")
        position_count = None
    else:
        position_count = len(positions)
        if position_count:
            # This public read does not carry a durable CHILI ownership/reconciliation
            # proof.  Any exposure is therefore unknown and must block staged entry.
            blockers.append("alpaca_unknown_positions_present")

    if orders is None:
        if "alpaca_open_orders_read_failed" not in blockers:
            blockers.append("alpaca_open_orders_unreadable")
        order_count: int | None = None
    elif not isinstance(orders, Sequence) or isinstance(orders, (str, bytes)):
        blockers.append("alpaca_open_orders_malformed")
        order_count = None
    else:
        order_count = len(orders)
        if order_count:
            blockers.append("alpaca_unknown_open_orders_present")

    return (
        {
            "account_readable": bool(isinstance(account, Mapping) and account.get("ok") is True),
            "account_identity_match": identity_match,
            "account_active_unblocked": active_unblocked,
            "account_operational": account_ok,
            "equity_readable_positive": equity is not None,
            "buying_power_readable_positive": buying_power is not None,
            "clock_readable": clock_ok,
            "market_open": market_open,
            "market_closed_is_nonblocking": market_open is False,
            "position_count": position_count,
            "open_order_count": order_count,
            "flat_and_no_open_orders": position_count == 0 and order_count == 0,
        },
        blockers,
    )


def _validate_benchmark_writer_health(
    health: Any,
    *,
    expected_events: int | None,
    label: str,
) -> int:
    if not isinstance(health, Mapping):
        raise EvidenceError(f"{label}_health_missing")
    ingress = health.get("ingress")
    resource = health.get("resource")
    if not isinstance(ingress, Mapping) or not isinstance(resource, Mapping):
        raise EvidenceError(f"{label}_nested_health_missing")
    written = int(health.get("events_written") or 0)
    if written < 0 or (expected_events is not None and written != expected_events):
        raise EvidenceError(f"{label}_event_reconciliation_failed")
    if (
        health.get("stopped_cleanly") is not True
        or health.get("last_error") not in (None, "")
        or bool(health.get("last_errors"))
    ):
        raise EvidenceError(f"{label}_not_clean")
    for name in (
        "dropped",
        "write_bandwidth_dropped",
        "reported_gap_lost",
        "post_close_submissions",
    ):
        if int(ingress.get(name) or 0) != 0:
            raise EvidenceError(f"{label}_{name}")
    if resource.get("fail_closed") is True or resource.get("resource_failure_reasons"):
        raise EvidenceError(f"{label}_resource_health_failed")
    sync = resource.get("sync")
    if isinstance(sync, Mapping) and (
        int(sync.get("failures") or 0) != 0
        or int(sync.get("dirty_objects") or 0) != 0
    ):
        raise EvidenceError(f"{label}_durability_health_failed")
    return written


def validate_capture_benchmark(
    path: Path | None,
    *,
    repo_root: Path,
    expected_artifact_sha256: str | None,
    evaluated_at: datetime | None = None,
    maximum_age_seconds: float = CAPTURE_BENCHMARK_MAX_AGE_SECONDS,
) -> tuple[dict[str, Any], dict[str, Any]]:
    expected_artifact = _require_sha256(
        expected_artifact_sha256, "capture_benchmark_expected_sha256"
    )
    payload, raw, artifact_path = _read_local_canonical_json(
        path, label="capture_benchmark"
    )
    artifact_hash = _sha256_bytes(raw)
    if artifact_hash != expected_artifact or artifact_path.stem != expected_artifact:
        raise EvidenceError("capture_benchmark_external_digest_mismatch")
    if payload.get("benchmark_schema_version") != CAPTURE_BENCHMARK_SCHEMA_VERSION:
        raise EvidenceError("capture_benchmark_schema_unsupported")

    source = payload.get("capture_runtime_source")
    if not isinstance(source, Mapping):
        raise EvidenceError("capture_benchmark_source_missing")
    contract_hash = _require_sha256(source.get("contract_sha256"), "capture_contract_hash")
    runtime_hash = _require_sha256(source.get("runtime_sha256"), "capture_runtime_hash")
    benchmark_hash = _require_sha256(
        source.get("benchmark_script_sha256"), "capture_benchmark_script_hash"
    )
    repository = Path(repo_root)
    source_root = repository / "app/services/trading/momentum_neural"
    expected_contract = _sha256_bytes(
        (source_root / "replay_capture_contract.py").read_bytes()
    )
    expected_runtime = _sha256_bytes(
        (source_root / "replay_capture_runtime.py").read_bytes()
    )
    expected_benchmark = _sha256_bytes(
        (repository / "scripts/benchmark_replay_capture_runtime.py").read_bytes()
    )
    if (
        contract_hash != expected_contract
        or runtime_hash != expected_runtime
        or benchmark_hash != expected_benchmark
    ):
        raise EvidenceError("capture_benchmark_code_generation_mismatch")

    acceptance = payload.get("acceptance")
    if acceptance != {"accepted": True, "reasons": []}:
        raise EvidenceError("capture_benchmark_not_accepted")
    authority = payload.get("authority")
    if not isinstance(authority, Mapping) or dict(authority) != {
        "capacity_authority": CAPTURE_BENCHMARK_CAPACITY_AUTHORITY,
        "empirical_calibration_receipt_sha256": None,
        "hot_symbol_limit_authorized": False,
        "reasons": [
            "empirical_hot_symbol_calibration_receipt_unavailable",
            "full_runner_watcher_resource_calibration_unavailable",
            "writer_scaling_calibration_unavailable",
        ],
        "watcher_limit_authorized": False,
        "writer_limit_authorized": False,
    }:
        raise EvidenceError("capture_benchmark_capacity_authority_invalid")

    measurement = payload.get("resource_measurement")
    if not isinstance(measurement, Mapping):
        raise EvidenceError("capture_benchmark_measurement_missing")
    measurement_core = {name: measurement.get(name) for name in MEASUREMENT_FIELDS}
    measurement_hash = _require_sha256(
        measurement.get("measurement_sha256"), "capture_measurement_hash"
    )
    measured_at = _parse_aware_datetime(
        measurement_core["measured_at"], "capture_measured_at"
    )
    now = (evaluated_at or datetime.now(UTC)).astimezone(UTC)
    try:
        max_age = float(maximum_age_seconds)
    except (TypeError, ValueError) as exc:
        raise EvidenceError("capture_benchmark_max_age_invalid") from exc
    if not math.isfinite(max_age) or max_age <= 0:
        raise EvidenceError("capture_benchmark_max_age_invalid")
    age_seconds = (now - measured_at).total_seconds()
    if age_seconds < 0:
        raise EvidenceError("capture_benchmark_measurement_from_future")
    if age_seconds > max_age:
        raise EvidenceError("capture_benchmark_measurement_stale")

    generated_at = _parse_aware_datetime(
        payload.get("generated_at"), "capture_benchmark_generated_at"
    )
    if generated_at < measured_at or generated_at > now:
        raise EvidenceError("capture_benchmark_generation_clock_invalid")
    freshness = payload.get("artifact_freshness")
    if not isinstance(freshness, Mapping):
        raise EvidenceError("capture_benchmark_freshness_missing")
    emit_age = (generated_at - measured_at).total_seconds()
    try:
        reported_emit_age = float(freshness.get("age_seconds_at_emit"))
        reported_max_age = float(freshness.get("max_age_seconds"))
    except (TypeError, ValueError) as exc:
        raise EvidenceError("capture_benchmark_freshness_invalid") from exc
    if (
        freshness.get("fresh_at_emit") is not True
        or not math.isclose(reported_emit_age, emit_age, rel_tol=0.0, abs_tol=1e-6)
        or not math.isfinite(reported_max_age)
        or reported_max_age <= 0
        or reported_max_age > max_age
        or emit_age > reported_max_age
    ):
        raise EvidenceError("capture_benchmark_freshness_invalid")
    # The consumer ceiling is an upper bound, not permission to outlive a
    # stricter lifetime selected when the exact benchmark was produced.
    if age_seconds > reported_max_age:
        raise EvidenceError("capture_benchmark_measurement_stale")

    try:
        from app.services.trading.momentum_neural import replay_capture_contract as contract
        from app.services.trading.momentum_neural import replay_capture_runtime as runtime

        typed_values = dict(measurement_core)
        typed_values["measured_at"] = measured_at
        typed_measurement = runtime.CaptureResourceMeasurement(**typed_values)
    except Exception as exc:
        raise EvidenceError("capture_benchmark_typed_measurement_invalid") from exc
    if typed_measurement.measurement_sha256 != measurement_hash:
        raise EvidenceError("capture_benchmark_measurement_hash_mismatch")
    current_host = _current_host_fingerprint(typed_measurement.total_memory_bytes)
    environment = payload.get("environment")
    if not isinstance(environment, Mapping):
        raise EvidenceError("capture_benchmark_environment_missing")
    if (
        typed_measurement.host_fingerprint_sha256 != current_host
        or int(typed_measurement.logical_cpu_count)
        != int(psutil.cpu_count(logical=True) or 1)
        or environment.get("measurement_host_fingerprint_sha256") != current_host
        or environment.get("current_host_fingerprint_sha256") != current_host
        or environment.get("host_fingerprint_matches") is not True
    ):
        raise EvidenceError("capture_benchmark_current_host_mismatch")

    binding_payload = payload.get("resolved_resource_binding")
    if not isinstance(binding_payload, Mapping):
        raise EvidenceError("capture_benchmark_resource_binding_missing")
    policy_payload = binding_payload.get("policy")
    if not isinstance(policy_payload, Mapping):
        raise EvidenceError("capture_benchmark_resource_policy_missing")
    try:
        typed_policy = runtime.CaptureBudgetPolicy(**dict(policy_payload))
        typed_binding = runtime.CaptureResourceBinding.resolve(
            typed_measurement, typed_policy
        )
        expected_binding = json.loads(
            contract.canonical_json_bytes(
                {
                    **typed_binding.to_record(),
                    "binding_sha256": typed_binding.binding_sha256,
                    "hashes": typed_binding.hashes,
                    "max_writer_threads": typed_binding.budget.max_writer_threads,
                }
            ).decode("utf-8")
        )
    except Exception as exc:
        raise EvidenceError("capture_benchmark_typed_binding_invalid") from exc
    if dict(binding_payload) != expected_binding:
        raise EvidenceError("capture_benchmark_resource_binding_mismatch")

    enqueue = payload.get("enqueue")
    writer = payload.get("writer")
    if not isinstance(enqueue, Mapping) or not isinstance(writer, Mapping):
        raise EvidenceError("capture_benchmark_counters_missing")
    submitted = int(enqueue.get("submitted") or 0)
    accepted = int(enqueue.get("accepted") or 0)
    if submitted <= 0 or accepted != submitted:
        raise EvidenceError("capture_benchmark_event_loss")
    written = _validate_benchmark_writer_health(
        writer.get("health"), expected_events=accepted, label="capture_benchmark_writer"
    )

    parameters = payload.get("parameters")
    shared = payload.get("shared_store_validation")
    if not isinstance(parameters, Mapping) or not isinstance(shared, Mapping):
        raise EvidenceError("capture_benchmark_shared_validation_missing")
    requested_writers = int(parameters.get("writers") or 0)
    shared_accepted = int(shared.get("accepted_events") or 0)
    aggregate = shared.get("aggregate_admission")
    writer_health_rows = shared.get("writer_health")
    if (
        requested_writers < 2
        or shared.get("executed") is not True
        or int(shared.get("requested_identity_count") or 0) != requested_writers
        or int(shared.get("identity_count") or 0) != requested_writers
        or shared.get("resource_binding_sha256") != typed_binding.binding_sha256
        or shared.get("writers_stopped_cleanly") is not True
        or shared.get("survivor_store_access_after_first_release") is not True
        or not isinstance(aggregate, Mapping)
        or not isinstance(writer_health_rows, list)
        or len(writer_health_rows) != requested_writers
    ):
        raise EvidenceError("capture_benchmark_shared_validation_not_clean")
    shared_written = sum(
        _validate_benchmark_writer_health(
            row, expected_events=None, label="capture_benchmark_shared_writer"
        )
        for row in writer_health_rows
    )
    if (
        shared_accepted <= 0
        or shared_written != shared_accepted
        or int(aggregate.get("completed") or 0) != shared_accepted
        or int(aggregate.get("outstanding_events") or 0) != 0
        or int(aggregate.get("outstanding_bytes") or 0) != 0
        or bool(aggregate.get("rejections"))
    ):
        raise EvidenceError("capture_benchmark_shared_validation_not_clean")

    summary = {
        "schema_version": CAPTURE_BENCHMARK_SCHEMA_VERSION,
        "artifact_sha256": artifact_hash,
        "measurement_sha256": measurement_hash,
        "binding_sha256": typed_binding.binding_sha256,
        "contract_sha256": contract_hash,
        "runtime_sha256": runtime_hash,
        "benchmark_script_sha256": benchmark_hash,
        "submitted_events": submitted,
        "written_events": written,
        "shared_written_events": shared_written,
        "writer_clean": True,
        "resource_health_clean": True,
        "current_host_verified": True,
        "fresh_at_validation": True,
        "capacity_authority": CAPTURE_BENCHMARK_CAPACITY_AUTHORITY,
        "capacity_limits_authorized": False,
        "empirical_calibration_receipt_verified": False,
        "ready": False,
        "reason": "capture_capacity_calibration_unavailable",
    }
    normalized_measurement = json.loads(
        contract.canonical_json_bytes(typed_binding.to_record()["measurement"]).decode(
            "utf-8"
        )
    )
    return summary, normalized_measurement


def _resolved_budget(measurement: Mapping[str, Any], policy: Mapping[str, Any]) -> dict[str, Any]:
    try:
        memory_headroom = int(measurement["available_memory_bytes"]) - int(
            policy["memory_reserve_bytes"]
        )
        disk_headroom = int(measurement["disk_free_bytes"]) - int(
            policy["disk_reserve_bytes"]
        )
        capture_memory = int(
            memory_headroom * float(policy["capture_fraction_of_memory_headroom"])
        )
        ring_bytes = int(capture_memory * float(policy["ring_fraction_of_capture_memory"]))
        queue_bytes = int(capture_memory * float(policy["queue_fraction_of_capture_memory"]))
        hot_bytes = capture_memory - ring_bytes - queue_bytes
        hot_capacity = hot_bytes // int(policy["calibrated_hot_symbol_bytes"])
        disk_quota = int(disk_headroom * float(policy["capture_fraction_of_disk_headroom"]))
        write_budget = int(
            float(measurement["sustained_append_bytes_per_second"])
            * float(policy["capture_fraction_of_measured_write_bandwidth"])
        )
    except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
        raise EvidenceError("capture_resource_policy_malformed") from exc
    if min(
        memory_headroom,
        disk_headroom,
        capture_memory,
        ring_bytes,
        queue_bytes,
        hot_bytes,
        hot_capacity,
        disk_quota,
        write_budget,
    ) <= 0:
        raise EvidenceError("capture_resource_budget_empty")
    return {
        "measurement_sha256": sha256_json(dict(measurement)),
        "policy_sha256": sha256_json(dict(policy)),
        "capture_memory_bytes": capture_memory,
        "pretrigger_ring_bytes": ring_bytes,
        "async_queue_bytes": queue_bytes,
        "hot_symbol_state_bytes": hot_bytes,
        "derived_hot_symbol_capacity": hot_capacity,
        "disk_quota_bytes": disk_quota,
        "sustained_write_budget_bytes_per_second": write_budget,
        "max_queue_events": int(policy["max_queue_events"]),
        "max_ring_events": int(policy["max_ring_events"]),
        "max_gap_keys": int(policy["max_gap_keys"]),
        "raw_retention_days": int(policy["raw_retention_days"]),
        "derived_retention_days": int(policy["derived_retention_days"]),
    }


def _validate_resource_binding(
    capture_root: Path,
    *,
    benchmark_measurement: Mapping[str, Any],
    benchmark_measurement_sha256: str,
) -> dict[str, Any]:
    audit_root = capture_root / "resource_audits"
    if not audit_root.is_dir():
        raise EvidenceError("capture_resource_binding_missing")
    matches: list[tuple[dict[str, Any], bytes, Path]] = []
    for path in sorted(audit_root.glob("*.json")):
        payload, raw = _read_json(path, label="capture_resource_binding")
        if payload.get("measurement_sha256") == benchmark_measurement_sha256:
            matches.append((payload, raw, path))
    if len(matches) != 1:
        raise EvidenceError("capture_resource_binding_not_unique_for_benchmark")
    payload, raw, path = matches[0]
    if payload.get("schema_version") != CAPTURE_RESOURCE_SCHEMA_VERSION:
        raise EvidenceError("capture_resource_binding_schema_unsupported")
    raw_hash = _sha256_bytes(raw)
    if raw_hash != path.stem or canonical_json_bytes(payload) != raw:
        raise EvidenceError("capture_resource_binding_content_address_mismatch")
    measurement = payload.get("measurement")
    policy = payload.get("policy")
    budget = payload.get("budget")
    if not all(isinstance(row, Mapping) for row in (measurement, policy, budget)):
        raise EvidenceError("capture_resource_binding_sections_missing")
    if dict(measurement) != dict(benchmark_measurement):
        raise EvidenceError("capture_resource_measurement_does_not_match_benchmark")
    if sha256_json(dict(measurement)) != payload.get("measurement_sha256"):
        raise EvidenceError("capture_resource_measurement_hash_mismatch")
    if sha256_json(dict(policy)) != payload.get("policy_sha256"):
        raise EvidenceError("capture_resource_policy_hash_mismatch")
    expected_budget = _resolved_budget(measurement, policy)
    if dict(budget) != expected_budget:
        raise EvidenceError("capture_resource_budget_resolution_mismatch")
    if sha256_json(dict(budget)) != payload.get("budget_sha256"):
        raise EvidenceError("capture_resource_budget_hash_mismatch")
    return {
        "binding_sha256": raw_hash,
        "measurement_sha256": str(payload["measurement_sha256"]),
        "policy_sha256": str(payload["policy_sha256"]),
        "budget_sha256": str(payload["budget_sha256"]),
        "finite_budget": True,
    }


def _load_official_resource_binding_record(
    capture_root: Path,
    *,
    benchmark_measurement: Mapping[str, Any],
    benchmark_measurement_sha256: str,
    runtime: Any,
    contract: Any,
) -> tuple[Any, dict[str, Any]]:
    """Recompute the v4 resource binding using immutable typed APIs only."""

    audit_root = capture_root / "resource_audits"
    if not audit_root.is_dir():
        raise EvidenceError("capture_resource_binding_missing")
    matches: list[tuple[dict[str, Any], bytes, Path]] = []
    for path in sorted(audit_root.glob("*.json")):
        payload, raw = _read_json(path, label="capture_resource_binding")
        if payload.get("measurement_sha256") == benchmark_measurement_sha256:
            matches.append((payload, raw, path))
    if len(matches) != 1:
        raise EvidenceError("capture_resource_binding_not_unique_for_benchmark")
    payload, raw, path = matches[0]
    measurement_payload = payload.get("measurement")
    policy_payload = payload.get("policy")
    if not isinstance(measurement_payload, Mapping) or not isinstance(
        policy_payload, Mapping
    ):
        raise EvidenceError("capture_resource_binding_sections_missing")
    try:
        measurement_values = dict(measurement_payload)
        measurement_values["measured_at"] = _parse_aware_datetime(
            measurement_values.get("measured_at"), "capture_resource_measured_at"
        )
        measurement = runtime.CaptureResourceMeasurement(**measurement_values)
        policy = runtime.CaptureBudgetPolicy(**dict(policy_payload))
        binding = runtime.CaptureResourceBinding.resolve(measurement, policy)
    except Exception as exc:
        raise EvidenceError("capture_resource_binding_typed_recomputation_failed") from exc
    if measurement.measurement_sha256 != benchmark_measurement_sha256:
        raise EvidenceError("capture_resource_measurement_does_not_match_benchmark")
    if dict(benchmark_measurement) != dict(measurement_payload):
        raise EvidenceError("capture_resource_measurement_does_not_match_benchmark")
    if contract.canonical_json_bytes(binding.to_record()) != raw:
        raise EvidenceError("capture_resource_binding_recomputation_mismatch")
    if binding.binding_sha256 != path.stem:
        raise EvidenceError("capture_resource_binding_content_address_mismatch")
    return binding, {
        "binding_sha256": binding.binding_sha256,
        **binding.hashes,
        "finite_budget": True,
        "typed_runtime_recomputed": True,
    }


def _capture_compression_codec(seal_payload: Mapping[str, Any]) -> str:
    objects = seal_payload.get("objects")
    if not isinstance(objects, list) or not objects:
        raise EvidenceError("capture_seal_objects_missing")
    suffixes: set[str] = set()
    for row in objects:
        if not isinstance(row, Mapping):
            raise EvidenceError("capture_seal_object_invalid")
        if row.get("kind") not in {"event_chunk", "gap_chunk"}:
            continue
        suffix = Path(str(row.get("relative_path") or "")).suffix.lower()
        if suffix not in {".zlib", ".zst"}:
            raise EvidenceError("capture_seal_object_compression_unknown")
        suffixes.add(suffix)
    if len(suffixes) != 1:
        raise EvidenceError("capture_seal_compression_not_unique")
    return "zlib" if suffixes == {".zlib"} else "zstd"


def _load_replay_coverage_request(
    path: Path | None,
    *,
    contract: Any,
    expected_final_seal_sha256: str,
    expected_identity_sha256: str,
) -> tuple[Any, str]:
    payload, raw = _read_json(path, label="capture_coverage_request")
    expected_fields = {
        "schema_version",
        "expected_final_seal_sha256",
        "expected_identity_sha256",
        "warmup_start_at",
        "decision_at",
        "exit_end_at",
        "required_streams",
        "decision_id",
        "decision_checkpoint_sha256",
        "required_read_ids",
        "symbol",
        "network_fallback_policy",
        "replay_driver",
    }
    if set(payload) != expected_fields:
        raise EvidenceError("capture_coverage_request_schema_invalid")
    if payload.get("schema_version") != REPLAY_COVERAGE_REQUEST_SCHEMA_VERSION:
        raise EvidenceError("capture_coverage_request_schema_unsupported")
    if canonical_json_bytes(payload) != raw:
        raise EvidenceError("capture_coverage_request_not_canonical")
    if payload.get("expected_final_seal_sha256") != expected_final_seal_sha256:
        raise EvidenceError("capture_coverage_request_seal_mismatch")
    if payload.get("expected_identity_sha256") != expected_identity_sha256:
        raise EvidenceError("capture_coverage_request_identity_mismatch")
    if payload.get("network_fallback_policy") != "deny":
        raise EvidenceError("capture_replay_network_fallback_not_denied")
    if payload.get("replay_driver") != "ReplayV3":
        raise EvidenceError("capture_replay_driver_mismatch")
    streams = payload.get("required_streams")
    read_ids = payload.get("required_read_ids")
    if not isinstance(streams, list) or not isinstance(read_ids, list):
        raise EvidenceError("capture_coverage_request_sets_invalid")
    request = contract.ReplayCoverageRequest(
        warmup_start_at=_parse_aware_datetime(
            payload.get("warmup_start_at"), "capture_warmup_start_at"
        ),
        decision_at=_parse_aware_datetime(
            payload.get("decision_at"), "capture_request_decision_at"
        ),
        exit_end_at=_parse_aware_datetime(
            payload.get("exit_end_at"), "capture_exit_end_at"
        ),
        required_streams=frozenset(
            contract.CaptureStream(str(stream)) for stream in streams
        ),
        decision_id=str(payload.get("decision_id") or ""),
        decision_checkpoint_sha256=str(
            payload.get("decision_checkpoint_sha256") or ""
        ),
        required_read_ids=frozenset(str(read_id) for read_id in read_ids),
        symbol=payload.get("symbol"),
        expected_identity_sha256=str(payload.get("expected_identity_sha256") or ""),
    )
    return request, _sha256_bytes(raw)


def _manifest_from_verified_capture(
    *,
    contract: Any,
    verified: Any,
    request: Any,
) -> Any:
    """Reconstruct typed control views only from the exact sealed inventory.

    ``CaptureCoverageManifest.from_verified_capture`` is the attesting builder;
    this helper merely parses its caller-friendly typed views.  It deliberately
    selects one exact decision and refuses ambiguous per-stream coverage instead
    of choosing whichever control event happened to be encountered last.
    """

    checkpoint_candidates: list[Any] = []
    receipts_by_id: dict[str, Any] = {}
    coverage_by_stream: dict[Any, Any] = {}
    coverage_control_fields = {
        "stream",
        "identity_sha256",
        "provider",
        "first_available_at",
        "last_available_at",
        "event_count",
        "exact_event_clock_complete",
        "content_verified",
        "continuity_complete",
        "watermark",
        "query_receipt_count",
        "symbol",
    }

    for event in sorted(verified.events, key=lambda row: row.sequence):
        payload = event.payload
        if event.stream is contract.CaptureStream.FSM_DECISION:
            if str(payload.get("decision_id") or "").strip() != request.decision_id:
                continue
            try:
                checkpoint = contract.CaptureDecisionCheckpoint(
                    identity_sha256=verified.identity.identity_sha256,
                    decision_id=str(payload.get("decision_id") or ""),
                    symbol=str(payload.get("symbol") or ""),
                    decision_at=_parse_aware_datetime(
                        payload.get("decision_at"), "capture_decision_at"
                    ),
                    available_at=event.clocks.available_at,
                    decision_event_sha256=event.event_sha256,
                    input_prefix_sequence=payload.get("input_prefix_sequence"),
                    input_prefix_root_sha256=str(
                        payload.get("input_prefix_root_sha256") or ""
                    ),
                    required_read_ids=tuple(payload.get("required_read_ids") or ()),
                    decision_payload=payload,
                )
            except Exception as exc:
                raise EvidenceError("capture_decision_checkpoint_invalid") from exc
            if checkpoint.checkpoint_sha256 == request.decision_checkpoint_sha256:
                checkpoint_candidates.append(checkpoint)
            continue

        if event.stream is contract.CaptureStream.READ_RECEIPT:
            try:
                receipt = contract.CaptureReadReceipt.from_dict(payload)
            except Exception as exc:
                raise EvidenceError("capture_read_receipt_invalid") from exc
            if (
                receipt.decision_id == request.decision_id
                and receipt.read_id in request.required_read_ids
            ):
                if receipt.read_id in receipts_by_id:
                    raise EvidenceError("capture_read_receipt_ambiguous")
                receipts_by_id[receipt.read_id] = receipt
            continue

        if event.stream is not contract.CaptureStream.CAPTURE_HEALTH:
            continue
        if set(payload) != coverage_control_fields:
            # Producer lifecycle and periodic resource-health facts share this
            # stream but are not StreamCoverage assertions.
            continue
        try:
            watermark_payload = payload.get("watermark")
            watermark = (
                None
                if watermark_payload is None
                else contract.ProviderWatermark.from_dict(watermark_payload)
            )
            coverage = contract.StreamCoverage(
                stream=payload.get("stream"),
                identity_sha256=str(payload.get("identity_sha256") or ""),
                provider=str(payload.get("provider") or ""),
                first_available_at=_parse_aware_datetime(
                    payload.get("first_available_at"),
                    "capture_coverage_first_available_at",
                ),
                last_available_at=_parse_aware_datetime(
                    payload.get("last_available_at"),
                    "capture_coverage_last_available_at",
                ),
                event_count=payload.get("event_count"),
                exact_event_clock_complete=payload.get(
                    "exact_event_clock_complete"
                ),
                content_verified=payload.get("content_verified"),
                continuity_complete=payload.get("continuity_complete"),
                watermark=watermark,
                query_receipt_count=payload.get("query_receipt_count"),
                symbol=payload.get("symbol"),
            )
        except Exception as exc:
            raise EvidenceError("capture_stream_coverage_invalid") from exc
        if coverage.stream in coverage_by_stream:
            raise EvidenceError("capture_stream_coverage_ambiguous")
        coverage_by_stream[coverage.stream] = coverage

    if len(checkpoint_candidates) != 1:
        raise EvidenceError("capture_decision_checkpoint_missing_or_ambiguous")
    return contract.CaptureCoverageManifest.from_verified_capture(
        verified,
        decision_checkpoints=tuple(checkpoint_candidates),
        stream_coverage=coverage_by_stream,
        read_receipts=tuple(receipts_by_id.values()),
    )


def validate_capture_seal(
    path: Path | None,
    *,
    expected_final_seal_sha256: str | None,
    coverage_request_path: Path | None,
    provenance: Mapping[str, Any],
    benchmark_measurement: Mapping[str, Any],
    benchmark_measurement_sha256: str,
) -> dict[str, Any]:
    """Pin immutable evidence and load v4 bytes through the read-only verifier.

    A verified store load and a complete coverage grade are necessary but not
    sufficient.  This audit remains red until a separate zero-egress ReplayV3
    proof reproduces the recorded FSM decision and broker lifecycle.
    """

    expected_seal = _require_sha256(
        expected_final_seal_sha256, "capture_expected_final_seal_sha256"
    )
    payload, raw = _read_json(path, label="capture_seal")
    assert path is not None
    seal_path = Path(path).expanduser().resolve()
    seal_schema = str(payload.get("schema_version") or "")
    if seal_schema not in {
        CAPTURE_SEAL_SCHEMA_VERSION,
        LEGACY_CAPTURE_SEAL_SCHEMA_VERSION,
    }:
        raise EvidenceError("capture_seal_schema_unsupported")
    if canonical_json_bytes(payload) != raw:
        raise EvidenceError("capture_seal_not_canonical")
    if _sha256_bytes(raw) != expected_seal or seal_path.stem != expected_seal:
        raise EvidenceError("capture_seal_does_not_match_external_expected_sha")
    if len(seal_path.parents) < 4 or seal_path.parents[2].name != "seals":
        raise EvidenceError("capture_seal_path_layout_invalid")
    capture_root = seal_path.parents[3]

    try:
        from app.services.trading.momentum_neural import replay_capture_contract as contract
        from app.services.trading.momentum_neural import replay_capture_runtime as runtime

        if (
            str(runtime.CAPTURE_RUN_SEAL_SCHEMA_VERSION)
            != CAPTURE_SEAL_SCHEMA_VERSION
        ):
            raise EvidenceError("capture_seal_runtime_schema_generation_mismatch")

        identity_payload = payload.get("identity")
        if not isinstance(identity_payload, Mapping):
            raise EvidenceError("capture_seal_identity_missing")
        identity = contract.CaptureRunIdentity.from_dict(identity_payload)
        expected_path = (
            capture_root
            / "seals"
            / f"run={identity.run_id}"
            / f"generation={identity.generation}"
            / f"{expected_seal}.json"
        ).resolve()
        if seal_path != expected_path:
            raise EvidenceError("capture_seal_path_layout_invalid")
        if identity.broker != "alpaca" or identity.broker_environment != "paper":
            raise EvidenceError("capture_seal_broker_posture_mismatch")
        for key in ("code_build_sha256", "config_sha256", "feature_flags_sha256"):
            if getattr(identity, key) != provenance.get(key):
                raise EvidenceError(f"capture_seal_{key}_mismatch")
        if identity.account_identity_sha256 != provenance.get(
            "expected_account_id_sha256"
        ):
            raise EvidenceError("capture_seal_account_identity_mismatch")

        request, request_sha256 = _load_replay_coverage_request(
            coverage_request_path,
            contract=contract,
            expected_final_seal_sha256=expected_seal,
            expected_identity_sha256=identity.identity_sha256,
        )
        _capture_compression_codec(payload)
    except EvidenceError:
        raise
    except Exception as exc:
        # The contract/runtime exception text can contain paths or provider data.
        raise EvidenceError("capture_official_certifying_load_failed") from exc

    if seal_schema == CAPTURE_SEAL_SCHEMA_VERSION:
        resource_binding, resource = _load_official_resource_binding_record(
            capture_root,
            benchmark_measurement=benchmark_measurement,
            benchmark_measurement_sha256=benchmark_measurement_sha256,
            runtime=runtime,
            contract=contract,
        )
    else:
        resource = _validate_resource_binding(
            capture_root,
            benchmark_measurement=benchmark_measurement,
            benchmark_measurement_sha256=benchmark_measurement_sha256,
        )
    if seal_schema == CAPTURE_SEAL_SCHEMA_VERSION:
        if payload.get("resource_hashes") != {
            "measurement_sha256": resource["measurement_sha256"],
            "policy_sha256": resource["policy_sha256"],
            "budget_sha256": resource["budget_sha256"],
            "binding_sha256": resource["binding_sha256"],
        }:
            raise EvidenceError("capture_seal_resource_binding_mismatch")

    if seal_schema != CAPTURE_SEAL_SCHEMA_VERSION:
        reasons = [
            "legacy_capture_not_resource_bound_v4",
            "capture_read_only_certifying_loader_unavailable",
            "exact_stream_and_read_receipt_coverage_unverified",
            "replay_v3_hermetic_certification_unavailable",
        ]
        return {
            "schema_version": seal_schema,
            "current_resource_bound_seal_schema": False,
            "ready": False,
            "seal_sha256": expected_seal,
            "external_expected_seal_sha256": expected_seal,
            "identity_sha256": identity.identity_sha256,
            "verified_store_load": False,
            "private_attestation_verified": False,
            "read_only_certifying_loader_available": True,
            "coverage_request_sha256": request_sha256,
            "coverage_manifest_sha256": None,
            "coverage_grade": "coverage_unavailable",
            "coverage_replayable": False,
            "coverage_reasons": reasons[:-1],
            "replay_network_fallback_count": None,
            "required_streams_full_fidelity": False,
            "hermetic_replay_v3_proven": False,
            "readiness_reasons": reasons,
            "resource_binding": resource,
        }

    try:
        verified = runtime.load_verified_replay_capture_v4(
            capture_root,
            identity,
            expected_final_seal_sha256=expected_seal,
            expected_resource_binding=resource_binding,
            coverage_request=request,
        )
    except Exception as exc:
        raise EvidenceError("capture_official_certifying_load_failed") from exc

    manifest = None
    grade = None
    control_reason: str | None = None
    try:
        manifest = _manifest_from_verified_capture(
            contract=contract,
            verified=verified,
            request=request,
        )
        grade = contract.grade_replay_coverage(request, manifest)
    except EvidenceError as exc:
        control_reason = str(exc)
    except Exception:
        control_reason = "capture_control_manifest_unavailable"

    if manifest is None or grade is None:
        coverage_grade = "coverage_unavailable"
        coverage_replayable = False
        coverage_reasons = [
            control_reason or "capture_control_manifest_unavailable"
        ]
        manifest_sha256 = None
        network_fallback_count = None
        full_fidelity = False
    else:
        coverage_grade = grade.grade
        coverage_replayable = grade.replayable
        coverage_reasons = list(grade.reasons)
        manifest_sha256 = manifest.manifest_sha256
        network_fallback_count = manifest.replay_network_fallback_count
        full_fidelity = manifest.required_streams_full_fidelity

    reasons = [
        *coverage_reasons,
        "replay_v3_hermetic_certification_unavailable",
    ]
    return {
        "schema_version": seal_schema,
        "current_resource_bound_seal_schema": True,
        "ready": False,
        "seal_sha256": expected_seal,
        "external_expected_seal_sha256": expected_seal,
        "identity_sha256": identity.identity_sha256,
        "verified_store_load": True,
        "private_attestation_verified": True,
        "read_only_certifying_loader_available": True,
        "coverage_request_sha256": request_sha256,
        "coverage_manifest_sha256": manifest_sha256,
        "coverage_grade": coverage_grade,
        "coverage_replayable": coverage_replayable,
        "coverage_reasons": coverage_reasons,
        "replay_network_fallback_count": network_fallback_count,
        "required_streams_full_fidelity": full_fidelity,
        "hermetic_replay_v3_proven": False,
        "readiness_reasons": reasons,
        "resource_binding": resource,
    }


def validate_adaptive_readiness(
    path: Path | None,
    *,
    expected_artifact_sha256: str | None,
    provenance: Mapping[str, Any],
) -> dict[str, Any]:
    """Recompute typed parity claims; never accept hash-shaped assertions.

    The runtime contract currently has no private-attested provider for bindings
    discovered from the four running order paths.  Typed claims are still useful
    diagnostics, but they cannot make the operational preflight green until an
    official current-runtime binding provider exists.
    """

    expected_sha = _require_sha256(
        expected_artifact_sha256, "adaptive_expected_artifact_sha256"
    )
    payload, raw = _read_json(path, label="adaptive_readiness")
    if canonical_json_bytes(payload) != raw:
        raise EvidenceError("adaptive_readiness_not_canonical")
    if _sha256_bytes(raw) != expected_sha:
        raise EvidenceError("adaptive_readiness_external_hash_mismatch")
    expected_top_level = {"schema_version", "policy", "bindings", "readiness"}
    if set(payload) != expected_top_level:
        raise EvidenceError("adaptive_readiness_schema_invalid")
    if payload.get("schema_version") != ADAPTIVE_READINESS_SCHEMA_VERSION:
        raise EvidenceError("adaptive_readiness_schema_unsupported")
    policy_payload = payload.get("policy")
    binding_payloads = payload.get("bindings")
    supplied_readiness = payload.get("readiness")
    if (
        not isinstance(policy_payload, Mapping)
        or not isinstance(binding_payloads, list)
        or not isinstance(supplied_readiness, Mapping)
    ):
        raise EvidenceError("adaptive_readiness_sections_missing")

    try:
        from app.services.trading.momentum_neural.adaptive_risk_policy import (
            AdaptiveRiskPolicy,
        )
        from app.services.trading.momentum_neural import (
            adaptive_risk_runtime_contract as runtime_contract,
        )

        policy_fields = {field.name for field in fields(AdaptiveRiskPolicy)}
        if set(policy_payload) != policy_fields:
            raise EvidenceError("adaptive_policy_schema_invalid")
        policy = AdaptiveRiskPolicy(**dict(policy_payload))
        binding_fields = {
            field.name for field in fields(runtime_contract.AdaptiveRiskRuntimeBinding)
        }
        bindings: list[Any] = []
        for raw_binding in binding_payloads:
            if not isinstance(raw_binding, Mapping) or set(raw_binding) != binding_fields:
                raise EvidenceError("adaptive_binding_schema_invalid")
            values = dict(raw_binding)
            dimensions = values.get("atomic_reservation_dimensions")
            dollar_caps = values.get("activation_only_dollar_caps")
            if not isinstance(dimensions, list) or not isinstance(dollar_caps, list):
                raise EvidenceError("adaptive_binding_collection_invalid")
            values["atomic_reservation_dimensions"] = frozenset(
                str(value) for value in dimensions
            )
            values["activation_only_dollar_caps"] = tuple(
                str(value) for value in dollar_caps
            )
            binding = runtime_contract.AdaptiveRiskRuntimeBinding(**values)
            if binding.policy_sha256 != policy.policy_sha256:
                raise EvidenceError("adaptive_binding_policy_hash_mismatch")
            if binding.code_build_sha256 != provenance.get("code_build_sha256"):
                raise EvidenceError("adaptive_binding_code_build_hash_mismatch")
            bindings.append(binding)
        recomputed = runtime_contract.assess_adaptive_risk_runtime_readiness(bindings)
    except EvidenceError:
        raise
    except Exception as exc:
        raise EvidenceError("adaptive_runtime_typed_recomputation_failed") from exc

    recomputed_payload = recomputed.to_payload()
    if canonical_json_bytes(dict(supplied_readiness)) != canonical_json_bytes(
        recomputed_payload
    ):
        raise EvidenceError("adaptive_readiness_recomputation_mismatch")
    if recomputed.common_policy_sha256 != policy.policy_sha256:
        raise EvidenceError("adaptive_readiness_policy_recomputation_mismatch")

    # These dataclass rows are caller assertions.  The contract does not yet
    # expose a private-token/current-build factory that proves the assertions
    # came from each concrete last risk-increasing boundary.  Therefore even a
    # syntactically ready recomputation is diagnostic only.
    attested_current_runtime_bindings = False
    reasons = list(recomputed.reasons)
    reasons.append("adaptive_runtime_binding_attestation_unavailable")
    return {
        "schema_version": ADAPTIVE_READINESS_SCHEMA_VERSION,
        "artifact_sha256": expected_sha,
        "common_policy_sha256": recomputed.common_policy_sha256,
        "binding_manifest_sha256": recomputed.binding_manifest_sha256,
        "surfaces": sorted(REQUIRED_ADAPTIVE_SURFACES),
        "typed_binding_claims_recomputed": True,
        "claimed_runtime_parity_ready": recomputed.ready,
        "attested_current_runtime_bindings": attested_current_runtime_bindings,
        "readiness_reasons": list(dict.fromkeys(reasons)),
        "ready": recomputed.ready and attested_current_runtime_bindings,
    }


def validate_historical_coverage(paths: Iterable[Path]) -> tuple[dict[str, Any], list[str]]:
    unavailable = 0
    diagnostic = 0
    artifact_hashes: list[str] = []
    blockers: list[str] = []
    supplied = 0
    for path in paths:
        supplied += 1
        try:
            payload, raw = _read_json(path, label="historical_coverage")
            if payload.get("schema_version") != ROSS_COVERAGE_SCHEMA_VERSION:
                raise EvidenceError("historical_coverage_schema_unsupported")
            if payload.get("read_only") is not True or payload.get(
                "certification_eligible"
            ) is not False:
                raise EvidenceError("historical_coverage_posture_invalid")
            rows = payload.get("rows")
            if not isinstance(rows, list):
                raise EvidenceError("historical_coverage_rows_invalid")
            for row in rows:
                if not isinstance(row, Mapping):
                    raise EvidenceError("historical_coverage_row_invalid")
                status = row.get("coverage_status")
                if status == "coverage_unavailable":
                    unavailable += 1
                    if not row.get("coverage_reasons"):
                        raise EvidenceError("historical_coverage_reason_missing")
                elif status == "diagnostic_only":
                    diagnostic += 1
                else:
                    raise EvidenceError("historical_coverage_status_unsupported")
            artifact_hashes.append(_sha256_bytes(raw))
        except EvidenceError as exc:
            blockers.append(str(exc))
    return (
        {
            "artifacts_supplied": supplied,
            "artifact_sha256s": artifact_hashes,
            "coverage_unavailable_count": unavailable,
            "diagnostic_only_count": diagnostic,
            "scored_pass_count": 0,
            "certification_claimed": False,
            "operational_soak_gate": "nonblocking",
            "interpretation": "coverage_unavailable_is_unscorable_not_a_pass",
        },
        blockers,
    )


def _evidence_check(
    name: str,
    callback: Any,
) -> tuple[dict[str, Any], list[str]]:
    try:
        return callback(), []
    except (EvidenceError, OSError, ValueError, TypeError, KeyError) as exc:
        reason = str(exc) if isinstance(exc, EvidenceError) else f"{name}_validation_failed"
        return {"ready": False, "reason": reason}, [reason]


def evaluate_preflight(
    *,
    adapter: Any,
    settings_values: Mapping[str, Any],
    capture_benchmark_path: Path | None,
    capture_benchmark_expected_sha256: str | None,
    capture_seal_path: Path | None,
    capture_expected_seal_sha256: str | None,
    capture_coverage_request_path: Path | None,
    adaptive_readiness_path: Path | None,
    adaptive_expected_sha256: str | None,
    historical_coverage_paths: Iterable[Path] = (),
    repo_root: Path,
    mode: str = "staged-alpaca-soak",
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    if mode not in {"audit", "staged-alpaca-soak"}:
        raise ValueError("unsupported preflight mode")
    now = (generated_at or datetime.now(UTC)).astimezone(UTC)
    blockers: list[str] = []

    topology, topology_blockers = inspect_topology(settings_values, mode=mode)
    blockers.extend(topology_blockers)
    try:
        provenance = provenance_payload(
            repo_root=Path(repo_root), settings_values=settings_values
        )
    except EvidenceError as exc:
        provenance = {
            "code_build_sha256": None,
            "config_sha256": None,
            "feature_flags_sha256": None,
            "expected_account_id_sha256": None,
        }
        blockers.append(str(exc))

    expected_account = str(
        settings_values.get("chili_alpaca_expected_account_id") or ""
    ).strip()
    if adapter is None:
        broker = {
            "account_readable": False,
            "account_identity_match": False,
            "clock_readable": False,
            "flat_and_no_open_orders": False,
        }
        blockers.append("alpaca_adapter_unavailable")
    else:
        broker, broker_blockers = probe_alpaca_read_only(
            adapter, expected_account_id=expected_account
        )
        blockers.extend(broker_blockers)

    benchmark: dict[str, Any]
    benchmark_measurement: dict[str, Any] | None = None
    try:
        benchmark, benchmark_measurement = validate_capture_benchmark(
            capture_benchmark_path,
            repo_root=Path(repo_root),
            expected_artifact_sha256=capture_benchmark_expected_sha256,
            evaluated_at=now,
        )
        if benchmark.get("capacity_limits_authorized") is not True:
            blockers.append("capture_capacity_calibration_unavailable")
    except (EvidenceError, OSError, ValueError, TypeError, KeyError):
        benchmark = {"ready": False, "reason": "capture_benchmark_not_ready"}
        blockers.append("capture_benchmark_not_ready")

    if benchmark_measurement is None:
        seal = {"ready": False, "reason": "capture_seal_not_ready"}
        blockers.append("capture_seal_not_ready")
    else:
        try:
            seal = validate_capture_seal(
                capture_seal_path,
                expected_final_seal_sha256=capture_expected_seal_sha256,
                coverage_request_path=capture_coverage_request_path,
                provenance=provenance,
                benchmark_measurement=benchmark_measurement,
                benchmark_measurement_sha256=str(benchmark["measurement_sha256"]),
            )
            if seal.get("coverage_replayable") is not True:
                blockers.append("capture_replay_coverage_unavailable")
            if seal.get("verified_store_load") is not True:
                blockers.append("capture_read_only_certifying_loader_unavailable")
            if seal.get("hermetic_replay_v3_proven") is not True:
                blockers.append("replay_v3_hermetic_certification_unavailable")
        except (EvidenceError, OSError, ValueError, TypeError, KeyError):
            seal = {"ready": False, "reason": "capture_seal_not_ready"}
            blockers.append("capture_seal_not_ready")

    try:
        adaptive = validate_adaptive_readiness(
            adaptive_readiness_path,
            expected_artifact_sha256=adaptive_expected_sha256,
            provenance=provenance,
        )
        if adaptive.get("ready") is not True:
            blockers.append("adaptive_runtime_not_ready")
    except (EvidenceError, OSError, ValueError, TypeError, KeyError):
        adaptive = {"ready": False, "reason": "adaptive_runtime_not_ready"}
        blockers.append("adaptive_runtime_not_ready")

    history, history_blockers = validate_historical_coverage(
        historical_coverage_paths
    )
    blockers.extend(history_blockers)
    unique_blockers = sorted(set(blockers))
    ready = not unique_blockers
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "generated_at": now.isoformat().replace("+00:00", "Z"),
        "mode": mode,
        "ready": ready,
        "execution_authorized": False,
        "profitability_certified": False,
        "blockers": unique_blockers,
        "checks": {
            "topology": topology,
            "broker_get_only": broker,
            "capture_benchmark": benchmark,
            "capture_seal": seal,
            "adaptive_runtime": adaptive,
            "historical_replay": history,
        },
        "provenance": provenance,
        "activation_boundary": {
            "flags_changed": False,
            "orders_submitted": False,
            "explicit_user_activation_still_required": True,
            "live_cash_authorized": False,
        },
    }


def report_exit_code(report: Mapping[str, Any]) -> int:
    return 0 if report.get("ready") is True else 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode", choices=("audit", "staged-alpaca-soak"), default="staged-alpaca-soak"
    )
    parser.add_argument("--capture-benchmark", type=Path)
    parser.add_argument("--capture-benchmark-expected-sha256")
    parser.add_argument("--capture-seal", type=Path)
    parser.add_argument("--capture-expected-seal-sha256")
    parser.add_argument("--capture-coverage-request", type=Path)
    parser.add_argument("--adaptive-readiness", type=Path)
    parser.add_argument("--adaptive-expected-sha256")
    parser.add_argument("--historical-coverage", type=Path, action="append", default=[])
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(list(argv) if argv is not None else None)
    adapter: Any = None
    try:
        from app.config import settings
        from app.services.trading.venue.alpaca_spot import AlpacaSpotAdapter

        values = settings_snapshot(settings)
        adapter = AlpacaSpotAdapter()
    except Exception:
        # Never echo import/config exceptions: SDK errors can contain request details.
        values = {name: None for name in SETTING_NAMES}
    try:
        report = evaluate_preflight(
            adapter=adapter,
            settings_values=values,
            capture_benchmark_path=args.capture_benchmark,
            capture_benchmark_expected_sha256=(
                args.capture_benchmark_expected_sha256
            ),
            capture_seal_path=args.capture_seal,
            capture_expected_seal_sha256=args.capture_expected_seal_sha256,
            capture_coverage_request_path=args.capture_coverage_request,
            adaptive_readiness_path=args.adaptive_readiness,
            adaptive_expected_sha256=args.adaptive_expected_sha256,
            historical_coverage_paths=args.historical_coverage,
            repo_root=args.repo_root,
            mode=args.mode,
        )
    except Exception:
        report = {
            "schema_version": REPORT_SCHEMA_VERSION,
            "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "mode": args.mode,
            "ready": False,
            "execution_authorized": False,
            "profitability_certified": False,
            "blockers": ["preflight_internal_error"],
            "checks": {},
            "provenance": {},
            "activation_boundary": {
                "flags_changed": False,
                "orders_submitted": False,
                "explicit_user_activation_still_required": True,
                "live_cash_authorized": False,
            },
        }
    sys.stdout.buffer.write(canonical_json_bytes(report) + b"\n")
    return report_exit_code(report)


if __name__ == "__main__":
    raise SystemExit(main())
