"""Build one content-addressed, inert IQFeed capture-bootstrap bundle.

The builder accepts an already-observed Alpaca PAPER account snapshot and an
already-verified resource benchmark.  It performs no broker, provider,
database, task-scheduler, or service I/O.  Every source is rehashed, the three
JSON objects are published by digest without overwrite, and the existing
diagnostic-only bootstrap loader validates the result before it is returned.

This artifact is capture composition input, not PAPER order authority.  The
separate captured-paper activation envelope binds it only after a no-order
smoke and all operational receipts pass.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
from types import MappingProxyType
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import parse_qsl, urlsplit
import uuid

from scripts import iqfeed_capture_bootstrap_preflight as preflight_module


UTC = timezone.utc
BUILDER_SCHEMA_VERSION = "chili.iqfeed-capture-bootstrap-bundle-builder.v1"
BUILD_REQUEST_SCHEMA_VERSION = "chili.iqfeed-capture-bootstrap-build-request.v1"
BUILDER_REPORT_SCHEMA_VERSION = "chili.iqfeed-capture-bootstrap-builder-report.v1"
BUNDLE_COMMIT_SCHEMA_VERSION = "chili.iqfeed-capture-bootstrap-bundle-commit.v1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_REPARSE_ATTRIBUTE = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
_MAX_SOURCE_BYTES = 64 * 1024 * 1024
_MAX_REQUEST_BYTES = 4 * 1024 * 1024
_MAX_REQUEST_AGE_SECONDS = 60.0
_MAX_ACCOUNT_SNAPSHOT_AGE_SECONDS = 30.0
# Startup evidence is a process-instance/bundle-identity record, not broker
# truth.  The operator chain has an exact 30-minute timeout and deliberately
# completes preselection plus its fixed regression/migration/lifecycle gates
# before the capture smoke consumes this bundle.  Bound startup identity to
# that same outer timeout so a valid chain cannot outlive it, while the fresh
# broker-account probe and transport-boundary re-reads continue to own account
# and order-time freshness.
# 2026-07-23 (a77 finding): the full ActivatePaper chain (61-test roster +
# rehearsal + smoke + finalize + cutover to service boot) measures 21-32 min
# live depending on host load, so the prior 30-min window made slow-but-valid
# chains fail STALE_EVIDENCE at the service's bootstrap preflight by ~1 min
# (a74=21min passed, a76=29min passed, a77=31min rejected).  This evidence is
# infrastructure startup proof, not market data -- account/order freshness is
# owned by the fresh broker probes and the tape gates.  60 min = ~2x the
# worst observed chain duration.
_MAX_STARTUP_EVIDENCE_AGE_SECONDS = 60 * 60.0
_MAX_FUTURE_SKEW_SECONDS = 5.0
_MAX_PROJECTION_DEPTH = 16
_MAX_PROJECTION_ITEMS = 10_000
_MAX_PROJECTION_STRING_BYTES = 64 * 1024

_BUILD_REQUEST_FIELDS = frozenset(
    {
        "schema_version",
        "repo_root",
        "artifact_root",
        "capture_store_root",
        "resource_benchmark",
        "source_sha256",
        "expected_account_id",
        "account_risk_snapshot",
        "account_query",
        "account_received_at",
        "account_available_at",
        "effective_config",
        "bridge_configuration",
        "activation_generation",
        "generated_at",
        "generation",
    }
)
_FORBIDDEN_KEY_TOKENS = frozenset(
    {
        "authorization",
        "auth",
        "accesskey",
        "accesstoken",
        "apikey",
        "apisecret",
        "cookie",
        "credential",
        "credentials",
        "headers",
        "password",
        "passwd",
        "privatekey",
        "databaseurl",
        "secret",
        "secrets",
        "token",
        "tokens",
    }
)
_FORBIDDEN_KEY_PAIRS = frozenset(
    {
        ("access", "key"),
        ("api", "key"),
        ("api", "secret"),
        ("database", "url"),
        ("http", "headers"),
        ("private", "key"),
        ("request", "headers"),
    }
)
_ACCOUNT_RISK_REQUIRED_FIELDS = frozenset({"equity", "buying_power"})
_ACCOUNT_RISK_ALLOWED_FIELDS = frozenset(
    {
        "equity",
        "last_equity",
        "buying_power",
        "cash",
        "broker_day_change",
        "status",
        "account_blocked",
        "trading_blocked",
        "transfers_blocked",
        "trade_suspended_by_user",
        "observed_at",
    }
)
_ACCOUNT_QUERY_ALLOWED_FIELDS = frozenset(
    {
        "endpoint",
        "operation",
        "environment",
        "account_id",
        "account_retrieved_at",
        "connection_generation",
        "connection_receipt_sha256",
        "open_order_census_sha256",
        "open_order_inventory_sha256",
    }
)

_SOURCE_RELATIVE_PATHS: Mapping[str, str] = MappingProxyType(
    {
        "app_migrations": "app/migrations.py",
        "benchmark_replay_capture_runtime": "scripts/benchmark_replay_capture_runtime.py",
        "iqfeed_capture_bootstrap": "scripts/iqfeed_capture_bootstrap.py",
        "iqfeed_capture_bootstrap_preflight": "scripts/iqfeed_capture_bootstrap_preflight.py",
        "iqfeed_capture_host": "scripts/iqfeed_capture_host.py",
        "iqfeed_capture_host_launcher": "scripts/start-iqfeed-capture-host.ps1",
        "iqfeed_depth_bridge": "scripts/iqfeed_depth_bridge.py",
        "iqfeed_l1_capture": "app/services/trading/momentum_neural/iqfeed_l1_capture.py",
        "iqfeed_l2_capture": "app/services/trading/momentum_neural/iqfeed_l2_capture.py",
        "iqfeed_trade_bridge": "scripts/iqfeed_trade_bridge.py",
        "live_replay_capture": "app/services/trading/momentum_neural/live_replay_capture.py",
        "replay_capture_contract": "app/services/trading/momentum_neural/replay_capture_contract.py",
        "replay_capture_runtime": "app/services/trading/momentum_neural/replay_capture_runtime.py",
    }
)


class IqfeedCaptureBootstrapBundleError(RuntimeError):
    """Fail-closed local bundle construction error."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "BOOTSTRAP_BUNDLE_REJECTED",
        visible_objects: Sequence[Mapping[str, str]] = (),
        commit_published: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = str(code)
        self.visible_objects = tuple(dict(row) for row in visible_objects)
        self.commit_published = bool(commit_published)


@dataclass(frozen=True, slots=True)
class BuiltIqfeedCaptureBootstrapBundle:
    manifest_path: Path
    manifest_sha256: str
    startup_evidence_path: Path
    startup_evidence_sha256: str
    resource_benchmark_path: Path
    resource_benchmark_sha256: str
    commit_path: Path
    commit_sha256: str
    capture_store_root: Path
    source_paths: Mapping[str, Path]
    source_hashes: Mapping[str, str]
    preflight: preflight_module.IqfeedCaptureBootstrapPreflight
    builder_receipt: Mapping[str, Any]


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise IqfeedCaptureBootstrapBundleError(
            "bootstrap bundle material is not canonical JSON"
        ) from exc


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _sha(value: Any, field: str) -> str:
    normalized = str(value or "").strip().lower()
    if _SHA256_RE.fullmatch(normalized) is None:
        raise IqfeedCaptureBootstrapBundleError(f"{field} is not a SHA-256")
    return normalized


def _utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise IqfeedCaptureBootstrapBundleError(
            f"{field} must be timezone-aware"
        )
    return value.astimezone(UTC)


def _iso(value: datetime) -> str:
    return _utc(value, "timestamp").isoformat().replace("+00:00", "Z")


def _uuid(value: Any, field: str) -> str:
    normalized = str(value or "").strip().lower()
    try:
        parsed = uuid.UUID(normalized)
    except (AttributeError, TypeError, ValueError) as exc:
        raise IqfeedCaptureBootstrapBundleError(
            f"{field} is not a canonical UUID"
        ) from exc
    if str(parsed) != normalized:
        raise IqfeedCaptureBootstrapBundleError(
            f"{field} is not a canonical UUID"
        )
    return normalized


def _reject_reparse_chain(path: Path) -> None:
    cursor = path
    while True:
        info = os.lstat(cursor)
        attrs = int(getattr(info, "st_file_attributes", 0) or 0)
        if stat.S_ISLNK(info.st_mode) or attrs & _REPARSE_ATTRIBUTE:
            raise IqfeedCaptureBootstrapBundleError(
                f"bootstrap path traverses a reparse point: {path}"
            )
        parent = cursor.parent
        if parent == cursor:
            return
        cursor = parent


def _local_dir(
    value: str | Path,
    field: str,
    *,
    local_drive_check: Callable[[Path], bool] = preflight_module._default_local_drive_check,
) -> Path:
    path = preflight_module._lexical_absolute_local_path(
        value,
        field=field,
        local_drive_check=local_drive_check,
    )
    _reject_reparse_chain(path)
    status = path.lstat()
    if not stat.S_ISDIR(status.st_mode):
        raise IqfeedCaptureBootstrapBundleError(f"{field} is not a directory")
    return path


def _local_file(
    value: str | Path,
    field: str,
    *,
    local_drive_check: Callable[[Path], bool] = preflight_module._default_local_drive_check,
) -> Path:
    path = preflight_module._lexical_absolute_local_path(
        value,
        field=field,
        local_drive_check=local_drive_check,
    )
    _reject_reparse_chain(path)
    status = path.lstat()
    if not stat.S_ISREG(status.st_mode):
        raise IqfeedCaptureBootstrapBundleError(f"{field} is not a file")
    return path


def _stable_sha256(path: Path) -> str:
    _reject_reparse_chain(path)
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_size <= 0:
        raise IqfeedCaptureBootstrapBundleError(
            f"bootstrap source is not a nonempty regular file: {path}"
        )
    if before.st_size > _MAX_SOURCE_BYTES:
        raise IqfeedCaptureBootstrapBundleError(
            f"bootstrap source exceeds bounded size: {path}"
        )
    try:
        raw = preflight_module._read_bytes_stable(
            path,
            field="bootstrap source",
            max_bytes=_MAX_SOURCE_BYTES,
        )
    except preflight_module.BootstrapPreflightError as exc:
        raise IqfeedCaptureBootstrapBundleError(
            f"bootstrap source could not be read stably: {path}"
        ) from exc
    if not raw:
        raise IqfeedCaptureBootstrapBundleError(
            f"bootstrap source is empty: {path}"
        )
    return hashlib.sha256(raw).hexdigest()


def _exact_keys(value: Mapping[str, Any], expected: Sequence[str] | set[str] | frozenset[str], field: str) -> None:
    wanted = set(expected)
    actual = set(value)
    if actual != wanted:
        raise IqfeedCaptureBootstrapBundleError(
            f"{field} keys differ; missing={sorted(wanted-actual)} "
            f"extra={sorted(actual-wanted)}"
        )


def _mapping(value: Any, field: str, *, nonempty: bool = False) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or (nonempty and not value):
        raise IqfeedCaptureBootstrapBundleError(f"{field} is not a valid object")
    return value


def _key_tokens(value: str) -> tuple[str, ...]:
    expanded = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
    return tuple(part.lower() for part in re.findall(r"[A-Za-z0-9]+", expanded))


def _credential_key(value: str) -> bool:
    tokens = _key_tokens(value)
    if not tokens or any(token in _FORBIDDEN_KEY_TOKENS for token in tokens):
        return True
    return any(pair in _FORBIDDEN_KEY_PAIRS for pair in zip(tokens, tokens[1:]))


def _reject_secret_string(value: str, field: str) -> None:
    if "\x00" in value or len(value.encode("utf-8")) > _MAX_PROJECTION_STRING_BYTES:
        raise IqfeedCaptureBootstrapBundleError(
            f"{field} contains an invalid or oversized string"
        )
    lowered = value.strip().lower()
    if lowered.startswith(("bearer ", "basic ", "-----begin private key")):
        raise IqfeedCaptureBootstrapBundleError(
            f"{field} contains credential-like material"
        )
    if "://" not in value and not value.startswith("//"):
        return
    parsed = urlsplit(value)
    if parsed.username is not None or parsed.password is not None:
        raise IqfeedCaptureBootstrapBundleError(
            f"{field} contains URL user information"
        )
    for key, _item in parse_qsl(parsed.query, keep_blank_values=True):
        if _credential_key(key):
            raise IqfeedCaptureBootstrapBundleError(
                f"{field} contains a credential-like URL query key"
            )


def _safe_projection(value: Any, field: str) -> Any:
    """Return a bounded JSON projection with credential-bearing shapes denied."""

    count = [0]

    def visit(item: Any, location: str, depth: int) -> Any:
        count[0] += 1
        if depth > _MAX_PROJECTION_DEPTH or count[0] > _MAX_PROJECTION_ITEMS:
            raise IqfeedCaptureBootstrapBundleError(
                f"{field} exceeds its bounded projection limits"
            )
        if isinstance(item, Mapping):
            result: dict[str, Any] = {}
            for raw_key, child in item.items():
                if not isinstance(raw_key, str):
                    raise IqfeedCaptureBootstrapBundleError(
                        f"{location} contains a non-string key"
                    )
                key = raw_key.strip()
                if not key or key in result or _credential_key(key):
                    raise IqfeedCaptureBootstrapBundleError(
                        f"{location} contains a forbidden credential-like key"
                    )
                result[key] = visit(child, f"{location}.{key}", depth + 1)
            return result
        if isinstance(item, list):
            return [
                visit(child, f"{location}[{index}]", depth + 1)
                for index, child in enumerate(item)
            ]
        if isinstance(item, str):
            _reject_secret_string(item, location)
            return item
        if item is None or isinstance(item, (bool, int, float)):
            if isinstance(item, float) and not (float("-inf") < item < float("inf")):
                raise IqfeedCaptureBootstrapBundleError(
                    f"{location} contains a non-finite number"
                )
            return item
        raise IqfeedCaptureBootstrapBundleError(
            f"{location} contains a non-JSON value"
        )

    return visit(value, field, 0)


def _safe_account_risk_snapshot(value: Any) -> Mapping[str, Any]:
    projected = _mapping(_safe_projection(value, "account_risk_snapshot"), "account_risk_snapshot")
    actual = set(projected)
    if not _ACCOUNT_RISK_REQUIRED_FIELDS.issubset(actual) or not actual.issubset(
        _ACCOUNT_RISK_ALLOWED_FIELDS
    ):
        raise IqfeedCaptureBootstrapBundleError(
            "account_risk_snapshot fields differ from the sanitized contract"
        )
    for field in ("equity", "buying_power", "last_equity", "cash", "broker_day_change"):
        if field not in projected:
            continue
        raw = projected[field]
        if isinstance(raw, bool):
            raise IqfeedCaptureBootstrapBundleError(
                f"account_risk_snapshot.{field} is not numeric"
            )
        try:
            parsed = Decimal(str(raw))
        except (InvalidOperation, ValueError) as exc:
            raise IqfeedCaptureBootstrapBundleError(
                f"account_risk_snapshot.{field} is not numeric"
            ) from exc
        if not parsed.is_finite():
            raise IqfeedCaptureBootstrapBundleError(
                f"account_risk_snapshot.{field} is not finite"
            )
        if field == "equity" and parsed <= 0:
            raise IqfeedCaptureBootstrapBundleError(
                f"account_risk_snapshot.{field} must be positive"
            )
        # 2026-07-23: Alpaca resets paper `last_equity` to 0 at the trading-day
        # boundary, so zero is a valid healthy state (informational prior-close
        # equity, not a safety gate) -- matched with the operator-chain posture.
        if field == "last_equity" and parsed < 0:
            raise IqfeedCaptureBootstrapBundleError(
                "account_risk_snapshot.last_equity must be nonnegative"
            )
        if field == "buying_power" and parsed < 0:
            raise IqfeedCaptureBootstrapBundleError(
                "account_risk_snapshot.buying_power must be nonnegative"
            )
    for field in (
        "account_blocked",
        "trading_blocked",
        "transfers_blocked",
        "trade_suspended_by_user",
    ):
        if field in projected and not isinstance(projected[field], bool):
            raise IqfeedCaptureBootstrapBundleError(
                f"account_risk_snapshot.{field} is not boolean"
            )
    if "status" in projected and (
        not isinstance(projected["status"], str) or not projected["status"].strip()
    ):
        raise IqfeedCaptureBootstrapBundleError(
            "account_risk_snapshot.status is invalid"
        )
    if "observed_at" in projected:
        preflight_module._parse_utc(
            projected["observed_at"],
            "account_risk_snapshot.observed_at",
        )
    return MappingProxyType(dict(projected))


def _safe_account_query(value: Any, *, expected_account_id: str) -> Mapping[str, Any]:
    projected = _mapping(_safe_projection(value, "account_query"), "account_query")
    if not set(projected).issubset(_ACCOUNT_QUERY_ALLOWED_FIELDS):
        raise IqfeedCaptureBootstrapBundleError(
            "account_query fields differ from the sanitized contract"
        )
    if projected.get("environment") != "paper":
        raise IqfeedCaptureBootstrapBundleError(
            "account_query environment is not Alpaca paper"
        )
    endpoint = projected.get("endpoint")
    operation = projected.get("operation")
    allowed_operations = {
        "get_account_snapshot",
        "get_account+list_positions+list_open_orders",
    }
    if (
        (endpoint is None and operation is None)
        or (endpoint is not None and endpoint != "/v2/account")
        or (operation is not None and operation not in allowed_operations)
    ):
        raise IqfeedCaptureBootstrapBundleError(
            "account_query operation is not an allowed local receipt projection"
        )
    if projected.get("account_id") != expected_account_id:
        raise IqfeedCaptureBootstrapBundleError(
            "account_query account UUID differs from expected_account_id"
        )
    for field in (
        "connection_receipt_sha256",
        "open_order_census_sha256",
        "open_order_inventory_sha256",
    ):
        if field in projected:
            _sha(projected[field], f"account_query.{field}")
    if "account_retrieved_at" in projected:
        preflight_module._parse_utc(
            projected["account_retrieved_at"],
            "account_query.account_retrieved_at",
        )
    if "connection_generation" in projected and (
        isinstance(projected["connection_generation"], bool)
        or not isinstance(projected["connection_generation"], int)
        or projected["connection_generation"] <= 0
    ):
        raise IqfeedCaptureBootstrapBundleError(
            "account_query.connection_generation is invalid"
        )
    return MappingProxyType(dict(projected))


def _reject_embedded_secrets(value: Any, field: str) -> None:
    """Compatibility wrapper around the typed bounded projection validator."""

    _safe_projection(value, field)


def _source_pins(value: Any) -> Mapping[str, str]:
    pins = _mapping(value, "source_sha256", nonempty=True)
    if set(pins) != set(_SOURCE_RELATIVE_PATHS):
        raise IqfeedCaptureBootstrapBundleError(
            "source_sha256 roles differ from the bootstrap source contract"
        )
    return MappingProxyType(
        {
            role: _sha(pins.get(role), f"source_sha256.{role}")
            for role in sorted(_SOURCE_RELATIVE_PATHS)
        }
    )


def _request_path(
    value: Any,
    *,
    roots: Sequence[Path],
    field: str,
    write_target: bool,
    require_directory: bool,
    local_drive_check: Callable[[Path], bool] = preflight_module._default_local_drive_check,
) -> Path:
    path = preflight_module._lexical_absolute_local_path(
        value,
        field=field,
        local_drive_check=local_drive_check,
    )
    if not preflight_module._inside_any(
        path,
        roots,
        allow_equal=not write_target,
    ):
        raise IqfeedCaptureBootstrapBundleError(
            f"{field} is outside the operator allowlist"
        )
    preflight_module._check_existing_components(
        path,
        require_leaf=require_directory,
        field=field,
    )
    if require_directory:
        status = path.lstat()
        if not stat.S_ISDIR(status.st_mode):
            raise IqfeedCaptureBootstrapBundleError(
                f"{field} is not a directory"
            )
    elif path.exists() and not path.is_dir():
        raise IqfeedCaptureBootstrapBundleError(
            f"{field} exists but is not a directory"
        )
    return path


def _sample_wall_clock(wall_clock: Callable[[], datetime]) -> datetime:
    if not callable(wall_clock):
        raise IqfeedCaptureBootstrapBundleError("bootstrap wall clock is not callable")
    return _utc(wall_clock(), "bootstrap wall clock")


def _validate_observed_freshness(
    observed_at: datetime,
    *,
    now: datetime,
    max_age_seconds: float,
    field: str,
) -> None:
    age = (now - _utc(observed_at, field)).total_seconds()
    if age < -_MAX_FUTURE_SKEW_SECONDS:
        raise IqfeedCaptureBootstrapBundleError(
            f"{field} is beyond the permitted future skew"
        )
    if age > max_age_seconds:
        raise IqfeedCaptureBootstrapBundleError(f"{field} is stale")


def _directory_identity(path: Path, field: str) -> tuple[int, int]:
    status = path.lstat()
    attrs = int(getattr(status, "st_file_attributes", 0) or 0)
    if stat.S_ISLNK(status.st_mode) or attrs & _REPARSE_ATTRIBUTE:
        raise IqfeedCaptureBootstrapBundleError(
            f"{field} contains a symlink/reparse point"
        )
    if not stat.S_ISDIR(status.st_mode):
        raise IqfeedCaptureBootstrapBundleError(f"{field} is not a directory")
    return int(status.st_dev), int(status.st_ino)


def _ensure_safe_directory(
    path: Path,
    *,
    roots: Sequence[Path],
    field: str,
    local_drive_check: Callable[[Path], bool],
) -> Path:
    lexical = preflight_module._lexical_absolute_local_path(
        path,
        field=field,
        local_drive_check=local_drive_check,
    )
    if not preflight_module._inside_any(lexical, roots, allow_equal=False):
        raise IqfeedCaptureBootstrapBundleError(
            f"{field} is outside the original operator write allowlist"
        )
    missing: list[str] = []
    cursor = lexical
    while True:
        try:
            cursor.lstat()
            break
        except FileNotFoundError:
            if cursor.parent == cursor:
                raise IqfeedCaptureBootstrapBundleError(
                    f"{field} has no existing local ancestor"
                )
            missing.append(cursor.name)
            cursor = cursor.parent
    _reject_reparse_chain(cursor)
    _directory_identity(cursor, field)
    for component in reversed(missing):
        parent_identity = _directory_identity(cursor, field)
        child = cursor / component
        try:
            child.mkdir()
        except FileExistsError:
            pass
        if _directory_identity(cursor, field) != parent_identity:
            raise IqfeedCaptureBootstrapBundleError(
                f"{field} parent changed while creating its directory"
            )
        _directory_identity(child, field)
        cursor = child
    _reject_reparse_chain(lexical)
    _directory_identity(lexical, field)
    return lexical


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _remove_private_staging(path: Path, *, artifact_root: Path) -> None:
    try:
        path.relative_to(artifact_root)
    except ValueError as exc:
        raise IqfeedCaptureBootstrapBundleError(
            "bootstrap staging directory escaped artifact_root"
        ) from exc

    def remove(directory: Path) -> None:
        _reject_reparse_chain(directory)
        _directory_identity(directory, "bootstrap private staging directory")
        with os.scandir(directory) as entries:
            children = list(entries)
        for entry in children:
            child = Path(entry.path)
            status = entry.stat(follow_symlinks=False)
            attrs = int(getattr(status, "st_file_attributes", 0) or 0)
            if stat.S_ISLNK(status.st_mode) or attrs & _REPARSE_ATTRIBUTE:
                raise IqfeedCaptureBootstrapBundleError(
                    "bootstrap staging cleanup encountered a reparse point"
                )
            if stat.S_ISDIR(status.st_mode):
                remove(child)
            elif stat.S_ISREG(status.st_mode):
                child.unlink()
            else:
                raise IqfeedCaptureBootstrapBundleError(
                    "bootstrap staging cleanup encountered a special file"
                )
        directory.rmdir()

    remove(path)


def _load_build_request(
    *,
    request_path: str | Path,
    request_sha256: str,
    allowed_read_roots: Sequence[str | Path],
    local_drive_check: Callable[[Path], bool] = preflight_module._default_local_drive_check,
) -> tuple[Mapping[str, Any], tuple[Path, ...]]:
    roots = preflight_module._normalized_roots(
        allowed_read_roots,
        field="allowed_read_roots",
        local_drive_check=local_drive_check,
    )
    artifact = preflight_module._read_hash_bound_json(
        request_path,
        request_sha256,
        field="bootstrap_build_request",
        roots=roots,
        max_bytes=_MAX_REQUEST_BYTES,
        local_drive_check=local_drive_check,
        content_addressed_filename=False,
    )
    request = artifact.document
    _exact_keys(request, _BUILD_REQUEST_FIELDS, "bootstrap_build_request")
    if request.get("schema_version") != BUILD_REQUEST_SCHEMA_VERSION:
        raise IqfeedCaptureBootstrapBundleError(
            "bootstrap build request schema is unsupported"
        )
    benchmark = _mapping(
        request.get("resource_benchmark"),
        "resource_benchmark",
        nonempty=True,
    )
    _exact_keys(benchmark, {"path", "sha256"}, "resource_benchmark")
    _source_pins(request.get("source_sha256"))
    account_id = _uuid(request.get("expected_account_id"), "expected_account_id")
    _safe_account_risk_snapshot(request.get("account_risk_snapshot"))
    _safe_account_query(request.get("account_query"), expected_account_id=account_id)
    for field in ("effective_config", "bridge_configuration"):
        embedded = _mapping(request.get(field), field, nonempty=True)
        _safe_projection(embedded, field)
    return request, roots


def _publish_object(
    root: Path,
    value: Mapping[str, Any],
    *,
    allowed_write_roots: Sequence[Path],
    local_drive_check: Callable[[Path], bool] = preflight_module._default_local_drive_check,
) -> tuple[Path, str]:
    raw = _canonical_json_bytes(value)
    if not raw or len(raw) > _MAX_REQUEST_BYTES:
        raise IqfeedCaptureBootstrapBundleError(
            "bootstrap object exceeds its bounded publication size"
        )
    digest = hashlib.sha256(raw).hexdigest()
    path = root / digest[:2] / f"{digest}.json"
    parent = _ensure_safe_directory(
        path.parent,
        roots=allowed_write_roots,
        field="bootstrap object directory",
        local_drive_check=local_drive_check,
    )
    parent_identity = _directory_identity(parent, "bootstrap object directory")
    pending = path.parent / f".{digest}.{uuid.uuid4()}.pending"
    visible = False
    pending_identity: tuple[int, int] | None = None
    try:
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(pending, flags, 0o600)
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                raise IqfeedCaptureBootstrapBundleError(
                    "bootstrap pending object is not a regular file"
                )
            pending_identity = (int(opened.st_dev), int(opened.st_ino))
            offset = 0
            while offset < len(raw):
                written = os.write(descriptor, raw[offset:])
                if written <= 0:
                    raise IqfeedCaptureBootstrapBundleError(
                        "bootstrap pending object write made no progress"
                    )
                offset += written
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _reject_reparse_chain(parent)
        if _directory_identity(parent, "bootstrap object directory") != parent_identity:
            raise IqfeedCaptureBootstrapBundleError(
                "bootstrap object directory changed before publication"
            )
        try:
            # A same-volume hard link gives no-overwrite atomic publication:
            # readers can observe either no object or the complete fsynced bytes.
            os.link(pending, path, follow_symlinks=False)
            visible = True
            if _directory_identity(parent, "bootstrap object directory") != parent_identity:
                raise IqfeedCaptureBootstrapBundleError(
                    "bootstrap object directory changed during publication"
                )
            _fsync_directory(parent)
        except FileExistsError:
            try:
                existing = preflight_module._read_bytes_stable(
                    path,
                    field="published bootstrap object",
                    max_bytes=_MAX_REQUEST_BYTES,
                )
            except preflight_module.BootstrapPreflightError as exc:
                raise IqfeedCaptureBootstrapBundleError(
                    "existing bootstrap object could not be verified"
                ) from exc
            if existing != raw:
                raise IqfeedCaptureBootstrapBundleError(
                    "content-addressed bootstrap object collision"
                )
            visible = True
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        if isinstance(exc, IqfeedCaptureBootstrapBundleError):
            error = exc
        else:
            error = IqfeedCaptureBootstrapBundleError(
                "bootstrap object publication failed"
            )
        if visible and not error.visible_objects:
            error.visible_objects = (
                {"path": str(path), "sha256": digest},
            )
        raise error from (None if error is exc else exc)
    finally:
        try:
            try:
                pending_status = pending.lstat()
            except FileNotFoundError:
                pending_status = None
            if pending_status is not None:
                attrs = int(
                    getattr(pending_status, "st_file_attributes", 0) or 0
                )
                if (
                    pending_identity is None
                    or stat.S_ISLNK(pending_status.st_mode)
                    or attrs & _REPARSE_ATTRIBUTE
                    or (int(pending_status.st_dev), int(pending_status.st_ino))
                    != pending_identity
                ):
                    raise IqfeedCaptureBootstrapBundleError(
                        "bootstrap pending object identity changed before cleanup",
                        visible_objects=(
                            ({"path": str(path), "sha256": digest},)
                            if visible
                            else ()
                        ),
                    )
                pending.unlink()
        except OSError as exc:
            raise IqfeedCaptureBootstrapBundleError(
                "bootstrap pending object cleanup failed",
                visible_objects=(
                    ({"path": str(path), "sha256": digest},) if visible else ()
                ),
            ) from exc
    if _stable_sha256(path) != digest:
        raise IqfeedCaptureBootstrapBundleError(
            "published bootstrap object failed its digest check",
            visible_objects=({"path": str(path), "sha256": digest},),
        )
    return path, digest


def _source_roster(
    repo_root: Path,
    *,
    expected_source_hashes: Mapping[str, str],
    local_drive_check: Callable[[Path], bool] = preflight_module._default_local_drive_check,
) -> tuple[list[dict[str, str]], dict[str, Path], dict[str, str]]:
    pins = _source_pins(expected_source_hashes)
    rows: list[dict[str, str]] = []
    paths: dict[str, Path] = {}
    hashes: dict[str, str] = {}
    for role, relative in sorted(_SOURCE_RELATIVE_PATHS.items()):
        path = _local_file(
            repo_root / relative,
            f"source.{role}",
            local_drive_check=local_drive_check,
        )
        try:
            path.relative_to(repo_root)
        except ValueError as exc:
            raise IqfeedCaptureBootstrapBundleError(
                f"source role escaped repository: {role}"
            ) from exc
        digest = _stable_sha256(path)
        if digest != pins[role]:
            raise IqfeedCaptureBootstrapBundleError(
                f"source.{role} content hash differs from its external pin"
            )
        paths[role] = path
        hashes[role] = digest
        rows.append({"role": role, "path": str(path), "sha256": digest})
    if set(paths) != set(preflight_module._REQUIRED_SOURCE_ROLES):
        raise IqfeedCaptureBootstrapBundleError(
            "bootstrap source role inventory differs from the preflight contract"
        )
    return rows, paths, hashes


def build_iqfeed_capture_bootstrap_bundle(
    *,
    repo_root: str | Path,
    artifact_root: str | Path,
    capture_store_root: str | Path,
    resource_benchmark_path: str | Path,
    resource_benchmark_sha256: str,
    expected_source_hashes: Mapping[str, str],
    expected_account_id: str,
    account_risk_snapshot: Mapping[str, Any],
    account_query: Mapping[str, Any],
    account_received_at: datetime,
    account_available_at: datetime,
    effective_config: Mapping[str, Any],
    bridge_configuration: Mapping[str, Any],
    activation_generation: str,
    request_generated_at: datetime,
    build_request_sha256: str,
    allowed_read_roots: Sequence[str | Path],
    allowed_write_roots: Sequence[str | Path],
    wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    host_fingerprint_provider: Callable[[], str] = preflight_module._host_fingerprint,
    local_drive_check: Callable[[Path], bool] = preflight_module._default_local_drive_check,
    generation: int = 1,
) -> BuiltIqfeedCaptureBootstrapBundle:
    """Publish and re-load one inert bootstrap bundle.

    The public boundary revalidates and projects every caller-provided object;
    callers cannot bypass request freshness, credential, or path checks.
    """

    now = _sample_wall_clock(wall_clock)
    request_at = _utc(request_generated_at, "request_generated_at")
    _validate_observed_freshness(
        request_at,
        now=now,
        max_age_seconds=_MAX_REQUEST_AGE_SECONDS,
        field="bootstrap build request",
    )
    request_sha = _sha(build_request_sha256, "build_request_sha256")
    read_roots = preflight_module._normalized_roots(
        allowed_read_roots,
        field="allowed_read_roots",
        local_drive_check=local_drive_check,
    )
    write_roots = preflight_module._normalized_roots(
        allowed_write_roots,
        field="allowed_write_roots",
        local_drive_check=local_drive_check,
    )
    repo = _request_path(
        repo_root,
        roots=read_roots,
        field="repo_root",
        write_target=False,
        require_directory=True,
        local_drive_check=local_drive_check,
    )
    artifacts = _request_path(
        artifact_root,
        roots=write_roots,
        field="artifact_root",
        write_target=True,
        require_directory=True,
        local_drive_check=local_drive_check,
    )
    capture_store = _request_path(
        capture_store_root,
        roots=write_roots,
        field="capture_store_root",
        write_target=True,
        require_directory=False,
        local_drive_check=local_drive_check,
    )
    benchmark_artifact = preflight_module._read_hash_bound_json(
        resource_benchmark_path,
        resource_benchmark_sha256,
        field="resource_benchmark",
        roots=read_roots,
        max_bytes=preflight_module._MAX_BENCHMARK_BYTES,
        local_drive_check=local_drive_check,
    )
    benchmark_path = benchmark_artifact.path
    benchmark_sha = benchmark_artifact.sha256
    benchmark = benchmark_artifact.document
    account_id = _uuid(expected_account_id, "expected_account_id")
    activation_id = _uuid(activation_generation, "activation_generation")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation <= 0:
        raise IqfeedCaptureBootstrapBundleError(
            "bootstrap generation must be positive"
        )
    received_at = _utc(account_received_at, "account_received_at")
    available_at = _utc(account_available_at, "account_available_at")
    if available_at < received_at or available_at > now:
        raise IqfeedCaptureBootstrapBundleError(
            "account snapshot clocks are causally inconsistent"
        )
    for value, field in (
        (received_at, "account_received_at"),
        (available_at, "account_available_at"),
    ):
        _validate_observed_freshness(
            value,
            now=now,
            max_age_seconds=_MAX_ACCOUNT_SNAPSHOT_AGE_SECONDS,
            field=field,
        )
    safe_risk = _safe_account_risk_snapshot(account_risk_snapshot)
    safe_query = _safe_account_query(account_query, expected_account_id=account_id)
    safe_effective_config = _mapping(
        _safe_projection(effective_config, "effective_config"),
        "effective_config",
        nonempty=True,
    )
    safe_bridge_configuration = _mapping(
        _safe_projection(bridge_configuration, "bridge_configuration"),
        "bridge_configuration",
        nonempty=True,
    )
    if set(safe_bridge_configuration) != {"iqfeed_l1", "iqfeed_l2"}:
        raise IqfeedCaptureBootstrapBundleError(
            "bridge configuration must contain exact L1/L2 lanes"
        )

    if benchmark.get("benchmark_schema_version") != preflight_module.BENCHMARK_SCHEMA_VERSION:
        raise IqfeedCaptureBootstrapBundleError(
            "resource benchmark schema is unsupported"
        )
    resolved = benchmark.get("resolved_resource_binding")
    if not isinstance(resolved, Mapping):
        raise IqfeedCaptureBootstrapBundleError(
            "resource benchmark lacks a resolved binding"
        )
    budget = resolved.get("budget")
    if not isinstance(budget, Mapping):
        raise IqfeedCaptureBootstrapBundleError(
            "resource benchmark lacks a resolved budget"
        )
    binding_sha256 = _sha(
        resolved.get("binding_sha256"), "resource_binding_sha256"
    )

    source_rows, source_paths, source_hashes = _source_roster(
        repo,
        expected_source_hashes=expected_source_hashes,
        local_drive_check=local_drive_check,
    )
    benchmark_sources = benchmark.get("capture_runtime_source")
    expected_benchmark_sources = {
        "benchmark_script_sha256": source_hashes[
            "benchmark_replay_capture_runtime"
        ],
        "contract_sha256": source_hashes["replay_capture_contract"],
        "runtime_sha256": source_hashes["replay_capture_runtime"],
    }
    if dict(benchmark_sources or {}) != expected_benchmark_sources:
        raise IqfeedCaptureBootstrapBundleError(
            "resource benchmark source bytes differ from the current capture runtime"
        )

    max_events = int(budget.get("max_queue_events") or 0)
    max_bytes = int(budget.get("async_queue_bytes") or 0)
    max_gaps = int(budget.get("max_gap_keys") or 0)
    if min(max_events, max_bytes, max_gaps) <= 4:
        raise IqfeedCaptureBootstrapBundleError(
            "resource benchmark budget is too small for two bounded handoffs"
        )
    l1 = {
        "max_pending_events": max(1, min(10_000, max_events // 8)),
        "max_pending_bytes": max(1, min(32 * 1024 * 1024, max_bytes // 8)),
        "max_gap_keys": max(1, min(512, max_gaps // 8)),
    }
    l2 = {
        "max_pending_events": max(1, min(20_000, max_events // 4)),
        "max_pending_bytes": max(1, min(64 * 1024 * 1024, max_bytes // 4)),
        "max_gap_keys": max(1, min(1024, max_gaps // 4)),
    }
    aggregate = {name: l1[name] + l2[name] for name in l1}
    downstream = {
        "max_pending_events": max_events - aggregate["max_pending_events"],
        "max_pending_bytes": max_bytes - aggregate["max_pending_bytes"],
        "max_gap_keys": max_gaps - aggregate["max_gap_keys"],
    }
    if min(downstream.values()) <= 0:
        raise IqfeedCaptureBootstrapBundleError(
            "handoff allocation leaves no downstream capture budget"
        )

    feature_flags = {
        "CHILI_MOMENTUM_LIVE_RUNNER_ENABLED": False,
        "CHILI_MOMENTUM_LIVE_RUNNER_SCHEDULER_ENABLED": False,
        "CHILI_MOMENTUM_LIVE_RUNNER_LOOP_ENABLED": False,
    }
    startup = {
        "schema_version": preflight_module.STARTUP_EVIDENCE_SCHEMA_VERSION,
        "captured_at": _iso(now),
        "generation": int(generation),
        "process_instance_id": str(uuid.uuid4()),
        "broker": "alpaca",
        "broker_environment": "paper",
        "code_build": {
            "schema_version": preflight_module.CODE_BUILD_SCHEMA_VERSION,
            "artifacts": source_rows,
        },
        "effective_config": {
            **dict(safe_effective_config),
            "activation_generation": activation_id,
            "capture_profile": "diagnostic_only_bootstrap",
            "paper_order_submission_enabled": False,
            "live_cash_order_submission_enabled": False,
        },
        "feature_flags": feature_flags,
        "account_identity": {
            "broker": "alpaca",
            "environment": "paper",
            "account_id": account_id,
            "account_scope": "alpaca:paper",
        },
        "account_risk_snapshot": dict(safe_risk),
        "account_query": dict(safe_query),
        "account_provider": "alpaca",
        "account_snapshot_clocks": {
            "provider_event_at": None,
            "received_at": _iso(received_at),
            "available_at": _iso(available_at),
        },
        "bridge_configuration": dict(safe_bridge_configuration),
        "bridge_configuration_sha256": _sha256_json(safe_bridge_configuration),
        "iqfeed_l1_clock_contract": {
            "schema_version": preflight_module.IQFEED_L1_CLOCK_CONTRACT_SCHEMA_VERSION,
            "exact_print": {
                "message_type": "Q",
                "selected_field_ack_required": True,
                "provider_event_at_available": True,
                "event_clock_basis": "most_recent_trade_date_plus_timems",
                "tick_identity_field": "TickID",
                "certifying_exact_event_clock": True,
            },
            "nbbo_quote": {
                "message_type": "Q",
                "provider_event_at_available": False,
                "market_reference_basis": "most_recent_trade_date_plus_timems",
                "certifying_exact_event_clock": False,
            },
        },
        "iqfeed_l2_clock_contract": {
            "schema_version": preflight_module.IQFEED_L2_CLOCK_CONTRACT_SCHEMA_VERSION,
            "delta": {
                "message_type": "6",
                "provider_event_at_available": True,
                "event_clock_basis": "type6_provider_date_plus_time",
                "certifying_exact_event_clock": True,
            },
            "checkpoint": {
                "provider_event_at_available": False,
                "per_level_exact_clocks_required": True,
                "initial_snapshot_complete": False,
                "certifying_snapshot_completion": False,
            },
        },
    }
    def manifest_for(startup_path: Path, startup_sha: str) -> dict[str, Any]:
        return {
            "schema_version": preflight_module.BOOTSTRAP_MANIFEST_SCHEMA_VERSION,
            "capture_mode": "diagnostic_only",
            "execution_boundary": {
                "alpaca_paper_order_submission_enabled": False,
                "live_cash_order_submission_enabled": False,
                "provider_socket_start_enabled": False,
                "database_write_start_enabled": False,
                "network_fallback_allowed": False,
                "current_database_fallback_allowed": False,
            },
            "freshness_policy": {
                "max_future_skew_seconds": _MAX_FUTURE_SKEW_SECONDS,
                "resource_benchmark_max_age_seconds": 7 * 24 * 60 * 60.0,
                "startup_evidence_max_age_seconds": _MAX_STARTUP_EVIDENCE_AGE_SECONDS,
            },
            "resource_benchmark": {
                "path": str(benchmark_path),
                "sha256": benchmark_sha,
                "binding_sha256": binding_sha256,
            },
            "startup_evidence": {
                "path": str(startup_path),
                "sha256": startup_sha,
            },
            "capture_store_root": str(capture_store),
            "run_configuration": {
                "schema_version": preflight_module.RUN_CONFIGURATION_SCHEMA_VERSION,
                "heartbeat_timeout_seconds": 30.0,
                "pretrigger_horizon_seconds": 300.0,
                "per_symbol_pretrigger_events": min(50_000, max(1, max_events // 4)),
                "writer_batch_events": 256,
                "writer_batch_bytes": min(
                    1024 * 1024,
                    max(1, downstream["max_pending_bytes"] // 8),
                ),
                "writer_poll_seconds": 0.05,
                "writer_flush_interval_seconds": 0.5,
                "max_change_keys": min(
                    2_000,
                    max(1, downstream["max_gap_keys"]),
                ),
                "max_read_sources": 1_000,
            },
            "handoff_configuration": {
                "schema_version": preflight_module.IQFEED_HANDOFF_BUDGET_SCHEMA_VERSION,
                "l1": l1,
                "l2": l2,
            },
        }

    preflight_read_roots = tuple(dict.fromkeys((*read_roots, *write_roots)))
    staging_root = artifacts / ".staging" / str(uuid.uuid4())
    _ensure_safe_directory(
        staging_root,
        roots=write_roots,
        field="bootstrap private staging directory",
        local_drive_check=local_drive_check,
    )
    try:
        staging_objects = staging_root / "objects"
        staged_startup_path, staged_startup_sha = _publish_object(
            staging_objects,
            startup,
            allowed_write_roots=write_roots,
            local_drive_check=local_drive_check,
        )
        staged_manifest = manifest_for(staged_startup_path, staged_startup_sha)
        staged_manifest_path, staged_manifest_sha = _publish_object(
            staging_objects,
            staged_manifest,
            allowed_write_roots=write_roots,
            local_drive_check=local_drive_check,
        )
        preflight_module.load_iqfeed_capture_bootstrap_preflight(
            staged_manifest_path,
            expected_manifest_sha256=staged_manifest_sha,
            allowed_read_roots=preflight_read_roots,
            allowed_write_roots=write_roots,
            wall_clock=lambda: now,
            host_fingerprint_provider=host_fingerprint_provider,
            local_drive_check=local_drive_check,
        )
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        if isinstance(exc, IqfeedCaptureBootstrapBundleError):
            raise IqfeedCaptureBootstrapBundleError(str(exc), code=exc.code) from exc
        raise
    finally:
        if staging_root.exists():
            _remove_private_staging(staging_root, artifact_root=artifacts)

    object_root = artifacts / "objects"
    visible_objects: list[dict[str, str]] = []
    try:
        startup_path, startup_sha = _publish_object(
            object_root,
            startup,
            allowed_write_roots=write_roots,
            local_drive_check=local_drive_check,
        )
        visible_objects.append({"path": str(startup_path), "sha256": startup_sha})
        manifest = manifest_for(startup_path, startup_sha)
        manifest_path, manifest_sha = _publish_object(
            object_root,
            manifest,
            allowed_write_roots=write_roots,
            local_drive_check=local_drive_check,
        )
        visible_objects.append({"path": str(manifest_path), "sha256": manifest_sha})
        loaded = preflight_module.load_iqfeed_capture_bootstrap_preflight(
            manifest_path,
            expected_manifest_sha256=manifest_sha,
            allowed_read_roots=preflight_read_roots,
            allowed_write_roots=write_roots,
            wall_clock=lambda: now,
            host_fingerprint_provider=host_fingerprint_provider,
            local_drive_check=local_drive_check,
        )
        commit_document = {
            "schema_version": BUNDLE_COMMIT_SCHEMA_VERSION,
            "accepted": True,
            "build_request_sha256": request_sha,
            "built_at": _iso(now),
            "activation_generation": activation_id,
            "expected_account_id": account_id,
            "manifest": {"path": str(manifest_path), "sha256": manifest_sha},
            "startup_evidence": {
                "path": str(startup_path),
                "sha256": startup_sha,
            },
            "resource_benchmark": {
                "path": str(benchmark_path),
                "sha256": benchmark_sha,
                "binding_sha256": binding_sha256,
            },
            "source_roster_sha256": _sha256_json(source_rows),
            "preflight_report_sha256": loaded.report["preflight_report_sha256"],
            "paper_order_submission_authorized": False,
            "live_cash_authorized": False,
        }
        commit_path, commit_sha = _publish_object(
            object_root,
            commit_document,
            allowed_write_roots=write_roots,
            local_drive_check=local_drive_check,
        )
        visible_objects.append({"path": str(commit_path), "sha256": commit_sha})
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        extra = tuple(getattr(exc, "visible_objects", ()))
        combined = tuple(
            dict(row)
            for row in {
                (row["path"], row["sha256"]): row
                for row in (*visible_objects, *extra)
            }.values()
        )
        if isinstance(exc, IqfeedCaptureBootstrapBundleError):
            raise IqfeedCaptureBootstrapBundleError(
                str(exc),
                code=exc.code,
                visible_objects=combined,
                commit_published=False,
            ) from exc
        raise IqfeedCaptureBootstrapBundleError(
            "bootstrap final publication failed",
            visible_objects=combined,
            commit_published=False,
        ) from exc

    receipt = {
        "schema_version": BUILDER_SCHEMA_VERSION,
        "build_request_sha256": request_sha,
        "manifest_path": str(manifest_path),
        "manifest_sha256": manifest_sha,
        "commit_path": str(commit_path),
        "commit_sha256": commit_sha,
        "startup_evidence_sha256": startup_sha,
        "resource_benchmark_sha256": benchmark_sha,
        "resource_binding_sha256": binding_sha256,
        "capture_store_root": str(capture_store),
        "activation_generation": activation_id,
        "expected_account_id": account_id,
        "source_roster_sha256": _sha256_json(source_rows),
        "preflight_report_sha256": loaded.report["preflight_report_sha256"],
        "paper_order_submission_authorized": False,
        "live_cash_authorized": False,
        "provider_sockets_started": False,
        "database_or_broker_started": False,
        "commit_published": True,
    }
    return BuiltIqfeedCaptureBootstrapBundle(
        manifest_path=manifest_path,
        manifest_sha256=manifest_sha,
        startup_evidence_path=startup_path,
        startup_evidence_sha256=startup_sha,
        resource_benchmark_path=benchmark_path,
        resource_benchmark_sha256=benchmark_sha,
        commit_path=commit_path,
        commit_sha256=commit_sha,
        capture_store_root=capture_store,
        source_paths=MappingProxyType(source_paths),
        source_hashes=MappingProxyType(source_hashes),
        preflight=loaded,
        builder_receipt=MappingProxyType(receipt),
    )


def build_iqfeed_capture_bootstrap_bundle_from_request(
    *,
    request_path: str | Path,
    request_sha256: str,
    allowed_read_roots: Sequence[str | Path],
    allowed_write_roots: Sequence[str | Path],
    wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    host_fingerprint_provider: Callable[[], str] = preflight_module._host_fingerprint,
    local_drive_check: Callable[[Path], bool] = preflight_module._default_local_drive_check,
) -> BuiltIqfeedCaptureBootstrapBundle:
    """Build from one canonical, externally pinned, local-only request."""

    request, read_roots = _load_build_request(
        request_path=request_path,
        request_sha256=request_sha256,
        allowed_read_roots=allowed_read_roots,
        local_drive_check=local_drive_check,
    )
    now = _sample_wall_clock(wall_clock)
    request_generated_at = preflight_module._parse_utc(
        request.get("generated_at"),
        "generated_at",
    )
    _validate_observed_freshness(
        request_generated_at,
        now=now,
        max_age_seconds=_MAX_REQUEST_AGE_SECONDS,
        field="bootstrap build request",
    )
    received_at = preflight_module._parse_utc(
        request.get("account_received_at"),
        "account_received_at",
    )
    available_at = preflight_module._parse_utc(
        request.get("account_available_at"),
        "account_available_at",
    )
    if available_at < received_at or available_at > now:
        raise IqfeedCaptureBootstrapBundleError(
            "account snapshot clocks are causally inconsistent"
        )
    for value, field in (
        (received_at, "account_received_at"),
        (available_at, "account_available_at"),
    ):
        _validate_observed_freshness(
            value,
            now=now,
            max_age_seconds=_MAX_ACCOUNT_SNAPSHOT_AGE_SECONDS,
            field=field,
        )
    write_roots = preflight_module._normalized_roots(
        allowed_write_roots,
        field="allowed_write_roots",
        local_drive_check=local_drive_check,
    )
    repo = _request_path(
        request.get("repo_root"),
        roots=read_roots,
        field="repo_root",
        write_target=False,
        require_directory=True,
        local_drive_check=local_drive_check,
    )
    artifacts = _request_path(
        request.get("artifact_root"),
        roots=write_roots,
        field="artifact_root",
        write_target=True,
        require_directory=True,
        local_drive_check=local_drive_check,
    )
    capture_store = _request_path(
        request.get("capture_store_root"),
        roots=write_roots,
        field="capture_store_root",
        write_target=True,
        require_directory=False,
        local_drive_check=local_drive_check,
    )
    benchmark_ref = _mapping(
        request.get("resource_benchmark"),
        "resource_benchmark",
        nonempty=True,
    )
    benchmark = preflight_module._read_hash_bound_json(
        benchmark_ref.get("path"),
        benchmark_ref.get("sha256"),
        field="resource_benchmark",
        roots=read_roots,
        max_bytes=preflight_module._MAX_BENCHMARK_BYTES,
        local_drive_check=local_drive_check,
    )
    return build_iqfeed_capture_bootstrap_bundle(
        repo_root=repo,
        artifact_root=artifacts,
        capture_store_root=capture_store,
        resource_benchmark_path=benchmark.path,
        resource_benchmark_sha256=benchmark.sha256,
        expected_source_hashes=_source_pins(request.get("source_sha256")),
        expected_account_id=str(request.get("expected_account_id") or ""),
        account_risk_snapshot=_mapping(
            request.get("account_risk_snapshot"),
            "account_risk_snapshot",
            nonempty=True,
        ),
        account_query=_mapping(
            request.get("account_query"),
            "account_query",
            nonempty=True,
        ),
        account_received_at=received_at,
        account_available_at=available_at,
        effective_config=_mapping(
            request.get("effective_config"),
            "effective_config",
            nonempty=True,
        ),
        bridge_configuration=_mapping(
            request.get("bridge_configuration"),
            "bridge_configuration",
            nonempty=True,
        ),
        activation_generation=str(request.get("activation_generation") or ""),
        request_generated_at=request_generated_at,
        build_request_sha256=_sha(request_sha256, "request_sha256"),
        allowed_read_roots=read_roots,
        allowed_write_roots=write_roots,
        wall_clock=lambda: now,
        host_fingerprint_provider=host_fingerprint_provider,
        local_drive_check=local_drive_check,
        generation=request.get("generation"),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True)
    parser.add_argument("--request-sha256", required=True)
    parser.add_argument("--allow-read-root", action="append", required=True)
    parser.add_argument("--allow-write-root", action="append", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(list(argv) if argv is not None else None)
    try:
        built = build_iqfeed_capture_bootstrap_bundle_from_request(
            request_path=args.request,
            request_sha256=args.request_sha256,
            allowed_read_roots=tuple(args.allow_read_root),
            allowed_write_roots=tuple(args.allow_write_root),
        )
        report: dict[str, Any] = {
            "schema_version": BUILDER_REPORT_SCHEMA_VERSION,
            "verdict": "IQFEED_CAPTURE_BOOTSTRAP_BUNDLE_PUBLISHED",
            "request_sha256": _sha(args.request_sha256, "request_sha256"),
            "manifest_path": str(built.manifest_path),
            "manifest_sha256": built.manifest_sha256,
            "commit_path": str(built.commit_path),
            "commit_sha256": built.commit_sha256,
            "startup_evidence_path": str(built.startup_evidence_path),
            "startup_evidence_sha256": built.startup_evidence_sha256,
            "resource_benchmark_path": str(built.resource_benchmark_path),
            "resource_benchmark_sha256": built.resource_benchmark_sha256,
            "capture_store_root": str(built.capture_store_root),
            "source_sha256": dict(sorted(built.source_hashes.items())),
            "builder_receipt_sha256": _sha256_json(dict(built.builder_receipt)),
            "offline_tooling_only": True,
            "bootstrap_artifact_published": True,
            "commit_published": True,
            "visible_objects": [
                {
                    "path": str(built.startup_evidence_path),
                    "sha256": built.startup_evidence_sha256,
                },
                {
                    "path": str(built.manifest_path),
                    "sha256": built.manifest_sha256,
                },
                {"path": str(built.commit_path), "sha256": built.commit_sha256},
            ],
            "provider_sockets_started": False,
            "database_accessed": False,
            "broker_accessed": False,
            "tasks_or_processes_changed": False,
            "paper_order_submission_authorized": False,
            "live_cash_authorized": False,
        }
        code = 0
    except (
        IqfeedCaptureBootstrapBundleError,
        preflight_module.BootstrapPreflightError,
        OSError,
        TypeError,
        ValueError,
    ) as exc:
        report = {
            "schema_version": BUILDER_REPORT_SCHEMA_VERSION,
            "verdict": "IQFEED_CAPTURE_BOOTSTRAP_BUNDLE_REJECTED",
            "error_code": str(
                getattr(exc, "code", "BOOTSTRAP_BUNDLE_REJECTED")
            ),
            "offline_tooling_only": True,
            "bootstrap_artifact_published": False,
            "commit_published": bool(getattr(exc, "commit_published", False)),
            "visible_objects": list(getattr(exc, "visible_objects", ())),
            "provider_sockets_started": False,
            "database_accessed": False,
            "broker_accessed": False,
            "tasks_or_processes_changed": False,
            "paper_order_submission_authorized": False,
            "live_cash_authorized": False,
        }
        code = 2
    sys.stdout.buffer.write(_canonical_json_bytes(report) + b"\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "BUILD_REQUEST_SCHEMA_VERSION",
    "BUILDER_REPORT_SCHEMA_VERSION",
    "BUILDER_SCHEMA_VERSION",
    "BUNDLE_COMMIT_SCHEMA_VERSION",
    "BuiltIqfeedCaptureBootstrapBundle",
    "IqfeedCaptureBootstrapBundleError",
    "build_iqfeed_capture_bootstrap_bundle",
    "build_iqfeed_capture_bootstrap_bundle_from_request",
    "main",
]
