"""Hash-bound environment loader for the captured Alpaca PAPER service.

The existing desktop ``.env`` contains credentials and switches for several
unrelated execution rails.  A captured-paper worker must not inherit that
entire authority surface.  This module runs before any ``app`` import, loads a
small allowlist, removes cash-broker credentials from the process, and applies
an explicit fake-money/equity-only runtime posture.

No secret value is returned or serialized.  The receipt records only a
domain-separated digest for secret inputs and the exact effective value for
non-secret policy/configuration inputs.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
from types import MappingProxyType
from typing import Mapping, MutableMapping
import uuid

from dotenv.parser import parse_stream


RUNTIME_ENV_SCHEMA_VERSION = "chili.captured-paper-runtime-env.v1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_IQFEED_NOTIFY_CHANNEL_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_IQFEED_BRIDGE_BUILD_RE = re.compile(
    r"^iqfeed-l1-exact-print-provenance-v3\+sha256:[0-9a-f]{16}$"
)
_DEFAULT_IQFEED_NOTIFY_CHANNEL = "momentum_iqfeed_l1"
_MAX_ENVIRONMENT_BYTES = 16 * 1024 * 1024
_EXACT_ALLOWED = frozenset(
    {
        "DATABASE_URL",
        "MASSIVE_API_KEY",
        "POLYGON_API_KEY",
        "CHILI_ORTEX_API_KEY",
        "CHILI_ALPACA_API_KEY",
        "CHILI_ALPACA_API_SECRET",
        "CHILI_ALPACA_DATA_FEED",
        "CHILI_ALPACA_EXPECTED_ACCOUNT_ID",
        "CHILI_ALPACA_QUOTE_MAX_AGE_SECONDS",
        "CHILI_AUTOTRADER_USER_ID",
        "CHILI_AUTOPILOT_PRICE_BUS_ENABLED",
        "CHILI_CAPTURED_PAPER_CONFIG_ISOLATED",
        "CHILI_EQUITY_EXECUTION_RAIL",
    }
)
_ALLOWED_PREFIXES = (
    "CHILI_MOMENTUM_",
    "CHILI_IQFEED_",
    "IQFEED_",
    "CHILI_MASSIVE_",
    "MASSIVE_",
    "POLYGON_",
)
_SECRET_KEYS = frozenset(
    {
        "DATABASE_URL",
        "MASSIVE_API_KEY",
        "POLYGON_API_KEY",
        "CHILI_ORTEX_API_KEY",
        "CHILI_ALPACA_API_KEY",
        "CHILI_ALPACA_API_SECRET",
    }
)
_REQUIRED_SECRET_KEYS = frozenset(
    {"DATABASE_URL", "CHILI_ALPACA_API_KEY", "CHILI_ALPACA_API_SECRET"}
)

_CAPTURED_PAPER_OPERATIONAL_SETTING_NAMES = (
    "chili_momentum_captured_paper_action_claim_lease_seconds",
    "chili_momentum_captured_paper_outbox_max_attempts",
    "chili_momentum_captured_paper_outbox_max_reconciliation_attempts",
    "chili_momentum_captured_paper_reconciliation_retry_delay_seconds",
    "chili_momentum_captured_paper_reconciliation_health_escalation_seconds",
    "chili_momentum_captured_paper_time_in_force",
    "chili_momentum_captured_paper_extended_hours",
    "chili_momentum_captured_paper_worker_idle_poll_seconds",
    "chili_momentum_captured_paper_trigger_max_attempts",
    "chili_momentum_captured_paper_trigger_retry_delay_seconds",
    "chili_momentum_captured_paper_trigger_future_tolerance_seconds",
    "chili_momentum_captured_paper_trigger_exact_print_window_seconds",
)

# Remove these even when they came from the parent process instead of the file.
# This is deliberately broader than the import allowlist: the dedicated worker
# has no legitimate reason to possess a cash-broker credential.
_FORBIDDEN_EXACT = frozenset(
    {
        "CHILI_ALPACA_LIVE_API_KEY",
        "CHILI_ALPACA_LIVE_API_SECRET",
        "ALPACA_API_KEY",
        "ALPACA_API_SECRET",
        "APCA_API_KEY_ID",
        "APCA_API_SECRET_KEY",
        "APCA_API_BASE_URL",
        "CDP_API_KEY_NAME",
        "CDP_API_KEY_PRIVATE_KEY",
        "COINBASE_API_KEY",
        "COINBASE_API_SECRET",
        "COINBASE_PRIVATE_KEY",
        "ROBINHOOD_USERNAME",
        "ROBINHOOD_PASSWORD",
        "ROBINHOOD_MFA_CODE",
    }
)
_FORBIDDEN_PREFIXES = (
    "CHILI_ALPACA_LIVE_",
    "ALPACA_LIVE_",
    "CHILI_ROBINHOOD_",
    "ROBINHOOD_",
    "ROBIN_STOCKS_",
    "CHILI_COINBASE_",
    "COINBASE_",
    "CHILI_KRAKEN_",
    "KRAKEN_",
    "CHILI_BINANCE_",
    "BINANCE_",
    "CHILI_OANDA_",
    "OANDA_",
    "CHILI_TRADIER_",
    "TRADIER_",
    "CHILI_HYPERLIQUID_",
    "HYPERLIQUID_",
    "CHILI_DYDX_",
    "DYDX_",
)


class CapturedPaperRuntimeEnvError(RuntimeError):
    """Stable bootstrap failure before application imports or external I/O."""


def _canonical_json(value: Mapping[str, object]) -> bytes:
    return json.dumps(
        dict(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _secret_fingerprint(key: str, value: str) -> str:
    return _sha256_bytes(
        b"chili.captured-paper.secret-fingerprint.v1\0"
        + key.encode("utf-8")
        + b"\0"
        + value.encode("utf-8")
    )


def _canonical_uuid(value: str) -> str:
    raw = str(value or "").strip().lower()
    try:
        parsed = uuid.UUID(raw)
    except (AttributeError, TypeError, ValueError) as exc:
        raise CapturedPaperRuntimeEnvError(
            "captured PAPER expected account id is not a canonical UUID"
        ) from exc
    if str(parsed) != raw:
        raise CapturedPaperRuntimeEnvError(
            "captured PAPER expected account id is not a canonical UUID"
        )
    return raw


def _canonical_positive_user_id(value: object) -> int:
    raw = str(value or "").strip()
    if re.fullmatch(r"[1-9][0-9]*", raw) is None:
        raise CapturedPaperRuntimeEnvError(
            "captured PAPER CHILI_AUTOTRADER_USER_ID must be a canonical "
            "positive integer"
        )
    parsed = int(raw)
    if parsed > 2_147_483_647:
        raise CapturedPaperRuntimeEnvError(
            "captured PAPER CHILI_AUTOTRADER_USER_ID is outside the DB integer range"
        )
    return parsed


def _is_forbidden(key: str) -> bool:
    upper = str(key or "").strip().upper()
    return upper in _FORBIDDEN_EXACT or upper.startswith(_FORBIDDEN_PREFIXES)


def _is_allowed(key: str) -> bool:
    upper = str(key or "").strip().upper()
    return upper in _EXACT_ALLOWED or upper.startswith(_ALLOWED_PREFIXES)


def _strict_local_file(path: str | Path) -> Path:
    raw = Path(path)
    if not raw.is_absolute() or str(raw).startswith(("\\\\", "//")):
        raise CapturedPaperRuntimeEnvError(
            "captured PAPER environment path must be absolute and local"
        )
    lexical = Path(os.path.abspath(str(raw)))
    cursor: Path | None = lexical
    while cursor is not None:
        try:
            metadata = cursor.lstat()
        except OSError as exc:
            raise CapturedPaperRuntimeEnvError(
                "captured PAPER environment path is unreadable"
            ) from exc
        file_attributes = int(getattr(metadata, "st_file_attributes", 0))
        if cursor.is_symlink() or bool(
            file_attributes & int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
        ):
            raise CapturedPaperRuntimeEnvError(
                "captured PAPER environment path contains a reparse link"
            )
        parent = cursor.parent
        cursor = None if parent == cursor else parent
    resolved = lexical.resolve(strict=True)
    if not resolved.is_file():
        raise CapturedPaperRuntimeEnvError(
            "captured PAPER environment path is not a regular file"
        )
    cursor: Path | None = resolved
    while cursor is not None:
        metadata = cursor.lstat()
        file_attributes = int(getattr(metadata, "st_file_attributes", 0))
        if cursor.is_symlink() or bool(
            file_attributes & int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
        ):
            raise CapturedPaperRuntimeEnvError(
                "captured PAPER environment path contains a reparse link"
            )
        parent = cursor.parent
        cursor = None if parent == cursor else parent
    return resolved


def _read_hash_bound_env(
    path: str | Path,
    *,
    expected_sha256: str,
) -> tuple[Path, Mapping[str, str]]:
    expected = str(expected_sha256 or "").strip().lower()
    if _SHA256_RE.fullmatch(expected) is None:
        raise CapturedPaperRuntimeEnvError(
            "captured PAPER environment SHA-256 is malformed"
        )
    resolved = _strict_local_file(path)
    try:
        path_before = resolved.stat()
        if path_before.st_size < 0 or path_before.st_size > _MAX_ENVIRONMENT_BYTES:
            raise CapturedPaperRuntimeEnvError(
                "captured PAPER environment exceeds its bounded size"
            )
        with resolved.open("rb") as handle:
            handle_before = os.fstat(handle.fileno())
            raw = handle.read(_MAX_ENVIRONMENT_BYTES + 1)
            handle_after = os.fstat(handle.fileno())
        path_after = resolved.stat()
    except CapturedPaperRuntimeEnvError:
        raise
    except OSError as exc:
        raise CapturedPaperRuntimeEnvError(
            "captured PAPER environment changed or became unreadable"
        ) from exc
    identity = lambda item: (
        item.st_dev,
        item.st_ino,
        item.st_size,
        item.st_mtime_ns,
    )
    if (
        len(raw) > _MAX_ENVIRONMENT_BYTES
        or len(raw) != handle_after.st_size
        or identity(path_before) != identity(handle_before)
        or identity(handle_before) != identity(handle_after)
        or identity(handle_after) != identity(path_after)
    ):
        raise CapturedPaperRuntimeEnvError(
            "captured PAPER environment changed while being read"
        )
    if _sha256_bytes(raw) != expected:
        raise CapturedPaperRuntimeEnvError(
            "captured PAPER environment content hash mismatch"
        )
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CapturedPaperRuntimeEnvError(
            "captured PAPER environment is not UTF-8"
        ) from exc

    try:
        bindings = tuple(parse_stream(io.StringIO(text)))
    except Exception as exc:
        raise CapturedPaperRuntimeEnvError(
            "captured PAPER environment could not be parsed"
        ) from exc
    seen: set[str] = set()
    normalized: dict[str, str] = {}
    for binding in bindings:
        if binding.error:
            raise CapturedPaperRuntimeEnvError(
                "captured PAPER environment contains a malformed assignment"
            )
        key = str(binding.key or "").strip().upper()
        if not (
            _is_allowed(key)
            or _is_forbidden(key)
            or key.startswith("CHILI_ALPACA_")
        ):
            continue
        if key in seen:
            raise CapturedPaperRuntimeEnvError(
                "captured PAPER environment has duplicate key"
            )
        seen.add(key)
        raw_value = binding.value
        if _KEY_RE.fullmatch(key) is None or raw_value is None:
            raise CapturedPaperRuntimeEnvError(
                "captured PAPER environment contains a malformed assignment"
            )
        value = str(raw_value)
        if "\x00" in value:
            raise CapturedPaperRuntimeEnvError(
                "captured PAPER environment contains a NUL byte"
            )
        normalized[key] = value
    return resolved, MappingProxyType(normalized)


def _runtime_overrides(
    *,
    expected_account_id: str,
    first_dip_policy_mode: str,
    iqfeed_notify_channel: str,
    iqfeed_bridge_build: str,
) -> Mapping[str, str]:
    mode = str(first_dip_policy_mode or "").strip().lower()
    if mode != "candidate":
        raise CapturedPaperRuntimeEnvError(
            "prospective PAPER must exercise first-dip candidate policy"
        )
    return MappingProxyType(
        {
            "CHILI_ALPACA_ENABLED": "true",
            "CHILI_ALPACA_PAPER": "true",
            "CHILI_ALPACA_EXPECTED_ACCOUNT_ID": expected_account_id,
            "CHILI_CAPTURED_PAPER_CONFIG_ISOLATED": "true",
            "CHILI_ALPACA_QUOTES_VIA_IQFEED": "false",
            "CHILI_EQUITY_EXECUTION_RAIL": "alpaca",
            "CHILI_MOMENTUM_EQUITY_EXECUTION_VIA_ALPACA_PAPER": "true",
            "CHILI_MOMENTUM_CRYPTO_EXECUTION_VIA_ALPACA_PAPER": "false",
            "CHILI_MOMENTUM_AUTO_ARM_CRYPTO_ONLY": "false",
            "CHILI_MOMENTUM_AUTO_ARM_EQUITY_ONLY": "true",
            "CHILI_MOMENTUM_PAPER_RUNNER_ENABLED": "false",
            "CHILI_MOMENTUM_PAPER_RUNNER_SCHEDULER_ENABLED": "false",
            "CHILI_MOMENTUM_PAPER_RUNNER_DEV_TICK_ENABLED": "false",
            "CHILI_MOMENTUM_LIVE_RUNNER_ENABLED": "true",
            "CHILI_MOMENTUM_LIVE_RUNNER_SCHEDULER_ENABLED": "false",
            "CHILI_MOMENTUM_LIVE_RUNNER_LOOP_ENABLED": "true",
            "CHILI_MOMENTUM_LIVE_RUNNER_LOOP_IQFEED_NOTIFY_ENABLED": "true",
            "CHILI_MOMENTUM_LIVE_RUNNER_LOOP_IQFEED_NOTIFY_CHANNEL": (
                iqfeed_notify_channel
            ),
            "IQFEED_NOTIFY_ENABLED": "1",
            "IQFEED_NOTIFY_CHANNEL": iqfeed_notify_channel,
            "CHILI_IQFEED_L1_AUTHORITATIVE_BRIDGE_BUILD": iqfeed_bridge_build,
            "CHILI_MOMENTUM_LIVE_RUNNER_DEV_TICK_ENABLED": "false",
            "CHILI_MOMENTUM_AUTO_ARM_LIVE_ENABLED": "true",
            "CHILI_MOMENTUM_AUTO_ARM_LIVE_SCHEDULER_ENABLED": "false",
            "CHILI_MOMENTUM_FIRST_DIP_RECLAIM_POLICY_MODE": mode,
            "CHILI_MOMENTUM_SHORT_ENABLED": "false",
            "CHILI_MOMENTUM_SHORT_LANE_ENABLED": "false",
            "CHILI_AUTOPILOT_PRICE_BUS_ENABLED": "true",
        }
    )


@dataclass(frozen=True, slots=True)
class CapturedPaperRuntimeEnvironmentReceipt:
    source_path: str
    source_sha256: str
    expected_account_id: str
    first_dip_policy_mode: str
    effective_config: Mapping[str, str]
    secret_fingerprints: Mapping[str, str]
    removed_forbidden_keys: tuple[str, ...]
    configuration_sha256: str
    schema_version: str = RUNTIME_ENV_SCHEMA_VERSION

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "source_path": self.source_path,
            "source_sha256": self.source_sha256,
            "expected_account_id": self.expected_account_id,
            "first_dip_policy_mode": self.first_dip_policy_mode,
            "effective_config": dict(self.effective_config),
            "secret_fingerprints": dict(self.secret_fingerprints),
            "removed_forbidden_keys": list(self.removed_forbidden_keys),
            "configuration_sha256": self.configuration_sha256,
        }


def validate_installed_captured_paper_settings(
    settings: object,
    receipt: CapturedPaperRuntimeEnvironmentReceipt,
    *,
    environ: Mapping[str, str] | None = None,
) -> Mapping[str, object]:
    """Re-validate the parsed Pydantic settings before any runtime starts."""

    if type(receipt) is not CapturedPaperRuntimeEnvironmentReceipt:
        raise CapturedPaperRuntimeEnvError(
            "captured PAPER environment receipt is malformed"
        )
    expected_user_id = _canonical_positive_user_id(
        receipt.effective_config.get("CHILI_AUTOTRADER_USER_ID")
    )
    expected = {
        "chili_alpaca_enabled": True,
        "chili_alpaca_paper": True,
        "chili_alpaca_expected_account_id": receipt.expected_account_id,
        "chili_alpaca_quotes_via_iqfeed": False,
        "chili_equity_execution_rail": "alpaca",
        "chili_momentum_equity_execution_via_alpaca_paper": True,
        "chili_momentum_crypto_execution_via_alpaca_paper": False,
        "chili_momentum_auto_arm_crypto_only": False,
        "chili_momentum_auto_arm_equity_only": True,
        "chili_momentum_paper_runner_enabled": False,
        "chili_momentum_paper_runner_scheduler_enabled": False,
        "chili_momentum_paper_runner_dev_tick_enabled": False,
        "chili_momentum_live_runner_enabled": True,
        "chili_momentum_live_runner_scheduler_enabled": False,
        "chili_momentum_live_runner_loop_enabled": True,
        "chili_momentum_live_runner_loop_iqfeed_notify_enabled": True,
        "chili_momentum_live_runner_loop_iqfeed_notify_channel": str(
            receipt.effective_config.get("IQFEED_NOTIFY_CHANNEL") or ""
        ),
        "chili_iqfeed_l1_authoritative_bridge_build": str(
            receipt.effective_config.get(
                "CHILI_IQFEED_L1_AUTHORITATIVE_BRIDGE_BUILD"
            )
            or ""
        ),
        "chili_momentum_live_runner_dev_tick_enabled": False,
        "chili_momentum_auto_arm_live_enabled": True,
        "chili_momentum_auto_arm_live_scheduler_enabled": False,
        "chili_autopilot_price_bus_enabled": True,
        "chili_momentum_first_dip_reclaim_policy_mode": (
            receipt.first_dip_policy_mode
        ),
        "chili_momentum_short_enabled": False,
        "chili_momentum_short_lane_enabled": False,
        "chili_autotrader_user_id": expected_user_id,
    }
    mismatches = {
        name: {
            "expected": value,
            "actual": getattr(settings, name, None),
        }
        for name, value in expected.items()
        if getattr(settings, name, None) != value
    }
    if mismatches:
        raise CapturedPaperRuntimeEnvError(
            "captured PAPER parsed settings escaped the activation posture:"
            + ",".join(sorted(mismatches))
        )
    for name in ("chili_alpaca_api_key", "chili_alpaca_api_secret"):
        if not str(getattr(settings, name, "") or "").strip():
            raise CapturedPaperRuntimeEnvError(
                "captured PAPER parsed paper credential is missing"
            )
    for name in ("chili_alpaca_live_api_key", "chili_alpaca_live_api_secret"):
        if str(getattr(settings, name, "") or "").strip():
            raise CapturedPaperRuntimeEnvError(
                "captured PAPER parsed live-cash credential is present"
            )

    current_env = os.environ if environ is None else environ
    if current_env.get("CHILI_CAPTURED_PAPER_CONFIG_ISOLATED") != "true":
        raise CapturedPaperRuntimeEnvError(
            "captured PAPER Settings env-file isolation is absent"
        )
    leaked = sorted(key for key in current_env if _is_forbidden(key))
    if leaked:
        raise CapturedPaperRuntimeEnvError(
            "captured PAPER forbidden broker authority reappeared in process"
        )
    bridge_notify_enabled = str(
        current_env.get("IQFEED_NOTIFY_ENABLED") or ""
    ).strip().lower()
    if bridge_notify_enabled not in {"1", "true", "yes", "on"}:
        raise CapturedPaperRuntimeEnvError(
            "captured PAPER IQFeed bridge notify admission is disabled"
        )
    bridge_channel = str(
        current_env.get("IQFEED_NOTIFY_CHANNEL") or ""
    ).strip()
    consumer_channel = str(
        getattr(
            settings,
            "chili_momentum_live_runner_loop_iqfeed_notify_channel",
            "",
        )
        or ""
    ).strip()
    if (
        _IQFEED_NOTIFY_CHANNEL_RE.fullmatch(bridge_channel) is None
        or consumer_channel != bridge_channel
    ):
        raise CapturedPaperRuntimeEnvError(
            "captured PAPER IQFeed bridge and listener channels do not match"
        )
    bridge_build = str(
        getattr(settings, "chili_iqfeed_l1_authoritative_bridge_build", "") or ""
    ).strip()
    if _IQFEED_BRIDGE_BUILD_RE.fullmatch(bridge_build) is None:
        raise CapturedPaperRuntimeEnvError(
            "captured PAPER authoritative IQFeed bridge build is invalid"
        )
    # Imported only after the caller has installed the dedicated PAPER
    # environment.  The service entry point also hash-pins this module before
    # accepting its P&L-affecting projection.
    from app.services.trading.momentum_neural.adaptive_risk_policy import (
        adaptive_risk_policy_settings_projection,
    )

    adaptive_risk_policy = adaptive_risk_policy_settings_projection(settings)
    operational_policy_settings = {
        name: getattr(settings, name)
        for name in _CAPTURED_PAPER_OPERATIONAL_SETTING_NAMES
    }
    projection: dict[str, object] = {
        "schema_version": "chili.captured-paper-settings-projection.v1",
        "runtime_environment_sha256": receipt.configuration_sha256,
        "settings": dict(sorted(expected.items())),
        "adaptive_risk_policy": adaptive_risk_policy,
        "captured_paper_operational_policy": dict(
            sorted(operational_policy_settings.items())
        ),
        "captured_paper_config_isolated": True,
        "paper_credentials_present": True,
        "live_cash_credentials_present": False,
        "cash_broker_environment_keys_present": False,
    }
    projection["settings_projection_sha256"] = _sha256_bytes(
        _canonical_json(projection)
    )
    return MappingProxyType(projection)


def install_captured_paper_runtime_environment(
    env_path: str | Path,
    *,
    expected_env_sha256: str,
    expected_account_id: str,
    first_dip_policy_mode: str = "candidate",
    environ: MutableMapping[str, str] | None = None,
) -> CapturedPaperRuntimeEnvironmentReceipt:
    """Install the only environment an Alpaca PAPER worker may inherit.

    This function must run before importing ``app.config``.  It performs no
    database, provider, or broker I/O.
    """

    target = os.environ if environ is None else environ
    account_id = _canonical_uuid(expected_account_id)
    resolved, parsed = _read_hash_bound_env(
        env_path,
        expected_sha256=expected_env_sha256,
    )

    imported: dict[str, str] = {}
    for key, value in parsed.items():
        if _is_forbidden(key) or not _is_allowed(key):
            continue
        imported[key] = value

    missing = sorted(
        key for key in _REQUIRED_SECRET_KEYS if not imported.get(key, "").strip()
    )
    if missing:
        raise CapturedPaperRuntimeEnvError(
            "captured PAPER required environment inputs are missing:"
            + ",".join(missing)
        )
    # The dedicated service may never fall back to a generic/default app user.
    # Bind the exact positive DB owner into both the runtime-environment receipt
    # and the P&L-affecting settings projection before any application import.
    _canonical_positive_user_id(imported.get("CHILI_AUTOTRADER_USER_ID"))
    try:
        file_account_id = _canonical_uuid(
            imported.get("CHILI_ALPACA_EXPECTED_ACCOUNT_ID", "")
        )
    except CapturedPaperRuntimeEnvError as exc:
        raise CapturedPaperRuntimeEnvError(
            "captured PAPER hash-bound account id is missing or malformed"
        ) from exc
    if file_account_id != account_id:
        raise CapturedPaperRuntimeEnvError(
            "captured PAPER hash-bound account id does not match the expected UUID"
        )

    bridge_channel = str(
        imported.get("IQFEED_NOTIFY_CHANNEL")
        or imported.get("CHILI_MOMENTUM_LIVE_RUNNER_LOOP_IQFEED_NOTIFY_CHANNEL")
        or _DEFAULT_IQFEED_NOTIFY_CHANNEL
    ).strip()
    consumer_channel = str(
        imported.get("CHILI_MOMENTUM_LIVE_RUNNER_LOOP_IQFEED_NOTIFY_CHANNEL")
        or bridge_channel
    ).strip()
    if (
        _IQFEED_NOTIFY_CHANNEL_RE.fullmatch(bridge_channel) is None
        or consumer_channel != bridge_channel
    ):
        raise CapturedPaperRuntimeEnvError(
            "captured PAPER IQFeed bridge and listener channels do not match"
        )
    bridge_build = str(
        imported.get("CHILI_IQFEED_L1_AUTHORITATIVE_BRIDGE_BUILD") or ""
    ).strip()
    if _IQFEED_BRIDGE_BUILD_RE.fullmatch(bridge_build) is None:
        raise CapturedPaperRuntimeEnvError(
            "captured PAPER authoritative IQFeed bridge build is invalid"
        )
    overrides = _runtime_overrides(
        expected_account_id=account_id,
        first_dip_policy_mode=first_dip_policy_mode,
        iqfeed_notify_channel=bridge_channel,
        iqfeed_bridge_build=bridge_build,
    )
    effective = {**imported, **dict(overrides)}

    # Finish all validation before mutating the target process environment.
    # A malformed dedicated identity must not partially sanitize/install a
    # process and leave the caller with an ambiguous failed bootstrap.
    removed: set[str] = set()
    for existing in tuple(target):
        if _is_forbidden(existing):
            target.pop(existing, None)
            removed.add(existing.upper())

    # Remove stale trading settings inherited from a parent before installing
    # the hash-bound values.  Unrelated OS/application variables remain intact.
    for existing in tuple(target):
        upper = existing.upper()
        if _is_allowed(upper) or upper.startswith("CHILI_ALPACA_"):
            target.pop(existing, None)
    target.update(effective)

    leaked = sorted(key for key in target if _is_forbidden(key))
    if leaked:
        raise CapturedPaperRuntimeEnvError(
            "captured PAPER forbidden broker authority remained in process"
        )

    nonsecret = {
        key: value for key, value in effective.items() if key not in _SECRET_KEYS
    }
    secret_fingerprints = {
        key: _secret_fingerprint(key, effective[key])
        for key in sorted(_SECRET_KEYS & effective.keys())
    }
    config_body: dict[str, object] = {
        "schema_version": RUNTIME_ENV_SCHEMA_VERSION,
        "source_path": str(resolved),
        "source_sha256": expected_env_sha256.lower(),
        "expected_account_id": account_id,
        "first_dip_policy_mode": first_dip_policy_mode.lower(),
        "effective_config": dict(sorted(nonsecret.items())),
        "secret_fingerprints": secret_fingerprints,
    }
    configuration_sha256 = _sha256_bytes(_canonical_json(config_body))
    return CapturedPaperRuntimeEnvironmentReceipt(
        source_path=str(resolved),
        source_sha256=expected_env_sha256.lower(),
        expected_account_id=account_id,
        first_dip_policy_mode=first_dip_policy_mode.lower(),
        effective_config=MappingProxyType(dict(sorted(nonsecret.items()))),
        secret_fingerprints=MappingProxyType(secret_fingerprints),
        removed_forbidden_keys=tuple(sorted(removed)),
        configuration_sha256=configuration_sha256,
    )


__all__ = [
    "CapturedPaperRuntimeEnvError",
    "CapturedPaperRuntimeEnvironmentReceipt",
    "RUNTIME_ENV_SCHEMA_VERSION",
    "install_captured_paper_runtime_environment",
    "validate_installed_captured_paper_settings",
]
