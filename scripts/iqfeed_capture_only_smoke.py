"""Bounded capture-only IQFeed preactivation smoke.

This module deliberately stops at the capture boundary.  It revalidates the
hash-bound IQFeed bootstrap, constructs the real bounded L1/L2 ingress graph,
binds the exact candidate bridge modules, runs their supervised provider lanes
for one bounded observation, and tears everything down before returning.

It does not import the captured-PAPER dispatcher, live runner loop, broker
adapter, order transport, or activation finalizer.  Importing this module is
inert.  Standalone execution has no trusted runtime configuration and fails
closed; the owning preactivation process must inject one typed configuration.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import threading
import time
from types import MappingProxyType
from typing import Any, Callable, Mapping, Protocol, Sequence
import uuid

# Direct ``python scripts/...`` execution otherwise exposes only ``scripts/``.
# Add this file's fixed repository root; no caller-selected import path is used.
_REPO_ROOT = str(Path(__file__).resolve().parents[1])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

UTC = timezone.utc
CAPTURE_ONLY_SMOKE_SCHEMA_VERSION = "chili.iqfeed-capture-only-smoke.v1"
CAPTURE_ONLY_SMOKE_ERROR_SCHEMA_VERSION = (
    "chili.iqfeed-capture-only-smoke-error.v1"
)
_SHA256_CHARS = frozenset("0123456789abcdef")
_REPARSE_ATTRIBUTE = 0x400
_MAX_SOURCE_BYTES = 64 * 1024 * 1024
_REQUIRED_CAPTURE_SOURCE_ROLES = (
    "iqfeed_capture_host",
    "iqfeed_trade_bridge",
    "iqfeed_depth_bridge",
    "iqfeed_l1_capture",
    "iqfeed_l2_capture",
)


class CaptureOnlySmokeError(RuntimeError):
    """Stable fail-closed capture-only smoke error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = str(code)
        self.message = str(message)


def _utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise CaptureOnlySmokeError("INVALID_CLOCK", f"{field} is not aware")
    return value.astimezone(UTC)


def _iso(value: datetime) -> str:
    return _utc(value, "timestamp").isoformat()


def _sha(value: Any, field: str) -> str:
    digest = str(value or "").strip().lower()
    if len(digest) != 64 or any(char not in _SHA256_CHARS for char in digest):
        raise CaptureOnlySmokeError("INVALID_SHA256", f"{field} is malformed")
    return digest


def _positive_seconds(value: Any, field: str) -> float:
    try:
        resolved = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise CaptureOnlySmokeError("INVALID_TIMEOUT", f"{field} is malformed") from exc
    if not math.isfinite(resolved) or resolved <= 0:
        raise CaptureOnlySmokeError("INVALID_TIMEOUT", f"{field} must be positive")
    return resolved


def _count(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CaptureOnlySmokeError("INVALID_COUNT", f"{field} is malformed")
    return int(value)


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CaptureOnlySmokeError(
            "NON_CANONICAL_EVIDENCE", "capture-only evidence is not canonical JSON"
        ) from exc


def _is_reparse(status: os.stat_result) -> bool:
    return bool(getattr(status, "st_file_attributes", 0) & _REPARSE_ATTRIBUTE)


def _stable_source_sha256(path: Path, *, expected: str, role: str) -> str:
    expected_digest = _sha(expected, f"{role} expected source")
    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise CaptureOnlySmokeError(
            "SOURCE_IDENTITY_MISMATCH", f"{role} source is not a regular file"
        )
    for component in (resolved, *resolved.parents):
        if _is_reparse(component.lstat()):
            raise CaptureOnlySmokeError(
                "SOURCE_IDENTITY_MISMATCH", f"{role} source crosses a reparse point"
            )
    before = resolved.stat()
    if before.st_size <= 0 or before.st_size > _MAX_SOURCE_BYTES:
        raise CaptureOnlySmokeError(
            "SOURCE_IDENTITY_MISMATCH", f"{role} source size is invalid"
        )
    raw = resolved.read_bytes()
    after = resolved.stat()
    identity_before = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )
    identity_after = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    )
    digest = hashlib.sha256(raw).hexdigest()
    if identity_before != identity_after or digest != expected_digest:
        raise CaptureOnlySmokeError(
            "SOURCE_IDENTITY_MISMATCH", f"{role} source changed or escaped its hash"
        )
    return digest


def _normalized_symbols(values: Sequence[str], field: str) -> tuple[str, ...]:
    symbols = tuple(
        str(value or "").strip().upper()
        for value in values
        if str(value or "").strip()
    )
    if not symbols or len(symbols) != len(set(symbols)):
        raise CaptureOnlySmokeError(
            "INVALID_SYMBOL_ROSTER", f"{field} must be nonempty and unique"
        )
    return symbols


@dataclass(frozen=True, slots=True)
class CaptureOnlyHealthObservation:
    """Concrete capture-store/event health observed during this smoke."""

    observed_at: datetime
    capture_store_root: str
    capture_store_probe_sha256: str
    resource_binding_sha256: str
    l1_bridge_source_sha256: str
    l2_bridge_source_sha256: str
    capture_store_writable: bool
    exact_print_event_count: int
    exact_print_inventory_sha256: str
    last_exact_print_available_at: datetime | None
    dropped_event_count: int
    overflow_count: int
    unreported_gap_count: int


class CaptureOnlyHealthAuthority(Protocol):
    def observe(
        self,
        *,
        composition: Any,
        provider_health: Mapping[str, Any],
    ) -> CaptureOnlyHealthObservation: ...


@dataclass(slots=True)
class IngressCaptureOnlyHealthAuthority:
    """Inspect the real pretrigger ring and prove the capture root writable.

    The promotion snapshot is non-destructive and always aborted.  It provides
    the exact typed event clocks and gap inventory already accepted by the
    candidate L1 handoff, without reading a provider or current database a
    second time.  The local write marker is create-new/content-addressed and is
    retained as forensic evidence; it never overwrites capture data.
    """

    preflight: Any
    certification_symbol: str
    wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    _marker_sha256: str | None = None

    def __post_init__(self) -> None:
        from scripts.iqfeed_capture_bootstrap_preflight import (
            IqfeedCaptureBootstrapPreflight,
        )

        if not isinstance(self.preflight, IqfeedCaptureBootstrapPreflight):
            raise CaptureOnlySmokeError(
                "PREFLIGHT_UNAVAILABLE", "capture health preflight is unavailable"
            )
        symbol = str(self.certification_symbol or "").strip().upper()
        if not symbol:
            raise CaptureOnlySmokeError(
                "INVALID_SYMBOL_ROSTER", "capture health symbol is unavailable"
            )
        if not callable(self.wall_clock):
            raise CaptureOnlySmokeError(
                "INVALID_CLOCK", "capture health wall clock is unavailable"
            )
        self.certification_symbol = symbol

    def _prove_store_writable(self, observed_at: datetime) -> str:
        if self._marker_sha256 is not None:
            prior = (
                self.preflight.capture_store_root.resolve(strict=True)
                / ".preactivation-capture-smoke"
                / f"{self._marker_sha256}.json"
            )
            try:
                raw = prior.read_bytes()
                status = prior.lstat()
            except OSError as exc:
                raise CaptureOnlySmokeError(
                    "CAPTURE_STORE_UNAVAILABLE", "capture marker disappeared"
                ) from exc
            if (
                _is_reparse(status)
                or not prior.is_file()
                or hashlib.sha256(raw).hexdigest() != self._marker_sha256
            ):
                raise CaptureOnlySmokeError(
                    "CAPTURE_STORE_UNAVAILABLE", "capture marker identity changed"
                )
            return self._marker_sha256
        root = self.preflight.capture_store_root.resolve(strict=True)
        if not root.is_dir():
            raise CaptureOnlySmokeError(
                "CAPTURE_STORE_UNAVAILABLE", "capture store root is not a directory"
            )
        for component in (root, *root.parents):
            if _is_reparse(component.lstat()):
                raise CaptureOnlySmokeError(
                    "CAPTURE_STORE_UNAVAILABLE", "capture store crosses a reparse point"
                )
        marker_root = root / ".preactivation-capture-smoke"
        marker_root.mkdir(mode=0o700, parents=False, exist_ok=True)
        if _is_reparse(marker_root.lstat()) or not marker_root.is_dir():
            raise CaptureOnlySmokeError(
                "CAPTURE_STORE_UNAVAILABLE", "capture marker root is unsafe"
            )
        marker = {
            "schema_version": "chili.iqfeed-capture-only-store-probe.v1",
            "observed_at": _iso(observed_at),
            "nonce": str(uuid.uuid4()),
            "capture_store_root": str(root),
            "resource_binding_sha256": self.preflight.resource_binding.binding_sha256,
            "bootstrap_manifest_sha256": self.preflight.manifest_sha256,
        }
        raw = _canonical_json_bytes(marker)
        digest = hashlib.sha256(raw).hexdigest()
        path = marker_root / f"{digest}.json"
        descriptor: int | None = None
        try:
            descriptor = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                0o600,
            )
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                descriptor = None
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            if path.read_bytes() != raw or _is_reparse(path.lstat()):
                raise CaptureOnlySmokeError(
                    "CAPTURE_STORE_UNAVAILABLE", "capture marker did not round-trip"
                )
        except FileExistsError as exc:
            # The nonce makes collision practically impossible; never trust or
            # overwrite a surprising existing path.
            raise CaptureOnlySmokeError(
                "CAPTURE_STORE_UNAVAILABLE", "capture marker path already exists"
            ) from exc
        except OSError as exc:
            raise CaptureOnlySmokeError(
                "CAPTURE_STORE_UNAVAILABLE", "capture store write probe failed"
            ) from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
        self._marker_sha256 = digest
        return digest

    def observe(
        self,
        *,
        composition: Any,
        provider_health: Mapping[str, Any],
    ) -> CaptureOnlyHealthObservation:
        from app.services.trading.momentum_neural.replay_capture_contract import (
            CaptureStream,
        )

        if provider_health.get("all_ready") is not True:
            raise CaptureOnlySmokeError(
                "PROVIDER_HEALTH_UNAVAILABLE", "provider lanes are not jointly ready"
            )
        observed_at = _utc(self.wall_clock(), "capture health wall clock")
        ring = getattr(getattr(composition, "supervisor", None), "pretrigger_ring", None)
        identity = getattr(getattr(composition, "supervisor", None), "identity", None)
        if not callable(getattr(ring, "begin_promotion", None)) or not callable(
            getattr(ring, "abort_promotion", None)
        ):
            raise CaptureOnlySmokeError(
                "CAPTURE_HEALTH_UNAVAILABLE", "candidate pretrigger ring is unavailable"
            )
        try:
            transfer = ring.begin_promotion(
                self.certification_symbol,
                promoted_at=observed_at,
                source_identity=identity,
            )
        except BaseException as exc:
            raise CaptureOnlySmokeError(
                "CAPTURE_HEALTH_UNAVAILABLE", "capture inventory snapshot failed"
            ) from exc
        try:
            gaps = tuple(transfer.gaps)
            exact = tuple(
                event
                for event in transfer.events
                if event.stream is CaptureStream.IQFEED_PRINT
                and event.clocks.provider_event_at is not None
            )
            exact_at = (
                None
                if not exact
                else max(event.clocks.available_at for event in exact)
            )
            gap_count = sum(int(gap.lost_count) for gap in gaps)
            exact_inventory_sha256 = hashlib.sha256(
                _canonical_json_bytes(
                    {
                        "promotion_inventory_sha256": transfer.inventory_sha256,
                        "exact_print_event_sha256s": [
                            event.event_sha256 for event in exact
                        ],
                    }
                )
            ).hexdigest()
        finally:
            if ring.abort_promotion(transfer) is not True:
                raise CaptureOnlySmokeError(
                    "CAPTURE_HEALTH_UNAVAILABLE",
                    "non-destructive capture inventory reservation was not released",
                )
        store_probe_sha256 = self._prove_store_writable(observed_at)
        return CaptureOnlyHealthObservation(
            observed_at=observed_at,
            capture_store_root=str(self.preflight.capture_store_root),
            capture_store_probe_sha256=store_probe_sha256,
            resource_binding_sha256=self.preflight.resource_binding.binding_sha256,
            l1_bridge_source_sha256=self.preflight.source_hashes[
                "iqfeed_trade_bridge"
            ],
            l2_bridge_source_sha256=self.preflight.source_hashes[
                "iqfeed_depth_bridge"
            ],
            capture_store_writable=True,
            exact_print_event_count=len(exact),
            exact_print_inventory_sha256=exact_inventory_sha256,
            last_exact_print_available_at=exact_at,
            dropped_event_count=gap_count,
            overflow_count=0,
            unreported_gap_count=gap_count,
        )


@dataclass(frozen=True, slots=True)
class CaptureOnlySmokeConfiguration:
    """Trusted in-process inputs for one real capture-only smoke."""

    preflight: Any
    pressure_sample: Any
    capture_health_authority: CaptureOnlyHealthAuthority
    trade_forced_symbols: tuple[str, ...]
    depth_forced_symbols: tuple[str, ...]
    readiness_timeout_seconds: float = 15.0
    observation_timeout_seconds: float = 30.0
    join_timeout_seconds: float = 20.0
    reconnect_wait_seconds: float = 10.0
    trade_bridge: Any | None = None
    depth_bridge: Any | None = None

    def __post_init__(self) -> None:
        # Runtime-only imports keep module import and standalone fail-closed
        # execution free of Settings/DB construction.
        from app.services.trading.momentum_neural.replay_capture_runtime import (
            CapturePressureSample,
        )
        from scripts.iqfeed_capture_bootstrap_preflight import (
            IqfeedCaptureBootstrapPreflight,
        )

        if not isinstance(self.preflight, IqfeedCaptureBootstrapPreflight):
            raise CaptureOnlySmokeError(
                "PREFLIGHT_UNAVAILABLE", "typed IQFeed bootstrap preflight is unavailable"
            )
        if not isinstance(self.pressure_sample, CapturePressureSample):
            raise CaptureOnlySmokeError(
                "PRESSURE_SAMPLE_UNAVAILABLE", "fresh capture pressure sample is unavailable"
            )
        if not callable(getattr(self.capture_health_authority, "observe", None)):
            raise CaptureOnlySmokeError(
                "CAPTURE_HEALTH_UNAVAILABLE", "capture health authority is unavailable"
            )
        object.__setattr__(
            self,
            "trade_forced_symbols",
            _normalized_symbols(self.trade_forced_symbols, "trade symbols"),
        )
        object.__setattr__(
            self,
            "depth_forced_symbols",
            _normalized_symbols(self.depth_forced_symbols, "depth symbols"),
        )
        for field in (
            "readiness_timeout_seconds",
            "observation_timeout_seconds",
            "join_timeout_seconds",
            "reconnect_wait_seconds",
        ):
            object.__setattr__(self, field, _positive_seconds(getattr(self, field), field))
        if (self.trade_bridge is None) != (self.depth_bridge is None):
            raise CaptureOnlySmokeError(
                "BRIDGE_ROSTER_INVALID", "L1 and L2 bridges must be supplied together"
            )


@dataclass(frozen=True, slots=True)
class CaptureOnlySmokeEvidence:
    """Typed raw evidence consumed by the v3 capture-host producer."""

    bootstrap_manifest_sha256: str
    capture_store_root: str
    source_hashes: Mapping[str, str]
    host_binding: Mapping[str, Any]
    capture_health: Mapping[str, Any]
    provider_health: Mapping[str, Any]
    started_at: datetime
    completed_at: datetime
    closure: Mapping[str, Any]
    schema_version: str = CAPTURE_ONLY_SMOKE_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "schema_version": self.schema_version,
            "bootstrap_manifest_sha256": self.bootstrap_manifest_sha256,
            "capture_store_root": self.capture_store_root,
            "source_hashes": dict(sorted(self.source_hashes.items())),
            "host_binding": dict(self.host_binding),
            "capture_health": dict(self.capture_health),
            "provider_health": dict(self.provider_health),
            "started_at": _iso(self.started_at),
            "completed_at": _iso(self.completed_at),
            "closure": dict(self.closure),
        }
        payload["evidence_sha256"] = hashlib.sha256(
            _canonical_json_bytes(payload)
        ).hexdigest()
        return payload

    @property
    def evidence_sha256(self) -> str:
        return self.to_dict()["evidence_sha256"]


class _CaptureOnlyProviderSupervisor:
    """Two-lane, one-shot provider supervisor with a shared stop boundary."""

    def __init__(self, *, trade_bridge: Any, depth_bridge: Any) -> None:
        self._bridges = {"trade": trade_bridge, "depth": depth_bridge}
        for lane, bridge in self._bridges.items():
            if not callable(getattr(bridge, "run_supervised", None)):
                raise CaptureOnlySmokeError(
                    "BRIDGE_API_INVALID", f"{lane} bridge lacks run_supervised"
                )
        self._stop = threading.Event()
        self._changed = threading.Event()
        self._schema = {lane: threading.Event() for lane in self._bridges}
        self._connected = {lane: threading.Event() for lane in self._bridges}
        self._ready = {lane: threading.Event() for lane in self._bridges}
        self._threads: dict[str, threading.Thread] = {}
        self._failures: dict[str, str] = {}
        self._state = "prepared"
        self._safe_to_unbind = True
        self._lock = threading.RLock()

    def _fail(self, lane: str, exc: BaseException) -> None:
        with self._lock:
            if bool(getattr(exc, "provider_reader_may_be_alive", False)):
                self._safe_to_unbind = False
            self._failures.setdefault(lane, f"{type(exc).__name__}:{str(exc)[:256]}")
            self._state = "failed"
            self._stop.set()
            self._changed.set()

    def _run(self, lane: str, symbols: tuple[str, ...], reconnect: float) -> None:
        try:
            self._bridges[lane].run_supervised(
                stop_event=self._stop,
                schema_ready_event=self._schema[lane],
                connected_event=self._connected[lane],
                ready_event=self._ready[lane],
                forced_symbols=symbols,
                reconnect_wait_seconds=reconnect,
            )
        except BaseException as exc:
            self._fail(lane, exc)
        else:
            if not self._stop.is_set():
                self._fail(lane, RuntimeError("provider lane returned before stop"))
        finally:
            self._ready[lane].clear()
            self._connected[lane].clear()
            self._changed.set()

    def start(
        self,
        *,
        readiness_timeout_seconds: float,
        reconnect_wait_seconds: float,
        trade_symbols: tuple[str, ...],
        depth_symbols: tuple[str, ...],
    ) -> Mapping[str, Any]:
        readiness = _positive_seconds(readiness_timeout_seconds, "provider readiness")
        reconnect = _positive_seconds(reconnect_wait_seconds, "provider reconnect")
        with self._lock:
            if self._state != "prepared":
                raise CaptureOnlySmokeError(
                    "PROVIDER_SUPERVISOR_REUSED", "provider supervisor is one-shot"
                )
            self._state = "starting"
            for lane, symbols in (
                ("trade", trade_symbols),
                ("depth", depth_symbols),
            ):
                self._threads[lane] = threading.Thread(
                    target=self._run,
                    args=(lane, symbols, reconnect),
                    name=f"chili-capture-only-{lane}-provider",
                    daemon=False,
                )
            try:
                for lane in ("trade", "depth"):
                    self._threads[lane].start()
            except BaseException as exc:
                self._fail("thread_start", exc)

        deadline = time.monotonic() + readiness
        while time.monotonic() < deadline:
            with self._lock:
                if self._failures:
                    break
                if all(event.is_set() for event in self._ready.values()):
                    self._state = "running"
                    return self.health()
            self._changed.wait(timeout=min(0.02, max(0.0, deadline - time.monotonic())))
            self._changed.clear()
        if not self._failures:
            self._fail("readiness", TimeoutError("provider readiness deadline expired"))
        raise CaptureOnlySmokeError(
            "PROVIDER_NOT_READY", "both provider lanes did not become ready"
        )

    def close(self, *, join_timeout_seconds: float) -> Mapping[str, Any]:
        timeout = _positive_seconds(join_timeout_seconds, "provider join")
        with self._lock:
            if self._state == "stopped":
                return self.health()
            self._stop.set()
            self._changed.set()
            if self._state != "failed":
                self._state = "stopping"
        deadline = time.monotonic() + timeout
        for lane in ("trade", "depth"):
            thread = self._threads.get(lane)
            if thread is not None and thread.ident is not None:
                thread.join(timeout=max(0.0, deadline - time.monotonic()))
        alive = tuple(
            lane for lane, thread in self._threads.items() if thread.is_alive()
        )
        if alive:
            self._fail("shutdown", TimeoutError("provider lanes did not join"))
            raise CaptureOnlySmokeError(
                "PROVIDER_NOT_QUIESCENT", "provider lanes did not stop before unbind"
            )
        with self._lock:
            if not self._safe_to_unbind:
                raise CaptureOnlySmokeError(
                    "PROVIDER_NOT_QUIESCENT", "provider reader may still be alive"
                )
            if self._state != "failed":
                self._state = "stopped"
            return self.health()

    def health(self) -> Mapping[str, Any]:
        with self._lock:
            lanes = {
                lane: {
                    "thread_alive": bool(
                        self._threads.get(lane) and self._threads[lane].is_alive()
                    ),
                    "thread_daemon": (
                        None
                        if lane not in self._threads
                        else self._threads[lane].daemon
                    ),
                    "schema_verified": self._schema[lane].is_set(),
                    "socket_connected": self._connected[lane].is_set(),
                    "ready": self._ready[lane].is_set(),
                }
                for lane in ("trade", "depth")
            }
            return MappingProxyType(
                {
                    "state": self._state,
                    "all_ready": all(row["ready"] for row in lanes.values()),
                    "provider_sockets_started": any(
                        row["socket_connected"] for row in lanes.values()
                    ),
                    "safe_to_unbind": self._safe_to_unbind,
                    "lanes": lanes,
                    "failures": dict(self._failures),
                }
            )


class _CaptureOnlyHost:
    """Exact bridge binding owner with no decision or order surface."""

    def __init__(
        self,
        composition: Any,
        *,
        trade_bridge: Any,
        depth_bridge: Any,
    ) -> None:
        from scripts.iqfeed_capture_bootstrap import (
            IqfeedCaptureIngressComposition,
            IqfeedIngressCompositionState,
        )

        if not isinstance(composition, IqfeedCaptureIngressComposition):
            raise CaptureOnlySmokeError(
                "COMPOSITION_INVALID", "capture ingress composition is malformed"
            )
        if composition.state is not IqfeedIngressCompositionState.PREPARED:
            raise CaptureOnlySmokeError(
                "COMPOSITION_INVALID", "capture ingress composition is not prepared"
            )
        self.composition = composition
        self.trade_bridge = trade_bridge
        self.depth_bridge = depth_bridge
        self._bound = {"trade": False, "depth": False}
        self._supervisor: _CaptureOnlyProviderSupervisor | None = None

    def bind(self) -> None:
        try:
            self.composition.start_ingress()
            self.trade_bridge.bind_capture_handoff(self.composition.l1_handoff)
            self._bound["trade"] = True
            self.depth_bridge.bind_capture_handoff(self.composition.l2_handoff)
            self._bound["depth"] = True
        except BaseException as exc:
            try:
                self.close(join_timeout_seconds=1.0)
            except BaseException:
                pass
            raise CaptureOnlySmokeError(
                "CAPTURE_BIND_FAILED", "L1/L2 capture binding failed atomically"
            ) from exc

    def start_provider(
        self,
        *,
        readiness_timeout_seconds: float,
        reconnect_wait_seconds: float,
        trade_symbols: tuple[str, ...],
        depth_symbols: tuple[str, ...],
    ) -> Mapping[str, Any]:
        if not all(self._bound.values()) or self._supervisor is not None:
            raise CaptureOnlySmokeError(
                "CAPTURE_HOST_STATE_INVALID", "provider start requires one exact binding"
            )
        self._supervisor = _CaptureOnlyProviderSupervisor(
            trade_bridge=self.trade_bridge,
            depth_bridge=self.depth_bridge,
        )
        return self._supervisor.start(
            readiness_timeout_seconds=readiness_timeout_seconds,
            reconnect_wait_seconds=reconnect_wait_seconds,
            trade_symbols=trade_symbols,
            depth_symbols=depth_symbols,
        )

    def close(self, *, join_timeout_seconds: float) -> Mapping[str, Any]:
        provider: Mapping[str, Any] | None = None
        failures: list[BaseException] = []
        if self._supervisor is not None:
            try:
                provider = self._supervisor.close(
                    join_timeout_seconds=join_timeout_seconds
                )
            except BaseException as exc:
                failures.append(exc)
        if not failures:
            for lane, bridge, handoff in (
                ("depth", self.depth_bridge, self.composition.l2_handoff),
                ("trade", self.trade_bridge, self.composition.l1_handoff),
            ):
                if self._bound[lane]:
                    try:
                        bridge.unbind_capture_handoff(handoff)
                        self._bound[lane] = False
                    except BaseException as exc:
                        failures.append(exc)
        composition_health: Mapping[str, Any] | None = None
        if not failures:
            try:
                composition_health = self.composition.close()
            except BaseException as exc:
                failures.append(exc)
        if failures:
            raise CaptureOnlySmokeError(
                "CAPTURE_TEARDOWN_FAILED", "capture-only smoke did not quiesce cleanly"
            ) from failures[0]
        return MappingProxyType(
            {
                "provider": None if provider is None else dict(provider),
                "composition": (
                    None if composition_health is None else dict(composition_health)
                ),
                "trade_bridge_bound": self._bound["trade"],
                "depth_bridge_bound": self._bound["depth"],
            }
        )


def _resolve_bridges(configuration: CaptureOnlySmokeConfiguration) -> tuple[Any, Any]:
    if configuration.trade_bridge is not None:
        return configuration.trade_bridge, configuration.depth_bridge
    # Lazy by design: importing this module does not create even the bridges'
    # inert SQLAlchemy engine objects.  A trusted operational call explicitly
    # crosses this boundary only after the bootstrap and pressure inputs exist.
    from scripts import iqfeed_depth_bridge, iqfeed_trade_bridge

    return iqfeed_trade_bridge, iqfeed_depth_bridge


def _verify_bridge_api_and_source(
    bridge: Any,
    *,
    role: str,
    preflight: Any,
) -> str:
    required = ("bind_capture_handoff", "unbind_capture_handoff", "run_supervised")
    if any(not callable(getattr(bridge, name, None)) for name in required):
        raise CaptureOnlySmokeError(
            "BRIDGE_API_INVALID", f"{role} bridge API is incomplete"
        )
    module_path = Path(str(getattr(bridge, "__file__", "") or ""))
    expected_path = preflight.source_paths[role].resolve()
    try:
        resolved = module_path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise CaptureOnlySmokeError(
            "SOURCE_IDENTITY_MISMATCH", f"loaded {role} source is unavailable"
        ) from exc
    if resolved != expected_path:
        raise CaptureOnlySmokeError(
            "SOURCE_IDENTITY_MISMATCH", f"loaded {role} path escaped preflight"
        )
    return _stable_source_sha256(
        resolved,
        expected=preflight.source_hashes[role],
        role=role,
    )


def _handoff_capture_health(
    composition: Any,
) -> Mapping[str, Any]:
    l1 = composition.l1_handoff.health()
    l2 = composition.l2_handoff.health()
    for lane, health in (("l1", l1), ("l2", l2)):
        if (
            health.get("started") is not True
            or health.get("accepting") is not True
            or health.get("terminal_error") is not None
            or _count(health.get("unpersisted_gap_count"), f"{lane} unpersisted gaps")
            != 0
            or _count(health.get("pending_gap_keys"), f"{lane} pending gaps") != 0
            or health.get("gap_ledger_overflow") is not False
            or health.get("capture_resource_binding_sha256")
            != composition.binding.binding_sha256
        ):
            raise CaptureOnlySmokeError(
                "CAPTURE_HANDOFF_UNHEALTHY", f"{lane} handoff is incomplete or gapped"
            )
    dropped = sum(
        _count(health.get(field), f"{lane} {field}")
        for lane, health in (("l1", l1), ("l2", l2))
        for field in (
            "queue_overflow_lost",
            "byte_overflow_lost",
            "oversized_envelope_lost",
        )
    )
    overflow = sum(
        _count(health.get(field, 0), f"{lane} {field}")
        for lane, health in (("l1", l1), ("l2", l2))
        for field in ("queue_overflow_incidents", "byte_overflow_incidents")
    )
    return MappingProxyType(
        {
            "dropped_event_count": dropped,
            "overflow_count": overflow,
            "unreported_gap_count": 0,
        }
    )


def _validate_capture_only_composition_boundary(composition: Any) -> None:
    """Reject any composition that already carries an executable hot run."""

    if not callable(getattr(composition, "health", None)):
        raise CaptureOnlySmokeError(
            "COMPOSITION_INVALID", "capture composition has no health boundary"
        )
    health = composition.health()
    service = health.get("service") if isinstance(health, Mapping) else None
    if (
        not isinstance(health, Mapping)
        or health.get("activation_authorized") is not False
        or health.get("hot_admission_available") is not False
        or health.get("hot_run_factory_installed") is not False
        or health.get("network_fallback_allowed") is not False
        or not isinstance(service, Mapping)
        or tuple(service.get("pending_symbols") or ())
        or tuple(service.get("running_symbols") or ())
    ):
        raise CaptureOnlySmokeError(
            "EXECUTION_SURFACE_PRESENT",
            "capture-only composition contains hot-run or activation authority",
        )


def _validated_capture_observation(
    value: CaptureOnlyHealthObservation,
    *,
    configuration: CaptureOnlySmokeConfiguration,
    started_at: datetime,
    observed_now: datetime,
    l1_source_sha256: str,
    l2_source_sha256: str,
) -> CaptureOnlyHealthObservation:
    if type(value) is not CaptureOnlyHealthObservation:
        raise CaptureOnlySmokeError(
            "CAPTURE_HEALTH_INVALID", "capture authority returned shaped evidence"
        )
    observed_at = _utc(value.observed_at, "capture health observed_at")
    if observed_at < started_at or observed_at > observed_now:
        raise CaptureOnlySmokeError(
            "CAPTURE_HEALTH_INVALID", "capture health is stale or future-dated"
        )
    exact_at = (
        None
        if value.last_exact_print_available_at is None
        else _utc(value.last_exact_print_available_at, "exact print available_at")
    )
    exact_count = _count(value.exact_print_event_count, "exact print events")
    _sha(value.capture_store_probe_sha256, "capture store probe")
    _sha(value.exact_print_inventory_sha256, "exact print inventory")
    if exact_count <= 0 or exact_at is None or not started_at <= exact_at <= observed_at:
        raise CaptureOnlySmokeError(
            "EXACT_PRINT_UNAVAILABLE",
            "current smoke lacks a provider-clocked exact print accepted during its window",
        )
    if (
        Path(value.capture_store_root).resolve()
        != configuration.preflight.capture_store_root.resolve()
        or _sha(value.resource_binding_sha256, "capture health binding")
        != configuration.preflight.resource_binding.binding_sha256
        or _sha(value.l1_bridge_source_sha256, "capture health L1 source")
        != l1_source_sha256
        or _sha(value.l2_bridge_source_sha256, "capture health L2 source")
        != l2_source_sha256
        or value.capture_store_writable is not True
        or _count(value.dropped_event_count, "capture dropped events") != 0
        or _count(value.overflow_count, "capture overflows") != 0
        or _count(value.unreported_gap_count, "capture unreported gaps") != 0
    ):
        raise CaptureOnlySmokeError(
            "CAPTURE_HEALTH_INVALID", "capture health is foreign, lossy, or unwritable"
        )
    return value


def run_capture_only_preactivation_smoke(
    configuration: CaptureOnlySmokeConfiguration,
    *,
    wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    monotonic_clock: Callable[[], float] = time.monotonic,
    composition_factory: Callable[..., Any] | None = None,
) -> CaptureOnlySmokeEvidence:
    """Run one bounded, zero-order capture/provider smoke and return raw evidence."""

    if type(configuration) is not CaptureOnlySmokeConfiguration:
        raise CaptureOnlySmokeError(
            "CONFIGURATION_UNAVAILABLE", "typed capture-only configuration is unavailable"
        )
    if not callable(wall_clock) or not callable(monotonic_clock):
        raise CaptureOnlySmokeError("INVALID_CLOCK", "smoke clocks are unavailable")
    from scripts.iqfeed_capture_bootstrap import (
        IqfeedCaptureIngressComposition,
        prepare_iqfeed_capture_ingress,
    )

    factory = composition_factory or prepare_iqfeed_capture_ingress
    started_at = _utc(wall_clock(), "capture-only start clock")
    preflight = configuration.preflight
    for role in _REQUIRED_CAPTURE_SOURCE_ROLES:
        _stable_source_sha256(
            preflight.source_paths[role],
            expected=preflight.source_hashes[role],
            role=role,
        )
    trade_bridge, depth_bridge = _resolve_bridges(configuration)
    l1_source_sha256 = _verify_bridge_api_and_source(
        trade_bridge,
        role="iqfeed_trade_bridge",
        preflight=preflight,
    )
    l2_source_sha256 = _verify_bridge_api_and_source(
        depth_bridge,
        role="iqfeed_depth_bridge",
        preflight=preflight,
    )
    try:
        composition = factory(
            preflight,
            pressure_sample=configuration.pressure_sample,
            wall_clock=wall_clock,
            monotonic_clock=monotonic_clock,
        )
    except BaseException as exc:
        raise CaptureOnlySmokeError(
            "COMPOSITION_UNAVAILABLE", "real capture ingress composition failed closed"
        ) from exc
    if not isinstance(composition, IqfeedCaptureIngressComposition):
        raise CaptureOnlySmokeError(
            "COMPOSITION_INVALID", "composition factory returned a foreign object"
        )
    try:
        _validate_capture_only_composition_boundary(composition)
    except BaseException as exc:
        try:
            composition.close()
        except BaseException as close_exc:
            raise CaptureOnlySmokeError(
                "CAPTURE_TEARDOWN_FAILED",
                "rejected capture composition did not close cleanly",
            ) from close_exc
        raise exc
    host = _CaptureOnlyHost(
        composition,
        trade_bridge=trade_bridge,
        depth_bridge=depth_bridge,
    )
    running_provider: Mapping[str, Any] | None = None
    captured: CaptureOnlyHealthObservation | None = None
    closure: Mapping[str, Any] | None = None
    primary_error: BaseException | None = None
    try:
        host.bind()
        running_provider = host.start_provider(
            readiness_timeout_seconds=configuration.readiness_timeout_seconds,
            reconnect_wait_seconds=configuration.reconnect_wait_seconds,
            trade_symbols=configuration.trade_forced_symbols,
            depth_symbols=configuration.depth_forced_symbols,
        )
        _validate_capture_only_composition_boundary(composition)
        deadline = monotonic_clock() + configuration.observation_timeout_seconds
        last_exact_print_error: CaptureOnlySmokeError | None = None
        while monotonic_clock() < deadline:
            provider = host._supervisor.health() if host._supervisor else {}
            if provider.get("all_ready") is not True or provider.get("failures"):
                raise CaptureOnlySmokeError(
                    "PROVIDER_HEALTH_UNAVAILABLE", "provider readiness was lost during smoke"
                )
            handoff_health = _handoff_capture_health(composition)
            if any(handoff_health.values()):
                raise CaptureOnlySmokeError(
                    "CAPTURE_LOSS_OBSERVED", "bounded handoff recorded loss or overflow"
                )
            observed = configuration.capture_health_authority.observe(
                composition=composition,
                provider_health=provider,
            )
            now = _utc(wall_clock(), "capture-only observation clock")
            try:
                captured = _validated_capture_observation(
                    observed,
                    configuration=configuration,
                    started_at=started_at,
                    observed_now=now,
                    l1_source_sha256=l1_source_sha256,
                    l2_source_sha256=l2_source_sha256,
                )
            except CaptureOnlySmokeError as exc:
                if exc.code != "EXACT_PRINT_UNAVAILABLE":
                    raise
                last_exact_print_error = exc
                if not threading.Event().wait(
                    min(0.02, max(0.0, deadline - monotonic_clock()))
                ):
                    continue
            else:
                running_provider = provider
                break
        if captured is None:
            raise last_exact_print_error or CaptureOnlySmokeError(
                "CAPTURE_HEALTH_UNAVAILABLE", "capture health deadline expired"
            )
    except BaseException as exc:
        primary_error = exc
    finally:
        try:
            closure = host.close(
                join_timeout_seconds=configuration.join_timeout_seconds
            )
        except BaseException as close_exc:
            if primary_error is None:
                primary_error = close_exc
    if primary_error is not None:
        if isinstance(primary_error, CaptureOnlySmokeError):
            raise primary_error
        raise CaptureOnlySmokeError(
            "CAPTURE_ONLY_SMOKE_FAILED", "capture-only smoke failed closed"
        ) from primary_error
    assert captured is not None and running_provider is not None and closure is not None
    closed_provider = closure.get("provider")
    if (
        not isinstance(closed_provider, Mapping)
        or closed_provider.get("state") != "stopped"
        or any(
            row.get("thread_alive") is not False
            for row in closed_provider.get("lanes", {}).values()
        )
        or closure.get("trade_bridge_bound") is not False
        or closure.get("depth_bridge_bound") is not False
    ):
        raise CaptureOnlySmokeError(
            "CAPTURE_TEARDOWN_UNPROVEN", "capture-only close was not fully observed"
        )
    completed_at = _utc(wall_clock(), "capture-only completion clock")
    if completed_at < started_at:
        raise CaptureOnlySmokeError("INVALID_CLOCK", "smoke completion predates start")
    source_hashes = {
        role: _sha(preflight.source_hashes[role], f"{role} source")
        for role in _REQUIRED_CAPTURE_SOURCE_ROLES
    }
    return CaptureOnlySmokeEvidence(
        bootstrap_manifest_sha256=_sha(
            preflight.manifest_sha256, "bootstrap manifest"
        ),
        capture_store_root=str(preflight.capture_store_root),
        source_hashes=MappingProxyType(source_hashes),
        host_binding=MappingProxyType(
            {
                "trade_bridge_bound": True,
                "depth_bridge_bound": True,
                "execution_surface": "capture_only",
                "dispatcher_constructed": False,
                "live_runner_loop_constructed": False,
                "broker_adapter_constructed": False,
                "order_transport_constructed": False,
            }
        ),
        capture_health=MappingProxyType(
            {
                "capture_store_writable": True,
                "capture_store_probe_sha256": captured.capture_store_probe_sha256,
                "dropped_event_count": 0,
                "overflow_count": 0,
                "unreported_gap_count": 0,
            }
        ),
        provider_health=MappingProxyType(
            {
                "observed_at": _iso(captured.observed_at),
                "socket_readable": True,
                "exact_print_clock_observed": True,
                "exact_print_event_count": captured.exact_print_event_count,
                "exact_print_inventory_sha256": (
                    captured.exact_print_inventory_sha256
                ),
                "last_exact_print_available_at": _iso(
                    captured.last_exact_print_available_at
                ),
            }
        ),
        started_at=started_at,
        completed_at=completed_at,
        closure=MappingProxyType(
            {
                "provider_state": closed_provider["state"],
                "trade_thread_alive": closed_provider["lanes"]["trade"][
                    "thread_alive"
                ],
                "depth_thread_alive": closed_provider["lanes"]["depth"][
                    "thread_alive"
                ],
                "bridges_unbound": True,
                "orders_submitted": False,
            }
        ),
    )


def _parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(description=__doc__)


def main(
    argv: Sequence[str] | None = None,
    *,
    configuration_provider: Callable[[], CaptureOnlySmokeConfiguration] | None = None,
) -> int:
    _parser().parse_args(argv)
    try:
        if configuration_provider is None:
            raise CaptureOnlySmokeError(
                "CONFIGURATION_UNAVAILABLE",
                "standalone capture-only smoke has no trusted provider/capture config",
            )
        configuration = configuration_provider()
        evidence = run_capture_only_preactivation_smoke(configuration)
    except CaptureOnlySmokeError as exc:
        print(
            _canonical_json_bytes(
                {
                    "schema_version": CAPTURE_ONLY_SMOKE_ERROR_SCHEMA_VERSION,
                    "error_code": exc.code,
                }
            ).decode("utf-8"),
            file=sys.stderr,
        )
        return 2
    print(_canonical_json_bytes(evidence.to_dict()).decode("utf-8"))
    return 0


__all__ = [
    "CAPTURE_ONLY_SMOKE_SCHEMA_VERSION",
    "CaptureOnlyHealthAuthority",
    "CaptureOnlyHealthObservation",
    "IngressCaptureOnlyHealthAuthority",
    "CaptureOnlySmokeConfiguration",
    "CaptureOnlySmokeError",
    "CaptureOnlySmokeEvidence",
    "main",
    "run_capture_only_preactivation_smoke",
]


if __name__ == "__main__":
    raise SystemExit(main())
