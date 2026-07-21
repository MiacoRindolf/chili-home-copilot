"""Build one fresh, hash-bound captured Alpaca PAPER operator generation.

The chain performs one exact Alpaca PAPER account read, one bounded
candidate-build capture-only IQFeed preselection, and one read-only host
snapshot.  The preselection may append captured market evidence to its pinned
store/database; it has no dispatcher, live runner, broker adapter, or order
transport.  The chain does not submit an order, mutate Task Scheduler, stop a
host process, or authorize cash.

Every mutable input is supplied by a canonical content-addressed request and
is also bound to the canonical outer activation request.  Importing this
module performs no I/O.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import UTC, datetime, time as datetime_time
from decimal import Decimal, InvalidOperation
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import sys
import time
from typing import Any, Mapping, Sequence
import uuid
from zoneinfo import ZoneInfo

from scripts import build_iqfeed_capture_bootstrap_bundle as bootstrap
from scripts import captured_paper_activation_runner as activation_runner
from scripts import captured_paper_operator_flow as operator_flow
from scripts import collect_captured_paper_host_snapshot as host_snapshot
from scripts.captured_paper_runtime_env import (
    install_captured_paper_runtime_environment,
)


CHAIN_REQUEST_SCHEMA_VERSION = "chili.captured-paper-operator-chain-request.v1"
CHAIN_ERROR_SCHEMA_VERSION = "chili.captured-paper-operator-chain-error.v1"
ACCOUNT_SCOPE = "alpaca:paper"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PRINCIPAL_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,128}$")
_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,15}$")
_MAX_REQUEST_BYTES = 1024 * 1024
_MAX_BENCHMARK_BYTES = 64 * 1024 * 1024
_PRESELECTION_SEED_LIMIT = 8
_PRESELECTION_SEED_TAIL_ROWS = 100_000
_EQUITY_SESSION_TIMEZONE = ZoneInfo("America/New_York")
_CHAIN_KEYS = frozenset(
    {
        "schema_version",
        "account_scope",
        "live_cash_authorized",
        "resource_benchmark",
        "legacy_root",
        "python_dependency_root",
        "python_dependency_root_identity_sha256",
        "bootstrap_stage0_script",
        "bootstrap_stage0_script_sha256",
        "host_principal_user_id",
        "bridge_configuration",
    }
)


class CapturedPaperOperatorChainError(RuntimeError):
    """Sanitized, stable rejection from the read-only operator chain."""

    def __init__(self, code: str, message: str) -> None:
        self.code = str(code)
        super().__init__(f"{self.code}: {message}")


@dataclass(frozen=True, slots=True)
class ExactPrintPreselectionReceipt:
    """Bounded candidate-capture proof used only to select the smoke symbol."""

    evidence_path: Path
    evidence_sha256: str
    started_at: datetime
    completed_at: datetime
    bridge_version: str
    bridge_run_id: str
    timestamp_basis: str
    bridge_source_sha256: str


@dataclass(frozen=True, slots=True)
class IqfeedRealtimeProbe:
    """Exact IQFeed L1 entitlement and reference-delay observation."""

    customer_realtime: bool
    exact_fields_selected: bool
    quote_received: bool
    delay_minutes: int | None
    message_type: str | None

    @property
    def delay_zero(self) -> bool:
        return self.delay_minutes == 0


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
        raise CapturedPaperOperatorChainError(
            "JSON_NOT_CANONICAL", "operator chain value is not canonical JSON"
        ) from exc


def _strict_json(raw: bytes, *, field: str) -> Mapping[str, Any]:
    def pairs(rows: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in rows:
            if key in result:
                raise CapturedPaperOperatorChainError(
                    "JSON_DUPLICATE_KEY", f"{field} repeats key {key}"
                )
            result[key] = value
        return result

    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=lambda constant: (_ for _ in ()).throw(
                CapturedPaperOperatorChainError(
                    "JSON_NONFINITE", f"{field} contains {constant}"
                )
            ),
        )
    except CapturedPaperOperatorChainError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CapturedPaperOperatorChainError(
            "JSON_INVALID", f"{field} is not strict UTF-8 JSON"
        ) from exc
    if not isinstance(value, dict):
        raise CapturedPaperOperatorChainError(
            "JSON_INVALID", f"{field} must be a JSON object"
        )
    return value


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sha256_file(path: Path, *, max_bytes: int | None = None) -> str:
    if max_bytes is not None and path.stat().st_size > max_bytes:
        raise CapturedPaperOperatorChainError(
            "FILE_OVERSIZED", "hash-bound input exceeds its size limit"
        )
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _inside(path: Path, roots: Sequence[Path]) -> bool:
    for root in roots:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _strict_path(
    value: Any,
    *,
    field: str,
    roots: Sequence[Path],
    directory: bool,
    expected_sha256: str | None = None,
    max_bytes: int | None = None,
) -> Path:
    path = Path(str(value or ""))
    if not path.is_absolute() or str(path).startswith(("\\\\", "//")):
        raise CapturedPaperOperatorChainError(
            "PATH_INVALID", f"{field} must be an absolute local path"
        )
    activation_runner._reject_reparse_chain(path)
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise CapturedPaperOperatorChainError(
            "PATH_UNAVAILABLE", f"{field} is unavailable"
        ) from exc
    activation_runner._reject_reparse_chain(resolved)
    if not _inside(resolved, roots):
        raise CapturedPaperOperatorChainError(
            "PATH_OUTSIDE_ALLOWLIST", f"{field} is outside allowed roots"
        )
    if directory:
        if not resolved.is_dir() or expected_sha256 is not None:
            raise CapturedPaperOperatorChainError(
                "PATH_INVALID", f"{field} must be a directory"
            )
        return resolved
    if not resolved.is_file():
        raise CapturedPaperOperatorChainError(
            "PATH_INVALID", f"{field} must be a regular file"
        )
    expected = str(expected_sha256 or "")
    if _SHA256_RE.fullmatch(expected) is None:
        raise CapturedPaperOperatorChainError(
            "HASH_INVALID", f"{field} SHA-256 is malformed"
        )
    if _sha256_file(resolved, max_bytes=max_bytes) != expected:
        raise CapturedPaperOperatorChainError(
            "FILE_HASH_MISMATCH", f"{field} differs from its pinned SHA-256"
        )
    return resolved


def _load_chain_request(
    *,
    request_path: str | Path,
    request_sha256: str,
    activation_request: activation_runner.ActivationRunnerRequest,
) -> Mapping[str, Any]:
    path = Path(request_path)
    activation_runner._reject_reparse_chain(path)
    if (
        not path.is_absolute()
        or _SHA256_RE.fullmatch(str(request_sha256)) is None
        or path.resolve(strict=True) != activation_request.chain_request_path
        or request_sha256 != activation_request.chain_request_sha256
    ):
        raise CapturedPaperOperatorChainError(
            "CHAIN_REQUEST_REFERENCE_INVALID",
            "chain request is not the request pinned by the activation envelope",
        )
    raw = path.read_bytes()
    if len(raw) > _MAX_REQUEST_BYTES or _sha256_bytes(raw) != request_sha256:
        raise CapturedPaperOperatorChainError(
            "CHAIN_REQUEST_HASH_MISMATCH", "chain request bytes differ"
        )
    value = _strict_json(raw, field="operator chain request")
    if _canonical_json_bytes(value) != raw or set(value) != set(_CHAIN_KEYS):
        raise CapturedPaperOperatorChainError(
            "CHAIN_REQUEST_SCHEMA_INVALID", "chain request is not exact canonical v1"
        )
    if (
        value.get("schema_version") != CHAIN_REQUEST_SCHEMA_VERSION
        or value.get("account_scope") != ACCOUNT_SCOPE
        or value.get("live_cash_authorized") is not False
    ):
        raise CapturedPaperOperatorChainError(
            "PAPER_SCOPE_INVALID", "chain request is not bound to fake-money PAPER"
        )
    return value


def _publish_once(path: Path, raw: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    activation_runner._reject_reparse_chain(path.parent)
    try:
        with path.open("xb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        if not path.is_file() or path.read_bytes() != raw:
            raise CapturedPaperOperatorChainError(
                "APPEND_ONLY_CONFLICT", "content-addressed chain artifact conflicts"
            )


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _enum_text(value: Any) -> str:
    return str(getattr(value, "value", value) or "")


def _strict_bool(value: Any, *, field: str) -> bool:
    if type(value) is not bool:
        raise CapturedPaperOperatorChainError(
            "ACCOUNT_POSTURE_INVALID", f"Alpaca PAPER {field} is not a native bool"
        )
    return value


def _positive_decimal(value: Any, *, field: str, allow_zero: bool = False) -> str:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise CapturedPaperOperatorChainError(
            "ACCOUNT_POSTURE_INVALID", f"Alpaca PAPER {field} is not numeric"
        ) from exc
    if not parsed.is_finite() or parsed < 0 or (not allow_zero and parsed == 0):
        raise CapturedPaperOperatorChainError(
            "ACCOUNT_POSTURE_INVALID", f"Alpaca PAPER {field} is invalid"
        )
    return format(parsed, "f")


def _read_exact_paper_account(
    *, expected_account_id: str
) -> tuple[dict[str, Any], dict[str, Any], datetime, datetime]:
    key = str(os.environ.get("CHILI_ALPACA_API_KEY") or "").strip()
    secret = str(os.environ.get("CHILI_ALPACA_API_SECRET") or "").strip()
    if not key or not secret:
        raise CapturedPaperOperatorChainError(
            "PAPER_CREDENTIALS_UNAVAILABLE", "protected PAPER credentials are absent"
        )
    try:
        from alpaca.trading.client import TradingClient

        client = TradingClient(key, secret, paper=True)
        requested_at = datetime.now(UTC)
        account = client.get_account()
        available_at = datetime.now(UTC)
    except Exception as exc:
        raise CapturedPaperOperatorChainError(
            "PAPER_ACCOUNT_READ_FAILED", "exact Alpaca PAPER account read failed"
        ) from exc
    account_id = str(getattr(account, "id", "") or "")
    if account_id != expected_account_id:
        raise CapturedPaperOperatorChainError(
            "PAPER_ACCOUNT_IDENTITY_MISMATCH",
            "Alpaca returned a different PAPER account UUID",
        )
    status = _enum_text(getattr(account, "status", None)).upper()
    if status != "ACTIVE":
        raise CapturedPaperOperatorChainError(
            "PAPER_ACCOUNT_INACTIVE", "Alpaca PAPER account is not ACTIVE"
        )
    posture: dict[str, Any] = {
        "equity": _positive_decimal(getattr(account, "equity", None), field="equity"),
        "last_equity": _positive_decimal(
            getattr(account, "last_equity", None), field="last_equity"
        ),
        "buying_power": _positive_decimal(
            getattr(account, "buying_power", None),
            field="buying_power",
            allow_zero=True,
        ),
        "cash": _positive_decimal(
            getattr(account, "cash", None), field="cash", allow_zero=True
        ),
        "status": status,
        "account_blocked": _strict_bool(
            getattr(account, "account_blocked", None), field="account_blocked"
        ),
        "trading_blocked": _strict_bool(
            getattr(account, "trading_blocked", None), field="trading_blocked"
        ),
        "transfers_blocked": _strict_bool(
            getattr(account, "transfers_blocked", None), field="transfers_blocked"
        ),
        "trade_suspended_by_user": _strict_bool(
            getattr(account, "trade_suspended_by_user", None),
            field="trade_suspended_by_user",
        ),
        "observed_at": _iso(available_at),
    }
    if any(
        posture[field]
        for field in (
            "account_blocked",
            "trading_blocked",
            "transfers_blocked",
            "trade_suspended_by_user",
        )
    ):
        raise CapturedPaperOperatorChainError(
            "PAPER_ACCOUNT_BLOCKED", "Alpaca PAPER account blocks trading"
        )
    query = {
        "endpoint": "/v2/account",
        "operation": "get_account_snapshot",
        "environment": "paper",
        "account_id": expected_account_id,
        "account_retrieved_at": _iso(available_at),
    }
    return posture, query, requested_at, available_at


def _probe_iqfeed_realtime_symbol(
    symbol: str, *, timeout_seconds: float = 4.0
) -> IqfeedRealtimeProbe:
    try:
        connection = socket.create_connection(("127.0.0.1", 5009), timeout=timeout_seconds)
    except OSError:
        return IqfeedRealtimeProbe(False, False, False, None, None)
    try:
        connection.settimeout(timeout_seconds)
        connection.sendall(b"S,SET PROTOCOL,6.2\r\n")
        connection.sendall(b"S,SELECT UPDATE FIELDS,Most Recent Trade,Delay\r\n")
        connection.sendall(f"w{symbol}\r\n".encode("ascii"))
        deadline = time.monotonic() + timeout_seconds
        buffer = b""
        server_connected = False
        customer_realtime = False
        delay_field_selected = False
        while time.monotonic() < deadline:
            try:
                chunk = connection.recv(4096)
            except OSError:
                break
            if not chunk:
                break
            buffer += chunk
            while b"\n" in buffer:
                raw_line, buffer = buffer.split(b"\n", 1)
                line = raw_line.rstrip(b"\r").decode("ascii", errors="replace")
                parts = line.split(",")
                if parts[:2] == ["S", "SERVER CONNECTED"]:
                    server_connected = True
                elif len(parts) >= 3 and parts[:2] == ["S", "CUST"]:
                    customer_realtime = parts[2].strip().lower() == "real_time"
                elif parts[:2] == ["S", "CURRENT UPDATE FIELDNAMES"]:
                    fields = tuple(part.strip() for part in parts[2:] if part.strip())
                    delay_field_selected = fields == (
                        "Symbol",
                        "Most Recent Trade",
                        "Delay",
                    )
                elif (
                    len(parts) >= 4
                    and parts[0] in {"Q", "P"}
                    and parts[1] == symbol
                ):
                    if not (
                        server_connected
                        and customer_realtime
                        and delay_field_selected
                    ):
                        continue
                    # The Delay value is the age, in minutes, of the most
                    # recent trade reference.  It can increase after the
                    # extended session closes even for a real-time customer.
                    delay_raw = (parts[3] or "").strip()
                    if not delay_raw:
                        delay_minutes = 0
                    else:
                        try:
                            delay_minutes = int(delay_raw)
                        except ValueError:
                            return IqfeedRealtimeProbe(
                                True, True, True, None, parts[0]
                            )
                        if delay_minutes < 0:
                            return IqfeedRealtimeProbe(
                                True, True, True, None, parts[0]
                            )
                    return IqfeedRealtimeProbe(
                        True,
                        True,
                        True,
                        delay_minutes,
                        parts[0],
                    )
        return IqfeedRealtimeProbe(
            customer_realtime,
            delay_field_selected,
            False,
            None,
            None,
        )
    finally:
        try:
            connection.sendall(f"r{symbol}\r\n".encode("ascii"))
        except OSError:
            pass
        connection.close()


def _delay_is_zero(symbol: str, *, timeout_seconds: float = 4.0) -> bool:
    """Strict intraday authority check retained for actual live tape."""

    probe = _probe_iqfeed_realtime_symbol(
        symbol,
        timeout_seconds=timeout_seconds,
    )
    return probe.message_type == "Q" and probe.delay_zero


def _equity_extended_session_is_open(*, as_of: datetime | None = None) -> bool:
    observed = as_of or datetime.now(UTC)
    if observed.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    eastern = observed.astimezone(_EQUITY_SESSION_TIMEZONE)
    return (
        eastern.weekday() < 5
        and datetime_time(4, 0) <= eastern.time() < datetime_time(20, 0)
    )


def _activation_preselection_is_eligible(
    symbol: str,
    *,
    timeout_seconds: float = 4.0,
    as_of: datetime | None = None,
) -> bool:
    """Allow service composition after close without weakening decisions.

    During the equity extended session, an exact zero-delay reference remains
    mandatory.  Outside that session, a real-time customer entitlement plus
    an exact selected-field Q/P reference is sufficient only to compose/start PAPER; the trading
    FSM still applies its event-local freshness and coverage gates before any
    opportunity, risk reservation, or broker transport.
    """

    probe = _probe_iqfeed_realtime_symbol(symbol, timeout_seconds=timeout_seconds)
    if not (
        probe.customer_realtime
        and probe.exact_fields_selected
        and probe.quote_received
        and probe.delay_minutes is not None
    ):
        return False
    if _equity_extended_session_is_open(as_of=as_of):
        return probe.message_type == "Q" and probe.delay_zero
    return True


def _discover_capture_seed_symbols(
    *, allow_stale_address_only: bool = False
) -> tuple[str, ...]:
    """Find live symbol names only; these rows are never treated as evidence.

    A legacy bridge may supply this bounded discovery roster.  The roster is
    useful only as an address for the candidate capture-only producer.  A
    symbol cannot become the certification symbol until a *new*, exact,
    candidate-build row is observed after the producer's attested start.
    """

    database_url = str(os.environ.get("DATABASE_URL") or "")
    if not database_url:
        raise CapturedPaperOperatorChainError(
            "DATABASE_AUTHORITY_UNAVAILABLE", "protected database authority is absent"
        )
    try:
        from sqlalchemy import create_engine, text

        engine = create_engine(database_url, pool_pre_ping=True)
        try:
            with engine.connect() as connection:
                connection.execute(text("SET TRANSACTION READ ONLY"))
                rows = connection.execute(
                    text(
                        "WITH recent_tail AS MATERIALIZED ("
                        "SELECT symbol, observed_at FROM iqfeed_trade_ticks "
                        "ORDER BY id DESC LIMIT :tail_rows"
                        ") SELECT symbol, count(*) AS n "
                        "FROM recent_tail "
                        "WHERE (:allow_stale_address_only "
                        "OR observed_at > now() - interval '15 minutes') "
                        "GROUP BY symbol ORDER BY n DESC, symbol ASC LIMIT :limit"
                    ),
                    {
                        "tail_rows": _PRESELECTION_SEED_TAIL_ROWS,
                        "limit": _PRESELECTION_SEED_LIMIT,
                        "allow_stale_address_only": bool(
                            allow_stale_address_only
                        ),
                    },
                ).fetchall()
        finally:
            engine.dispose()
    except Exception as exc:
        raise CapturedPaperOperatorChainError(
            "LIVE_TAPE_SEEDS_UNAVAILABLE",
            "bounded capture seed discovery failed",
        ) from exc
    seeds: list[str] = []
    for raw_symbol, _count in rows:
        symbol = str(raw_symbol or "").strip().upper()
        if (
            _SYMBOL_RE.fullmatch(symbol)
            and symbol not in seeds
            and _activation_preselection_is_eligible(symbol)
        ):
            seeds.append(symbol)
    if not seeds:
        raise CapturedPaperOperatorChainError(
            "LIVE_TAPE_REALTIME_SEED_UNAVAILABLE",
            "no real-time discovery seed is available for candidate capture",
        )
    return tuple(seeds)


def _capture_candidate_exact_print_preselection(
    *,
    bootstrap_manifest_path: Path,
    bootstrap_manifest_sha256: str,
    capture_store_root: Path,
    artifact_root: Path,
    allowed_read_roots: Sequence[str],
    seed_symbols: Sequence[str],
    allow_closed_session_activation_only: bool = False,
) -> ExactPrintPreselectionReceipt:
    """Run one bounded capture-only producer and publish its zero-order proof."""

    from scripts.iqfeed_capture_bootstrap_preflight import (
        load_iqfeed_capture_bootstrap_preflight,
    )
    from scripts.iqfeed_capture_only_smoke import (
        CaptureOnlySmokeConfiguration,
        CaptureOnlySmokeEvidence,
        IngressCaptureOnlyHealthAuthority,
        run_capture_only_preactivation_smoke,
    )

    normalized = tuple(
        str(value or "").strip().upper()
        for value in seed_symbols
        if _SYMBOL_RE.fullmatch(str(value or "").strip().upper())
    )
    if not normalized or len(normalized) != len(set(normalized)):
        raise CapturedPaperOperatorChainError(
            "CAPTURE_SEED_INVALID", "candidate capture seed roster is invalid"
        )
    try:
        preflight = load_iqfeed_capture_bootstrap_preflight(
            bootstrap_manifest_path,
            expected_manifest_sha256=bootstrap_manifest_sha256,
            allowed_read_roots=tuple(Path(value) for value in allowed_read_roots),
            allowed_write_roots=(artifact_root,),
        )
        if preflight.capture_store_root.resolve() != capture_store_root.resolve():
            raise CapturedPaperOperatorChainError(
                "CAPTURE_ROOT_MISMATCH", "candidate preselection changed capture root"
            )
        wall_clock = lambda: datetime.now(UTC)
        monotonic_clock = time.monotonic
        pressure = operator_flow._measure_capture_pressure(
            preflight=preflight,
            wall_clock=wall_clock,
            monotonic_clock=monotonic_clock,
        )
        # The first seed already has verified real-time Delay status.  Keep
        # this preselection lane to
        # one symbol bounds provider and depth work; the final selection still
        # rechecks both candidate DB provenance and current real-time Delay state.
        certification_seed = normalized[0]
        evidence = run_capture_only_preactivation_smoke(
            CaptureOnlySmokeConfiguration(
                preflight=preflight,
                pressure_sample=pressure,
                capture_health_authority=IngressCaptureOnlyHealthAuthority(
                    preflight=preflight,
                    certification_symbol=certification_seed,
                    wall_clock=wall_clock,
                ),
                trade_forced_symbols=(certification_seed,),
                depth_forced_symbols=(),
                l1_only_exact_print_preselection=True,
                activation_only_allow_closed_session_without_exact_print=(
                    allow_closed_session_activation_only
                ),
                pressure_sampler=lambda: operator_flow._measure_capture_pressure(
                    preflight=preflight,
                    wall_clock=wall_clock,
                    monotonic_clock=monotonic_clock,
                ),
            ),
            wall_clock=wall_clock,
            monotonic_clock=monotonic_clock,
        )
        if type(evidence) is not CaptureOnlySmokeEvidence:
            raise CapturedPaperOperatorChainError(
                "CAPTURE_ONLY_ATTESTATION_INVALID",
                "candidate preselection returned untyped evidence",
            )
        document = evidence.to_dict()
        host_binding = document.get("host_binding")
        closure = document.get("closure")
        source_hashes = document.get("source_hashes")
        capture_health = document.get("capture_health")
        provider_health = document.get("provider_health")
        embedded_evidence_sha256 = str(document.get("evidence_sha256") or "")
        embedded_payload = dict(document)
        embedded_payload.pop("evidence_sha256", None)
        exact_print_observed = bool(
            isinstance(provider_health, Mapping)
            and provider_health.get("exact_print_clock_observed") is True
            and int(provider_health.get("exact_print_event_count") or 0) > 0
        )
        closed_session_activation_only = bool(
            allow_closed_session_activation_only
            and isinstance(provider_health, Mapping)
            and provider_health.get(
                "activation_only_closed_session_without_exact_print"
            )
            is True
        )
        if (
            document.get("schema_version")
            != "chili.iqfeed-l1-exact-print-preselection-smoke.v1"
            or _SHA256_RE.fullmatch(embedded_evidence_sha256) is None
            or _sha256_bytes(_canonical_json_bytes(embedded_payload))
            != embedded_evidence_sha256
            or not isinstance(host_binding, Mapping)
            or host_binding.get("execution_surface") != "capture_only"
            or host_binding.get("provider_scope")
            != "l1_exact_print_preselection"
            or host_binding.get("trade_bridge_bound") is not True
            or host_binding.get("depth_bridge_bound") is not False
            or host_binding.get("l2_snapshot_completion_required") is not False
            or host_binding.get("l2_decision_coverage_policy")
            != "decision_local_fail_closed"
            or host_binding.get("dispatcher_constructed") is not False
            or host_binding.get("live_runner_loop_constructed") is not False
            or host_binding.get("broker_adapter_constructed") is not False
            or host_binding.get("order_transport_constructed") is not False
            or not isinstance(closure, Mapping)
            or closure.get("orders_submitted") is not False
            or closure.get("bridges_unbound") is not True
            or closure.get("l2_opportunity_consumed") is not False
            or closure.get("l2_risk_reserved") is not False
            or not isinstance(source_hashes, Mapping)
            or not isinstance(capture_health, Mapping)
            or capture_health.get("dropped_event_count") != 0
            or capture_health.get("overflow_count") != 0
            or capture_health.get("unreported_gap_count") != 0
            or not isinstance(provider_health, Mapping)
            or not (exact_print_observed or closed_session_activation_only)
            or provider_health.get("depth_provider_started") is not False
        ):
            raise CapturedPaperOperatorChainError(
                "CAPTURE_ONLY_ATTESTATION_INVALID",
                "candidate preselection exposed execution authority or did not close",
            )
        from scripts import iqfeed_trade_bridge

        bridge_source_sha256 = str(
            source_hashes.get("iqfeed_trade_bridge") or ""
        ).strip().lower()
        bridge_version = str(iqfeed_trade_bridge.BRIDGE_BUILD or "").strip()
        bridge_run_id = str(iqfeed_trade_bridge.BRIDGE_RUN_ID or "").strip()
        timestamp_basis = str(
            iqfeed_trade_bridge.EXACT_PRINT_TIMESTAMP_BASIS or ""
        ).strip()
        if (
            _SHA256_RE.fullmatch(bridge_source_sha256) is None
            or bridge_source_sha256 != iqfeed_trade_bridge.BRIDGE_SOURCE_SHA256
            or not bridge_version.endswith(bridge_source_sha256[:16])
            or str(uuid.UUID(bridge_run_id)) != bridge_run_id
            or not timestamp_basis
        ):
            raise CapturedPaperOperatorChainError(
                "CAPTURE_ONLY_ATTESTATION_INVALID",
                "candidate preselection bridge identity is not exact",
            )
        raw = _canonical_json_bytes(document)
        # The smoke's embedded digest binds its payload-before-digest.  This
        # outer digest binds the exact append-only bytes consumed here.
        evidence_sha256 = _sha256_bytes(raw)
        evidence_path = (
            artifact_root
            / "capture-preselection"
            / f"{evidence_sha256}.evidence.json"
        )
        _publish_once(evidence_path, raw)
        return ExactPrintPreselectionReceipt(
            evidence_path=evidence_path.resolve(strict=True),
            evidence_sha256=evidence_sha256,
            started_at=evidence.started_at,
            completed_at=evidence.completed_at,
            bridge_version=bridge_version,
            bridge_run_id=bridge_run_id,
            timestamp_basis=timestamp_basis,
            bridge_source_sha256=bridge_source_sha256,
        )
    except CapturedPaperOperatorChainError:
        raise
    except Exception as exc:
        raise CapturedPaperOperatorChainError(
            "CANDIDATE_EXACT_PRINT_PRESELECTION_FAILED",
            "bounded candidate exact-print producer failed closed",
        ) from exc


def _select_live_certification_symbol(
    *, preselection: ExactPrintPreselectionReceipt
) -> str:
    if type(preselection) is not ExactPrintPreselectionReceipt:
        raise CapturedPaperOperatorChainError(
            "CAPTURE_ONLY_ATTESTATION_INVALID",
            "typed candidate preselection receipt is required",
        )
    started_at = preselection.started_at
    completed_at = preselection.completed_at
    try:
        activation_runner._reject_reparse_chain(preselection.evidence_path)
        evidence_digest = _sha256_file(preselection.evidence_path)
        run_id = str(uuid.UUID(preselection.bridge_run_id))
    except (OSError, ValueError, AttributeError) as exc:
        raise CapturedPaperOperatorChainError(
            "CAPTURE_ONLY_ATTESTATION_INVALID",
            "candidate preselection identity is unavailable",
        ) from exc
    if (
        not isinstance(started_at, datetime)
        or started_at.tzinfo is None
        or not isinstance(completed_at, datetime)
        or completed_at.tzinfo is None
        or completed_at.astimezone(UTC) < started_at.astimezone(UTC)
        or _SHA256_RE.fullmatch(preselection.evidence_sha256) is None
        or evidence_digest != preselection.evidence_sha256
        or _SHA256_RE.fullmatch(preselection.bridge_source_sha256) is None
        or not preselection.bridge_version.endswith(
            preselection.bridge_source_sha256[:16]
        )
        or run_id != preselection.bridge_run_id
        or not preselection.timestamp_basis
    ):
        raise CapturedPaperOperatorChainError(
            "CAPTURE_ONLY_ATTESTATION_INVALID",
            "candidate preselection receipt is inconsistent",
        )
    database_url = str(os.environ.get("DATABASE_URL") or "")
    if not database_url:
        raise CapturedPaperOperatorChainError(
            "DATABASE_AUTHORITY_UNAVAILABLE", "protected database authority is absent"
        )
    try:
        from sqlalchemy import create_engine, text

        engine = create_engine(database_url, pool_pre_ping=True)
        try:
            with engine.connect() as connection:
                connection.execute(text("SET TRANSACTION READ ONLY"))
                rows = connection.execute(
                    text(
                        "SELECT symbol, count(*) AS n "
                        "FROM iqfeed_trade_ticks "
                        "WHERE received_at >= :started_at "
                        "AND received_at <= :completed_at "
                        "AND available_at IS NOT NULL "
                        "AND provider_event_at IS NOT NULL "
                        "AND provider_trade_reference_at = provider_event_at "
                        "AND timestamp_basis = :timestamp_basis "
                        "AND bridge_version = :bridge_version "
                        "AND bridge_run_id = :bridge_run_id "
                        "AND message_type = 'Q' "
                        "AND source_frame_sha256 IS NOT NULL "
                        "GROUP BY symbol ORDER BY n DESC, symbol ASC LIMIT 40"
                    ),
                    {
                        "started_at": started_at.astimezone(UTC),
                        "completed_at": preselection.completed_at.astimezone(UTC),
                        "timestamp_basis": preselection.timestamp_basis,
                        "bridge_version": preselection.bridge_version,
                        "bridge_run_id": preselection.bridge_run_id,
                    },
                ).fetchall()
        finally:
            engine.dispose()
    except Exception as exc:
        raise CapturedPaperOperatorChainError(
            "LIVE_TAPE_CANDIDATES_UNAVAILABLE",
            "current exact-print candidate query failed",
        ) from exc
    for raw_symbol, _count in rows:
        symbol = str(raw_symbol or "").strip().upper()
        if _SYMBOL_RE.fullmatch(symbol) and _activation_preselection_is_eligible(symbol):
            return symbol
    raise CapturedPaperOperatorChainError(
        "LIVE_TAPE_REALTIME_SYMBOL_UNAVAILABLE",
        "no current real-time exact-print symbol can certify capture",
    )


def _sha_source_inventory(candidate_root: Path) -> dict[str, str]:
    return {
        role: _sha256_file(candidate_root / relative)
        for role, relative in bootstrap._SOURCE_RELATIVE_PATHS.items()
    }


def run_operator_chain(
    *,
    activation_request: activation_runner.ActivationRunnerRequest,
    chain_document: Mapping[str, Any],
) -> Mapping[str, Any]:
    roots = tuple(Path(value).resolve(strict=True) for value in activation_request.allowed_read_roots)
    benchmark_ref = chain_document.get("resource_benchmark")
    if not isinstance(benchmark_ref, dict) or set(benchmark_ref) != {"path", "sha256"}:
        raise CapturedPaperOperatorChainError(
            "BENCHMARK_REFERENCE_INVALID", "resource benchmark reference is malformed"
        )
    benchmark = _strict_path(
        benchmark_ref.get("path"),
        field="resource_benchmark",
        roots=roots,
        directory=False,
        expected_sha256=str(benchmark_ref.get("sha256") or ""),
        max_bytes=_MAX_BENCHMARK_BYTES,
    )
    legacy_root = _strict_path(
        chain_document.get("legacy_root"),
        field="legacy_root",
        roots=roots,
        directory=True,
    )
    dependency_root = _strict_path(
        chain_document.get("python_dependency_root"),
        field="python_dependency_root",
        roots=roots,
        directory=True,
    )
    stage0_path = _strict_path(
        chain_document.get("bootstrap_stage0_script"),
        field="bootstrap_stage0_script",
        roots=roots,
        directory=False,
        expected_sha256=str(
            chain_document.get("bootstrap_stage0_script_sha256") or ""
        ),
    )
    if not (
        dependency_root == activation_request.python_dependency_root
        and chain_document.get("python_dependency_root_identity_sha256")
        == activation_request.python_dependency_root_identity_sha256
        and stage0_path == activation_request.bootstrap_stage0_script
        and chain_document.get("bootstrap_stage0_script_sha256")
        == activation_request.bootstrap_stage0_script_sha256
    ):
        raise CapturedPaperOperatorChainError(
            "BOOTSTRAP_AUTHORITY_MISMATCH",
            "chain dependency/stage0 authority differs from the outer request",
        )
    principal = str(chain_document.get("host_principal_user_id") or "")
    if _PRINCIPAL_RE.fullmatch(principal) is None or principal.casefold() != getpass.getuser().casefold():
        raise CapturedPaperOperatorChainError(
            "HOST_PRINCIPAL_MISMATCH", "chain principal differs from the current host user"
        )
    bridge_configuration = chain_document.get("bridge_configuration")
    if not isinstance(bridge_configuration, dict) or set(bridge_configuration) != {
        "iqfeed_l1",
        "iqfeed_l2",
    }:
        raise CapturedPaperOperatorChainError(
            "BRIDGE_CONFIGURATION_INVALID", "bridge configuration is incomplete"
        )

    if "app.config" in sys.modules:
        raise CapturedPaperOperatorChainError(
            "APPLICATION_IMPORTED_TOO_EARLY",
            "app.config was loaded before the sealed PAPER environment install",
        )
    preinstalled_runtime_receipt = install_captured_paper_runtime_environment(
        activation_request.runtime_env_path,
        expected_env_sha256=activation_request.runtime_env_sha256,
        expected_account_id=activation_request.expected_account_id,
        first_dip_policy_mode="candidate",
    )
    if "app.config" in sys.modules:
        raise CapturedPaperOperatorChainError(
            "APPLICATION_IMPORTED_DURING_INSTALL",
            "app.config loaded before the sealed PAPER environment install completed",
        )
    account, account_query, requested_at, account_available_at = _read_exact_paper_account(
        expected_account_id=activation_request.expected_account_id
    )
    generation = str(uuid.uuid4())
    now = datetime.now(UTC)
    artifact_root = activation_request.artifact_root
    capture_store_root = artifact_root / "capture-store"
    capture_store_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    bootstrap_artifacts = artifact_root / "bootstrap" / "artifacts"
    bootstrap_artifacts.mkdir(mode=0o700, parents=True, exist_ok=True)

    build_request = {
        "schema_version": bootstrap.BUILD_REQUEST_SCHEMA_VERSION,
        "repo_root": str(activation_request.candidate_root),
        "artifact_root": str(bootstrap_artifacts),
        "capture_store_root": str(capture_store_root),
        "resource_benchmark": {
            "path": str(benchmark),
            "sha256": str(benchmark_ref["sha256"]),
        },
        "source_sha256": _sha_source_inventory(activation_request.candidate_root),
        "expected_account_id": activation_request.expected_account_id,
        "account_risk_snapshot": account,
        "account_query": account_query,
        "account_received_at": _iso(requested_at),
        "account_available_at": _iso(account_available_at),
        "effective_config": {
            "capture_profile": "captured_paper_activation_candidate",
            "runtime_environment_sha256": activation_request.runtime_env_sha256,
        },
        "bridge_configuration": bridge_configuration,
        "activation_generation": generation,
        "generated_at": _iso(now),
        "generation": 1,
    }
    build_raw = _canonical_json_bytes(build_request)
    build_sha = _sha256_bytes(build_raw)
    build_path = artifact_root / "bootstrap" / "inputs" / f"{build_sha}.request.json"
    _publish_once(build_path, build_raw)
    built = bootstrap.build_iqfeed_capture_bootstrap_bundle_from_request(
        request_path=build_path,
        request_sha256=build_sha,
        allowed_read_roots=activation_request.allowed_read_roots,
        allowed_write_roots=(artifact_root,),
    )

    extended_session_open = _equity_extended_session_is_open()
    seed_symbols = _discover_capture_seed_symbols(
        allow_stale_address_only=not extended_session_open
    )
    preselection = _capture_candidate_exact_print_preselection(
        bootstrap_manifest_path=built.manifest_path,
        bootstrap_manifest_sha256=built.manifest_sha256,
        capture_store_root=capture_store_root,
        artifact_root=artifact_root,
        allowed_read_roots=activation_request.allowed_read_roots,
        seed_symbols=seed_symbols,
        allow_closed_session_activation_only=not extended_session_open,
    )
    certification_symbol = (
        _select_live_certification_symbol(preselection=preselection)
        if extended_session_open
        else seed_symbols[0]
    )
    selection_document = {
        "schema_version": "chili.captured-paper-exact-print-selection.v1",
        "activation_generation": generation,
        "symbol": certification_symbol,
        "selected_at": _iso(datetime.now(UTC)),
        "preselection_evidence_path": str(preselection.evidence_path),
        "preselection_evidence_sha256": preselection.evidence_sha256,
        "candidate_bridge_version": preselection.bridge_version,
        "candidate_bridge_run_id": preselection.bridge_run_id,
        "candidate_bridge_source_sha256": preselection.bridge_source_sha256,
        "timestamp_basis": preselection.timestamp_basis,
        "candidate_capture_started_at": _iso(preselection.started_at),
        "candidate_capture_completed_at": _iso(preselection.completed_at),
        "preselection_provider_scope": (
            "l1_exact_print_preselection"
            if extended_session_open
            else "l1_closed_session_activation_connectivity"
        ),
        "l2_snapshot_completion_required_for_preselection": False,
        "l2_decision_coverage_policy": "decision_local_fail_closed",
        "iqfeed_customer_realtime_required": True,
        "equity_extended_session_open": extended_session_open,
        "delay_zero_required_for_activation": extended_session_open,
        "closed_session_reference_allowed_for_activation": not extended_session_open,
        "decision_event_freshness_policy": "decision_local_fail_closed",
        "legacy_rows_accepted_as_evidence": False,
        "broker_adapter_constructed": False,
        "order_transport_constructed": False,
        "orders_submitted": False,
        "live_cash_authorized": False,
    }
    selection_raw = _canonical_json_bytes(selection_document)
    selection_sha256 = _sha256_bytes(selection_raw)
    selection_path = (
        artifact_root
        / "capture-preselection"
        / f"{selection_sha256}.selection.json"
    )
    _publish_once(selection_path, selection_raw)

    snapshot_root = artifact_root / "host-snapshots" / generation
    snapshot_root.mkdir(mode=0o700, parents=True, exist_ok=False)
    observed = host_snapshot.collect_host_snapshot(
        probe=host_snapshot.WindowsReadOnlyHostProbe(),
        legacy_root=legacy_root,
        captured_at=datetime.now(UTC),
    )
    persisted = host_snapshot.persist_host_snapshot(observed, output_root=snapshot_root)
    if persisted.verdict != "VALIDATED":
        raise CapturedPaperOperatorChainError(
            "HOST_SNAPSHOT_REJECTED", "read-only host rollback baseline rejected"
        )
    paths = dict(persisted.artifact_paths)
    hashes = dict(persisted.artifact_sha256s)

    operator_output_root = artifact_root / "operator"
    preactivation_output_root = artifact_root / "preactivation"
    activation_artifact_root = artifact_root / "activation"
    receipt_output_root = artifact_root / "receipts"
    for output_root in (
        operator_output_root,
        preactivation_output_root,
        activation_artifact_root,
        receipt_output_root,
    ):
        output_root.mkdir(mode=0o700, parents=False, exist_ok=False)

    plan = {
        "schema_version": operator_flow.OPERATOR_PLAN_SCHEMA_VERSION,
        "activation_generation": generation,
        "expected_account_id": activation_request.expected_account_id,
        "candidate_root": str(activation_request.candidate_root),
        "operator_output_root": str(operator_output_root),
        "preactivation_output_root": str(preactivation_output_root),
        "activation_artifact_root": str(activation_artifact_root),
        "capture_store_root": str(capture_store_root),
        "runtime_env_path": str(activation_request.runtime_env_path),
        "runtime_env_sha256": activation_request.runtime_env_sha256,
        "iqfeed_bootstrap_manifest_path": str(built.manifest_path),
        "iqfeed_bootstrap_manifest_sha256": built.manifest_sha256,
        "python_executable": str(activation_request.python_executable),
        "python_dependency_root": str(dependency_root),
        "no_order_receipt_output": str(
            receipt_output_root / f"no-order-receipt-{generation}.json"
        ),
        "powershell_executable": str(activation_request.powershell_executable),
        "host_principal_user_id": principal,
        "task_snapshot_path": str(paths["task_snapshot"]),
        "task_snapshot_sha256": hashes["task_snapshot"],
        "process_snapshot_path": str(paths["process_snapshot"]),
        "process_snapshot_sha256": hashes["process_snapshot"],
        "restore_plan_path": str(paths["restore_plan"]),
        "restore_plan_sha256": hashes["restore_plan"],
        "capture_certification_symbol": certification_symbol,
        "allowed_read_roots": list(activation_request.allowed_read_roots),
    }
    plan_raw = _canonical_json_bytes(plan)
    plan_sha = _sha256_bytes(plan_raw)
    plan_path = operator_output_root / f"{plan_sha}.plan.json"
    _publish_once(plan_path, plan_raw)

    configuration = operator_flow.configuration_from_plan(plan)
    composition = operator_flow.build_live_operator_composition(
        configuration,
        preinstalled_runtime_receipt=preinstalled_runtime_receipt,
    )
    result = operator_flow.run_captured_paper_operator_flow(composition)
    document = result.to_dict()
    document["activation_runner_request_sha256"] = activation_request.request_sha256
    document["operator_chain_request_sha256"] = activation_request.chain_request_sha256
    document["resource_benchmark_sha256"] = str(benchmark_ref["sha256"])
    document["exact_print_preselection_evidence"] = {
        "path": str(preselection.evidence_path),
        "sha256": preselection.evidence_sha256,
    }
    document["exact_print_selection_receipt"] = {
        "path": str(selection_path.resolve(strict=True)),
        "sha256": selection_sha256,
    }
    document["live_cash_authorized"] = False
    document["paper_order_submission_authorized"] = False
    document["paper_service_started"] = False
    print(f"PLAN: {plan_sha}")
    return document


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True)
    parser.add_argument("--request-sha256", required=True)
    parser.add_argument("--activation-request", required=True)
    parser.add_argument("--activation-request-sha256", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(list(argv) if argv is not None else None)
    try:
        activation_request = activation_runner.load_activation_runner_request(
            request_path=arguments.activation_request,
            request_sha256=arguments.activation_request_sha256,
        )
        chain_document = _load_chain_request(
            request_path=arguments.request,
            request_sha256=arguments.request_sha256,
            activation_request=activation_request,
        )
        result = run_operator_chain(
            activation_request=activation_request,
            chain_document=chain_document,
        )
    except Exception as exc:
        result = {
            "schema_version": CHAIN_ERROR_SCHEMA_VERSION,
            "verdict": "CAPTURED_ALPACA_PAPER_BUILD_REJECTED",
            "reason_code": str(
                getattr(exc, "code", "CAPTURED_PAPER_OPERATOR_CHAIN_REJECTED")
            ),
            "account_scope": ACCOUNT_SCOPE,
            "paper_order_submission_authorized": False,
            "paper_service_started": False,
            "host_cutover_invoked": False,
            "live_cash_authorized": False,
        }
        code = 2
    else:
        code = 0
    print(_canonical_json_bytes(result).decode("utf-8"))
    return code


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ACCOUNT_SCOPE",
    "CHAIN_ERROR_SCHEMA_VERSION",
    "CHAIN_REQUEST_SCHEMA_VERSION",
    "CapturedPaperOperatorChainError",
    "ExactPrintPreselectionReceipt",
    "main",
    "run_operator_chain",
]
