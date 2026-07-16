from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from app.services.trading.momentum_neural import (
    captured_paper_dispatcher as dispatcher,
)
from app.services.trading.momentum_neural.captured_paper_service_supervisor import (
    CapturedPaperActiveStartAuthority,
    CapturedPaperManagedWorker,
    CapturedPaperServiceState,
    CapturedPaperServiceSupervisor,
    CapturedPaperServiceSupervisorError,
)


ACCOUNT_ID = "3e0776af-76cd-4afd-8fe1-f2ee8dc6242f"
GENERATION = "df0d0942-bbc0-4dc7-8218-ef387a8761db"


def _runtime():
    return dispatcher.CapturedPaperRuntime(
        handler=lambda *_args: None,
        expected_account_id=ACCOUNT_ID,
        code_build_sha256="a" * 64,
        config_sha256="b" * 64,
        capture_receipt_sha256="c" * 64,
        runtime_generation=GENERATION,
        first_dip_policy_mode="candidate",
    )


def _active_authority(events=None, *, verdict=True):
    def consume():
        if events is not None:
            events.append("active_authority_consume")
        return {
            "verdict": (
                "CAPTURED_ALPACA_PAPER_ACTIVE_START_AUTHORIZED"
                if verdict
                else "REJECTED"
            ),
            "account_scope": "alpaca:paper",
            "expected_account_id": ACCOUNT_ID,
            "runtime_generation": GENERATION,
            "paper_order_submission_authorized": True,
            "launcher_attestation_consumed": True,
            "host_activation_permit_sha256": "d" * 64,
            "host_activation_permit_consumed": True,
            "live_cash_authorized": False,
            "real_money_authorized": False,
        }

    return CapturedPaperActiveStartAuthority(
        expected_account_id=ACCOUNT_ID,
        runtime_generation=GENERATION,
        consume=consume,
        assert_current=lambda: None,
    )


def _fenced_prestart(events=None, *, drift=False, recovery_count=0):
    def revalidate():
        if events is not None:
            events.append("fenced_prestart_revalidate")
        body = {
            "schema_version": "chili.captured-paper-fenced-prestart.v1",
            "verdict": "CAPTURED_ALPACA_PAPER_FENCED_PRESTART_REVALIDATED",
            "account_scope": "alpaca:paper",
            "expected_account_id": ACCOUNT_ID,
            "runtime_generation": GENERATION,
            "baseline_restart_gate_receipt_sha256": "e" * 64,
            "restart_gate_receipt_sha256": "f" * 64,
            "admission_inventory_sha256": "1" * 64,
            "initial_recovery_count": recovery_count,
            "initial_recovery_inventory_sha256": "2" * 64,
            "durable_admission_drift": bool(drift),
            "broker_inventory_flat": True,
            "paper_execution_only": True,
            "live_cash_authorized": False,
            "real_money_authorized": False,
        }
        canonical = json.dumps(
            body,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return {
            **body,
            "receipt_sha256": hashlib.sha256(canonical).hexdigest(),
        }

    return revalidate


class _Handle:
    def __init__(self, events):
        self.events = events

    def close(self):
        self.events.append("runtime_close")


class _Host:
    def __init__(self, events):
        self.events = events
        self.running = False

    def start_provider_loops(self, **kwargs):
        self.events.append(("provider_start", dict(kwargs)))
        self.running = True
        return {"binding_receipt_sha256": "d" * 64}

    def health(self):
        return {
            "provider_loop_supervisor": {
                "state": "running" if self.running else "stopped",
                "all_ready": self.running,
                "provider_sockets_started": self.running,
                "failures": {},
            }
        }

    def close(self):
        self.events.append("host_close")
        self.running = False
        return self.health()


class _Worker:
    def __init__(self, name, events, *, fatal=False):
        self.name = name
        self.events = events
        self.running = False
        self.fatal = fatal

    def start(self):
        self.events.append(f"{self.name}_start")
        self.running = True

    def close(self, *, join_timeout_seconds):
        self.events.append((f"{self.name}_close", join_timeout_seconds))
        self.running = False

    def health(self):
        return {
            "ever_started": self.running or any(
                event == f"{self.name}_start" for event in self.events
            ),
            "running": self.running,
            "fatal": self.fatal,
        }


class _Fence:
    def __init__(self, events, *, acquire_error=None):
        self.events = events
        self.held = False
        self.acquire_error = acquire_error

    def acquire(self):
        self.events.append("service_fence_acquire")
        if self.acquire_error is not None:
            raise self.acquire_error
        self.held = True
        return {
            "account_scope": "alpaca:paper",
            "held": True,
            "live_cash_authorized": False,
            "real_money_authorized": False,
        }

    def assert_held(self):
        if not self.held:
            raise RuntimeError("service fence lost")

    def release(self):
        self.events.append("service_fence_release")
        self.held = False
        return {
            "account_scope": "alpaca:paper",
            "held": False,
            "live_cash_authorized": False,
            "real_money_authorized": False,
        }

    def health(self):
        return {
            "account_scope": "alpaca:paper",
            "held": self.held,
            "live_cash_authorized": False,
            "real_money_authorized": False,
        }


def _supervisor(events, *, workers=()):
    host = _Host(events)
    fence = _Fence(events)
    live = {"running": False}

    def register(_runtime_value):
        events.append("runtime_register")
        return _Handle(events)

    def start_live():
        events.append("live_start")
        live["running"] = True
        return True

    def stop_live():
        events.append("live_stop")
        live["running"] = False
        return True

    supervisor = CapturedPaperServiceSupervisor(
        host=host,
        runtime=_runtime(),
        service_fence=fence,
        fenced_prestart_revalidate=_fenced_prestart(events),
        managed_workers=tuple(
            CapturedPaperManagedWorker(name=name, worker=worker)
            for name, worker in workers
        ),
        live_loop_start=start_live,
        live_loop_stop=stop_live,
        live_loop_health=lambda: live["running"],
        runtime_registrar=register,
    )
    return supervisor, host, live


def test_active_start_order_puts_live_ticks_after_durable_workers():
    events = []
    transport = _Worker("transport", events)
    fills = _Worker("fills", events)
    supervisor, _host, _live = _supervisor(
        events,
        workers=(("transport", transport), ("fills", fills)),
    )

    health = supervisor.start_active(
        start_authority=_active_authority(events),
        provider_options={"readiness_timeout_seconds": 10.0}
    )

    assert events == [
        "service_fence_acquire",
        "fenced_prestart_revalidate",
        ("provider_start", {"readiness_timeout_seconds": 10.0}),
        "runtime_register",
        "active_authority_consume",
        "transport_start",
        "fills_start",
        "live_start",
    ]
    assert health["state"] == "active"
    assert health["live_cash_authorized"] is False
    assert health["real_money_authorized"] is False

    supervisor.close(join_timeout_seconds=1.0, quiesce_timeout_seconds=1.0)
    assert events[-6:] == [
        "live_stop",
        ("fills_close", 1.0),
        ("transport_close", 1.0),
        "runtime_close",
        "host_close",
        "service_fence_release",
    ]


def test_revoked_authority_stops_before_next_worker_or_live_loop():
    events = []
    first = _Worker("first", events)
    second = _Worker("second", events)
    supervisor, host, live = _supervisor(
        events,
        workers=(("first", first), ("second", second)),
    )
    checks = 0
    base = _active_authority(events)

    def assert_current():
        nonlocal checks
        checks += 1
        if checks == 2:
            raise RuntimeError("host activation revoked")

    authority = CapturedPaperActiveStartAuthority(
        expected_account_id=ACCOUNT_ID,
        runtime_generation=GENERATION,
        consume=base.consume,
        assert_current=assert_current,
    )
    with pytest.raises(RuntimeError, match="revoked"):
        supervisor.start_active(start_authority=authority)

    assert "first_start" in events
    assert "second_start" not in events
    assert "live_start" not in events
    assert first.running is False
    assert second.running is False
    assert live["running"] is False
    assert host.running is False


def test_no_order_smoke_structurally_omits_workers_and_live_loop():
    events = []
    transport = _Worker("transport", events)
    supervisor, _host, _live = _supervisor(
        events, workers=(("transport", transport),)
    )

    health = supervisor.start_no_order_smoke()

    assert events == [
        "service_fence_acquire",
        "fenced_prestart_revalidate",
        ("provider_start", {}),
        "runtime_register",
    ]
    assert health["state"] == "no_order_smoke"
    assert health["service_fence_acquired"] is True
    assert health["service_fence"]["held"] is True
    assert health["live_loop_started"] is False
    assert transport.running is False
    supervisor.close(join_timeout_seconds=1.0, quiesce_timeout_seconds=1.0)


def test_worker_health_failure_rolls_back_without_starting_live_loop():
    events = []
    bad = _Worker("transport", events, fatal=True)
    supervisor, host, live = _supervisor(
        events, workers=(("transport", bad),)
    )

    with pytest.raises(
        CapturedPaperServiceSupervisorError,
        match="transport_start_unconfirmed",
    ):
        supervisor.start_active(start_authority=_active_authority(events))

    assert "live_start" not in events
    assert live["running"] is False
    assert host.running is False
    assert supervisor.state is CapturedPaperServiceState.STOPPED


def test_final_authority_rejection_occurs_after_runtime_but_before_any_worker():
    events = []
    worker = _Worker("transport", events)
    supervisor, host, live = _supervisor(
        events, workers=(("transport", worker),)
    )

    with pytest.raises(
        CapturedPaperServiceSupervisorError,
        match="active_start_authority_rejected",
    ):
        supervisor.start_active(
            start_authority=_active_authority(events, verdict=False)
        )

    assert events[:5] == [
        "service_fence_acquire",
        "fenced_prestart_revalidate",
        ("provider_start", {}),
        "runtime_register",
        "active_authority_consume",
    ]
    assert "transport_start" not in events
    assert "live_start" not in events
    assert worker.running is False
    assert live["running"] is False
    assert host.running is False


def test_foreign_active_authority_fails_before_provider_start():
    events = []
    supervisor, host, _live = _supervisor(events)
    authority = CapturedPaperActiveStartAuthority(
        expected_account_id="4d08effa-21c2-4b2c-86ff-72b86af8a5dc",
        runtime_generation=GENERATION,
        consume=lambda: pytest.fail("foreign authority must not be consumed"),
        assert_current=lambda: None,
    )

    with pytest.raises(
        CapturedPaperServiceSupervisorError,
        match="active_start_authority_mismatch",
    ):
        supervisor.start_active(start_authority=authority)

    assert events == []
    assert host.running is False


def test_runtime_registration_failure_closes_provider():
    events = []
    host = _Host(events)

    def fail_register(_runtime_value):
        events.append("runtime_register_failed")
        raise RuntimeError("registration failed")

    supervisor = CapturedPaperServiceSupervisor(
        host=host,
        runtime=_runtime(),
        service_fence=_Fence(events),
        fenced_prestart_revalidate=_fenced_prestart(events),
        managed_workers=(),
        live_loop_start=lambda: pytest.fail("live loop must not start"),
        live_loop_stop=lambda: False,
        live_loop_health=lambda: False,
        runtime_registrar=fail_register,
    )
    with pytest.raises(RuntimeError, match="registration failed"):
        supervisor.start_active(start_authority=_active_authority(events))

    assert events == [
        "service_fence_acquire",
        "fenced_prestart_revalidate",
        ("provider_start", {}),
        "runtime_register_failed",
        "host_close",
        "service_fence_release",
    ]
    assert host.running is False


def test_active_health_loss_is_fail_closed_and_visible():
    events = []
    worker = _Worker("transport", events)
    supervisor, _host, live = _supervisor(
        events, workers=(("transport", worker),)
    )
    supervisor.start_active(start_authority=_active_authority(events))
    live["running"] = False

    with pytest.raises(
        CapturedPaperServiceSupervisorError,
        match="live_loop_health_lost",
    ):
        supervisor.assert_healthy()


def test_duplicate_worker_names_and_unknown_provider_options_reject():
    events = []
    worker = _Worker("transport", events)
    with pytest.raises(
        CapturedPaperServiceSupervisorError,
        match="duplicated",
    ):
        CapturedPaperServiceSupervisor(
            host=_Host(events),
            runtime=_runtime(),
            service_fence=_Fence(events),
            fenced_prestart_revalidate=_fenced_prestart(events),
            managed_workers=(
                CapturedPaperManagedWorker("transport", worker),
                CapturedPaperManagedWorker("transport", worker),
            ),
            live_loop_start=lambda: True,
            live_loop_stop=lambda: True,
            live_loop_health=lambda: True,
        )

    supervisor, _host, _live = _supervisor(events)
    with pytest.raises(
        CapturedPaperServiceSupervisorError,
        match="provider_options_invalid",
    ):
        supervisor.start_no_order_smoke(provider_options={"symbols": ["A"]})


def test_service_fence_failure_precedes_every_provider_runtime_or_worker_effect():
    events = []
    host = _Host(events)
    fence = _Fence(events, acquire_error=RuntimeError("held elsewhere"))
    supervisor = CapturedPaperServiceSupervisor(
        host=host,
        runtime=_runtime(),
        service_fence=fence,
        fenced_prestart_revalidate=_fenced_prestart(events),
        managed_workers=(),
        live_loop_start=lambda: pytest.fail("live loop must not start"),
        live_loop_stop=lambda: True,
        live_loop_health=lambda: False,
    )

    with pytest.raises(RuntimeError, match="held elsewhere"):
        supervisor.start_no_order_smoke()

    assert events == ["service_fence_acquire", "host_close"]
    assert host.running is False


def test_lost_service_fence_fails_health_and_clean_shutdown_releases_last():
    events = []
    supervisor, host, _live = _supervisor(events)
    supervisor.start_active(start_authority=_active_authority(events))
    fence = supervisor._service_fence
    fence.held = False

    with pytest.raises(RuntimeError, match="service fence lost"):
        supervisor.assert_healthy()

    # Restore the synthetic lock only so this unit test can exercise the normal
    # release-last ordering.  The real PostgreSQL fence invalidates its physical
    # connection when ownership is lost.
    fence.held = True
    supervisor.close(join_timeout_seconds=1.0, quiesce_timeout_seconds=1.0)
    assert host.running is False
    assert events[-1] == "service_fence_release"


def test_fenced_prestart_drift_rejects_before_provider_and_releases_last():
    events = []
    host = _Host(events)
    supervisor = CapturedPaperServiceSupervisor(
        host=host,
        runtime=_runtime(),
        service_fence=_Fence(events),
        fenced_prestart_revalidate=_fenced_prestart(events, drift=True),
        managed_workers=(),
        live_loop_start=lambda: pytest.fail("live loop must not start"),
        live_loop_stop=lambda: True,
        live_loop_health=lambda: False,
    )

    with pytest.raises(
        CapturedPaperServiceSupervisorError,
        match="fenced_prestart_revalidation_rejected",
    ):
        supervisor.start_no_order_smoke()

    assert events == [
        "service_fence_acquire",
        "fenced_prestart_revalidate",
        "host_close",
        "service_fence_release",
    ]
    assert host.running is False


def test_fenced_prestart_rejects_invalid_recovery_count_before_provider():
    events = []
    host = _Host(events)
    supervisor = CapturedPaperServiceSupervisor(
        host=host,
        runtime=_runtime(),
        service_fence=_Fence(events),
        fenced_prestart_revalidate=_fenced_prestart(
            events, recovery_count=-1
        ),
        managed_workers=(),
        live_loop_start=lambda: pytest.fail("live loop must not start"),
        live_loop_stop=lambda: True,
        live_loop_health=lambda: False,
    )

    with pytest.raises(
        CapturedPaperServiceSupervisorError,
        match="fenced_prestart_revalidation_rejected",
    ):
        supervisor.start_no_order_smoke()

    assert events == [
        "service_fence_acquire",
        "fenced_prestart_revalidate",
        "host_close",
        "service_fence_release",
    ]
    assert host.running is False
