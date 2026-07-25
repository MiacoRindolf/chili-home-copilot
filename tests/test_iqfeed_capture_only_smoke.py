from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from types import MappingProxyType, SimpleNamespace

import pytest

from app.services.trading.momentum_neural.replay_capture_contract import (
    CaptureStream,
)
from app.services.trading.momentum_neural.replay_capture_runtime import (
    CapturePressureSample,
)
from scripts.iqfeed_capture_bootstrap import (
    IqfeedCaptureIngressComposition,
    IqfeedIngressCompositionState,
)
from scripts.iqfeed_capture_bootstrap_preflight import (
    IqfeedCaptureBootstrapPreflight,
)
from scripts import iqfeed_capture_only_smoke as smoke
from scripts import run_captured_paper_preactivation_probes as probes


UTC = timezone.utc
NOW = datetime(2026, 7, 16, 17, 10, tzinfo=UTC)
REPO = Path(__file__).resolve().parents[1]
BINDING_SHA = hashlib.sha256(b"capture-only-binding").hexdigest()


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class _Handoff:
    def __init__(self, *, bridge_sha: str) -> None:
        self.started = False
        self.bridge_sha = bridge_sha

    def health(self):
        return {
            "started": self.started,
            "accepting": self.started,
            "terminal_error": None,
            "unpersisted_gap_count": 0,
            "pending_gap_keys": 0,
            "gap_ledger_overflow": False,
            "capture_resource_binding_sha256": BINDING_SHA,
            "queue_overflow_lost": 0,
            "queue_overflow_incidents": 0,
            "byte_overflow_lost": 0,
            "byte_overflow_incidents": 0,
            "oversized_envelope_lost": 0,
            "bridge_source_sha256": self.bridge_sha,
        }

    def start(self):
        assert self.started is False
        self.started = True

    def close(self):
        self.started = False


class _Composition(IqfeedCaptureIngressComposition):
    def __init__(self, preflight: IqfeedCaptureBootstrapPreflight) -> None:
        self.preflight = preflight
        self.binding = SimpleNamespace(binding_sha256=BINDING_SHA)
        self.l1_handoff = _Handoff(
            bridge_sha=preflight.source_hashes["iqfeed_trade_bridge"]
        )
        self.l2_handoff = _Handoff(
            bridge_sha=preflight.source_hashes["iqfeed_depth_bridge"]
        )
        self._fixture_state = IqfeedIngressCompositionState.PREPARED

    @property
    def state(self):
        return self._fixture_state

    def start_ingress(self):
        assert self._fixture_state is IqfeedIngressCompositionState.PREPARED
        self.l1_handoff.started = True
        self.l2_handoff.started = True
        self._fixture_state = IqfeedIngressCompositionState.INGRESS_RUNNING

    def health(self):
        return {
            "activation_authorized": False,
            "hot_admission_available": False,
            "hot_run_factory_installed": False,
            "network_fallback_allowed": False,
            "service": {"pending_symbols": (), "running_symbols": ()},
        }

    def close(self):
        self.l1_handoff.started = False
        self.l2_handoff.started = False
        self._fixture_state = IqfeedIngressCompositionState.CLOSED
        return {
            "state": "closed",
            "provider_socket_started": False,
            "database_or_broker_started": False,
        }


class _Bridge:
    def __init__(self, path: Path, lane: str) -> None:
        self.__file__ = str(path)
        self.lane = lane
        self.bound = None
        self.stopped = threading.Event()

    def bind_capture_handoff(self, handoff) -> None:
        assert self.bound is None
        self.bound = handoff

    def unbind_capture_handoff(self, handoff) -> None:
        assert self.bound is handoff
        self.bound = None

    def run_supervised(
        self,
        *,
        stop_event,
        schema_ready_event,
        connected_event,
        ready_event,
        forced_symbols,
        reconnect_wait_seconds,
    ) -> None:
        assert self.bound is not None
        assert forced_symbols == ("VEEE",)
        assert reconnect_wait_seconds > 0
        schema_ready_event.set()
        connected_event.set()
        ready_event.set()
        stop_event.wait(timeout=2.0)
        ready_event.clear()
        connected_event.clear()
        self.stopped.set()


class _Health:
    def __init__(
        self,
        *,
        root: Path,
        l1_sha: str,
        l2_sha: str,
        exact_print_count: int = 1,
        observed_at: datetime = NOW,
    ) -> None:
        self.root = root
        self.l1_sha = l1_sha
        self.l2_sha = l2_sha
        self.exact_print_count = exact_print_count
        self.observed_at = observed_at

    def observe(self, *, composition, provider_health):
        assert isinstance(composition, _Composition)
        assert provider_health["all_ready"] is True
        return smoke.CaptureOnlyHealthObservation(
            observed_at=self.observed_at,
            capture_store_root=str(self.root),
            capture_store_probe_sha256="a" * 64,
            resource_binding_sha256=BINDING_SHA,
            l1_bridge_source_sha256=self.l1_sha,
            l2_bridge_source_sha256=self.l2_sha,
            capture_store_writable=True,
            exact_print_event_count=self.exact_print_count,
            exact_print_inventory_sha256="b" * 64,
            last_exact_print_available_at=(
                self.observed_at if self.exact_print_count else None
            ),
            dropped_event_count=0,
            overflow_count=0,
            unreported_gap_count=0,
        )


def _preflight(tmp_path: Path):
    source_paths = {}
    source_hashes = {}
    for role in (
        "iqfeed_capture_host",
        "iqfeed_trade_bridge",
        "iqfeed_depth_bridge",
        "iqfeed_l1_capture",
        "iqfeed_l2_capture",
    ):
        path = tmp_path / f"{role}.py"
        path.write_text(f"ROLE = {role!r}\n", encoding="utf-8")
        source_paths[role] = path
        source_hashes[role] = _sha(path)
    placeholder = tmp_path / "placeholder.json"
    placeholder.write_text("{}", encoding="utf-8")
    capture_root = tmp_path / "capture"
    capture_root.mkdir()
    return IqfeedCaptureBootstrapPreflight(
        manifest_path=placeholder,
        manifest_sha256=_sha(placeholder),
        startup_evidence_path=placeholder,
        startup_evidence_sha256=_sha(placeholder),
        resource_benchmark_path=placeholder,
        resource_benchmark_sha256=_sha(placeholder),
        resource_binding=SimpleNamespace(binding_sha256=BINDING_SHA),
        capture_store_root=capture_root,
        run_configuration={},
        handoff_configuration={},
        source_paths=source_paths,
        source_hashes=source_hashes,
        startup_evidence_hashes={},
        startup_captured_at=NOW,
        startup_process_instance_id="00000000-0000-0000-0000-000000000001",
        startup_generation=1,
        broker="alpaca",
        broker_environment="paper",
        bridge_configuration={},
        benchmark_authority_reasons=(),
    )


def _fixture(
    tmp_path: Path,
    *,
    exact_print_count: int = 1,
    observed_at: datetime = NOW,
):
    preflight = _preflight(tmp_path)
    trade = _Bridge(preflight.source_paths["iqfeed_trade_bridge"], "trade")
    depth = _Bridge(preflight.source_paths["iqfeed_depth_bridge"], "depth")
    health = _Health(
        root=preflight.capture_store_root,
        l1_sha=preflight.source_hashes["iqfeed_trade_bridge"],
        l2_sha=preflight.source_hashes["iqfeed_depth_bridge"],
        exact_print_count=exact_print_count,
        observed_at=observed_at,
    )
    pressure = CapturePressureSample(
        observed_at=NOW,
        resource_binding_sha256=BINDING_SHA,
        cpu_percent=1.0,
        available_memory_bytes=1,
        disk_free_bytes=1,
        write_latency_milliseconds=1.0,
    )
    config = smoke.CaptureOnlySmokeConfiguration(
        preflight=preflight,
        pressure_sample=pressure,
        capture_health_authority=health,
        trade_forced_symbols=("VEEE",),
        depth_forced_symbols=("VEEE",),
        readiness_timeout_seconds=0.5,
        observation_timeout_seconds=0.05,
        join_timeout_seconds=0.5,
        reconnect_wait_seconds=0.01,
        trade_bridge=trade,
        depth_bridge=depth,
    )
    composition = _Composition(preflight)
    return config, composition, trade, depth


def test_capture_only_smoke_binds_real_shape_checks_exact_print_and_quiesces(tmp_path):
    config, composition, trade, depth = _fixture(tmp_path)

    evidence = smoke.run_capture_only_preactivation_smoke(
        config,
        wall_clock=lambda: NOW,
        composition_factory=lambda *_args, **_kwargs: composition,
    )

    assert evidence.capture_health == {
        "capture_store_writable": True,
        "capture_store_probe_sha256": "a" * 64,
        "dropped_event_count": 0,
        "overflow_count": 0,
        "unreported_gap_count": 0,
    }
    assert evidence.provider_health["exact_print_clock_observed"] is True
    assert evidence.closure == {
        "provider_state": "stopped",
        "trade_thread_alive": False,
        "depth_thread_alive": False,
        "bridges_unbound": True,
        "orders_submitted": False,
    }
    assert evidence.host_binding["execution_surface"] == "capture_only"
    assert evidence.host_binding["order_transport_constructed"] is False
    assert trade.bound is None and depth.bound is None
    assert trade.stopped.is_set() and depth.stopped.is_set()
    assert composition.state is IqfeedIngressCompositionState.CLOSED
    assert len(evidence.evidence_sha256) == 64


def test_capture_only_smoke_missing_current_exact_print_fails_closed_and_stops(tmp_path):
    config, composition, trade, depth = _fixture(
        tmp_path, exact_print_count=0
    )

    with pytest.raises(smoke.CaptureOnlySmokeError, match="EXACT_PRINT_UNAVAILABLE"):
        smoke.run_capture_only_preactivation_smoke(
            config,
            wall_clock=lambda: NOW,
            composition_factory=lambda *_args, **_kwargs: composition,
        )

    assert trade.bound is None and depth.bound is None
    assert trade.stopped.is_set() and depth.stopped.is_set()
    assert composition.state is IqfeedIngressCompositionState.CLOSED


def test_closed_session_activation_smoke_starts_without_claiming_exact_print(tmp_path):
    closed = datetime(2026, 7, 17, 0, 10, tzinfo=UTC)
    config, composition, trade, depth = _fixture(
        tmp_path,
        exact_print_count=0,
        observed_at=closed,
    )
    config = replace(
        config,
        activation_only_allow_closed_session_without_exact_print=True,
    )

    evidence = smoke.run_capture_only_preactivation_smoke(
        config,
        wall_clock=lambda: closed,
        composition_factory=lambda *_args, **_kwargs: composition,
    )

    assert evidence.provider_health["exact_print_clock_observed"] is False
    assert evidence.provider_health[
        "activation_only_closed_session_without_exact_print"
    ] is True
    assert evidence.provider_health["last_exact_print_available_at"] is None
    assert trade.bound is None and depth.bound is None
    assert composition.state is IqfeedIngressCompositionState.CLOSED


def test_l1_exact_print_preselection_never_constructs_or_requires_depth_provider(
    tmp_path,
):
    config, composition, trade, depth = _fixture(tmp_path)
    config = smoke.CaptureOnlySmokeConfiguration(
        preflight=config.preflight,
        pressure_sample=config.pressure_sample,
        capture_health_authority=config.capture_health_authority,
        trade_forced_symbols=config.trade_forced_symbols,
        depth_forced_symbols=(),
        l1_only_exact_print_preselection=True,
        readiness_timeout_seconds=config.readiness_timeout_seconds,
        observation_timeout_seconds=config.observation_timeout_seconds,
        join_timeout_seconds=config.join_timeout_seconds,
        reconnect_wait_seconds=config.reconnect_wait_seconds,
        trade_bridge=trade,
        depth_bridge=None,
    )

    evidence = smoke.run_capture_only_preactivation_smoke(
        config,
        wall_clock=lambda: NOW,
        composition_factory=lambda *_args, **_kwargs: composition,
    )

    assert evidence.schema_version == (
        "chili.iqfeed-l1-exact-print-preselection-smoke.v1"
    )
    assert evidence.host_binding["provider_scope"] == "l1_exact_print_preselection"
    assert evidence.host_binding["trade_bridge_bound"] is True
    assert evidence.host_binding["depth_bridge_bound"] is False
    assert evidence.host_binding["l2_snapshot_completion_required"] is False
    assert evidence.host_binding["l2_decision_coverage_policy"] == (
        "decision_local_fail_closed"
    )
    assert evidence.provider_health["depth_provider_started"] is False
    assert evidence.closure["l2_opportunity_consumed"] is False
    assert evidence.closure["l2_risk_reserved"] is False
    assert trade.stopped.is_set()
    assert depth.stopped.is_set() is False
    assert depth.bound is None
    assert composition.l2_handoff.started is False
    assert composition.state is IqfeedIngressCompositionState.CLOSED
    payload = evidence.to_dict()
    embedded = dict(payload)
    digest = embedded.pop("evidence_sha256")
    assert digest == hashlib.sha256(
        smoke._canonical_json_bytes(embedded)
    ).hexdigest()


def test_capture_only_smoke_rejects_installed_hot_run_surface_before_provider_start(
    tmp_path, monkeypatch
):
    config, composition, trade, depth = _fixture(tmp_path)
    original = composition.health
    monkeypatch.setattr(
        composition,
        "health",
        lambda: {**original(), "hot_run_factory_installed": True},
    )

    with pytest.raises(smoke.CaptureOnlySmokeError, match="EXECUTION_SURFACE_PRESENT"):
        smoke.run_capture_only_preactivation_smoke(
            config,
            wall_clock=lambda: NOW,
            composition_factory=lambda *_args, **_kwargs: composition,
        )

    assert trade.bound is None and depth.bound is None
    assert not trade.stopped.is_set() and not depth.stopped.is_set()
    assert composition.state is IqfeedIngressCompositionState.CLOSED


def test_concrete_health_authority_uses_non_destructive_ring_and_store_marker(tmp_path):
    preflight = _preflight(tmp_path)

    class Ring:
        def __init__(self):
            self.aborted = None

        def begin_promotion(self, symbol, *, promoted_at, source_identity):
            assert symbol == "VEEE"
            assert promoted_at == NOW
            assert source_identity == "fixture-identity"
            clocks = SimpleNamespace(
                provider_event_at=NOW,
                available_at=NOW,
            )
            event = SimpleNamespace(
                stream=CaptureStream.IQFEED_PRINT,
                clocks=clocks,
                event_sha256="c" * 64,
            )
            return SimpleNamespace(
                events=(event,),
                gaps=(),
                inventory_sha256="d" * 64,
            )

        def abort_promotion(self, transfer):
            self.aborted = transfer
            return True

    ring = Ring()
    composition = SimpleNamespace(
        supervisor=SimpleNamespace(
            pretrigger_ring=ring,
            identity="fixture-identity",
        )
    )
    authority = smoke.IngressCaptureOnlyHealthAuthority(
        preflight=preflight,
        certification_symbol="veee",
        wall_clock=lambda: NOW,
    )

    observed = authority.observe(
        composition=composition,
        provider_health={"all_ready": True},
    )

    assert observed.exact_print_event_count == 1
    assert len(observed.capture_store_probe_sha256) == 64
    assert len(observed.exact_print_inventory_sha256) == 64
    assert observed.last_exact_print_available_at == NOW
    assert observed.unreported_gap_count == 0
    assert ring.aborted is not None
    markers = list(
        (preflight.capture_store_root / ".preactivation-capture-smoke").glob(
            "*.json"
        )
    )
    assert len(markers) == 1
    marker = json.loads(markers[0].read_text(encoding="utf-8"))
    assert marker["resource_binding_sha256"] == BINDING_SHA


def test_v3_capture_authority_accepts_only_typed_quiesced_smoke(tmp_path):
    config, composition, _trade, _depth = _fixture(tmp_path)
    evidence = smoke.run_capture_only_preactivation_smoke(
        config,
        wall_clock=lambda: NOW,
        composition_factory=lambda *_args, **_kwargs: composition,
    )
    authority = probes.CaptureOnlySmokeReadAuthority(lambda: evidence)

    native = authority.observe()

    assert type(native) is probes.CaptureHostNativeObservation
    assert native.host_binding["trade_bridge_bound"] is True
    assert native.provider_health["socket_readable"] is True


def test_standalone_import_and_cli_fail_closed_before_app_settings_or_forbidden_imports():
    command = (
        "import json,runpy,sys; "
        "sys.path.insert(0, r'" + str(REPO) + "'); "
        "import scripts.iqfeed_capture_only_smoke as m; "
        "forbidden=[n for n in sys.modules if "
        "n.endswith('captured_paper_dispatcher') or "
        "n.endswith('live_runner_loop') or "
        "n.endswith('captured_paper_transport_coordinator') or "
        "n.endswith('captured_alpaca_paper_adapter') or "
        "n.endswith('finalize_captured_paper_activation')]; "
        "print(json.dumps({'forbidden':forbidden,'exit':m.main([])}))"
    )
    environment = dict(os.environ)
    environment.pop("DATABASE_URL", None)
    completed = subprocess.run(
        [sys.executable, "-B", "-c", command],
        cwd=REPO,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )

    assert completed.returncode == 0
    result = json.loads(completed.stdout.strip())
    assert result == {"forbidden": [], "exit": 2}
    error = json.loads(completed.stderr.strip())
    assert error["error_code"] == "CONFIGURATION_UNAVAILABLE"
