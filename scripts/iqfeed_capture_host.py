"""Unified, inert ownership boundary for the candidate IQFeed capture host.

Importing or preparing this module never opens an IQFeed socket, creates a
capture store, connects to a database, submits an order, or mutates a Windows
task/service.  The host binds an already-prepared, bounded capture composition
to the exact L1/L2 bridge modules in one process.  Provider-loop launch remains
a separate recertification gate.
"""

from __future__ import annotations

import argparse
from collections import OrderedDict
from contextlib import contextmanager, nullcontext
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import sys
import threading
import time
from types import MappingProxyType
from typing import Any, Callable, Generic, Mapping, Sequence, TypeVar
import uuid
from zoneinfo import ZoneInfo

from sqlalchemy.engine import Engine

from app.models.trading import TradingAutomationSession

from app.services.trading.momentum_neural.alpaca_buying_power_reflection import (
    AlpacaBuyingPowerReflectionError,
    PreparedAlpacaPaperBuyingPowerDoubleCensus,
    verify_alpaca_paper_buying_power_double_census,
)
from app.services.trading.momentum_neural.adaptive_risk_account_lock import (
    AdaptiveRiskAccountLockIdentity,
    acquire_adaptive_risk_account_locks,
)
from app.services.trading.momentum_neural.captured_paper_admission import (
    CapturedFirstDipDetectorAudit,
    CapturedPaperFinalExecutedReadAuthority,
    CapturedPaperAdmissionInputs,
    CommittedCapturedPaperAdmission,
    commit_captured_paper_admission,
    read_committed_captured_paper_admission,
)
from app.services.trading.momentum_neural.captured_paper_dispatcher import (
    CapturedPaperDispatchRequest,
    CapturedPaperRuntime,
)
from app.services.trading.momentum_neural.captured_paper_pending_owner import (
    activate_captured_paper_session_owner_before_tick,
)
from app.services.trading.momentum_neural.captured_paper_initial_recovery import (
    recover_captured_paper_initial_preowner,
)
from app.services.trading.momentum_neural.captured_paper_entry_intent import (
    CapturedPaperPostCommitRequest,
)
from app.services.trading.momentum_neural.captured_paper_financial_breaker import (
    CapturedPaperFinancialBreakerReceipt,
    SqlAlchemyCapturedPaperFinancialBreakerIssuer,
)
from app.services.trading.momentum_neural.captured_paper_phase_one_handoff import (
    acknowledge_captured_paper_phase_one_handoff,
    record_captured_paper_phase_one_handoff,
    verify_captured_paper_executed_read_inventory,
)
from app.services.trading.momentum_neural.captured_paper_selection import (
    CapturedPaperObservationContext,
    CapturedPaperSelectionContext,
    install_captured_paper_observation_context,
    install_captured_paper_selection_context,
)
from app.services.trading.momentum_neural.captured_paper_production_material import (
    CapturedPaperBoundInputScope,
    CapturedPaperProductionMaterialFactory,
    CapturedPaperProductionMaterialUnavailable,
    PreparedCapturedPaperObservation,
)
from app.services.trading.momentum_neural.adaptive_risk_reservation import (
    AdaptiveRiskOpportunityKey,
    AdaptiveRiskReservationRequest,
    load_adaptive_risk_reservation_request,
)
from app.services.trading.momentum_neural.first_dip_tape_decision import (
    _installed_captured_db_paper_first_dip_tape_decision_authority,
    _installed_captured_first_dip_detector_retention_provider,
    _installed_captured_first_dip_final_authority_provider,
    _issue_first_dip_final_authority_handoff,
)

from app.services.trading.momentum_neural.replay_capture_contract import (
    ActiveCaptureInputPrefixAttestation,
    CaptureContractError,
    CaptureScannerProfile,
    CaptureStream,
    sha256_json,
    verify_active_capture_input_attestation,
)
from app.services.trading.momentum_neural.first_dip_tape_policy import (
    FirstDipTapePolicy,
)
from app.services.trading.momentum_neural.live_replay_capture import (
    CapturedReadResult,
    ExecutedCaptureReadInventory,
    FirstDipFinalCaptureRead,
    FirstDipFinalCaptureFrontier,
    FirstDipFinalReadProvider,
    LiveMicrostructureCaptureBridge,
    LiveOhlcvCaptureBridge,
    LiveScannerSnapshotCaptureBridge,
    executed_capture_read_evidence,
)
from app.services.trading.momentum_neural import live_runner as momentum_live_runner
from app.services.trading.momentum_neural.replay_capture_runtime import (
    CapturePressureSample,
)
from scripts import iqfeed_depth_bridge, iqfeed_trade_bridge
from scripts.iqfeed_capture_bootstrap import (
    IqfeedCaptureIngressComposition,
    IqfeedIngressCompositionState,
    prepare_iqfeed_capture_ingress,
)
from scripts.iqfeed_capture_bootstrap_preflight import (
    BootstrapPreflightError,
    IqfeedCaptureBootstrapPreflight,
    load_iqfeed_capture_bootstrap_preflight,
)


UTC = timezone.utc
_HOST_SCHEMA_VERSION = "chili.iqfeed-capture-host.v1"
_HOST_BINDING_RECEIPT_SCHEMA_VERSION = "chili.iqfeed-capture-host-binding.v1"
_LAUNCH_VALIDATION_SCHEMA_VERSION = "chili.iqfeed-capture-host-launch-validation.v1"
_MAX_SOURCE_BYTES = 16 * 1024 * 1024
_REPARSE_ATTRIBUTE = 0x400


class IqfeedCaptureHostState(str, Enum):
    PREPARED = "prepared"
    BOUND = "bound"
    CLOSED = "closed"
    FAILED = "failed"


class IqfeedProviderLoopSupervisorState(str, Enum):
    PREPARED = "prepared"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


class IqfeedProviderLoopSupervisor:
    """Own the two non-daemon bridge lanes and their shared stop boundary.

    The class does not bind capture handoffs itself; ``IqfeedCaptureHost`` owns
    that authority and calls ``start`` only after both handoffs are bound.  A
    terminal return or exception from either lane sets one shared stop event,
    so the peer lane closes its concrete socket generation before host teardown
    unbinds either handoff.  Readiness means both sockets are concurrently up
    and each bridge has reached its protocol-specific ready point.
    """

    def __init__(
        self,
        *,
        trade_bridge: Any,
        depth_bridge: Any,
    ) -> None:
        for bridge, role in (
            (trade_bridge, "trade"),
            (depth_bridge, "depth"),
        ):
            if not callable(getattr(bridge, "run_supervised", None)):
                raise CaptureContractError(
                    f"IQFeed {role} bridge lacks run_supervised"
                )
        self._bridges = {
            "trade": trade_bridge,
            "depth": depth_bridge,
        }
        self._stop_event = threading.Event()
        self._state_changed = threading.Event()
        self._connected = {
            "trade": threading.Event(),
            "depth": threading.Event(),
        }
        self._ready = {
            "trade": threading.Event(),
            "depth": threading.Event(),
        }
        self._schema_ready = {
            "trade": threading.Event(),
            "depth": threading.Event(),
        }
        self._threads: dict[str, threading.Thread] = {}
        self._failures: dict[str, dict[str, str]] = {}
        self._safe_to_unbind = True
        self._state = IqfeedProviderLoopSupervisorState.PREPARED
        self._ever_started = False
        self._lock = threading.RLock()

    def _record_failure(self, lane: str, exc: BaseException) -> None:
        with self._lock:
            if bool(getattr(exc, "provider_reader_may_be_alive", False)):
                self._safe_to_unbind = False
            self._failures.setdefault(
                lane,
                {
                    "type": type(exc).__name__,
                    "message": str(exc)[:512],
                },
            )
            self._state = IqfeedProviderLoopSupervisorState.FAILED
            self._stop_event.set()
            self._state_changed.set()

    def _run_lane(
        self,
        lane: str,
        *,
        forced_symbols: tuple[str, ...],
        reconnect_wait_seconds: float,
    ) -> None:
        bridge = self._bridges[lane]
        try:
            bridge.run_supervised(
                stop_event=self._stop_event,
                schema_ready_event=self._schema_ready[lane],
                connected_event=self._connected[lane],
                ready_event=self._ready[lane],
                forced_symbols=forced_symbols,
                reconnect_wait_seconds=reconnect_wait_seconds,
            )
        except BaseException as exc:
            self._record_failure(lane, exc)
        else:
            if not self._stop_event.is_set():
                self._record_failure(
                    lane,
                    RuntimeError(
                        f"IQFeed {lane} supervised lane returned without stop"
                    ),
                )
        finally:
            self._ready[lane].clear()
            self._connected[lane].clear()
            self._state_changed.set()

    def start(
        self,
        *,
        readiness_timeout_seconds: float,
        join_timeout_seconds: float,
        reconnect_wait_seconds: float = 10.0,
        trade_forced_symbols: Sequence[str] = (),
        depth_forced_symbols: Sequence[str] = (),
    ) -> Mapping[str, Any]:
        readiness_timeout = float(readiness_timeout_seconds)
        join_timeout = float(join_timeout_seconds)
        reconnect_wait = float(reconnect_wait_seconds)
        for value, label in (
            (readiness_timeout, "readiness timeout"),
            (join_timeout, "join timeout"),
            (reconnect_wait, "reconnect wait"),
        ):
            if not math.isfinite(value) or value <= 0:
                raise CaptureContractError(
                    f"IQFeed provider-loop {label} must be positive"
                )
        with self._lock:
            if self._state is not IqfeedProviderLoopSupervisorState.PREPARED:
                raise CaptureContractError(
                    "IQFeed provider-loop supervisor start is one-shot"
                )
            self._state = IqfeedProviderLoopSupervisorState.STARTING
            self._ever_started = True
            forced_by_lane = {
                "trade": tuple(
                    str(symbol or "").strip().upper()
                    for symbol in trade_forced_symbols
                    if str(symbol or "").strip()
                ),
                "depth": tuple(
                    str(symbol or "").strip().upper()
                    for symbol in depth_forced_symbols
                    if str(symbol or "").strip()
                ),
            }
            for lane in ("trade", "depth"):
                self._threads[lane] = threading.Thread(
                    target=self._run_lane,
                    kwargs={
                        "lane": lane,
                        "forced_symbols": forced_by_lane[lane],
                        "reconnect_wait_seconds": reconnect_wait,
                    },
                    daemon=False,
                    name=f"chili-iqfeed-{lane}-provider-loop",
                )
            started: list[threading.Thread] = []
            try:
                for lane in ("trade", "depth"):
                    thread = self._threads[lane]
                    thread.start()
                    started.append(thread)
            except BaseException as exc:
                self._record_failure("supervisor_start", exc)
                self._stop_event.set()

        if len(started) != 2:
            self._join_threads(join_timeout)
            raise CaptureContractError(
                "IQFeed provider-loop thread start failed"
            )

        deadline = time.monotonic() + readiness_timeout
        while True:
            with self._lock:
                if self._failures:
                    break
                if all(event.is_set() for event in self._ready.values()):
                    self._state = IqfeedProviderLoopSupervisorState.RUNNING
                    return self.health()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._record_failure(
                    "readiness",
                    TimeoutError(
                        "IQFeed trade/depth readiness deadline expired"
                    ),
                )
                break
            self._state_changed.wait(min(0.05, remaining))
            self._state_changed.clear()

        self._stop_event.set()
        self._join_threads(join_timeout)
        failure = self.health()["failures"]
        raise CaptureContractError(
            "IQFeed provider-loop startup failed closed: "
            + json.dumps(failure, sort_keys=True)
        )

    def _join_threads(self, timeout_seconds: float) -> None:
        deadline = time.monotonic() + float(timeout_seconds)
        current = threading.current_thread()
        for lane in ("trade", "depth"):
            thread = self._threads.get(lane)
            if (
                thread is None
                or thread is current
                or thread.ident is None
            ):
                continue
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        alive = tuple(
            lane
            for lane, thread in self._threads.items()
            if thread is not current and thread.is_alive()
        )
        if alive:
            self._record_failure(
                "shutdown",
                TimeoutError(
                    "IQFeed provider lanes did not join: " + ",".join(alive)
                ),
            )
            raise CaptureContractError(
                "IQFeed provider-loop shutdown did not quiesce both lanes"
            )

    def close(self, *, join_timeout_seconds: float) -> Mapping[str, Any]:
        join_timeout = float(join_timeout_seconds)
        if not math.isfinite(join_timeout) or join_timeout <= 0:
            raise CaptureContractError(
                "IQFeed provider-loop join timeout must be positive"
            )
        with self._lock:
            if self._state is IqfeedProviderLoopSupervisorState.STOPPED:
                return self.health()
            if self._state is IqfeedProviderLoopSupervisorState.PREPARED:
                self._state = IqfeedProviderLoopSupervisorState.STOPPED
                return self.health()
            if self._state is not IqfeedProviderLoopSupervisorState.FAILED:
                self._state = IqfeedProviderLoopSupervisorState.STOPPING
            self._stop_event.set()
            self._state_changed.set()
        self._join_threads(join_timeout)
        with self._lock:
            if not self._safe_to_unbind:
                raise CaptureContractError(
                    "IQFeed provider reader may still be alive; refusing unbind"
                )
            if self._state is not IqfeedProviderLoopSupervisorState.FAILED:
                self._state = IqfeedProviderLoopSupervisorState.STOPPED
            return self.health()

    def health(self) -> Mapping[str, Any]:
        with self._lock:
            lanes = {
                lane: {
                    "thread_started": bool(
                        self._threads.get(lane)
                        and self._threads[lane].ident is not None
                    ),
                    "thread_alive": bool(
                        self._threads.get(lane)
                        and self._threads[lane].is_alive()
                    ),
                    "thread_daemon": (
                        None
                        if lane not in self._threads
                        else self._threads[lane].daemon
                    ),
                    "socket_connected": self._connected[lane].is_set(),
                    "schema_verified": self._schema_ready[lane].is_set(),
                    "ready": self._ready[lane].is_set(),
                }
                for lane in ("trade", "depth")
            }
            return MappingProxyType(
                {
                    "state": self._state.value,
                    "ever_started": self._ever_started,
                    "stop_requested": self._stop_event.is_set(),
                    "all_ready": all(
                        lane["ready"] for lane in lanes.values()
                    ),
                    "provider_sockets_started": any(
                        lane["socket_connected"] for lane in lanes.values()
                    ),
                    "safe_to_unbind": self._safe_to_unbind,
                    "lanes": lanes,
                    "failures": {
                        lane: dict(value)
                        for lane, value in self._failures.items()
                    },
                }
            )


def _utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise CaptureContractError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _iso(value: datetime) -> str:
    return _utc(value, "IQFeed host receipt time").isoformat().replace("+00:00", "Z")


def _source_identity(
    module: Any,
    *,
    expected_path: Path,
    expected_sha256: str,
    role: str,
) -> Mapping[str, str]:
    actual_path = Path(str(getattr(module, "__file__", "") or ""))
    expected = Path(expected_path)
    if not actual_path.is_absolute() or actual_path.resolve() != expected.resolve():
        raise CaptureContractError(f"loaded {role} module path escaped preflight")
    digest = _stable_source_sha256(actual_path, role=role)
    expected_hash = str(expected_sha256 or "").strip().lower()
    declared_hash = str(getattr(module, "BRIDGE_SOURCE_SHA256", "") or "").strip().lower()
    if digest != expected_hash or declared_hash != expected_hash:
        raise CaptureContractError(f"loaded {role} module source hash escaped preflight")
    return {
        "role": role,
        "path": str(actual_path.resolve()),
        "sha256": digest,
    }


def _stable_source_sha256(path: Path, *, role: str) -> str:
    _reject_reparse_chain(path, role=role)
    try:
        before = os.stat(path, follow_symlinks=False)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size <= 0
            or before.st_size > _MAX_SOURCE_BYTES
        ):
            raise CaptureContractError(f"loaded {role} module source size is invalid")
        digest = hashlib.sha256()
        total = 0
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > _MAX_SOURCE_BYTES:
                    raise CaptureContractError(
                        f"loaded {role} module source exceeded its bound"
                    )
                digest.update(chunk)
        after = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise CaptureContractError(f"loaded {role} module source is unavailable") from exc
    before_identity = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )
    after_identity = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    )
    if before_identity != after_identity or total != before.st_size:
        raise CaptureContractError(f"loaded {role} module source drifted while hashing")
    return digest.hexdigest()


def _reject_reparse_chain(path: Path, *, role: str) -> None:
    current = Path(path)
    while True:
        try:
            info = os.lstat(current)
        except OSError as exc:
            raise CaptureContractError(f"{role} path is unavailable") from exc
        attributes = int(getattr(info, "st_file_attributes", 0) or 0)
        if stat.S_ISLNK(info.st_mode) or attributes & _REPARSE_ATTRIBUTE:
            raise CaptureContractError(f"{role} path traverses a reparse point")
        parent = current.parent
        if parent == current:
            return
        current = parent


@dataclass(frozen=True)
class IqfeedCaptureHostBindingReceipt:
    process_id: int
    process_instance_id: str
    bound_at: datetime
    python_executable: str
    composition_sha256: str
    resource_binding_sha256: str
    manifest_sha256: str
    host_source_sha256: str
    trade_bridge: Mapping[str, str]
    depth_bridge: Mapping[str, str]
    schema_version: str = _HOST_BINDING_RECEIPT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != _HOST_BINDING_RECEIPT_SCHEMA_VERSION:
            raise CaptureContractError("IQFeed host binding receipt schema is unsupported")
        if isinstance(self.process_id, bool) or int(self.process_id) <= 0:
            raise CaptureContractError("IQFeed host binding receipt PID is malformed")
        object.__setattr__(self, "process_id", int(self.process_id))
        try:
            process_instance_id = str(uuid.UUID(str(self.process_instance_id or "")))
        except (ValueError, AttributeError, TypeError) as exc:
            raise CaptureContractError(
                "IQFeed host binding receipt process instance is malformed"
            ) from exc
        object.__setattr__(self, "process_instance_id", process_instance_id)
        object.__setattr__(self, "bound_at", _utc(self.bound_at, "IQFeed host bound_at"))
        executable = Path(str(self.python_executable or ""))
        if not executable.is_absolute():
            raise CaptureContractError("IQFeed host Python executable is malformed")
        object.__setattr__(self, "python_executable", str(executable.resolve()))
        for name in (
            "composition_sha256",
            "resource_binding_sha256",
            "manifest_sha256",
            "host_source_sha256",
        ):
            value = str(getattr(self, name) or "").strip().lower()
            if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
                raise CaptureContractError(f"IQFeed host receipt {name} is malformed")
            object.__setattr__(self, name, value)
        for name, expected_role in (
            ("trade_bridge", "iqfeed_trade_bridge"),
            ("depth_bridge", "iqfeed_depth_bridge"),
        ):
            value = dict(getattr(self, name))
            path = Path(str(value.get("path") or ""))
            digest = str(value.get("sha256") or "").strip().lower()
            if (
                set(value) != {"role", "path", "sha256"}
                or value.get("role") != expected_role
                or not path.is_absolute()
                or len(digest) != 64
                or any(ch not in "0123456789abcdef" for ch in digest)
            ):
                raise CaptureContractError(f"IQFeed host receipt {name} is malformed")
            value["path"] = str(path.resolve())
            value["sha256"] = digest
            object.__setattr__(self, name, MappingProxyType(value))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "process_id": self.process_id,
            "process_instance_id": self.process_instance_id,
            "bound_at": _iso(self.bound_at),
            "python_executable": self.python_executable,
            "composition_sha256": self.composition_sha256,
            "resource_binding_sha256": self.resource_binding_sha256,
            "manifest_sha256": self.manifest_sha256,
            "host_source_sha256": self.host_source_sha256,
            "trade_bridge": dict(self.trade_bridge),
            "depth_bridge": dict(self.depth_bridge),
            "provider_sockets_started": False,
            "database_or_broker_started": False,
            "paper_live_execution_enabled": False,
            "task_or_service_mutated": False,
        }

    @property
    def receipt_sha256(self) -> str:
        return sha256_json(self.to_dict())


@dataclass(frozen=True)
class IqfeedCaptureHostAdmission:
    symbol: str
    capture_ready: bool
    l2_checkpoint_queued: bool
    rejected_reason: str | None


def _executed_inventory_from_reads(
    *,
    proof: ActiveCaptureInputPrefixAttestation,
    captured_reads: Sequence[CapturedReadResult],
) -> ExecutedCaptureReadInventory:
    """Convert the original durable objects; never reconstruct from read IDs."""

    verified = verify_active_capture_input_attestation(proof)
    reads = tuple(captured_reads)
    if not reads or any(type(row) is not CapturedReadResult for row in reads):
        raise CaptureContractError(
            "captured PAPER executed read objects are unavailable"
        )
    evidence = tuple(executed_capture_read_evidence(row) for row in reads)
    ordered = tuple(
        sorted(
            evidence,
            key=lambda row: (row.receipt_event_sequence, row.read_id),
        )
    )
    if (
        len({row.read_id for row in ordered}) != len(ordered)
        or len({row.receipt_event_sequence for row in ordered}) != len(ordered)
        or any(
            row.run_id != verified.run_id
            or row.generation != verified.generation
            or row.identity_sha256 != verified.identity_sha256
            or row.decision_id != verified.decision_id
            for row in ordered
        )
    ):
        raise CaptureContractError(
            "captured PAPER executed read identity is inconsistent"
        )
    return ExecutedCaptureReadInventory(
        run_id=verified.run_id,
        generation=verified.generation,
        identity_sha256=verified.identity_sha256,
        decision_id=verified.decision_id,
        reads=ordered,
    )


@dataclass(frozen=True)
class IqfeedCapturedPaperTickResult:
    """One FSM result plus the final frontier that must be checkpointed."""

    decision_at: datetime
    fsm_result: Mapping[str, Any] | CapturedPaperPostCommitRequest
    first_dip_final_capture_frontier: FirstDipFinalCaptureFrontier | None
    scanner_snapshot_read_ids: tuple[str, ...]
    ohlcv_read_ids: tuple[str, ...]
    microstructure_read_ids: tuple[str, ...]
    captured_reads: tuple[CapturedReadResult, ...] = ()
    executed_read_inventory: ExecutedCaptureReadInventory | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "decision_at",
            _utc(self.decision_at, "captured PAPER decision clock"),
        )
        if type(self.fsm_result) is CapturedPaperPostCommitRequest:
            self.fsm_result.verify()
        elif isinstance(self.fsm_result, Mapping):
            object.__setattr__(
                self,
                "fsm_result",
                MappingProxyType(dict(self.fsm_result)),
            )
        else:
            raise CaptureContractError("captured PAPER FSM result is malformed")
        reads = tuple(self.captured_reads)
        execution_required = type(self.fsm_result) is CapturedPaperPostCommitRequest
        if (not reads) != (self.executed_read_inventory is None):
            raise CaptureContractError(
                "captured PAPER tick executed read pair is incomplete"
            )
        if execution_required and not reads:
            raise CaptureContractError(
                "captured PAPER tick executed reads are unavailable"
            )
        if reads and (
            any(type(row) is not CapturedReadResult or not row.durable for row in reads)
            or type(self.executed_read_inventory) is not ExecutedCaptureReadInventory
        ):
            raise CaptureContractError(
                "captured PAPER tick executed reads are unavailable"
            )
        object.__setattr__(self, "captured_reads", reads)
        if reads:
            expected_evidence = tuple(
                sorted(
                    (executed_capture_read_evidence(row) for row in reads),
                    key=lambda row: (row.receipt_event_sequence, row.read_id),
                )
            )
            assert self.executed_read_inventory is not None
            if tuple(self.executed_read_inventory.reads) != expected_evidence:
                raise CaptureContractError(
                    "captured PAPER tick inventory differs from durable reads"
                )
        for values, label in (
            (self.scanner_snapshot_read_ids, "scanner"),
            (self.ohlcv_read_ids, "OHLCV"),
            (self.microstructure_read_ids, "microstructure"),
        ):
            if any(not str(value or "").strip() for value in values):
                raise CaptureContractError(
                    f"captured PAPER {label} read id is malformed"
                )


@dataclass(frozen=True, slots=True)
class IqfeedCapturedPaperDecisionMaterial:
    """One no-fetch decision packet staged by the capture owner.

    The object binds the phase-zero selection draft to the raw exact captured
    inputs consumed by the later admission transaction.  It intentionally does
    not accept an adaptive source: PAPER can build that source only after the
    account/reservation rows are locked by ``commit_captured_paper_admission``.
    The object carries no SQLAlchemy Session and has no deserialization path.
    First-dip material additionally carries the local durable-read callback
    needed to issue a fresh final receipt inside that locked admission.
    """

    selection_context: CapturedPaperSelectionContext
    admission_inputs: CapturedPaperAdmissionInputs
    predecision_captured_reads: tuple[CapturedReadResult, ...] = field(
        repr=False,
        compare=False,
    )
    predecision_executed_read_inventory: ExecutedCaptureReadInventory = field(
        repr=False,
        compare=False,
    )
    final_read_provider: FirstDipFinalReadProvider | None = None
    bound_input_scope: CapturedPaperBoundInputScope | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    candidate_sha256: str | None = None
    material_sha256: str = ""

    def __post_init__(self) -> None:
        body = self._verified_body()
        digest = sha256_json(body)
        supplied = str(self.material_sha256 or "").strip()
        if supplied and supplied != digest:
            raise CaptureContractError(
                "captured PAPER decision material hash mismatch"
            )
        object.__setattr__(self, "material_sha256", digest)

    def _verified_body(self) -> dict[str, Any]:
        context = self.selection_context
        admission = self.admission_inputs
        if type(context) is not CapturedPaperSelectionContext:
            raise CaptureContractError(
                "captured PAPER selection context is not typed"
            )
        if type(admission) is not CapturedPaperAdmissionInputs:
            raise CaptureContractError(
                "captured PAPER admission material is not typed"
            )
        context.verify()
        dispatch = context.dispatch_request
        request = context.draft
        admission.dispatch_request.verify()
        admission.post_commit_request.verify()
        if (
            admission.dispatch_request.provenance_sha256
            != dispatch.provenance_sha256
            or admission.dispatch_request.route_token.route_token_sha256
            != dispatch.route_token.route_token_sha256
            or admission.post_commit_request.completion_sha256
            != request.completion_sha256
            or admission.post_commit_request.to_canonical_json()
            != request.to_canonical_json()
        ):
            raise CaptureContractError(
                "captured PAPER selection/admission request mismatch"
            )

        proof = verify_active_capture_input_attestation(
            admission.active_input_attestation
        )
        predecision_reads = tuple(self.predecision_captured_reads)
        if (
            not predecision_reads
            or any(
                type(row) is not CapturedReadResult or not row.durable
                for row in predecision_reads
            )
            or type(self.predecision_executed_read_inventory)
            is not ExecutedCaptureReadInventory
            or admission.executed_read_inventory
            is not self.predecision_executed_read_inventory
            or _executed_inventory_from_reads(
                proof=proof,
                captured_reads=predecision_reads,
            ).to_dict()
            != self.predecision_executed_read_inventory.to_dict()
        ):
            raise CaptureContractError(
                "captured PAPER predecision executed reads mismatch"
            )
        intent = request.intent
        route = intent.route_token
        authority = admission.broker_account_facts.capture_authority
        if (
            proof.decision_id != intent.decision_id
            or proof.code_build_sha256 != route.code_build_sha256
            or proof.config_sha256 != route.config_sha256
            or proof.feature_flags_sha256 != intent.feature_flags_sha256
            or admission.policy_spec.policy.policy_sha256
            != intent.policy_sha256
            or admission.policy_spec.code_build_sha256
            != route.code_build_sha256
            or admission.policy_spec.effective_config_sha256
            != route.config_sha256
            or admission.policy_spec.feature_flags_sha256
            != intent.feature_flags_sha256
            or admission.operational_policy.config_provenance_sha256
            != route.config_sha256
            or admission.broker_account_facts.account_identity_sha256
            != proof.account_identity_sha256
            or admission.broker_account_facts.account_scope
            != route.account_scope
            or admission.broker_account_facts.execution_family
            != route.execution_family
            or admission.broker_account_facts.broker_environment != "paper"
            or admission.broker_account_facts.venue != "alpaca"
            or authority.account_id != route.expected_account_id
            or authority.account_identity_sha256
            != proof.account_identity_sha256
            or authority.decision_id != proof.decision_id
            or authority.run_id != proof.run_id
            or authority.generation != proof.generation
            or authority.active_input_attestation_sha256
            != proof.attestation_sha256
            or authority.expires_at > proof.expires_at
            or authority.expires_at < intent.decision_at
        ):
            raise CaptureContractError(
                "captured PAPER decision provenance mismatch"
            )

        bbo_rows = tuple(
            row
            for row in proof.read_evidence
            if row.receipt.read_id == admission.exact_bbo.read_id
        )
        if (
            len(bbo_rows) != 1
            or bbo_rows[0].receipt_sha256 != intent.bbo_receipt_sha256
            or admission.exact_bbo.source_event_sha256
            not in bbo_rows[0].receipt.source_event_sha256s
            or admission.account_receipt.read_id
            != admission.broker_account_facts.capture_authority.account_read_id
            or admission.account_receipt.source_event_sha256
            != admission.broker_account_facts.capture_authority.account_source_event_sha256
            or intent.account_receipt_sha256
            != admission.broker_account_facts.capture_authority.account_read_receipt_sha256
            or admission.account_receipt.read_id != authority.account_read_id
            or admission.account_receipt.source_event_sha256
            != authority.account_source_event_sha256
            or str(admission.economics.structural_stop)
            != str(float(intent.structural_stop_price))
        ):
            raise CaptureContractError(
                "captured PAPER BBO/stop/account receipt mismatch"
            )

        first_dip = intent.setup_family == "first_dip_reclaim"
        detector_audit = admission.first_dip_detector_audit
        census = admission.buying_power_double_census
        if census is not None:
            if (
                type(census) is not PreparedAlpacaPaperBuyingPowerDoubleCensus
                or census.account_authority
                is not admission.broker_account_facts.capture_authority
            ):
                raise CaptureContractError(
                    "captured PAPER buying-power census binding mismatch"
                )
            try:
                verify_alpaca_paper_buying_power_double_census(
                    census,
                    verified_at=intent.decision_at,
                )
            except (AlpacaBuyingPowerReflectionError, TypeError, ValueError) as exc:
                raise CaptureContractError(
                    "captured PAPER buying-power census is invalid"
                ) from exc
        if first_dip:
            opportunity = intent.opportunity_key
            if opportunity is None:
                raise CaptureContractError(
                    "captured PAPER first-dip opportunity is missing"
                )
            adaptive_opportunity = AdaptiveRiskOpportunityKey.from_payload(
                {
                    "account_scope": opportunity.account_scope,
                    "symbol": opportunity.symbol,
                    "trading_date": opportunity.trading_date.isoformat(),
                    "setup_family": opportunity.setup_family,
                }
            )
            if (
                type(detector_audit) is not CapturedFirstDipDetectorAudit
                or not isinstance(
                    self.final_read_provider, FirstDipFinalReadProvider
                )
                or proof.first_dip_tape_read_id is None
                or detector_audit.detector_receipt_binding_sha256
                != intent.setup_evidence_sha256
                or detector_audit.detector_opportunity_key_sha256
                != adaptive_opportunity.key_sha256
            ):
                raise CaptureContractError(
                    "captured PAPER first-dip material mismatch"
                )
        elif self.final_read_provider is not None or detector_audit is not None:
            raise CaptureContractError(
                "captured PAPER non-first-dip material has first-dip authority"
            )

        if self.bound_input_scope is not None and type(
            self.bound_input_scope
        ) is not CapturedPaperBoundInputScope:
            raise CaptureContractError(
                "captured PAPER bound input scope is malformed"
            )
        candidate_sha256 = str(self.candidate_sha256 or "").strip() or None
        if candidate_sha256 is not None and (
            len(candidate_sha256) != 64
            or any(ch not in "0123456789abcdef" for ch in candidate_sha256)
        ):
            raise CaptureContractError(
                "captured PAPER candidate hash is malformed"
            )

        fact_inventory = {
            name: {
                "content_sha256": fact.content_sha256,
                "source": fact.source,
                "observed_at": fact.observed_at.isoformat(),
                "available_at": fact.available_at.isoformat(),
                "provider_generation": fact.provider_generation,
                "source_read_ids": list(fact.source_read_ids),
            }
            for name, fact in admission.fact_evidence.as_mapping().items()
        }
        return {
            "schema_version": "chili.iqfeed-captured-paper-decision-material.v1",
            "selection_context_sha256": context.context_sha256,
            "dispatch_provenance_sha256": dispatch.provenance_sha256,
            "route_token_sha256": route.route_token_sha256,
            "runtime_generation": route.runtime_generation,
            "completion_sha256": request.completion_sha256,
            "confirmed_arm_generation_sha256": (
                intent.confirmed_arm_generation.confirmed_arm_generation_sha256
            ),
            "decision_id": intent.decision_id,
            "setup_family": intent.setup_family,
            "structural_stop_price": intent.structural_stop_price,
            "entry_limit_ceiling_price": intent.entry_limit_ceiling_price,
            "account_receipt_sha256": intent.account_receipt_sha256,
            "bbo_receipt_sha256": intent.bbo_receipt_sha256,
            "setup_evidence_sha256": intent.setup_evidence_sha256,
            "policy_sha256": intent.policy_sha256,
            "feature_flags_sha256": intent.feature_flags_sha256,
            "active_input_attestation_sha256": proof.attestation_sha256,
            "active_input_prefix_root_sha256": proof.input_prefix_root_sha256,
            "active_input_generation": proof.generation,
            "active_input_run_id": proof.run_id,
            "predecision_executed_read_inventory_sha256": (
                self.predecision_executed_read_inventory.inventory_sha256
            ),
            "adaptive_policy_provenance_sha256": (
                admission.policy_spec.provenance_sha256
            ),
            "operational_policy_sha256": (
                admission.operational_policy.policy_sha256
            ),
            "account_authority_sha256": (
                admission.broker_account_facts.capture_authority.authority_sha256
            ),
            "exact_bbo": {
                "read_id": admission.exact_bbo.read_id,
                "source_event_sha256": admission.exact_bbo.source_event_sha256,
                "payload_sha256": hashlib.sha256(
                    admission.exact_bbo.payload_json.encode("utf-8")
                ).hexdigest(),
            },
            "account_receipt": {
                "read_id": admission.account_receipt.read_id,
                "source_event_sha256": (
                    admission.account_receipt.source_event_sha256
                ),
                "payload_sha256": hashlib.sha256(
                    admission.account_receipt.payload_json.encode("utf-8")
                ).hexdigest(),
            },
            "economics": asdict(admission.economics),
            "fact_evidence": fact_inventory,
            "correlation_cluster": admission.correlation_cluster,
            "buying_power_double_census_sha256": (
                None if census is None else census.batch_content_sha256
            ),
            "first_dip_detector_audit_sha256": (
                None if detector_audit is None else detector_audit.audit_sha256
            ),
            "first_dip_final_read_provider_required": first_dip,
            "candidate_sha256": candidate_sha256,
            "bound_input_scope_sha256": (
                None
                if self.bound_input_scope is None
                else self.bound_input_scope.scope_sha256
            ),
        }

    def verify(self) -> None:
        if sha256_json(self._verified_body()) != self.material_sha256:
            raise CaptureContractError(
                "captured PAPER decision material changed after staging"
            )

    def verify_for_dispatch(
        self,
        request: CapturedPaperDispatchRequest,
    ) -> None:
        if type(request) is not CapturedPaperDispatchRequest:
            raise CaptureContractError(
                "captured PAPER dispatch request is not typed"
            )
        request.verify()
        self.verify()
        expected = self.selection_context.dispatch_request
        if (
            request.provenance_sha256 != expected.provenance_sha256
            or request.route_token.route_token_sha256
            != expected.route_token.route_token_sha256
            or request.runtime_generation != expected.runtime_generation
        ):
            raise CaptureContractError(
                "captured PAPER staged decision route mismatch"
            )


@dataclass(frozen=True, slots=True)
class IqfeedCapturedPaperAdmissionHandoff:
    """Retained phase-two material plus the complete phase-one tick reads."""

    decision_material: IqfeedCapturedPaperDecisionMaterial
    captured_reads: tuple[CapturedReadResult, ...]
    executed_read_inventory: ExecutedCaptureReadInventory

    def __post_init__(self) -> None:
        if type(self.decision_material) is not IqfeedCapturedPaperDecisionMaterial:
            raise CaptureContractError(
                "captured PAPER admission decision material is malformed"
            )
        self.decision_material.verify()
        verify_captured_paper_executed_read_inventory(
            inventory=self.executed_read_inventory,
            captured_reads=self.captured_reads,
            active_input_attestation=(
                self.decision_material.admission_inputs.active_input_attestation
            ),
            request=self.decision_material.selection_context.draft,
            material_sha256=self.decision_material.material_sha256,
            require_exact_attestation=False,
        )


class _IqfeedCapturedPaperObservationFirstDipScope:
    """Detector-only exact-print authority; cannot build or reserve risk."""

    def __init__(
        self,
        *,
        coordinator: Any,
        prepared: PreparedCapturedPaperObservation,
    ) -> None:
        if type(prepared) is not PreparedCapturedPaperObservation:
            raise CaptureContractError(
                "captured PAPER observation material is not typed"
            )
        context = prepared.observation_context
        context.verify()
        proof = verify_active_capture_input_attestation(
            prepared.active_input_attestation
        )
        policy = prepared.first_dip_detector_policy
        if (
            type(policy) is not FirstDipTapePolicy
            or prepared.first_dip_tape_read_id is None
            or proof.first_dip_tape_read_id
            != prepared.first_dip_tape_read_id
            or proof.decision_id != context.observation_decision_id
        ):
            raise CaptureContractError(
                "captured PAPER observation exact-print authority is unavailable"
            )
        observed_now = _utc(
            coordinator._observed_now(),
            "captured PAPER observation coordinator clock",
        )
        if not proof.attested_available_at <= observed_now <= proof.expires_at:
            raise CaptureContractError(
                "captured PAPER observation exact-print proof is stale"
            )
        self.coordinator = coordinator
        self.context = context
        self.policy = policy
        self.proof = proof
        self._authority = coordinator.prepare_captured_first_dip_tape_authority(
            attestation=proof,
            policy=policy,
            purpose="detector",
        )
        self._started = False
        self._lock = threading.Lock()

    @property
    def network_fallback_allowed(self) -> bool:
        return False

    def _retain_detector(
        self,
        resolution: object,
        opportunity_key: Mapping[str, object],
    ) -> str:
        payload = dict(opportunity_key)
        supplied_scope = str(payload.get("account_scope") or "").strip()
        expected_scope = self.context.dispatch_request.account_scope
        if supplied_scope and supplied_scope != expected_scope:
            raise CaptureContractError(
                "captured PAPER observation opportunity account changed"
            )
        payload["account_scope"] = expected_scope
        opportunity = AdaptiveRiskOpportunityKey.from_payload(payload)
        expected_date = self.context.decision_at.astimezone(
            ZoneInfo("America/New_York")
        ).date()
        if (
            opportunity.symbol != self.context.dispatch_request.symbol
            or opportunity.setup_family != "first_dip_reclaim"
            or opportunity.trading_date != expected_date
        ):
            raise CaptureContractError(
                "captured PAPER observation opportunity changed"
            )
        return self.coordinator.retain_accepted_first_dip_detector(
            resolution=resolution,
            opportunity_key_sha256=opportunity.key_sha256,
        )

    @contextmanager
    def install(self):
        with self._lock:
            if self._started:
                raise CaptureContractError(
                    "captured PAPER observation first-dip scope is one-shot"
                )
            self._started = True
        with (
            _installed_captured_first_dip_detector_retention_provider(
                self._retain_detector
            ),
            _installed_captured_db_paper_first_dip_tape_decision_authority(
                self._authority
            ),
        ):
            yield self


class _IqfeedCapturedPaperFirstDipScopes:
    """Split detector and locked-final first-dip capabilities.

    Phase one needs only the capture-issued detector authority plus retention
    sink.  The final provider is installed separately around the admission
    transaction, where the adaptive request can first be built from the locked
    account/risk bundle.  No pre-lock adaptive source exists on this path.
    """

    def __init__(
        self,
        *,
        coordinator: Any,
        decision_material: IqfeedCapturedPaperDecisionMaterial,
    ) -> None:
        if type(decision_material) is not IqfeedCapturedPaperDecisionMaterial:
            raise CaptureContractError(
                "captured PAPER first-dip material is not typed"
            )
        decision_material.verify()
        admission = decision_material.admission_inputs
        intent = decision_material.selection_context.draft.intent
        audit = admission.first_dip_detector_audit
        opportunity = intent.opportunity_key
        if (
            intent.setup_family != "first_dip_reclaim"
            or type(audit) is not CapturedFirstDipDetectorAudit
            or opportunity is None
            or not isinstance(
                decision_material.final_read_provider,
                FirstDipFinalReadProvider,
            )
        ):
            raise CaptureContractError(
                "captured PAPER first-dip split scope is incomplete"
            )
        proof = verify_active_capture_input_attestation(
            admission.active_input_attestation
        )
        observed_now = _utc(
            coordinator._observed_now(),
            "captured PAPER first-dip coordinator clock",
        )
        if (
            proof.decision_id != intent.decision_id
            or proof.first_dip_tape_read_id is None
            or observed_now < proof.attested_available_at
            or observed_now > proof.expires_at
        ):
            raise CaptureContractError(
                "captured PAPER first-dip detector proof is unavailable"
            )
        adaptive_opportunity = AdaptiveRiskOpportunityKey.from_payload(
            {
                "account_scope": opportunity.account_scope,
                "symbol": opportunity.symbol,
                "trading_date": opportunity.trading_date.isoformat(),
                "setup_family": opportunity.setup_family,
            }
        )
        if adaptive_opportunity.key_sha256 != audit.detector_opportunity_key_sha256:
            raise CaptureContractError(
                "captured PAPER first-dip adaptive opportunity changed"
            )
        detector_authority = coordinator.prepare_captured_first_dip_tape_authority(
            attestation=proof,
            policy=audit.detector_policy,
            purpose="detector",
        )
        self.coordinator = coordinator
        self.decision_material = decision_material
        self.detector_attestation = proof
        self.detector_policy = audit.detector_policy
        self.detector_audit = audit
        self.adaptive_opportunity = adaptive_opportunity
        self.final_read_provider = decision_material.final_read_provider
        self._detector_authority = detector_authority
        self._scope_lock = threading.Lock()
        self._detector_scope_started = False
        self._final_scope_started = False
        self._final_capture_frontier: FirstDipFinalCaptureFrontier | None = None
        self._final_input_attestation: ActiveCaptureInputPrefixAttestation | None = None
        self._final_captured_reads: tuple[CapturedReadResult, ...] | None = None
        self._final_executed_read_inventory: ExecutedCaptureReadInventory | None = None

    @property
    def network_fallback_allowed(self) -> bool:
        return False

    @property
    def final_capture_frontier(self) -> FirstDipFinalCaptureFrontier | None:
        with self._scope_lock:
            return self._final_capture_frontier

    def final_executed_read_binding(
        self,
    ) -> CapturedPaperFinalExecutedReadAuthority:
        """Return the exact final read only after the provider was consumed."""

        with self._scope_lock:
            frontier = self._final_capture_frontier
            proof = self._final_input_attestation
            captured_reads = self._final_captured_reads
            inventory = self._final_executed_read_inventory
        if (
            frontier is None
            or proof is None
            or captured_reads is None
            or inventory is None
        ):
            raise CaptureContractError(
                "captured PAPER first-dip final executed reads are unavailable"
            )
        return CapturedPaperFinalExecutedReadAuthority(
            inventory=inventory,
            captured_reads=captured_reads,
            active_input_attestation=proof,
            frontier=frontier,
        )

    def _retain_detector(
        self,
        resolution: object,
        opportunity_key: Mapping[str, object],
    ) -> str:
        if not isinstance(opportunity_key, Mapping):
            raise CaptureContractError(
                "captured PAPER first-dip detector opportunity is malformed"
            )
        payload = dict(opportunity_key)
        supplied_scope = str(payload.get("account_scope") or "").strip()
        expected_scope = self.adaptive_opportunity.account_scope
        if supplied_scope and supplied_scope != expected_scope:
            raise CaptureContractError(
                "captured PAPER first-dip detector account scope changed"
            )
        payload["account_scope"] = expected_scope
        retained_opportunity = AdaptiveRiskOpportunityKey.from_payload(payload)
        if (
            retained_opportunity.to_payload()
            != self.adaptive_opportunity.to_payload()
            or retained_opportunity.key_sha256
            != self.detector_audit.detector_opportunity_key_sha256
        ):
            raise CaptureContractError(
                "captured PAPER first-dip detector opportunity changed"
            )
        return self.coordinator.retain_accepted_first_dip_detector(
            resolution=resolution,
            opportunity_key_sha256=retained_opportunity.key_sha256,
        )

    @contextmanager
    def detector_scope(self):
        """Install detector-only authority; never install an adaptive source."""

        with self._scope_lock:
            if self._detector_scope_started:
                raise CaptureContractError(
                    "captured PAPER first-dip detector scope is one-shot"
                )
            self._detector_scope_started = True
        with (
            _installed_captured_first_dip_detector_retention_provider(
                self._retain_detector
            ),
            _installed_captured_db_paper_first_dip_tape_decision_authority(
                self._detector_authority
            ),
        ):
            yield self

    def _verify_locked_adaptive_request(
        self,
        request: AdaptiveRiskReservationRequest,
        *,
        final_boundary_available_at: datetime,
    ) -> None:
        admission = self.decision_material.admission_inputs
        intent = self.decision_material.selection_context.draft.intent
        route = intent.route_token
        proof = self.detector_attestation
        inputs = request.inputs
        snapshot = request.account_snapshot
        economics = admission.economics
        try:
            bbo = json.loads(admission.exact_bbo.payload_json)
            bid = float(bbo["bid"])
            ask = float(bbo["ask"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise CaptureContractError(
                "captured PAPER first-dip BBO payload is malformed"
            ) from exc
        exact_text = {
            "decision_id": (inputs.decision_id, intent.decision_id),
            "run_id": (inputs.replay_or_paper_run_id, proof.run_id),
            "execution_surface": (inputs.execution_surface, "alpaca_paper"),
            "execution_family": (inputs.execution_family, route.execution_family),
            "venue": (inputs.venue, "alpaca"),
            "broker_environment": (inputs.broker_environment, "paper"),
            "symbol": (inputs.symbol, route.symbol),
            "account_identity": (
                inputs.account_identity_sha256,
                proof.account_identity_sha256,
            ),
            "code_build": (inputs.code_build_sha256, route.code_build_sha256),
            "config": (inputs.effective_config_sha256, route.config_sha256),
            "feature_flags": (
                inputs.feature_flags_sha256,
                intent.feature_flags_sha256,
            ),
            "capture_prefix": (
                inputs.capture_prefix_root_sha256,
                proof.input_prefix_root_sha256,
            ),
            "request_account_scope": (request.account_scope, route.account_scope),
            "snapshot_account_scope": (snapshot.account_scope, route.account_scope),
            "snapshot_account_identity": (
                snapshot.account_identity_sha256,
                proof.account_identity_sha256,
            ),
            "setup_family": (request.setup_family, intent.setup_family),
            "correlation_cluster": (
                request.correlation_cluster,
                admission.correlation_cluster,
            ),
            "input_correlation_cluster": (
                inputs.correlation_cluster_id,
                admission.correlation_cluster,
            ),
            "client_order_id": (request.client_order_id, intent.client_order_id),
        }
        changed = sorted(
            name for name, (actual, expected) in exact_text.items()
            if actual != expected
        )
        numeric = {
            "entry_limit": (
                request.entry_limit_price,
                float(intent.entry_limit_ceiling_price),
            ),
            "bid": (inputs.bid, bid),
            "ask": (inputs.ask, ask),
            "structural_stop": (
                inputs.structural_stop,
                economics.structural_stop,
            ),
            "entry_slippage": (
                inputs.entry_slippage_bps,
                economics.entry_slippage_bps,
            ),
            "exit_slippage": (
                inputs.exit_slippage_bps,
                economics.exit_slippage_bps,
            ),
            "fees": (inputs.fees_per_share_usd, economics.fees_per_share_usd),
            "setup_quality": (inputs.setup_quality, economics.setup_quality),
            "volatility": (
                inputs.realized_volatility_fraction,
                economics.realized_volatility_fraction,
            ),
            "adv": (
                inputs.average_daily_volume_shares,
                economics.average_daily_volume_shares,
            ),
            "recent_volume": (
                inputs.recent_volume_shares,
                economics.recent_volume_shares,
            ),
            "depth": (
                inputs.executable_depth_shares,
                economics.executable_depth_shares,
            ),
            "candidate_buying_power": (
                inputs.candidate_buying_power_impact_per_share_usd,
                economics.candidate_buying_power_impact_per_share_usd,
            ),
        }
        changed.extend(
            name for name, (actual, expected) in numeric.items()
            if float(actual) != float(expected)
        )
        if (
            changed
            or request.policy.policy_sha256
            != admission.policy_spec.policy.policy_sha256
            or request.opportunity_key is None
            or request.opportunity_key.to_payload()
            != self.adaptive_opportunity.to_payload()
            or request.opportunity_key.key_sha256
            != self.detector_audit.detector_opportunity_key_sha256
            or inputs.generation != proof.generation
            or inputs.as_of < intent.decision_at
            or inputs.as_of > final_boundary_available_at
            or snapshot.execution_family != route.execution_family
            or snapshot.broker_environment != "paper"
            or snapshot.venue != "alpaca"
            or snapshot.equity_usd
            != admission.broker_account_facts.equity_usd
            or snapshot.buying_power_usd
            != admission.broker_account_facts.buying_power_usd
            or snapshot.broker_day_change_usd
            != admission.broker_account_facts.broker_day_change_usd
        ):
            suffix = "" if not changed else ":" + ",".join(sorted(set(changed)))
            raise CaptureContractError(
                "captured PAPER first-dip locked adaptive request mismatch" + suffix
            )

    def _final_authority(
        self,
        *,
        adaptive_request: object,
        detector_policy: FirstDipTapePolicy,
        final_boundary_available_at: datetime,
    ) -> object:
        if type(adaptive_request) is not AdaptiveRiskReservationRequest:
            raise CaptureContractError(
                "captured PAPER first-dip final request is not typed"
            )
        try:
            request = load_adaptive_risk_reservation_request(
                adaptive_request.to_payload()
            )
        except Exception as exc:
            raise CaptureContractError(
                "captured PAPER first-dip final request is invalid"
            ) from exc
        boundary = _utc(
            final_boundary_available_at,
            "captured PAPER first-dip final boundary",
        )
        if (
            type(detector_policy) is not FirstDipTapePolicy
            or detector_policy.to_dict() != self.detector_policy.to_dict()
            or detector_policy.policy_sha256 != self.detector_policy.policy_sha256
        ):
            raise CaptureContractError(
                "captured PAPER first-dip final policy changed"
            )
        self._verify_locked_adaptive_request(
            request,
            final_boundary_available_at=boundary,
        )
        captured = self.final_read_provider(
            adaptive_request=request,
            detector_policy=detector_policy,
            final_boundary_available_at=boundary,
        )
        if not isinstance(captured, FirstDipFinalCaptureRead):
            raise CaptureContractError(
                "captured PAPER first-dip final read is untyped"
            )
        matching = tuple(
            row
            for row in captured.captured_reads
            if row.receipt is not None
            and row.receipt.read_id == captured.first_dip_tape_read_id
        )
        if len(matching) != 1 or matching[0].receipt is None:
            raise CaptureContractError(
                "captured PAPER first-dip final receipt is missing"
            )
        if matching[0].receipt.returned_at > boundary:
            raise CaptureContractError(
                "captured PAPER first-dip final read is from the future"
            )
        proof = self.coordinator.attest_first_dip_pre_reservation_inputs(
            adaptive_request=request,
            dependency_profile=captured.dependency_profile,
            captured_reads=captured.captured_reads,
            first_dip_tape_read_id=captured.first_dip_tape_read_id,
        )
        resolved_boundary = _utc(
            self.coordinator._observed_now(),
            "captured PAPER first-dip resolved boundary",
        )
        if resolved_boundary < boundary:
            raise CaptureContractError(
                "captured PAPER first-dip final clock moved backwards"
            )
        authority = self.coordinator.prepare_captured_first_dip_tape_authority(
            attestation=proof,
            policy=detector_policy,
            purpose="pre_reservation",
            final_boundary_available_at=resolved_boundary,
        )
        frontier = self.coordinator.first_dip_final_capture_frontier(proof)
        inventory = _executed_inventory_from_reads(
            proof=proof,
            captured_reads=captured.captured_reads,
        )
        with self._scope_lock:
            if self._final_capture_frontier is not None:
                raise CaptureContractError(
                    "captured PAPER first-dip final frontier already exists"
                )
            self._final_capture_frontier = frontier
            self._final_input_attestation = proof
            self._final_captured_reads = tuple(captured.captured_reads)
            self._final_executed_read_inventory = inventory
        return _issue_first_dip_final_authority_handoff(
            authority=authority,
            final_boundary_available_at=resolved_boundary,
            source="captured_db_paper",
        )

    @contextmanager
    def final_scope(self):
        """Install only the locked-admission final authority provider."""

        with self._scope_lock:
            if self._final_scope_started:
                raise CaptureContractError(
                    "captured PAPER first-dip final scope is one-shot"
                )
            self._final_scope_started = True
        with _installed_captured_first_dip_final_authority_provider(
            self._final_authority
        ):
            yield self


_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class _TimedOneShotValue(Generic[_T]):
    value: _T
    expires_monotonic: float


class _BoundedOneShotStore(Generic[_T]):
    """Process-local bounded queue; expired/full state fails closed."""

    def __init__(
        self,
        *,
        max_entries: int,
        ttl_seconds: float,
        monotonic_clock: Callable[[], float],
    ) -> None:
        if (
            isinstance(max_entries, bool)
            or int(max_entries) <= 0
            or int(max_entries) > 65_536
        ):
            raise CaptureContractError("captured PAPER handoff size is invalid")
        ttl = float(ttl_seconds)
        if not math.isfinite(ttl) or ttl <= 0.0 or ttl > 86_400.0:
            raise CaptureContractError("captured PAPER handoff TTL is invalid")
        if not callable(monotonic_clock):
            raise CaptureContractError(
                "captured PAPER monotonic clock is invalid"
            )
        self._max_entries = int(max_entries)
        self._ttl_seconds = ttl
        self._monotonic_clock = monotonic_clock
        self._entries: OrderedDict[str, _TimedOneShotValue[_T]] = OrderedDict()
        self._leases: dict[str, str] = {}
        self._lock = threading.RLock()

    def _now(self) -> float:
        value = float(self._monotonic_clock())
        if not math.isfinite(value):
            raise CaptureContractError(
                "captured PAPER monotonic clock is non-finite"
            )
        return value

    def _purge_expired_locked(self, now: float) -> None:
        expired = tuple(
            key
            for key, row in self._entries.items()
            if row.expires_monotonic <= now and key not in self._leases
        )
        for key in expired:
            self._entries.pop(key, None)
            self._leases.pop(key, None)

    def stage(self, key: str, value: _T) -> None:
        normalized = str(key or "").strip()
        if (
            len(normalized) != 64
            or any(ch not in "0123456789abcdef" for ch in normalized)
        ):
            raise CaptureContractError("captured PAPER handoff key is invalid")
        now = self._now()
        with self._lock:
            self._purge_expired_locked(now)
            if normalized in self._entries:
                raise CaptureContractError(
                    "captured PAPER handoff duplicate key"
                )
            if len(self._entries) >= self._max_entries:
                raise CaptureContractError(
                    "captured PAPER handoff capacity unavailable"
                )
            self._entries[normalized] = _TimedOneShotValue(
                value=value,
                expires_monotonic=now + self._ttl_seconds,
            )

    def stage_or_match(
        self,
        key: str,
        value: _T,
        *,
        matches: Callable[[_T, _T], bool],
    ) -> bool:
        """Stage once or accept the exact same immutable retry material."""

        if not callable(matches):
            raise CaptureContractError(
                "captured PAPER handoff matcher is invalid"
            )
        normalized = str(key or "").strip()
        if (
            len(normalized) != 64
            or any(ch not in "0123456789abcdef" for ch in normalized)
        ):
            raise CaptureContractError("captured PAPER handoff key is invalid")
        now = self._now()
        with self._lock:
            self._purge_expired_locked(now)
            row = self._entries.get(normalized)
            if row is not None:
                if matches(row.value, value) is not True:
                    raise CaptureContractError(
                        "captured PAPER handoff duplicate key mismatch"
                    )
                return False
            if len(self._entries) >= self._max_entries:
                raise CaptureContractError(
                    "captured PAPER handoff capacity unavailable"
                )
            self._entries[normalized] = _TimedOneShotValue(
                value=value,
                expires_monotonic=now + self._ttl_seconds,
            )
            return True

    def lease(self, key: str) -> tuple[str, _T]:
        """Acquire one process-local attempt without destroying its material."""

        normalized = str(key or "").strip()
        now = self._now()
        with self._lock:
            row = self._entries.get(normalized)
            if row is None:
                self._purge_expired_locked(now)
                raise CaptureContractError(
                    "captured PAPER handoff material unavailable"
                )
            if row.expires_monotonic <= now:
                self._entries.pop(normalized, None)
                self._leases.pop(normalized, None)
                raise CaptureContractError(
                    "captured PAPER handoff material expired"
                )
            if normalized in self._leases:
                raise CaptureContractError(
                    "captured PAPER handoff material already in flight"
                )
            token = str(uuid.uuid4())
            self._leases[normalized] = token
            return token, row.value

    def release(self, key: str, lease_token: str) -> None:
        """Release only the exact failed attempt; retain retry material."""

        normalized = str(key or "").strip()
        token = str(lease_token or "").strip()
        with self._lock:
            if self._leases.get(normalized) != token:
                raise CaptureContractError(
                    "captured PAPER handoff lease mismatch"
                )
            self._leases.pop(normalized, None)

    def ack(self, key: str, lease_token: str) -> _T:
        """Destroy material only after durable commit/readback is confirmed."""

        normalized = str(key or "").strip()
        token = str(lease_token or "").strip()
        with self._lock:
            if self._leases.get(normalized) != token:
                raise CaptureContractError(
                    "captured PAPER handoff lease mismatch"
                )
            row = self._entries.pop(normalized, None)
            self._leases.pop(normalized, None)
            if row is None:
                raise CaptureContractError(
                    "captured PAPER handoff material unavailable"
                )
            return row.value

    def available_keys(self, *, limit: int) -> tuple[str, ...]:
        if isinstance(limit, bool) or int(limit) <= 0:
            raise CaptureContractError(
                "captured PAPER handoff retry limit is invalid"
            )
        now = self._now()
        with self._lock:
            self._purge_expired_locked(now)
            return tuple(
                key
                for key in self._entries
                if key not in self._leases
            )[: int(limit)]

    def any_match(self, predicate: Callable[[_T], bool]) -> bool:
        if not callable(predicate):
            raise CaptureContractError(
                "captured PAPER handoff predicate is invalid"
            )
        now = self._now()
        with self._lock:
            self._purge_expired_locked(now)
            return any(predicate(row.value) for row in self._entries.values())

    def consume(self, key: str) -> _T:
        normalized = str(key or "").strip()
        now = self._now()
        with self._lock:
            row = self._entries.get(normalized)
            if row is None:
                self._purge_expired_locked(now)
                raise CaptureContractError(
                    "captured PAPER handoff material unavailable"
                )
            if row.expires_monotonic <= now:
                self._entries.pop(normalized, None)
                raise CaptureContractError(
                    "captured PAPER handoff material expired"
                )
            # Pop while holding the lock.  A retry, concurrent callback, or
            # downstream exception can never consume the same capability twice.
            self._entries.pop(normalized)
            return row.value

    def health(self) -> Mapping[str, Any]:
        now = self._now()
        with self._lock:
            self._purge_expired_locked(now)
            return MappingProxyType(
                {
                    "pending": len(self._entries),
                    "in_flight": len(self._leases),
                    "max_entries": self._max_entries,
                    "ttl_seconds": self._ttl_seconds,
                }
            )


class IqfeedCaptureHost:
    """Own exact bridge bindings and hot L2 activation without launching I/O."""

    def __init__(
        self,
        composition: IqfeedCaptureIngressComposition,
        *,
        trade_bridge: Any = iqfeed_trade_bridge,
        depth_bridge: Any = iqfeed_depth_bridge,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not isinstance(composition, IqfeedCaptureIngressComposition):
            raise CaptureContractError("IQFeed host composition is malformed")
        if composition.state is not IqfeedIngressCompositionState.PREPARED:
            raise CaptureContractError("IQFeed host requires a prepared composition")
        if not callable(wall_clock):
            raise CaptureContractError("IQFeed host wall clock is malformed")
        for module, role in (
            (trade_bridge, "iqfeed_trade_bridge"),
            (depth_bridge, "iqfeed_depth_bridge"),
        ):
            for method in (
                "bind_capture_handoff",
                "unbind_capture_handoff",
            ):
                if not callable(getattr(module, method, None)):
                    raise CaptureContractError(f"{role} lacks {method}")
        if not callable(getattr(depth_bridge, "activate_capture_symbol", None)) or not callable(
            getattr(depth_bridge, "deactivate_capture_symbol", None)
        ):
            raise CaptureContractError("IQFeed depth bridge lacks hot-symbol lifecycle")
        preflight = composition.preflight
        self._trade_source = _source_identity(
            trade_bridge,
            expected_path=preflight.source_paths["iqfeed_trade_bridge"],
            expected_sha256=preflight.source_hashes["iqfeed_trade_bridge"],
            role="iqfeed_trade_bridge",
        )
        self._depth_source = _source_identity(
            depth_bridge,
            expected_path=preflight.source_paths["iqfeed_depth_bridge"],
            expected_sha256=preflight.source_hashes["iqfeed_depth_bridge"],
            role="iqfeed_depth_bridge",
        )
        host_path = Path(__file__).resolve()
        expected_host_path = preflight.source_paths["iqfeed_capture_host"].resolve()
        if host_path != expected_host_path:
            raise CaptureContractError("loaded IQFeed host module path escaped preflight")
        self._host_source_sha256 = _stable_source_sha256(
            host_path,
            role="iqfeed_capture_host",
        )
        if self._host_source_sha256 != preflight.source_hashes["iqfeed_capture_host"]:
            raise CaptureContractError("loaded IQFeed host module source hash escaped preflight")
        self.composition = composition
        self.trade_bridge = trade_bridge
        self.depth_bridge = depth_bridge
        self._wall_clock = wall_clock
        self._state = IqfeedCaptureHostState.PREPARED
        self._receipt: IqfeedCaptureHostBindingReceipt | None = None
        self._trade_bound = False
        self._depth_bound = False
        self._captured_paper_runner_symbols: set[str] = set()
        self._captured_paper_admission_symbols: set[str] = set()
        self._macro_feature_caches: dict[str, dict] = {}
        self._provider_supervisor: IqfeedProviderLoopSupervisor | None = None
        self._provider_join_timeout_seconds = 20.0
        self._lock = threading.RLock()

    @property
    def state(self) -> IqfeedCaptureHostState:
        with self._lock:
            return self._state

    def bind(self) -> IqfeedCaptureHostBindingReceipt:
        """Start only local drains, then bind both bridge APIs atomically."""

        with self._lock:
            if self._state is not IqfeedCaptureHostState.PREPARED:
                raise CaptureContractError("IQFeed host binding is one-shot")
            try:
                self.composition.start_ingress()
                self.trade_bridge.bind_capture_handoff(self.composition.l1_handoff)
                self._trade_bound = True
                self.depth_bridge.bind_capture_handoff(self.composition.l2_handoff)
                self._depth_bound = True
                bound_at = _utc(self._wall_clock(), "IQFeed host binding clock")
                receipt = IqfeedCaptureHostBindingReceipt(
                    process_id=os.getpid(),
                    process_instance_id=(
                        self.composition.preflight.startup_process_instance_id
                    ),
                    bound_at=bound_at,
                    python_executable=sys.executable,
                    composition_sha256=(
                        self.composition.provenance.composition_sha256
                    ),
                    resource_binding_sha256=(
                        self.composition.binding.binding_sha256
                    ),
                    manifest_sha256=self.composition.preflight.manifest_sha256,
                    host_source_sha256=self._host_source_sha256,
                    trade_bridge=self._trade_source,
                    depth_bridge=self._depth_source,
                )
            except BaseException as exc:
                rollback_failures: list[BaseException] = []
                if self._depth_bound:
                    try:
                        self.depth_bridge.unbind_capture_handoff(
                            self.composition.l2_handoff
                        )
                        self._depth_bound = False
                    except BaseException as rollback_exc:
                        rollback_failures.append(rollback_exc)
                if self._trade_bound:
                    try:
                        self.trade_bridge.unbind_capture_handoff(
                            self.composition.l1_handoff
                        )
                        self._trade_bound = False
                    except BaseException as rollback_exc:
                        rollback_failures.append(rollback_exc)
                try:
                    self.composition.close()
                except BaseException as rollback_exc:
                    rollback_failures.append(rollback_exc)
                self._state = IqfeedCaptureHostState.FAILED
                detail = " with rollback failure" if rollback_failures else ""
                raise CaptureContractError(
                    "IQFeed host binding failed atomically" + detail
                ) from exc
            self._receipt = receipt
            self._state = IqfeedCaptureHostState.BOUND
            return receipt

    def admit_hot_symbol(
        self,
        symbol: str,
        *,
        required_l1_stream: CaptureStream = CaptureStream.NBBO_QUOTE,
    ) -> IqfeedCaptureHostAdmission:
        """Admit the capture run first, then request its L2 checkpoint/deltas."""

        with self._lock:
            if self._state is not IqfeedCaptureHostState.BOUND:
                raise CaptureContractError("IQFeed host is not bound")
            if required_l1_stream not in {
                CaptureStream.IQFEED_PRINT,
                CaptureStream.NBBO_QUOTE,
            }:
                raise CaptureContractError(
                    "IQFeed host hot admission requires an L1 pretrigger stream"
                )
            admission = self.composition.service.admit_hot_symbol(
                symbol,
                required_stream=required_l1_stream,
            )
            if not admission.capture_ready:
                return IqfeedCaptureHostAdmission(
                    symbol=admission.symbol,
                    capture_ready=False,
                    l2_checkpoint_queued=False,
                    rejected_reason="l1_pretrigger_promotion_unavailable",
                )
            try:
                checkpointed = bool(
                    self.depth_bridge.activate_capture_symbol(
                        admission.symbol,
                        available_at=_utc(
                            self._wall_clock(), "IQFeed L2 hot activation clock"
                        ),
                    )
                )
            except BaseException as exc:
                self.composition.service.abort_symbol(
                    admission.symbol,
                    reason="iqfeed_l2_activation_exception",
                )
                raise CaptureContractError(
                    "IQFeed L2 hot activation failed; capture run aborted"
                ) from exc
            return IqfeedCaptureHostAdmission(
                symbol=admission.symbol,
                capture_ready=True,
                l2_checkpoint_queued=checkpointed,
                rejected_reason=(
                    None
                    if checkpointed
                    else "iqfeed_l2_checkpoint_coverage_unavailable"
                ),
            )

    def abort_hot_symbol(self, symbol: str, *, reason: str) -> Any:
        with self._lock:
            if self._state is not IqfeedCaptureHostState.BOUND:
                raise CaptureContractError("IQFeed host is not bound")
            self.depth_bridge.deactivate_capture_symbol(symbol)
            self._macro_feature_caches.pop(
                str(symbol or "").strip().upper(), None
            )
            return self.composition.service.abort_symbol(symbol, reason=reason)

    def captured_paper_config_evidence_for(
        self, symbol: str
    ) -> Mapping[str, Any]:
        """Return the exact active per-symbol capture configuration."""

        with self._lock:
            if self._state is not IqfeedCaptureHostState.BOUND:
                raise CaptureContractError("IQFeed host is not bound")
            return self.composition.service.config_evidence_for(symbol)

    def captured_paper_config_sha256_for(self, symbol: str) -> str:
        """Resolve the final config digest used by capture proofs/routes."""

        config = self.captured_paper_config_evidence_for(symbol)
        return sha256_json(config)

    def tick_captured_alpaca_paper_observation_session(
        self,
        db: Any,
        *,
        dispatch_request: CapturedPaperDispatchRequest,
        prepared: PreparedCapturedPaperObservation,
    ) -> IqfeedCapturedPaperTickResult:
        """Run one capture-only WATCHING/QUEUED tick with no admission object."""

        if type(dispatch_request) is not CapturedPaperDispatchRequest:
            raise CaptureContractError(
                "captured PAPER observation dispatch request is not typed"
            )
        dispatch_request.verify()
        if type(prepared) is not PreparedCapturedPaperObservation:
            raise CaptureContractError(
                "captured PAPER observation material is not typed"
            )
        context = prepared.observation_context
        context.verify()
        if (
            context.dispatch_request.provenance_sha256
            != dispatch_request.provenance_sha256
            or context.dispatch_request.route_token.route_token_sha256
            != dispatch_request.route_token.route_token_sha256
        ):
            raise CaptureContractError(
                "captured PAPER observation dispatch route mismatch"
            )
        normalized = dispatch_request.symbol
        with self._lock:
            if self._state is not IqfeedCaptureHostState.BOUND:
                raise CaptureContractError("IQFeed host is not bound")
            if normalized in self._captured_paper_runner_symbols:
                raise CaptureContractError(
                    "captured PAPER runner already owns this symbol"
                )
            coordinator = self.composition.service.coordinator_for(normalized)
            if coordinator.certification_symbol != normalized:
                raise CaptureContractError(
                    "captured PAPER coordinator symbol mismatch"
                )
            self._captured_paper_runner_symbols.add(normalized)
            macro_cache = self._macro_feature_caches.setdefault(normalized, {})
        try:
            decision_at = _utc(
                context.decision_at,
                "captured PAPER observation decision clock",
            )
            proof = verify_active_capture_input_attestation(
                prepared.active_input_attestation
            )
            if proof.decision_id != context.observation_decision_id:
                raise CaptureContractError(
                    "captured PAPER observation proof identity mismatch"
                )
            first_dip_scope = (
                _IqfeedCapturedPaperObservationFirstDipScope(
                    coordinator=coordinator,
                    prepared=prepared,
                )
                if prepared.first_dip_tape_read_id is not None
                else None
            )
            from app.services.trading.momentum_neural.universe import (
                EQUITY_ROSS_SMALLCAP,
            )

            live_profile = EQUITY_ROSS_SMALLCAP
            scanner_profile = CaptureScannerProfile(
                profile_id=live_profile.profile_id,
                asset_class=live_profile.asset_class,
                price_min=live_profile.price_min,
                price_max=live_profile.price_max,
                min_dollar_volume=live_profile.min_dollar_volume,
                min_change_pct=live_profile.min_change_pct,
                snapshot_max_age_seconds=live_profile.snapshot_max_age_seconds,
            )
            scanner_bridge = LiveScannerSnapshotCaptureBridge(
                coordinator=coordinator,
                decision_id=context.observation_decision_id,
                profile=scanner_profile,
                include_otc=False,
            )
            ohlcv_bridge = LiveOhlcvCaptureBridge(
                coordinator=coordinator,
                decision_id=context.observation_decision_id,
                macro_cache=macro_cache,
            )
            microstructure_bridge = LiveMicrostructureCaptureBridge(
                coordinator=coordinator,
                decision_id=context.observation_decision_id,
            )
            with (
                momentum_live_runner.replay_clock(decision_at),
                scanner_bridge.install(),
                ohlcv_bridge.install(),
                microstructure_bridge.install(),
                prepared.bound_input_scope.install(proof),
                (
                    first_dip_scope.install()
                    if first_dip_scope is not None
                    else nullcontext()
                ),
                install_captured_paper_observation_context(context),
            ):
                result = momentum_live_runner.tick_live_session(
                    db,
                    dispatch_request.session_id,
                    adapter_factory=prepared.adapter_factory,
                )
            if not isinstance(result, Mapping) or type(result) is CapturedPaperPostCommitRequest:
                raise CaptureContractError(
                    "captured PAPER observation escaped into admission"
                )
            return IqfeedCapturedPaperTickResult(
                decision_at=decision_at,
                fsm_result=result,
                first_dip_final_capture_frontier=None,
                scanner_snapshot_read_ids=tuple(
                    captured.receipt.read_id
                    for captured in scanner_bridge.captured_reads
                    if captured.receipt is not None
                ),
                ohlcv_read_ids=tuple(
                    captured.receipt.read_id
                    for captured in ohlcv_bridge.captured_reads
                    if captured.receipt is not None
                ),
                microstructure_read_ids=tuple(
                    captured.receipt.read_id
                    for captured in microstructure_bridge.captured_reads
                    if captured.receipt is not None
                ),
            )
        finally:
            with self._lock:
                self._captured_paper_runner_symbols.discard(normalized)

    def tick_captured_alpaca_paper_session(
        self,
        db: Any,
        *,
        dispatch_request: CapturedPaperDispatchRequest,
        decision_material: IqfeedCapturedPaperDecisionMaterial,
        adapter_factory: Callable[[], object],
    ) -> IqfeedCapturedPaperTickResult:
        """Run one real FSM tick inside the exact captured-paper capabilities.

        This is composition, not activation: no launcher calls it and the host
        CLI remains validate-only.  A future recertified application caller
        must explicitly stage the exact selection/admission material and broker
        adapter factory.  Missing or foreign material fails before
        ``tick_live_session``.  The host never falls back to current DB/provider
        reads to manufacture a capability.
        """

        if type(dispatch_request) is not CapturedPaperDispatchRequest:
            raise CaptureContractError(
                "captured PAPER dispatch request is not typed"
            )
        dispatch_request.verify()
        if type(decision_material) is not IqfeedCapturedPaperDecisionMaterial:
            raise CaptureContractError(
                "captured PAPER decision material is not typed"
            )
        decision_material.verify_for_dispatch(dispatch_request)
        normalized = dispatch_request.symbol
        if not callable(adapter_factory):
            raise CaptureContractError("captured PAPER adapter factory is invalid")
        with self._lock:
            if self._state is not IqfeedCaptureHostState.BOUND:
                raise CaptureContractError("IQFeed host is not bound")
            if normalized in self._captured_paper_runner_symbols:
                raise CaptureContractError(
                    "captured PAPER runner already owns this symbol"
                )
            coordinator = self.composition.service.coordinator_for(normalized)
            if coordinator.certification_symbol != normalized:
                raise CaptureContractError(
                    "captured PAPER coordinator symbol mismatch"
                )
            self._captured_paper_runner_symbols.add(normalized)
            macro_cache = self._macro_feature_caches.setdefault(normalized, {})
        try:
            # One captured FSM invocation has one causal decision clock.  LIVE
            # wall time is sampled once and then frozen only for this call so
            # every query receipt can be reproduced under ReplayV3's identical
            # clock seam.  The returned aware timestamp is the checkpoint clock
            # a future activation caller must persist.
            decision_at = _utc(
                decision_material.selection_context.draft.intent.decision_at,
                "captured PAPER decision clock",
            )
            proof = verify_active_capture_input_attestation(
                decision_material.admission_inputs.active_input_attestation
            )
            first_dip_scopes: _IqfeedCapturedPaperFirstDipScopes | None = None
            if decision_material.selection_context.draft.intent.setup_family == "first_dip_reclaim":
                first_dip_scopes = _IqfeedCapturedPaperFirstDipScopes(
                    coordinator=coordinator,
                    decision_material=decision_material,
                )
            from app.services.trading.momentum_neural.universe import (
                EQUITY_ROSS_SMALLCAP,
            )

            live_profile = EQUITY_ROSS_SMALLCAP
            scanner_profile = CaptureScannerProfile(
                profile_id=live_profile.profile_id,
                asset_class=live_profile.asset_class,
                price_min=live_profile.price_min,
                price_max=live_profile.price_max,
                min_dollar_volume=live_profile.min_dollar_volume,
                min_change_pct=live_profile.min_change_pct,
                snapshot_max_age_seconds=live_profile.snapshot_max_age_seconds,
            )
            scanner_decision_id = str(
                getattr(proof, "decision_id", "") or ""
            ).strip()
            if not scanner_decision_id:
                raise CaptureContractError(
                    "captured PAPER detector decision id is missing"
                )
            scanner_bridge = LiveScannerSnapshotCaptureBridge(
                coordinator=coordinator,
                decision_id=scanner_decision_id,
                profile=scanner_profile,
                include_otc=False,
            )
            ohlcv_bridge = LiveOhlcvCaptureBridge(
                coordinator=coordinator,
                decision_id=scanner_decision_id,
                macro_cache=macro_cache,
            )
            microstructure_bridge = LiveMicrostructureCaptureBridge(
                coordinator=coordinator,
                decision_id=scanner_decision_id,
            )
            with (
                momentum_live_runner.replay_clock(decision_at),
                scanner_bridge.install(),
                ohlcv_bridge.install(),
                microstructure_bridge.install(),
                (
                    decision_material.bound_input_scope.install(proof)
                    if decision_material.bound_input_scope is not None
                    else nullcontext()
                ),
                (
                    first_dip_scopes.detector_scope()
                    if first_dip_scopes is not None
                    else nullcontext()
                ),
                install_captured_paper_selection_context(
                    decision_material.selection_context
                ),
            ):
                result = momentum_live_runner.tick_live_session(
                    db,
                    dispatch_request.session_id,
                    adapter_factory=adapter_factory,
                )
            if not (
                type(result) is CapturedPaperPostCommitRequest
                or isinstance(result, Mapping)
            ):
                raise CaptureContractError(
                    "captured PAPER FSM tick returned a malformed result"
                )
            executed_reads = (
                *decision_material.predecision_captured_reads,
                *scanner_bridge.captured_reads,
                *ohlcv_bridge.captured_reads,
                *microstructure_bridge.captured_reads,
            )
            executed_inventory = _executed_inventory_from_reads(
                proof=proof,
                captured_reads=executed_reads,
            )
            return IqfeedCapturedPaperTickResult(
                decision_at=decision_at,
                fsm_result=result,
                first_dip_final_capture_frontier=(
                    None
                    if first_dip_scopes is None
                    else first_dip_scopes.final_capture_frontier
                ),
                scanner_snapshot_read_ids=tuple(
                    captured.receipt.read_id
                    for captured in scanner_bridge.captured_reads
                    if captured.receipt is not None
                ),
                ohlcv_read_ids=tuple(
                    captured.receipt.read_id
                    for captured in ohlcv_bridge.captured_reads
                    if captured.receipt is not None
                ),
                microstructure_read_ids=tuple(
                    captured.receipt.read_id
                    for captured in microstructure_bridge.captured_reads
                    if captured.receipt is not None
                ),
                captured_reads=executed_reads,
                executed_read_inventory=executed_inventory,
            )
        finally:
            with self._lock:
                self._captured_paper_runner_symbols.discard(normalized)

    @contextmanager
    def captured_paper_post_commit_scope(
        self,
        decision_material: IqfeedCapturedPaperDecisionMaterial,
    ):
        """Install a fresh final-authority scope for admission only.

        Phase one cannot leave ContextVar providers open across its transaction
        commit.  First-dip admission therefore creates a second one-shot bridge
        from the same hash-bound material after the dispatcher has committed.
        The scope retains no caller Session and never invokes broker transport.
        """

        if type(decision_material) is not IqfeedCapturedPaperDecisionMaterial:
            raise CaptureContractError(
                "captured PAPER admission material is not typed"
            )
        decision_material.verify()
        symbol = decision_material.selection_context.dispatch_request.symbol
        with self._lock:
            if self._state is not IqfeedCaptureHostState.BOUND:
                raise CaptureContractError("IQFeed host is not bound")
            if symbol in self._captured_paper_admission_symbols:
                raise CaptureContractError(
                    "captured PAPER admission already owns this symbol"
                )
            coordinator = self.composition.service.coordinator_for(symbol)
            if coordinator.certification_symbol != symbol:
                raise CaptureContractError(
                    "captured PAPER admission coordinator symbol mismatch"
                )
            self._captured_paper_admission_symbols.add(symbol)
        first_dip_scopes: _IqfeedCapturedPaperFirstDipScopes | None = None
        try:
            intent = decision_material.selection_context.draft.intent
            if intent.setup_family == "first_dip_reclaim":
                first_dip_scopes = _IqfeedCapturedPaperFirstDipScopes(
                    coordinator=coordinator,
                    decision_material=decision_material,
                )
            with (
                first_dip_scopes.final_scope()
                if first_dip_scopes is not None
                else nullcontext()
            ):
                yield first_dip_scopes
        finally:
            with self._lock:
                self._captured_paper_admission_symbols.discard(symbol)

    def _close_bound_capture_resources(self) -> Mapping[str, Any]:
        """Unbind only after provider quiescence, then drain the composition."""

        failures: list[BaseException] = []
        if self._depth_bound:
            try:
                self.depth_bridge.unbind_capture_handoff(
                    self.composition.l2_handoff
                )
                self._depth_bound = False
            except BaseException as exc:
                failures.append(exc)
        if self._trade_bound:
            try:
                self.trade_bridge.unbind_capture_handoff(
                    self.composition.l1_handoff
                )
                self._trade_bound = False
            except BaseException as exc:
                failures.append(exc)
        if failures:
            raise CaptureContractError(
                "IQFeed host bridge unbinding failed closed"
            ) from failures[0]
        return self.composition.close()

    def start_provider_loops(
        self,
        *,
        readiness_timeout_seconds: float = 15.0,
        join_timeout_seconds: float = 20.0,
        reconnect_wait_seconds: float = 10.0,
        trade_forced_symbols: Sequence[str] = (),
        depth_forced_symbols: Sequence[str] = (),
    ) -> Mapping[str, Any]:
        """Bind first, then start and jointly admit the L1/L2 provider lanes.

        This method is deliberately not called by the validate-only CLI.  A
        future SHA-bound launcher may invoke it only after its external
        activation gates pass.  Startup failure stops and joins both lanes
        before either capture handoff is unbound or its queue is drained.
        """

        timeout_values = (
            (float(readiness_timeout_seconds), "readiness timeout"),
            (float(join_timeout_seconds), "join timeout"),
            (float(reconnect_wait_seconds), "reconnect wait"),
        )
        for value, label in timeout_values:
            if not math.isfinite(value) or value <= 0:
                raise CaptureContractError(
                    f"IQFeed provider-loop {label} must be positive"
                )
        supervisor = IqfeedProviderLoopSupervisor(
            trade_bridge=self.trade_bridge,
            depth_bridge=self.depth_bridge,
        )
        with self._lock:
            if self._provider_supervisor is not None:
                raise CaptureContractError(
                    "IQFeed provider-loop supervision is one-shot"
                )
            if self._state is IqfeedCaptureHostState.PREPARED:
                receipt = self.bind()
            elif self._state is IqfeedCaptureHostState.BOUND:
                if self._receipt is None:
                    raise CaptureContractError(
                        "IQFeed host binding receipt is unavailable"
                    )
                receipt = self._receipt
            else:
                raise CaptureContractError(
                    "IQFeed provider loops require a prepared or bound host"
                )
            self._provider_supervisor = supervisor
            self._provider_join_timeout_seconds = float(join_timeout_seconds)
            try:
                provider_health = supervisor.start(
                    readiness_timeout_seconds=readiness_timeout_seconds,
                    join_timeout_seconds=join_timeout_seconds,
                    reconnect_wait_seconds=reconnect_wait_seconds,
                    trade_forced_symbols=trade_forced_symbols,
                    depth_forced_symbols=depth_forced_symbols,
                )
            except BaseException as exc:
                cleanup_failures: list[BaseException] = []
                try:
                    supervisor.close(
                        join_timeout_seconds=join_timeout_seconds
                    )
                except BaseException as cleanup_exc:
                    cleanup_failures.append(cleanup_exc)
                if not cleanup_failures:
                    try:
                        self._close_bound_capture_resources()
                    except BaseException as cleanup_exc:
                        cleanup_failures.append(cleanup_exc)
                self._state = IqfeedCaptureHostState.FAILED
                detail = (
                    " with teardown failure" if cleanup_failures else ""
                )
                raise CaptureContractError(
                    "IQFeed provider-loop startup failed closed" + detail
                ) from exc
            return MappingProxyType(
                {
                    "binding_receipt": receipt.to_dict(),
                    "binding_receipt_sha256": receipt.receipt_sha256,
                    "provider_loop_supervisor": dict(provider_health),
                }
            )

    def close(self) -> Mapping[str, Any]:
        with self._lock:
            if self._state is IqfeedCaptureHostState.CLOSED:
                return self.health()
            if self._state is IqfeedCaptureHostState.FAILED:
                raise CaptureContractError("failed IQFeed host cannot be cleanly closed")
            service_health = self.composition.service.health()
            if (
                service_health["pending_symbols"]
                or service_health["running_symbols"]
                or self._captured_paper_runner_symbols
                or self._captured_paper_admission_symbols
            ):
                raise CaptureContractError(
                    "cannot close IQFeed host with active capture runs"
                )
            if self._provider_supervisor is not None:
                try:
                    self._provider_supervisor.close(
                        join_timeout_seconds=self._provider_join_timeout_seconds
                    )
                except BaseException as exc:
                    self._state = IqfeedCaptureHostState.FAILED
                    raise CaptureContractError(
                        "IQFeed provider lanes did not quiesce before unbind"
                    ) from exc
            try:
                result = self._close_bound_capture_resources()
            except BaseException:
                self._state = IqfeedCaptureHostState.FAILED
                raise
            self._state = IqfeedCaptureHostState.CLOSED
            return {**self.health(), "composition": result}

    def health(self) -> Mapping[str, Any]:
        with self._lock:
            provider_health = (
                None
                if self._provider_supervisor is None
                else dict(self._provider_supervisor.health())
            )
            provider_started = bool(
                provider_health and provider_health["ever_started"]
            )
            database_started = bool(
                provider_health
                and any(
                    lane["schema_verified"]
                    for lane in provider_health["lanes"].values()
                )
            )
            provider_sockets_started = bool(
                provider_health
                and provider_health["provider_sockets_started"]
            )
            return {
                "schema_version": _HOST_SCHEMA_VERSION,
                "state": self._state.value,
                "trade_bridge_bound": self._trade_bound,
                "depth_bridge_bound": self._depth_bound,
                "provider_sockets_started": provider_sockets_started,
                "database_or_broker_started": database_started,
                "database_started": database_started,
                "broker_started": False,
                "paper_live_execution_enabled": False,
                "activation_authorized": False,
                "provider_loop_activation_requested": provider_started,
                "provider_loop_cli_wired": False,
                "provider_loop_supervisor": provider_health,
                "captured_paper_runner_invocations_in_flight": tuple(
                    sorted(self._captured_paper_runner_symbols)
                ),
                "captured_paper_admissions_in_flight": tuple(
                    sorted(self._captured_paper_admission_symbols)
                ),
                "task_or_service_mutated": False,
                "binding_receipt": (
                    None if self._receipt is None else self._receipt.to_dict()
                ),
                "binding_receipt_sha256": (
                    None if self._receipt is None else self._receipt.receipt_sha256
                ),
                "composition": self.composition.health(),
            }


class IqfeedCapturedPaperRuntimeOwner:
    """Prepared dispatcher owner for the real captured PAPER runner path.

    Construction does not register the runtime, start provider loops, open a
    database connection, or contact Alpaca.  Production mode first builds a
    capture-only WATCHING/QUEUED capability or an exact candidate packet from
    durable state; manual staging exists only in the explicit test mode.  Only
    an exact typed candidate completion enters the post-commit handoff.
    """

    def __init__(
        self,
        *,
        host: IqfeedCaptureHost,
        adapter_factory: Callable[[], object],
        admission_bind: Engine,
        expected_account_id: str,
        code_build_sha256: str,
        config_sha256: str,
        capture_receipt_sha256: str,
        runtime_generation: str,
        first_dip_policy_mode: str,
        decision_max_entries: int,
        decision_ttl_seconds: float,
        admission_max_entries: int,
        admission_ttl_seconds: float,
        settings_projection_sha256: str | None = None,
        config_sha256_resolver: Callable[[str], str] | None = None,
        production_material_factory: (
            CapturedPaperProductionMaterialFactory | None
        ) = None,
        financial_breaker_issuer: object | None = None,
        financial_breaker_clock: Callable[[], datetime] | None = None,
        assert_service_fence_held: Callable[[], None] | None = None,
        allow_manual_staging: bool = False,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(host, IqfeedCaptureHost):
            raise CaptureContractError(
                "captured PAPER runtime host is malformed"
            )
        if not callable(adapter_factory):
            raise CaptureContractError(
                "captured PAPER runtime adapter factory is malformed"
            )
        if not isinstance(admission_bind, Engine):
            raise CaptureContractError(
                "captured PAPER admission PostgreSQL engine is unavailable"
            )
        if production_material_factory is not None and type(
            production_material_factory
        ) is not CapturedPaperProductionMaterialFactory:
            raise CaptureContractError(
                "captured PAPER production material factory is malformed"
            )
        if type(allow_manual_staging) is not bool:
            raise CaptureContractError(
                "captured PAPER manual staging policy is malformed"
            )
        if production_material_factory is None and not allow_manual_staging:
            raise CaptureContractError(
                "captured PAPER production material factory is unavailable"
            )
        if production_material_factory is not None and allow_manual_staging:
            raise CaptureContractError(
                "captured PAPER production runtime cannot enable manual staging"
            )
        if production_material_factory is not None and (
            settings_projection_sha256 is None
            or not callable(config_sha256_resolver)
        ):
            raise CaptureContractError(
                "captured PAPER production runtime lacks per-symbol config provenance"
            )
        breaker_clock = financial_breaker_clock or (
            lambda: datetime.now(timezone.utc)
        )
        if not callable(breaker_clock):
            raise CaptureContractError(
                "captured PAPER financial-breaker clock is malformed"
            )
        breaker_issuer = financial_breaker_issuer or (
            SqlAlchemyCapturedPaperFinancialBreakerIssuer(
                admission_bind,
                observation_clock=breaker_clock,
            )
        )
        if not callable(getattr(breaker_issuer, "issue_for_request", None)):
            raise CaptureContractError(
                "captured PAPER financial-breaker issuer is malformed"
            )
        if not callable(assert_service_fence_held):
            raise CaptureContractError(
                "captured PAPER service-fence capability is unavailable"
            )
        self._host = host
        self._adapter_factory = adapter_factory
        self._admission_bind = admission_bind
        self._financial_breaker_issuer = breaker_issuer
        self._financial_breaker_clock = breaker_clock
        self._assert_service_fence_held = assert_service_fence_held
        self._production_material_factory = production_material_factory
        self._allow_manual_staging = allow_manual_staging
        self._decisions = _BoundedOneShotStore[
            IqfeedCapturedPaperDecisionMaterial
        ](
            max_entries=decision_max_entries,
            ttl_seconds=decision_ttl_seconds,
            monotonic_clock=monotonic_clock,
        )
        self._admissions = _BoundedOneShotStore[
            IqfeedCapturedPaperAdmissionHandoff
        ](
            max_entries=admission_max_entries,
            ttl_seconds=admission_ttl_seconds,
            monotonic_clock=monotonic_clock,
        )
        self._runtime = CapturedPaperRuntime(
            handler=self,
            post_commit_handler=self.post_commit,
            expected_account_id=expected_account_id,
            code_build_sha256=code_build_sha256,
            config_sha256=config_sha256,
            capture_receipt_sha256=capture_receipt_sha256,
            runtime_generation=runtime_generation,
            first_dip_policy_mode=first_dip_policy_mode,
            settings_projection_sha256=settings_projection_sha256,
            config_sha256_resolver=config_sha256_resolver,
        )

    @property
    def runtime(self) -> CapturedPaperRuntime:
        """Return the inert registration value; registration is external."""

        return self._runtime

    def _verify_runtime_request(
        self,
        request: CapturedPaperDispatchRequest,
    ) -> None:
        if type(request) is not CapturedPaperDispatchRequest:
            raise CaptureContractError(
                "captured PAPER runtime request is not typed"
            )
        request.verify()
        exact = {
            "account_scope": self._runtime.account_scope,
            "expected_account_id": self._runtime.expected_account_id,
            "code_build_sha256": self._runtime.code_build_sha256,
            "config_sha256": self._runtime.resolve_config_sha256(
                request.symbol
            ),
            "capture_receipt_sha256": self._runtime.capture_receipt_sha256,
            "runtime_generation": self._runtime.runtime_generation,
            "first_dip_policy_mode": self._runtime.first_dip_policy_mode,
        }
        if any(getattr(request, name) != value for name, value in exact.items()):
            raise CaptureContractError(
                "captured PAPER runtime request provenance mismatch"
            )

    @staticmethod
    def _join_dispatch_linearization_after_material_read(
        db: Any,
        request: CapturedPaperDispatchRequest,
    ) -> AdaptiveRiskAccountLockIdentity:
        """Join the broker-dispatch lock domain after deliberate read rollbacks.

        The production material factory closes every candidate/observation read
        transaction before yielding its sealed provider context.  Acquiring the
        transactional account locks any earlier would be ineffective because
        that rollback releases them.  This seam therefore runs immediately
        before the host invokes ``tick_live_session`` and before that tick can
        lock or mutate a session row.  The caller-owned phase-one commit or
        rollback releases the locks after the complete FSM mutation.
        """

        if type(request) is not CapturedPaperDispatchRequest:
            raise CaptureContractError(
                "captured PAPER writer lock request is malformed"
            )
        request.verify()
        if request.account_scope != "alpaca:paper":
            raise CaptureContractError(
                "captured PAPER writer lock scope is not fake-money"
            )
        in_transaction = getattr(db, "in_transaction", None)
        begin = getattr(db, "begin", None)
        if not callable(in_transaction) or not callable(begin):
            raise CaptureContractError(
                "captured PAPER writer transaction is unavailable"
            )
        if not in_transaction():
            begin()
        if not in_transaction():
            raise CaptureContractError(
                "captured PAPER writer transaction did not start"
            )
        identity = acquire_adaptive_risk_account_locks(
            db,
            account_scope=request.account_scope,
        )
        if identity.account_scope != request.account_scope:
            raise CaptureContractError(
                "captured PAPER writer lock scope mismatch"
            )
        return identity

    def stage_decision(
        self,
        material: IqfeedCapturedPaperDecisionMaterial,
    ) -> str:
        """Stage one already-captured decision without reading current state."""

        if not self._allow_manual_staging or self._production_material_factory is not None:
            raise CaptureContractError(
                "captured PAPER manual staging is disabled"
            )

        if type(material) is not IqfeedCapturedPaperDecisionMaterial:
            raise CaptureContractError(
                "captured PAPER staged decision is not typed"
            )
        material.verify()
        request = material.selection_context.dispatch_request
        self._verify_runtime_request(request)
        material.verify_for_dispatch(request)
        self._decisions.stage(request.provenance_sha256, material)
        return material.material_sha256

    def _recover_pending_initial_generation_before_material(
        self,
        db: Any,
        request: CapturedPaperDispatchRequest,
    ) -> Mapping[str, Any] | None:
        """Release expired PENDING_OWNER authority before provider/material reads.

        Dispatcher validation has already limited an owner-less dedicated row to
        the exact typed PENDING_OWNER shape.  End its caller read transaction,
        then let the recovery module acquire the canonical account/claim/session
        locks in its own atomic transaction.  Final-owned sessions skip this
        seam entirely.
        """

        if self._production_material_factory is None:
            # Manual staging is a test-only path with a pre-bound final owner;
            # it cannot create the production PREOWNER/PENDING_OWNER generation.
            return None
        session = (
            db.query(TradingAutomationSession)
            .populate_existing()
            .filter(TradingAutomationSession.id == request.session_id)
            .one_or_none()
        )
        if session is None:
            raise CaptureContractError(
                "captured PAPER initial recovery session is unavailable"
            )
        snapshot = getattr(session, "risk_snapshot_json", None)
        snapshot = snapshot if type(snapshot) is dict else {}
        if snapshot.get("captured_paper_session_owner") is not None:
            return None
        if snapshot.get("captured_paper_session_pending_owner") is None:
            raise CaptureContractError(
                "captured PAPER owner-less session is not recoverable"
            )
        rollback = getattr(db, "rollback", None)
        if not callable(rollback):
            raise CaptureContractError(
                "captured PAPER initial recovery transaction is unavailable"
            )
        rollback()
        receipt = recover_captured_paper_initial_preowner(
            self._admission_bind,
            session_id=request.session_id,
            expected_account_id=request.expected_account_id,
            expected_runtime_generation=request.runtime_generation,
            expected_code_build_sha256=request.code_build_sha256,
            expected_config_sha256=request.config_sha256,
            expected_capture_receipt_sha256=request.capture_receipt_sha256,
            assert_service_fence_held=self._assert_service_fence_held,
        )
        if receipt.disposition == "pending_owner_recovered":
            return None
        if receipt.disposition != "expired_released":
            raise CaptureContractError(
                "captured PAPER initial recovery disposition is invalid"
            )
        return MappingProxyType(
            {
                "ok": True,
                "deferred": False,
                "released": True,
                "reason": "captured_paper_initial_authority_expired_released",
                "session_id": receipt.session_id,
                "initial_recovery_receipt_sha256": receipt.receipt_sha256,
                "refresh_session_inventory": True,
                "opportunity_consumed": False,
                "risk_reserved": False,
                "outbox_created": False,
                "order_posted": False,
                "broker_order_post_calls": 0,
            }
        )

    def __call__(
        self,
        db: Any,
        request: CapturedPaperDispatchRequest,
    ) -> Mapping[str, Any] | CapturedPaperPostCommitRequest:
        """Invoke one captured observation or exact candidate FSM tick."""

        self._verify_runtime_request(request)
        recovered = self._recover_pending_initial_generation_before_material(
            db,
            request,
        )
        if recovered is not None:
            return recovered
        if self._admissions.any_match(
            lambda handoff: (
                handoff.decision_material.selection_context.dispatch_request.session_id
                == request.session_id
                and handoff.decision_material.selection_context.dispatch_request.symbol
                == request.symbol
            )
        ):
            return MappingProxyType(
                {
                    "ok": True,
                    "deferred": True,
                    "reason": "captured_paper_post_commit_retry_pending",
                    "opportunity_consumed": False,
                    "risk_reserved": False,
                    "order_posted": False,
                    "broker_order_post_calls": 0,
                }
            )
        factory = self._production_material_factory
        try:
            if factory is not None and factory.material_kind(db, request) == "observation":
                with factory.observation_scope(db, request) as prepared_observation:
                    account_lock_identity = self._join_dispatch_linearization_after_material_read(
                        db,
                        request,
                    )
                    # A PENDING_OWNER is not a generic bare Alpaca session.  Its
                    # exact stored material/action claim and the process-lifetime
                    # service fence are re-proven under the canonical locks, and
                    # the final owner is installed in this outer transaction
                    # before any FSM mutation can run.
                    activate_captured_paper_session_owner_before_tick(
                        db,
                        request=request,
                        account_lock_identity=account_lock_identity,
                        assert_service_fence_held=(
                            self._assert_service_fence_held
                        ),
                    )
                    observation_tick = (
                        self._host.tick_captured_alpaca_paper_observation_session(
                            db,
                            dispatch_request=request,
                            prepared=prepared_observation,
                        )
                    )
                    if (
                        type(observation_tick) is not IqfeedCapturedPaperTickResult
                        or not isinstance(observation_tick.fsm_result, Mapping)
                        or type(observation_tick.fsm_result)
                        is CapturedPaperPostCommitRequest
                    ):
                        raise CaptureContractError(
                            "captured PAPER observation tick result is malformed"
                        )
                observation_result = observation_tick.fsm_result
                if not isinstance(observation_result, Mapping):
                    raise CaptureContractError(
                        "captured PAPER observation returned admission material"
                    )
                return observation_result
            if factory is None:
                material = self._decisions.consume(request.provenance_sha256)
                material.verify_for_dispatch(request)
                material_scope = nullcontext(
                    (material, self._adapter_factory)
                )
            else:
                material_scope = self._production_material_scope(
                    factory,
                    db,
                    request,
                )
            with material_scope as (material, adapter_factory):
                material.verify_for_dispatch(request)
                account_lock_identity = self._join_dispatch_linearization_after_material_read(
                    db,
                    request,
                )
                activate_captured_paper_session_owner_before_tick(
                    db,
                    request=request,
                    account_lock_identity=account_lock_identity,
                    assert_service_fence_held=(
                        self._assert_service_fence_held
                    ),
                )
                tick = self._host.tick_captured_alpaca_paper_session(
                    db,
                    dispatch_request=request,
                    decision_material=material,
                    adapter_factory=adapter_factory,
                )
                if type(tick) is not IqfeedCapturedPaperTickResult or not (
                    type(tick.fsm_result) is CapturedPaperPostCommitRequest
                    or isinstance(tick.fsm_result, Mapping)
                ):
                    raise CaptureContractError(
                        "captured PAPER decision tick result is malformed"
                    )
        except CapturedPaperProductionMaterialUnavailable as exc:
            return MappingProxyType(
                {
                    "ok": True,
                    "deferred": True,
                    "reason": exc.reason,
                    "opportunity_consumed": False,
                    "risk_reserved": False,
                    "order_posted": False,
                    "broker_order_post_calls": 0,
                }
            )
        result = tick.fsm_result
        if type(result) is CapturedPaperPostCommitRequest:
            result.verify()
            if (
                result.completion_sha256
                != material.selection_context.draft.completion_sha256
                or result.to_canonical_json()
                != material.admission_inputs.post_commit_request.to_canonical_json()
            ):
                raise CaptureContractError(
                    "captured PAPER FSM completion escaped staged material"
                )
            material.verify()
            if (
                type(tick.executed_read_inventory)
                is not ExecutedCaptureReadInventory
            ):
                raise CaptureContractError(
                    "captured PAPER completed tick inventory is unavailable"
                )
            handoff = IqfeedCapturedPaperAdmissionHandoff(
                decision_material=material,
                captured_reads=tuple(tick.captured_reads or ()),
                executed_read_inventory=tick.executed_read_inventory,
            )
            # This write shares the caller-owned phase-one transaction.  It
            # creates no reservation, claim, outbox, or transport authority;
            # it only makes a process crash before phase two auditable.
            record_captured_paper_phase_one_handoff(
                db,
                request=result,
                material_sha256=material.material_sha256,
                executed_read_inventory=handoff.executed_read_inventory,
                captured_reads=handoff.captured_reads,
                active_input_attestation=(
                    material.admission_inputs.active_input_attestation
                ),
                candidate_sha256=material.candidate_sha256,
                bound_input_scope_sha256=(
                    None
                    if material.bound_input_scope is None
                    else material.bound_input_scope.scope_sha256
                ),
            )
            self._admissions.stage_or_match(
                result.completion_sha256,
                handoff,
                matches=lambda left, right: (
                    left.decision_material.material_sha256
                    == right.decision_material.material_sha256
                    and left.executed_read_inventory.inventory_sha256
                    == right.executed_read_inventory.inventory_sha256
                    and left.decision_material.admission_inputs.post_commit_request.to_canonical_json()
                    == right.decision_material.admission_inputs.post_commit_request.to_canonical_json()
                ),
            )
            return result
        if not isinstance(result, Mapping):
            raise CaptureContractError(
                "captured PAPER FSM result escaped typed boundary"
            )
        return result

    @contextmanager
    def _production_material_scope(
        self,
        factory: CapturedPaperProductionMaterialFactory,
        db: Any,
        request: CapturedPaperDispatchRequest,
    ):
        """Build and retain one exact adapter/provider scope through the FSM."""

        with factory.decision_scope(db, request) as prepared:
            material = IqfeedCapturedPaperDecisionMaterial(
                selection_context=prepared.selection_context,
                admission_inputs=prepared.admission_inputs,
                predecision_captured_reads=(
                    prepared.predecision_captured_reads
                ),
                predecision_executed_read_inventory=(
                    prepared.predecision_executed_read_inventory
                ),
                final_read_provider=prepared.final_read_provider,
                bound_input_scope=prepared.bound_input_scope,
                candidate_sha256=prepared.candidate_sha256,
            )
            material.verify_for_dispatch(request)
            yield material, prepared.adapter_factory

    def post_commit(
        self,
        request: CapturedPaperPostCommitRequest,
    ) -> CommittedCapturedPaperAdmission:
        """Commit admission only; broker transport remains a later owner."""

        if type(request) is not CapturedPaperPostCommitRequest:
            raise CaptureContractError(
                "captured PAPER completion request is not typed"
            )
        request.verify()
        self._verify_runtime_request(
            self._runtime_request_from_completion(request)
        )
        lease_token, handoff = self._admissions.lease(
            request.completion_sha256
        )
        material = handoff.decision_material
        acknowledged = False
        try:
            material.verify_for_dispatch(
                material.selection_context.dispatch_request
            )
            if (
                request.to_canonical_json()
                != material.admission_inputs.post_commit_request.to_canonical_json()
                or request.route_token.route_token_sha256
                != material.selection_context.dispatch_request.route_token.route_token_sha256
                or request.route_token.runtime_generation
                != self._runtime.runtime_generation
            ):
                raise CaptureContractError(
                    "captured PAPER post-commit handoff mismatch"
                )
            committed = read_committed_captured_paper_admission(
                self._admission_bind,
                request=request,
            )
            if committed is None:
                try:
                    financial_breaker_receipt = (
                        self._financial_breaker_issuer.issue_for_request(
                            request,
                            phase="pre_reservation",
                        )
                    )
                    if type(financial_breaker_receipt) is not CapturedPaperFinancialBreakerReceipt:
                        raise CaptureContractError(
                            "captured PAPER pre-reservation financial-breaker receipt is malformed"
                        )
                    financial_breaker_verification_at = (
                        self._financial_breaker_clock()
                    )
                    with self._host.captured_paper_post_commit_scope(
                        material
                    ) as first_dip_scopes:
                        committed = commit_captured_paper_admission(
                            self._admission_bind,
                            inputs=material.admission_inputs,
                            phase_one_material_sha256=material.material_sha256,
                            executed_read_inventory=(
                                handoff.executed_read_inventory
                            ),
                            executed_captured_reads=handoff.captured_reads,
                            financial_breaker_receipt=(
                                financial_breaker_receipt
                            ),
                            financial_breaker_verification_at=(
                                financial_breaker_verification_at
                            ),
                            final_executed_read_provider=(
                                None
                                if first_dip_scopes is None
                                else first_dip_scopes.final_executed_read_binding
                            ),
                        )
                except BaseException:
                    # A lost commit acknowledgement is indeterminate until an
                    # exact readback proves whether the immutable outbox exists.
                    committed = read_committed_captured_paper_admission(
                        self._admission_bind,
                        request=request,
                    )
                    if committed is None:
                        raise
            if (
                type(committed) is not CommittedCapturedPaperAdmission
                or committed.post_commit_request.completion_sha256
                != request.completion_sha256
                or committed.post_commit_request.to_canonical_json()
                != request.to_canonical_json()
            ):
                raise CaptureContractError(
                    "captured PAPER admission completion is malformed"
                )
            acknowledge_captured_paper_phase_one_handoff(
                self._admission_bind,
                request=request,
                material_sha256=material.material_sha256,
            )
            self._admissions.ack(request.completion_sha256, lease_token)
            acknowledged = True
            return committed
        finally:
            if not acknowledged:
                self._admissions.release(
                    request.completion_sha256,
                    lease_token,
                )

    def retry_pending_post_commits(
        self,
        *,
        limit: int,
    ) -> Mapping[str, Any]:
        """Retry exact retained admissions; never recompute or submit an order."""

        keys = self._admissions.available_keys(limit=limit)
        completed = 0
        failed: list[str] = []
        for completion_sha256 in keys:
            try:
                lease_token, handoff = self._admissions.lease(
                    completion_sha256
                )
            except CaptureContractError as exc:
                failed.append(str(exc))
                continue
            try:
                request = (
                    handoff.decision_material.admission_inputs.post_commit_request
                )
            finally:
                self._admissions.release(completion_sha256, lease_token)
            try:
                self.post_commit(request)
                completed += 1
            except BaseException as exc:
                failed.append(
                    str(getattr(exc, "reason", None) or type(exc).__name__)
                )
        return MappingProxyType(
            {
                "attempted": len(keys),
                "completed": completed,
                "failed": len(failed),
                "failure_reasons": tuple(failed),
                "remaining": self._admissions.health()["pending"],
            }
        )

    @staticmethod
    def _runtime_request_from_completion(
        request: CapturedPaperPostCommitRequest,
    ) -> CapturedPaperDispatchRequest:
        route = request.route_token
        return CapturedPaperDispatchRequest(
            session_id=route.session_id,
            symbol=route.symbol,
            execution_family=route.execution_family,
            account_scope=route.account_scope,
            expected_account_id=route.expected_account_id,
            code_build_sha256=route.code_build_sha256,
            config_sha256=route.config_sha256,
            capture_receipt_sha256=route.capture_receipt_sha256,
            runtime_generation=route.runtime_generation,
            first_dip_policy_mode=route.first_dip_policy_mode,
        )

    def health(self) -> Mapping[str, Any]:
        return MappingProxyType(
            {
                "schema_version": "chili.iqfeed-captured-paper-runtime-owner.v1",
                "runtime_generation": self._runtime.runtime_generation,
                "capture_receipt_sha256": self._runtime.capture_receipt_sha256,
                "settings_projection_sha256": (
                    self._runtime.settings_projection_sha256
                ),
                "per_symbol_config_resolution": (
                    self._runtime.config_sha256_resolver is not None
                ),
                "first_dip_policy_mode": self._runtime.first_dip_policy_mode,
                "production_material_factory_installed": (
                    self._production_material_factory is not None
                ),
                "manual_staging_enabled": self._allow_manual_staging,
                "staged_decisions": dict(self._decisions.health()),
                "post_commit_handoffs": dict(self._admissions.health()),
                "retained_sqlalchemy_sessions": 0,
                "runtime_registered": False,
                "provider_or_broker_started": False,
                "paper_live_execution_enabled": False,
            }
        )


def prepare_iqfeed_capture_host(
    preflight: IqfeedCaptureBootstrapPreflight,
    *,
    pressure_sample: CapturePressureSample,
    wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    monotonic_clock: Callable[[], float],
) -> IqfeedCaptureHost:
    """Application-level preflight-to-bridge composition; remains inert."""

    composition = prepare_iqfeed_capture_ingress(
        preflight,
        pressure_sample=pressure_sample,
        wall_clock=wall_clock,
        monotonic_clock=monotonic_clock,
    )
    return IqfeedCaptureHost(composition, wall_clock=wall_clock)


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
        raise CaptureContractError("IQFeed launch receipt is not canonical JSON") from exc


def validate_iqfeed_capture_host_launch(
    *,
    launcher_path: str | Path,
    launcher_sha256: str,
    python_executable: str | Path,
    manifest_path: str | Path,
    manifest_sha256: str,
    allowed_read_roots: Sequence[str | Path],
    allowed_write_roots: Sequence[str | Path],
    wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    host_fingerprint_provider: Callable[[], str] | None = None,
    local_drive_check: Callable[[Path], bool] | None = None,
) -> Mapping[str, Any]:
    """Validate an exact future task action without launching provider I/O."""

    provider_options: dict[str, Any] = {}
    if host_fingerprint_provider is not None:
        provider_options["host_fingerprint_provider"] = host_fingerprint_provider
    if local_drive_check is not None:
        provider_options["local_drive_check"] = local_drive_check
    preflight = load_iqfeed_capture_bootstrap_preflight(
        manifest_path,
        expected_manifest_sha256=manifest_sha256,
        allowed_read_roots=allowed_read_roots,
        allowed_write_roots=allowed_write_roots,
        wall_clock=wall_clock,
        **provider_options,
    )
    launcher = Path(launcher_path)
    expected_launcher = preflight.source_paths[
        "iqfeed_capture_host_launcher"
    ].resolve()
    if not launcher.is_absolute() or launcher.resolve() != expected_launcher:
        raise CaptureContractError("IQFeed launcher path escaped preflight")
    launcher_digest = _stable_source_sha256(
        launcher.resolve(), role="iqfeed_capture_host_launcher"
    )
    supplied_launcher_digest = str(launcher_sha256 or "").strip().lower()
    if (
        launcher_digest != supplied_launcher_digest
        or launcher_digest
        != preflight.source_hashes["iqfeed_capture_host_launcher"]
    ):
        raise CaptureContractError("IQFeed launcher source hash escaped preflight")
    python_path = Path(python_executable)
    running_python = Path(sys.executable).resolve()
    if python_path.is_absolute():
        _reject_reparse_chain(python_path, role="IQFeed launcher Python executable")
    if (
        not python_path.is_absolute()
        or python_path.resolve() != running_python
        or not running_python.is_file()
    ):
        raise CaptureContractError("IQFeed launcher Python executable mismatch")
    host_path = Path(__file__).resolve()
    if host_path != preflight.source_paths["iqfeed_capture_host"].resolve():
        raise CaptureContractError("IQFeed host script path escaped preflight")
    host_digest = _stable_source_sha256(host_path, role="iqfeed_capture_host")
    if host_digest != preflight.source_hashes["iqfeed_capture_host"]:
        raise CaptureContractError("IQFeed host script hash escaped preflight")
    trade_source = _source_identity(
        iqfeed_trade_bridge,
        expected_path=preflight.source_paths["iqfeed_trade_bridge"],
        expected_sha256=preflight.source_hashes["iqfeed_trade_bridge"],
        role="iqfeed_trade_bridge",
    )
    depth_source = _source_identity(
        iqfeed_depth_bridge,
        expected_path=preflight.source_paths["iqfeed_depth_bridge"],
        expected_sha256=preflight.source_hashes["iqfeed_depth_bridge"],
        role="iqfeed_depth_bridge",
    )
    payload: dict[str, Any] = {
        "schema_version": _LAUNCH_VALIDATION_SCHEMA_VERSION,
        "verdict": "IQFEED_CAPTURE_HOST_LAUNCH_VALIDATED_INERT",
        "process_id": os.getpid(),
        "python_executable": str(running_python),
        "launcher": {
            "path": str(launcher.resolve()),
            "sha256": launcher_digest,
        },
        "host_script": {"path": str(host_path), "sha256": host_digest},
        "trade_bridge": dict(trade_source),
        "depth_bridge": dict(depth_source),
        "manifest_sha256": preflight.manifest_sha256,
        "preflight_report_sha256": preflight.report["preflight_report_sha256"],
        "activation_authorized": False,
        "provider_sockets_started": False,
        "database_or_broker_started": False,
        "paper_live_execution_enabled": False,
        "task_or_service_mutated": False,
        "network_fallback_allowed": False,
        "blocking_reasons": list(preflight.report["blocking_reasons"]),
    }
    payload["launch_validation_sha256"] = sha256_json(payload)
    return payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--launcher-path", required=True)
    parser.add_argument("--launcher-sha256", required=True)
    parser.add_argument("--python-executable", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--allow-read-root", action="append", required=True)
    parser.add_argument("--allow-write-root", action="append", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if not args.validate_only:
            raise CaptureContractError(
                "IQFeed capture-host activation is unavailable pending recertification"
            )
        report = validate_iqfeed_capture_host_launch(
            launcher_path=args.launcher_path,
            launcher_sha256=args.launcher_sha256,
            python_executable=args.python_executable,
            manifest_path=args.manifest,
            manifest_sha256=args.manifest_sha256,
            allowed_read_roots=args.allow_read_root,
            allowed_write_roots=args.allow_write_root,
        )
    except (BootstrapPreflightError, CaptureContractError, OSError, ValueError) as exc:
        code = (
            exc.code
            if isinstance(exc, BootstrapPreflightError)
            else "LAUNCH_CONTRACT_REJECTED"
        )
        message = (
            exc.message if isinstance(exc, BootstrapPreflightError) else str(exc)
        )
        rejected = {
            "schema_version": _LAUNCH_VALIDATION_SCHEMA_VERSION,
            "verdict": "IQFEED_CAPTURE_HOST_LAUNCH_REJECTED",
            "error_code": code,
            "error": message,
            "activation_authorized": False,
            "provider_sockets_started": False,
            "database_or_broker_started": False,
            "paper_live_execution_enabled": False,
            "task_or_service_mutated": False,
        }
        print(_canonical_json_bytes(rejected).decode("utf-8"))
        return 2
    print(_canonical_json_bytes(report).decode("utf-8"))
    return 0


__all__ = [
    "IqfeedCapturedPaperTickResult",
    "IqfeedCaptureHost",
    "IqfeedCaptureHostAdmission",
    "IqfeedCaptureHostBindingReceipt",
    "IqfeedCaptureHostState",
    "IqfeedProviderLoopSupervisor",
    "IqfeedProviderLoopSupervisorState",
    "prepare_iqfeed_capture_host",
    "validate_iqfeed_capture_host_launch",
]


if __name__ == "__main__":
    raise SystemExit(main())
