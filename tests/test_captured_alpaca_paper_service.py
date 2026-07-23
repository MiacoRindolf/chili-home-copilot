from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
from types import MappingProxyType, SimpleNamespace
import importlib
import uuid

import pytest
import scripts.captured_alpaca_paper_service as service_module
from scripts import captured_paper_readiness_evidence as readiness_evidence

from scripts.captured_alpaca_paper_service import (
    CapturedAlpacaPaperServiceError,
    _CapturedPaperPolicyAuthority,
    _CapturedPaperServiceComposition,
    _PreparedCapturedPaperCapture,
    _assemble_service_composition,
    _assert_composition_broker_generation,
    _build_bracketed_restart_inventory_receipt,
    _build_fenced_prestart_revalidation_receipt,
    _build_policy_authority,
    _build_service_composition,
    _build_startup_evidence,
    _close_composition,
    _execute_no_order_smoke,
    _issue_post_smoke_refreshed_readiness,
    _no_order_smoke_receipt,
    _paper_broker_snapshot,
    _paper_broker_quiet_fixed_point,
    _paper_fill_activity_fence,
    _paper_kill_switch_snapshot,
    _paper_order_transition_fence,
    _publish_canonical_json_once,
    _recover_fenced_initial_generations,
    _strict_new_local_json_path,
    _validate_mode_arguments,
    _verify_database_schema,
    _verify_phase_one_reconciliation_receipt,
)
from scripts.captured_paper_activation_contract import sha256_json
from scripts.captured_paper_activation_contract import (
    VerifiedCapturedPaperPreactivation,
)


UTC = timezone.utc
NOW = datetime(2026, 7, 16, 6, 0, 0, tzinfo=UTC)
ACCOUNT = "3e0776af-76cd-4afd-8fe1-f2ee8dc6242f"
GENERATION = "df0d0942-bbc0-4dc7-8218-ef387a8761db"
SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


class _PaperAdapter:
    broker_environment = "paper"

    def __init__(
        self,
        *,
        orders: list[dict] | None = None,
        transition_orders: list[dict] | None = None,
        fill_activities: list[dict] | None = None,
    ) -> None:
        self.orders = list(orders or [])
        self.transition_orders = list(transition_orders or [])
        self.fill_activities = list(fill_activities or [])
        self.bound_account_id: str | None = None
        self.generation = "alpaca-paper-rest:" + "d" * 64
        self.audit_generation = "9cf6d0c5-614d-449c-a1e9-c21e3643d69c"
        self.submission_call_count = 0
        self.submission_chain_sha256 = hashlib.sha256(b"empty").hexdigest()

    def bind_account_id(self, account_id: str) -> bool:
        if account_id != ACCOUNT:
            return False
        self.bound_account_id = account_id
        return True

    def get_account_snapshot(self):
        return {
            "ok": True,
            "paper": True,
            "account_id": ACCOUNT,
            "status": "ACTIVE",
            "account_blocked": False,
            "trading_blocked": False,
            "transfers_blocked": False,
            "trade_suspended_by_user": False,
            "equity": 100_000.0,
            "last_equity": 99_000.0,
            "buying_power": 400_000.0,
            "cash": 100_000.0,
            "retrieved_at_utc": NOW.isoformat(),
        }

    def get_paper_connection_generation_receipt(self):
        body = {
            "schema_version": "chili.alpaca-paper-connection-generation.v1",
            "broker_environment": "paper",
            "asset_class": "us_equity",
            "provider_account_id": ACCOUNT,
            "adapter_connection_generation": self.generation,
            "adapter_build_sha256": SHA_A,
            "available_at": NOW.isoformat(),
        }
        encoded = _canonical(body)
        return {
            **body,
            "receipt_canonical_json": encoded,
            "receipt_sha256": hashlib.sha256(encoded.encode()).hexdigest(),
        }

    def list_positions(self):
        return [], object()

    def get_order_submission_audit_snapshot(self):
        body = {
            "schema_version": "chili.alpaca-paper-order-submission-audit.v1",
            "broker_environment": "paper",
            "asset_class": "us_equity",
            "provider_account_id": ACCOUNT,
            "adapter_connection_generation": self.generation,
            "adapter_build_sha256": SHA_A,
            "audit_generation": self.audit_generation,
            "submission_call_count": self.submission_call_count,
            "submission_chain_sha256": self.submission_chain_sha256,
        }
        encoded = _canonical(body)
        return {
            **body,
            "snapshot_canonical_json": encoded,
            "snapshot_sha256": hashlib.sha256(encoded.encode()).hexdigest(),
        }

    def get_paper_open_order_census(self, *, read_binding):
        assert read_binding["expected_account_id"] == ACCOUNT
        empty_hash = hashlib.sha256(b"[]").hexdigest()
        return {
            "readable": True,
            "pagination_complete": True,
            "broker_environment": "paper",
            "asset_class": "us_equity",
            "provider_account_id": ACCOUNT,
            "adapter_connection_generation": self.generation,
            "orders": list(self.orders),
            "inventory_sha256": empty_hash,
            "query_receipt_sha256": SHA_B,
        }

    def get_paper_order_transition_census(
        self, *, after, until, read_binding
    ):
        assert after < until
        encoded = _canonical(self.transition_orders)
        inventory_sha256 = hashlib.sha256(encoded.encode()).hexdigest()
        return {
            "readable": True,
            "pagination_complete": True,
            "broker_environment": "paper",
            "asset_class": "us_equity",
            "provider_account_id": ACCOUNT,
            "adapter_connection_generation": self.generation,
            "read_binding_sha256": sha256_json(read_binding),
            "orders": list(self.transition_orders),
            "inventory_sha256": inventory_sha256,
            "query_receipt_sha256": SHA_C,
            "available_at": NOW,
        }

    def get_paper_account_fill_activity_census(
        self, *, after, until, read_binding
    ):
        assert after < until
        encoded = _canonical(self.fill_activities)
        inventory_sha256 = hashlib.sha256(encoded.encode()).hexdigest()
        return {
            "readable": True,
            "pagination_complete": True,
            "broker_environment": "paper",
            "activity_scope": "all_account_fill_activities",
            "provider_account_id": ACCOUNT,
            "adapter_connection_generation": self.generation,
            "read_binding_sha256": sha256_json(read_binding),
            "activities": list(self.fill_activities),
            "inventory_sha256": inventory_sha256,
            "query_receipt_sha256": SHA_C,
            "query_after": after.isoformat(),
            "query_until": until.isoformat(),
            "available_at": NOW,
        }


def _verified_stub() -> SimpleNamespace:
    return SimpleNamespace(
        expected_account_id=ACCOUNT,
        activation_generation=GENERATION,
        manifest_sha256=SHA_A,
        generated_at=NOW - timedelta(seconds=5),
    )


def _active_start_authority_fixture(
    verified,
    *,
    permit_sha256: str,
    quiet_horizon_sha256: str,
) -> dict[str, object]:
    snapshot = {
        "position_count": 0,
        "open_order_count": 0,
        "order_submission_call_count": 0,
    }
    order_census = {"exact_order_count": 0}
    fill_census = {"exact_activity_count": 0}
    broker = {
        "schema_version": "chili.captured-paper-broker-fixed-point.v1",
        "verdict": "PAPER_BROKER_QUIET_FIXED_POINT",
        "account_scope": "alpaca:paper",
        "expected_account_id": verified.expected_account_id,
        "activation_generation": verified.activation_generation,
        "activation_manifest_sha256": verified.manifest_sha256,
        "assumption_bound": True,
        "live_cash_certification": False,
        "baseline_snapshot": snapshot,
        "first_snapshot": snapshot,
        "first_order_census": order_census,
        "first_fill_activity_census": fill_census,
        "second_snapshot": snapshot,
        "second_order_census": order_census,
        "second_fill_activity_census": fill_census,
    }
    kill_body = {
        "schema_version": "chili.captured-paper-kill-switch-query.v1",
        "activation_generation": verified.activation_generation,
        "account_scope": "alpaca:paper",
        "expected_account_id": verified.expected_account_id,
        "active": False,
    }
    final_kill = {
        **kill_body,
        "query_receipt_sha256": sha256_json(kill_body),
    }
    body = {
        "schema_version": "chili.captured-paper-active-start-authority.v2",
        "verdict": "CAPTURED_ALPACA_PAPER_ACTIVE_START_AUTHORIZED",
        "account_scope": "alpaca:paper",
        "expected_account_id": verified.expected_account_id,
        "runtime_generation": verified.activation_generation,
        "activation_manifest_sha256": verified.manifest_sha256,
        "kill_switch_receipt_sha256": SHA_B,
        "launcher_attestation_sha256": SHA_C,
        "launcher_attestation_consumed": True,
        "host_activation_permit_sha256": permit_sha256,
        "host_activation_permit_consumed": True,
        "host_quiet_horizon_event_sha256": quiet_horizon_sha256,
        "broker_fixed_point": broker,
        "broker_fixed_point_sha256": sha256_json(broker),
        "post_permit_broker_snapshot_sha256": sha256_json(snapshot),
        "order_transition_fence_sha256": sha256_json(order_census),
        "fill_activity_fence_sha256": sha256_json(fill_census),
        "final_kill_switch_query": final_kill,
        "final_kill_switch_query_sha256": sha256_json(final_kill),
        "paper_order_submission_authorized": True,
        "live_cash_authorized": False,
        "real_money_authorized": False,
    }
    body["authority_sha256"] = sha256_json(body)
    return body


def _preactivation(tmp_path: Path) -> VerifiedCapturedPaperPreactivation:
    return VerifiedCapturedPaperPreactivation(
        manifest_path=tmp_path / "preactivation.json",
        manifest_sha256=SHA_A,
        activation_generation=GENERATION,
        expected_account_id=ACCOUNT,
        code_build_sha256=SHA_B,
        effective_config_sha256=SHA_C,
        capture_receipt_sha256="d" * 64,
        source_paths=MappingProxyType({}),
        source_hashes=MappingProxyType(
            {
                "captured_alpaca_paper_adapter": "1" * 64,
                "activation_service": "2" * 64,
            }
        ),
        receipt_paths=MappingProxyType({}),
        receipt_hashes=MappingProxyType({}),
        launcher_path=tmp_path / "launcher.ps1",
        launcher_sha256="e" * 64,
        candidate_root=tmp_path,
        capture_store_root=tmp_path,
        iqfeed_bootstrap_manifest_path=tmp_path / "bootstrap.json",
        iqfeed_bootstrap_manifest_sha256="f" * 64,
        generated_at=NOW - timedelta(seconds=5),
        expires_at=NOW + timedelta(minutes=5),
        manifest=MappingProxyType(
            {
                "runtime_environment": {
                    "runtime_environment_sha256": "3" * 64,
                    "database_target_fingerprint": "4" * 64,
                },
                "cutover": {"launcher_arguments_sha256": "5" * 64},
            }
        ),
        envelope_stage="preactivation",
        paper_order_submission_authorized=False,
    )


def test_runtime_import_path_authority_removes_only_pinned_candidate_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    loaded_source = candidate / "loaded.py"
    loaded_source.write_text("VALUE = 1\n", encoding="utf-8")
    dependency = tmp_path / "dependencies" / ("d" * 64) / "site-packages"
    dependency.mkdir(parents=True)
    unrelated = tmp_path / "stdlib"
    unrelated.mkdir()
    role = "loaded_source"
    verified = SimpleNamespace(
        candidate_root=candidate,
        source_paths={role: loaded_source},
        source_hashes={role: hashlib.sha256(loaded_source.read_bytes()).hexdigest()},
        manifest={"cutover": {"python_dependency_root": str(dependency)}},
    )
    module_name = "_captured_paper_test_loaded_source"
    monkeypatch.setitem(
        sys.modules,
        module_name,
        SimpleNamespace(__file__=str(loaded_source)),
    )
    monkeypatch.setattr(
        sys,
        "path",
        [str(unrelated), str(candidate), str(dependency), str(dependency)],
    )

    service_module._restore_runtime_import_path_authority(verified)

    assert sys.path == [str(unrelated), str(dependency)]


def test_runtime_import_path_authority_rejects_unpinned_loaded_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    unpinned = candidate / "unreviewed.py"
    unpinned.write_text("VALUE = 2\n", encoding="utf-8")
    dependency = tmp_path / "dependencies" / ("e" * 64) / "site-packages"
    dependency.mkdir(parents=True)
    verified = SimpleNamespace(
        candidate_root=candidate,
        source_paths={},
        source_hashes={},
        manifest={"cutover": {"python_dependency_root": str(dependency)}},
    )
    monkeypatch.setitem(
        sys.modules,
        "_captured_paper_test_unpinned_source",
        SimpleNamespace(__file__=str(unpinned)),
    )
    monkeypatch.setattr(sys, "path", [str(dependency), str(candidate)])

    with pytest.raises(
        CapturedAlpacaPaperServiceError,
        match="loaded candidate module is absent from the sealed source roster",
    ):
        service_module._restore_runtime_import_path_authority(verified)


def _started_health() -> dict:
    return {
        "state": "no_order_smoke",
        "runtime_registered": True,
        "live_loop_started": False,
        "managed_workers": {
            "transport": {
                "ever_started": False,
                "running": False,
                "fatal": False,
            },
            "later_fill": {
                "ever_started": False,
                "running": False,
                "fatal": False,
            },
        },
        "host": {
            "provider_loop_supervisor": {
                "state": "running",
                "all_ready": True,
                "provider_sockets_started": True,
                "failures": {},
            }
        },
    }


def _phase_one_restart_receipt(
    *, committed: tuple[str, ...] = (), unavailable: tuple[str, ...] = ()
) -> dict:
    body = {
        "schema_version": (
            "chili.captured-paper-phase-one-restart-reconciliation.v1"
        ),
        "activation_generation": GENERATION,
        "initial_pending_count": len(committed) + len(unavailable),
        "remaining_pending_count": 0,
        "reconciliation_complete": True,
        "outbox_committed_count": len(committed),
        "decision_handoff_unavailable_count": len(unavailable),
        "outbox_committed_completion_sha256s": sorted(committed),
        "decision_handoff_unavailable_completion_sha256s": sorted(
            unavailable
        ),
        "phase_two_side_effects_inferred": False,
    }
    return {
        **body,
        "receipt_sha256": hashlib.sha256(_canonical(body).encode()).hexdigest(),
    }


class _BracketAdapter:
    def __init__(self, events: list[str], *, reuse_receipts: bool = False):
        self.events = events
        self.reuse_receipts = reuse_receipts
        self.counts = {"orders": 0, "positions": 0}

    def _census(self, kind: str, read_binding: MappingProxyType | dict):
        self.events.append(kind)
        self.counts[kind] += 1
        ordinal = 1 if self.reuse_receipts else self.counts[kind]
        return {
            kind: [],
            "inventory_sha256": hashlib.sha256(b"[]").hexdigest(),
            "query_receipt_sha256": hashlib.sha256(
                f"{kind}:{ordinal}".encode()
            ).hexdigest(),
            "read_binding": dict(read_binding),
        }

    def get_paper_open_order_census(self, *, read_binding):
        return self._census("orders", read_binding)

    def get_paper_position_census(self, *, read_binding):
        return self._census("positions", read_binding)


class _RestartInventoryModule:
    def __init__(self, events: list[str]):
        self.events = events

    def load_captured_paper_restart_lineages(self, _bind, **_kwargs):
        self.events.append("database")
        return ()

    @staticmethod
    def classify_captured_paper_restart_inventory(
        *,
        expected_account_id,
        expected_runtime_generation,
        expected_connection_generation,
        expected_adapter_build_sha256,
        expected_read_binding_sha256,
        open_order_census,
        position_census,
        durable_lineages,
        observed_at,
    ):
        assert durable_lineages == ()
        body = {
            "schema_version": "chili.captured-paper-restart-inventory.v1",
            "disposition": "strict_flat_first_cutover",
            "account_scope": "alpaca:paper",
            "expected_account_id": expected_account_id,
            "runtime_generation": expected_runtime_generation,
            "broker_connection_generation": expected_connection_generation,
            "broker_adapter_build_sha256": expected_adapter_build_sha256,
            "broker_read_binding_sha256": expected_read_binding_sha256,
            "open_order_census_sha256": open_order_census[
                "query_receipt_sha256"
            ],
            "open_order_inventory_sha256": open_order_census[
                "inventory_sha256"
            ],
            "position_census_sha256": position_census[
                "query_receipt_sha256"
            ],
            "position_inventory_sha256": position_census[
                "inventory_sha256"
            ],
            "durable_inventory_sha256": hashlib.sha256(b"[]").hexdigest(),
            "owned_open_orders": [],
            "owned_positions": [],
            "terminal_late_fill_quarantines": [],
            "recovery_required": False,
            "new_admissions_quarantined": False,
            "exposure_decreasing_only": False,
            "broker_inventory_flat": True,
            "observed_at": observed_at.isoformat().replace("+00:00", "Z"),
            "paper_execution_only": True,
            "live_cash_authorized": False,
            "real_money_authorized": False,
        }
        canonical = _canonical(body)
        return {
            **body,
            "receipt_canonical_json": canonical,
            "receipt_sha256": hashlib.sha256(canonical.encode()).hexdigest(),
        }


def _bracket_inputs(*, reuse_receipts: bool = False):
    events: list[str] = []
    connection_generation = "alpaca-paper-rest:" + "d" * 64
    adapter = _BracketAdapter(events, reuse_receipts=reuse_receipts)
    prepared = SimpleNamespace(
        adapter=adapter,
        broker_snapshot={
            "connection_receipt_sha256": SHA_C,
            "connection_receipt": {
                "adapter_connection_generation": connection_generation,
                "adapter_build_sha256": SHA_A,
            },
        },
    )
    verified = SimpleNamespace(
        expected_account_id=ACCOUNT,
        activation_generation=GENERATION,
        manifest_sha256=SHA_A,
        code_build_sha256=SHA_B,
        settings_projection_sha256=SHA_C,
        capture_receipt_sha256="d" * 64,
    )
    return events, prepared, verified


def _strict_restart_gate(verified, phase_one):
    events, prepared, _unused = _bracket_inputs()
    return _build_bracketed_restart_inventory_receipt(
        verified=verified,
        prepared=prepared,
        database_engine=object(),
        phase_one_reconciliation_receipt=phase_one,
        restart_inventory_module=_RestartInventoryModule(events),
        wall_clock=lambda: NOW,
    )


def _prestart_inventory(*, active_action_claims: int = 0) -> dict:
    counts = {
        "active_sessions": 0,
        "active_action_claims": int(active_action_claims),
        "active_reservations": 0,
        "reserved_opportunities": 0,
        "active_outbox_rows": 0,
        "active_fill_watches": 0,
    }
    body = {
        "schema_version": (
            "chili.captured-paper-prestart-admission-inventory.v1"
        ),
        "account_scope": "alpaca:paper",
        **counts,
        "active_total": sum(counts.values()),
        "empty": sum(counts.values()) == 0,
        "live_cash_authorized": False,
        "real_money_authorized": False,
    }
    canonical = _canonical(body)
    return {
        **body,
        "inventory_canonical_json": canonical,
        "inventory_sha256": hashlib.sha256(canonical.encode()).hexdigest(),
    }


def test_fenced_prestart_revalidation_repeats_flat_restart_under_lock() -> None:
    events, prepared, verified = _bracket_inputs()
    phase_one = _phase_one_restart_receipt()
    restart_module = _RestartInventoryModule(events)
    baseline = _build_bracketed_restart_inventory_receipt(
        verified=verified,
        prepared=prepared,
        database_engine=object(),
        phase_one_reconciliation_receipt=phase_one,
        restart_inventory_module=restart_module,
        wall_clock=lambda: NOW,
    )
    baseline_events = list(events)

    receipt = _build_fenced_prestart_revalidation_receipt(
        verified=verified,
        prepared=prepared,
        database_engine=object(),
        phase_one_reconciliation_receipt=phase_one,
        baseline_restart_inventory_receipt=baseline,
        restart_inventory_module=restart_module,
        service_fence_module=SimpleNamespace(
            read_captured_paper_prestart_admission_inventory=(
                lambda _bind: _prestart_inventory()
            )
        ),
        recover_initial_generations=lambda: (),
        wall_clock=lambda: NOW,
    )

    assert receipt["verdict"] == (
        "CAPTURED_ALPACA_PAPER_FENCED_PRESTART_REVALIDATED"
    )
    assert receipt["durable_admission_drift"] is False
    assert receipt["broker_inventory_flat"] is True
    assert receipt["initial_recovery_count"] == 0
    assert events[len(baseline_events):] == [
        "orders",
        "positions",
        "database",
        "positions",
        "orders",
    ]


def test_fenced_initial_recovery_queries_and_recovers_exact_bound_rows() -> None:
    events: list[object] = []

    class _Rows:
        @staticmethod
        def mappings():
            return _Rows()

        @staticmethod
        def all():
            return (
                {"id": 41, "symbol": " abcd "},
                {"id": 42, "symbol": "WXYZ"},
            )

    class _Connection:
        def __enter__(self):
            events.append("database_enter")
            return self

        def __exit__(self, *_args):
            events.append("database_exit")

        @staticmethod
        def execute(statement):
            sql = str(statement)
            assert "captured_paper_preowner" in sql
            assert "captured_paper_preowner_promotion" in sql
            events.append("inventory_query")
            return _Rows()

    class _Engine:
        @staticmethod
        def connect():
            return _Connection()

    engine = _Engine()

    def assert_fence_held() -> None:
        events.append("fence")

    def recover(bound_engine, **kwargs):
        assert bound_engine is engine
        events.append(("recover", dict(kwargs)))
        return SimpleNamespace(
            to_dict=lambda: {
                "schema_version": "test.initial-recovery.v1",
                "session_id": kwargs["session_id"],
            }
        )

    verified = SimpleNamespace(
        expected_account_id=ACCOUNT,
        activation_generation=GENERATION,
        code_build_sha256=SHA_B,
        capture_receipt_sha256="d" * 64,
    )
    prepared = SimpleNamespace(
        host=SimpleNamespace(
            captured_paper_config_sha256_for=lambda symbol: {
                "ABCD": "1" * 64,
                "WXYZ": "2" * 64,
            }[symbol]
        )
    )

    receipts = _recover_fenced_initial_generations(
        verified=verified,
        prepared=prepared,
        database_engine=engine,
        initial_recovery_module=SimpleNamespace(
            recover_captured_paper_initial_preowner=recover
        ),
        assert_service_fence_held=assert_fence_held,
    )

    assert receipts == (
        {"schema_version": "test.initial-recovery.v1", "session_id": 41},
        {"schema_version": "test.initial-recovery.v1", "session_id": 42},
    )
    recoveries = [event for event in events if isinstance(event, tuple)]
    assert [event[1]["session_id"] for event in recoveries] == [41, 42]
    assert [event[1]["expected_config_sha256"] for event in recoveries] == [
        "1" * 64,
        "2" * 64,
    ]
    for _name, arguments in recoveries:
        assert arguments["expected_account_id"] == ACCOUNT
        assert arguments["expected_runtime_generation"] == GENERATION
        assert arguments["expected_code_build_sha256"] == SHA_B
        assert arguments["expected_capture_receipt_sha256"] == "d" * 64
        assert arguments["assert_service_fence_held"] is assert_fence_held
    assert events[:4] == [
        "fence",
        "database_enter",
        "inventory_query",
        "database_exit",
    ]
    assert events[-1] == "fence"


def test_fenced_prestart_revalidation_rejects_bare_claim_before_broker_reads() -> None:
    events, prepared, verified = _bracket_inputs()
    phase_one = _phase_one_restart_receipt()
    restart_module = _RestartInventoryModule(events)
    baseline = _build_bracketed_restart_inventory_receipt(
        verified=verified,
        prepared=prepared,
        database_engine=object(),
        phase_one_reconciliation_receipt=phase_one,
        restart_inventory_module=restart_module,
        wall_clock=lambda: NOW,
    )
    event_count = len(events)

    with pytest.raises(
        CapturedAlpacaPaperServiceError,
        match="durable Alpaca arm/order owner appeared",
    ):
        _build_fenced_prestart_revalidation_receipt(
            verified=verified,
            prepared=prepared,
            database_engine=object(),
            phase_one_reconciliation_receipt=phase_one,
            baseline_restart_inventory_receipt=baseline,
            restart_inventory_module=restart_module,
            service_fence_module=SimpleNamespace(
                read_captured_paper_prestart_admission_inventory=(
                    lambda _bind: _prestart_inventory(
                        active_action_claims=1
                    )
                )
            ),
            recover_initial_generations=lambda: (),
            wall_clock=lambda: NOW,
        )

    assert len(events) == event_count


def _refreshed_readiness(
    preactivation: VerifiedCapturedPaperPreactivation,
    broker_snapshot: dict,
    *,
    observed_at: datetime = NOW,
) -> dict[str, dict]:
    result = _issue_post_smoke_refreshed_readiness(
        verified=preactivation,
        broker_snapshot=broker_snapshot,
        kill_switch_snapshot={
            "query_receipt_sha256": SHA_C,
            "state_version": 9,
            "observed_at": observed_at.isoformat(),
        },
        issued_at=observed_at,
    )
    return {kind: dict(value) for kind, value in result.items()}


def test_paper_broker_snapshot_binds_fresh_flat_exact_generation() -> None:
    result = _paper_broker_snapshot(
        _PaperAdapter(),
        verified=_verified_stub(),
        purpose="pre_smoke",
        wall_clock=lambda: NOW,
    )

    assert result["account_id"] == ACCOUNT
    assert result["account_equity"] == 100_000.0
    assert result["account_buying_power"] == 400_000.0
    assert result["broker_day_change"] == 1_000.0
    assert result["account_status"] == "ACTIVE"
    assert result["position_count"] == 0
    assert result["open_order_count"] == 0
    assert result["connection_generation"].startswith("alpaca-paper-rest:")


def test_paper_broker_snapshot_rejects_existing_open_order() -> None:
    with pytest.raises(CapturedAlpacaPaperServiceError, match="existing open orders"):
        _paper_broker_snapshot(
            _PaperAdapter(orders=[{"id": "broker-order"}]),
            verified=_verified_stub(),
            purpose="pre_smoke",
            wall_clock=lambda: NOW,
        )


def test_post_permit_order_transition_fence_binds_empty_all_status_census() -> None:
    verified = _verified_stub()
    adapter = _PaperAdapter()
    broker = _paper_broker_snapshot(
        adapter,
        verified=verified,
        purpose="post_permit",
        wall_clock=lambda: NOW,
    )

    fence = _paper_order_transition_fence(
        adapter,
        verified=verified,
        broker_snapshot=broker,
        after=verified.generated_at,
        until=NOW,
        purpose="post_permit",
        wall_clock=lambda: NOW,
    )

    assert fence["verdict"] == "NO_PAPER_ORDER_TRANSITION_DURING_CUTOVER"
    assert fence["account_scope"] == "alpaca:paper"
    assert fence["exact_order_count"] == 0


def test_post_permit_order_transition_fence_rejects_terminal_order() -> None:
    verified = _verified_stub()
    adapter = _PaperAdapter(
        transition_orders=[
            {
                "id": "legacy-terminal-order",
                "client_order_id": "legacy-terminal-cid",
                "status": "canceled",
            }
        ]
    )
    broker = _paper_broker_snapshot(
        adapter,
        verified=verified,
        purpose="post_permit",
        wall_clock=lambda: NOW,
    )

    with pytest.raises(
        CapturedAlpacaPaperServiceError,
        match="BROKER_RECENT_ORDER_TRANSITION_PRESENT",
    ):
        _paper_order_transition_fence(
            adapter,
            verified=verified,
            broker_snapshot=broker,
            after=verified.generated_at,
            until=NOW,
            purpose="post_permit",
            wall_clock=lambda: NOW,
        )


def test_post_permit_fill_activity_fence_binds_empty_paginated_census() -> None:
    verified = _verified_stub()
    adapter = _PaperAdapter()
    broker = _paper_broker_snapshot(
        adapter,
        verified=verified,
        purpose="post_permit_fill",
        wall_clock=lambda: NOW,
    )

    fence = _paper_fill_activity_fence(
        adapter,
        verified=verified,
        broker_snapshot=broker,
        after=verified.generated_at,
        until=NOW,
        purpose="post_permit_fill",
        wall_clock=lambda: NOW,
    )

    assert fence["verdict"] == "NO_PAPER_FILL_ACTIVITY_DURING_ACTIVATION"
    assert fence["exact_activity_count"] == 0


def test_post_permit_fill_activity_fence_rejects_delayed_fill() -> None:
    verified = _verified_stub()
    adapter = _PaperAdapter(
        fill_activities=[
            {
                "id": "fill-1",
                "order_id": "legacy-order",
                "activity_type": "FILL",
            }
        ]
    )
    broker = _paper_broker_snapshot(
        adapter,
        verified=verified,
        purpose="post_permit_fill",
        wall_clock=lambda: NOW,
    )

    with pytest.raises(
        CapturedAlpacaPaperServiceError,
        match="BROKER_RECENT_FILL_ACTIVITY_PRESENT",
    ):
        _paper_fill_activity_fence(
            adapter,
            verified=verified,
            broker_snapshot=broker,
            after=verified.generated_at,
            until=NOW,
            purpose="post_permit_fill",
            wall_clock=lambda: NOW,
        )


def test_final_broker_fixed_point_is_two_cycle_zero_post_and_fill(
    monkeypatch,
) -> None:
    verified = _verified_stub()
    adapter = _PaperAdapter()
    baseline = _paper_broker_snapshot(
        adapter,
        verified=verified,
        purpose="fixed_point_baseline",
        wall_clock=lambda: NOW,
    )
    composition = _CapturedPaperServiceComposition(
        supervisor=object(),
        shared_capture_store=object(),
        adapter=adapter,
        connection_generation_receipt=baseline["connection_receipt"],
        phase_one_reconciliation_receipt={},
        restart_inventory_receipt={},
        database_engine=object(),
        initial_broker_snapshot=baseline,
    )
    monotonic = [0.0]
    monkeypatch.setattr(
        service_module,
        "_no_order_smoke_order_window_start",
        lambda _verified: NOW - timedelta(seconds=5),
    )

    result = _paper_broker_quiet_fixed_point(
        adapter,
        verified=verified,
        composition=composition,
        baseline_snapshot=baseline,
        wall_clock=lambda: NOW,
        monotonic_clock=lambda: monotonic[0],
        wait=lambda seconds: monotonic.__setitem__(0, monotonic[0] + seconds),
    )

    assert result["verdict"] == "PAPER_BROKER_QUIET_FIXED_POINT"
    assert result["assumption_bound"] is True
    assert result["live_cash_certification"] is False
    assert result["first_order_census"]["exact_order_count"] == 0
    assert result["second_fill_activity_census"]["exact_activity_count"] == 0


def test_final_broker_fixed_point_rejects_local_submission_audit_drift(
    monkeypatch,
) -> None:
    verified = _verified_stub()
    adapter = _PaperAdapter()
    baseline = _paper_broker_snapshot(
        adapter,
        verified=verified,
        purpose="fixed_point_baseline",
        wall_clock=lambda: NOW,
    )
    adapter.submission_call_count = 1
    composition = _CapturedPaperServiceComposition(
        supervisor=object(),
        shared_capture_store=object(),
        adapter=adapter,
        connection_generation_receipt=baseline["connection_receipt"],
        phase_one_reconciliation_receipt={},
        restart_inventory_receipt={},
        database_engine=object(),
        initial_broker_snapshot=baseline,
    )
    monotonic = [0.0]
    monkeypatch.setattr(
        service_module,
        "_no_order_smoke_order_window_start",
        lambda _verified: NOW - timedelta(seconds=5),
    )

    with pytest.raises(
        CapturedAlpacaPaperServiceError,
        match="BROKER_FIXED_POINT_NOT_QUIET",
    ):
        _paper_broker_quiet_fixed_point(
            adapter,
            verified=verified,
            composition=composition,
            baseline_snapshot=baseline,
            wall_clock=lambda: NOW,
            monotonic_clock=lambda: monotonic[0],
            wait=lambda seconds: monotonic.__setitem__(
                0, monotonic[0] + seconds
            ),
        )


def test_post_smoke_kill_switch_snapshot_is_direct_durable_and_fail_closed(
    tmp_path: Path,
) -> None:
    class _Result:
        def __init__(self, row):
            self._row = row

        def fetchone(self):
            return self._row

    class _Connection:
        def __init__(self, row):
            self._row = row

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, statement):
            assert "regime = 'kill_switch'" in str(statement)
            return _Result(self._row)

    class _Engine:
        def __init__(self, row):
            self._row = row

        def connect(self):
            return _Connection(self._row)

    verified = _preactivation(tmp_path)
    result = _paper_kill_switch_snapshot(
        _Engine((9, False, None, NOW - timedelta(seconds=1))),
        verified=verified,
        wall_clock=lambda: NOW,
    )
    assert result["state_version"] == 9
    assert result["active"] is False
    assert result["query_receipt_sha256"] == sha256_json(
        {key: value for key, value in result.items() if key != "query_receipt_sha256"}
    )

    with pytest.raises(CapturedAlpacaPaperServiceError, match="forbids"):
        _paper_kill_switch_snapshot(
            _Engine((10, True, "manual", NOW)),
            verified=verified,
            wall_clock=lambda: NOW,
        )
    with pytest.raises(CapturedAlpacaPaperServiceError, match="no durable"):
        _paper_kill_switch_snapshot(
            _Engine(None),
            verified=verified,
            wall_clock=lambda: NOW,
        )


def test_fresh_broker_reread_must_keep_composition_generation() -> None:
    adapter = _PaperAdapter()
    first = _paper_broker_snapshot(
        adapter,
        verified=_verified_stub(),
        purpose="composition",
        wall_clock=lambda: NOW,
    )
    composition = _CapturedPaperServiceComposition(
        supervisor=object(),
        shared_capture_store=object(),
        adapter=adapter,
        connection_generation_receipt=first["connection_receipt"],
        phase_one_reconciliation_receipt={},
        restart_inventory_receipt={},
        database_engine=object(),
    )
    _assert_composition_broker_generation(composition, first)

    adapter.generation = "alpaca-paper-rest:" + "e" * 64
    rotated = _paper_broker_snapshot(
        adapter,
        verified=_verified_stub(),
        purpose="final_fence",
        wall_clock=lambda: NOW,
    )
    with pytest.raises(CapturedAlpacaPaperServiceError, match="generation changed"):
        _assert_composition_broker_generation(composition, rotated)


def test_phase_one_restart_receipt_is_exhaustive_and_content_addressed() -> None:
    receipt = _phase_one_restart_receipt(
        committed=("1" * 64,), unavailable=("2" * 64,)
    )
    verified = _verify_phase_one_reconciliation_receipt(
        receipt, activation_generation=GENERATION
    )
    assert verified == receipt

    forged = dict(receipt)
    forged["remaining_pending_count"] = 1
    body = dict(forged)
    body.pop("receipt_sha256")
    forged["receipt_sha256"] = hashlib.sha256(
        _canonical(body).encode()
    ).hexdigest()
    with pytest.raises(
        CapturedAlpacaPaperServiceError, match="did not exhaust"
    ):
        _verify_phase_one_reconciliation_receipt(
            forged, activation_generation=GENERATION
        )


def test_restart_inventory_brackets_durable_snapshot_with_fresh_reverse_census() -> None:
    events, prepared, verified = _bracket_inputs()
    receipt = _build_bracketed_restart_inventory_receipt(
        verified=verified,
        prepared=prepared,
        database_engine=object(),
        phase_one_reconciliation_receipt=_phase_one_restart_receipt(),
        restart_inventory_module=_RestartInventoryModule(events),
        wall_clock=lambda: NOW,
    )

    assert events == ["orders", "positions", "database", "positions", "orders"]
    assert receipt["disposition"] == "strict_flat_first_cutover"
    assert receipt["recovery_required"] is False
    assert receipt["broker_inventory_flat"] is True
    assert receipt["opening_open_order_census_sha256"] != receipt[
        "closing_open_order_census_sha256"
    ]
    assert receipt["opening_position_census_sha256"] != receipt[
        "closing_position_census_sha256"
    ]
    assert hashlib.sha256(
        receipt["receipt_canonical_json"].encode()
    ).hexdigest() == receipt["receipt_sha256"]


def test_restart_inventory_rejects_reused_census_receipts() -> None:
    events, prepared, verified = _bracket_inputs(reuse_receipts=True)
    with pytest.raises(
        CapturedAlpacaPaperServiceError, match="reused across the durable read"
    ):
        _build_bracketed_restart_inventory_receipt(
            verified=verified,
            prepared=prepared,
            database_engine=object(),
            phase_one_reconciliation_receipt=_phase_one_restart_receipt(),
            restart_inventory_module=_RestartInventoryModule(events),
            wall_clock=lambda: NOW,
        )


def test_no_order_receipt_requires_workers_and_live_loop_never_started(
    tmp_path: Path,
) -> None:
    preactivation = _preactivation(tmp_path)
    phase_one = _phase_one_restart_receipt()
    restart_gate = _strict_restart_gate(preactivation, phase_one)
    before = _paper_broker_snapshot(
        _PaperAdapter(),
        verified=_verified_stub(),
        purpose="before",
        wall_clock=lambda: NOW,
    )
    after = dict(before)
    stopped = {
        "state": "stopped",
        "runtime_registered": False,
        "live_loop_started": False,
    }
    receipt = _no_order_smoke_receipt(
        verified=preactivation,
        phase_one_reconciliation_receipt=phase_one,
        restart_inventory_receipt=restart_gate,
        before=before,
        after=after,
        started_health=_started_health(),
        stopped_health=stopped,
        refreshed_readiness=_refreshed_readiness(preactivation, after),
        captured_at=NOW,
    )

    claimed = receipt["receipt_sha256"]
    body = dict(receipt)
    body.pop("receipt_sha256")
    assert claimed == hashlib.sha256(_canonical(body).encode()).hexdigest()
    assert receipt["orders_submitted"] is False
    assert receipt["order_submission_audit"]["call_count_delta"] == 0
    assert receipt["order_submission_audit"]["before_call_count"] == 0
    assert all(receipt["checks"].values())

    bad = _started_health()
    bad["managed_workers"]["transport"]["ever_started"] = True
    with pytest.raises(CapturedAlpacaPaperServiceError, match="topology"):
        _no_order_smoke_receipt(
            verified=preactivation,
            phase_one_reconciliation_receipt=phase_one,
            restart_inventory_receipt=restart_gate,
            before=before,
            after=after,
            started_health=bad,
            stopped_health=stopped,
            refreshed_readiness=_refreshed_readiness(preactivation, after),
            captured_at=NOW,
        )

    changed = dict(after)
    changed["order_submission_call_count"] = 1
    changed["order_submission_chain_sha256"] = "f" * 64
    with pytest.raises(CapturedAlpacaPaperServiceError, match="topology"):
        _no_order_smoke_receipt(
            verified=preactivation,
            phase_one_reconciliation_receipt=phase_one,
            restart_inventory_receipt=restart_gate,
            before=before,
            after=changed,
            started_health=_started_health(),
            stopped_health=stopped,
            refreshed_readiness=_refreshed_readiness(preactivation, after),
            captured_at=NOW,
        )


def test_no_order_receipt_rejects_missing_stale_or_mismatched_refresh(
    tmp_path: Path,
) -> None:
    preactivation = _preactivation(tmp_path)
    phase_one = _phase_one_restart_receipt()
    restart_gate = _strict_restart_gate(preactivation, phase_one)
    broker = _paper_broker_snapshot(
        _PaperAdapter(),
        verified=_verified_stub(),
        purpose="post_shutdown",
        wall_clock=lambda: NOW,
    )
    stopped = {
        "state": "stopped",
        "runtime_registered": False,
        "live_loop_started": False,
    }
    common = {
        "verified": preactivation,
        "phase_one_reconciliation_receipt": phase_one,
        "restart_inventory_receipt": restart_gate,
        "before": broker,
        "after": dict(broker),
        "started_health": _started_health(),
        "stopped_health": stopped,
        "captured_at": NOW,
    }
    with pytest.raises(CapturedAlpacaPaperServiceError, match="requires exact"):
        _no_order_smoke_receipt(refreshed_readiness={}, **common)

    stale_broker = dict(broker)
    stale_broker["snapshot_observed_at"] = (NOW - timedelta(seconds=11)).isoformat()
    stale = _refreshed_readiness(
        preactivation,
        stale_broker,
        observed_at=NOW - timedelta(seconds=11),
    )
    with pytest.raises(CapturedAlpacaPaperServiceError, match="stale"):
        _no_order_smoke_receipt(refreshed_readiness=stale, **common)

    mismatched = _refreshed_readiness(preactivation, broker)
    forged = dict(mismatched["broker_account"])
    forged["expected_account_id"] = "95272674-963c-45da-8df8-822ec13fc6f0"
    forged.pop("receipt_sha256")
    forged["receipt_sha256"] = readiness_evidence.sha256_json(forged)
    mismatched["broker_account"] = forged
    with pytest.raises(CapturedAlpacaPaperServiceError, match="invalid"):
        _no_order_smoke_receipt(refreshed_readiness=mismatched, **common)


def test_no_order_execution_refreshes_broker_and_kill_after_shutdown(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    preactivation = _preactivation(tmp_path)
    phase_one = _phase_one_restart_receipt()
    restart_gate = _strict_restart_gate(preactivation, phase_one)

    class _Adapter(_PaperAdapter):
        def get_account_snapshot(self):
            events.append("broker_read")
            return super().get_account_snapshot()

    class _Supervisor:
        def start_no_order_smoke(self):
            events.append("start")
            return _started_health()

        def health(self):
            events.append("health")
            return _started_health()

        def close(self, **_kwargs):
            events.append("stop")
            return {
                "state": "stopped",
                "runtime_registered": False,
                "live_loop_started": False,
            }

    class _Store:
        def close(self):
            events.append("store_close")

    class _Result:
        def fetchone(self):
            return (9, False, None, NOW)

    class _Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, _statement):
            events.append("kill_read")
            return _Result()

    class _Engine:
        def connect(self):
            return _Connection()

    adapter = _Adapter()
    composition = _CapturedPaperServiceComposition(
        supervisor=_Supervisor(),
        shared_capture_store=_Store(),
        adapter=adapter,
        connection_generation_receipt=(
            adapter.get_paper_connection_generation_receipt()
        ),
        phase_one_reconciliation_receipt=phase_one,
        restart_inventory_receipt=restart_gate,
        database_engine=_Engine(),
    )
    result = _execute_no_order_smoke(
        verified=preactivation,
        composition=composition,
        receipt_output=tmp_path / "post-shutdown-no-order.json",
        allowed_output_roots=(tmp_path,),
        wall_clock=lambda: NOW,
    )

    assert events == [
        "broker_read",
        "start",
        "health",
        "broker_read",
        "stop",
        "store_close",
        "broker_read",
        "kill_read",
    ]
    receipt = json.loads(Path(result["no_order_smoke_path"]).read_text())
    assert receipt["schema_version"].endswith("no_order_smoke.v4")
    assert set(receipt["refreshed_readiness"]) == {
        "broker_account",
        "kill_switch",
    }
    assert receipt["orders_submitted"] is False


def test_no_order_receipt_output_is_local_append_only(tmp_path: Path) -> None:
    output = _strict_new_local_json_path(
        tmp_path / "no-order.json", allowed_roots=(tmp_path,)
    )
    digest = _publish_canonical_json_once(output, {"a": 1})
    assert digest == hashlib.sha256(b'{"a":1}').hexdigest()
    assert output.read_bytes() == b'{"a":1}'

    with pytest.raises(CapturedAlpacaPaperServiceError, match="append-only"):
        _strict_new_local_json_path(output, allowed_roots=(tmp_path,))


def test_mode_arguments_require_receipt_only_for_no_order_smoke() -> None:
    _validate_mode_arguments(
        SimpleNamespace(
            mode="no-order-smoke",
            no_order_receipt_output=r"D:\receipts\no-order.json",
        )
    )
    _validate_mode_arguments(
        SimpleNamespace(
            mode="activate-paper",
            no_order_receipt_output=None,
            host_ready_receipt=r"D:\receipts\host-ready.json",
        )
    )

    with pytest.raises(
        CapturedAlpacaPaperServiceError,
        match="requires one append-only receipt",
    ):
        _validate_mode_arguments(
            SimpleNamespace(mode="no-order-smoke", no_order_receipt_output=None)
        )
    with pytest.raises(
        CapturedAlpacaPaperServiceError,
        match="accepted only for no-order smoke",
    ):
        _validate_mode_arguments(
            SimpleNamespace(
                mode="activate-paper",
                no_order_receipt_output=r"D:\receipts\unexpected.json",
                host_ready_receipt=r"D:\receipts\host-ready.json",
            )
        )

    with pytest.raises(
        CapturedAlpacaPaperServiceError,
        match="requires a sealed two-phase host receipt",
    ):
        _validate_mode_arguments(
            SimpleNamespace(
                mode="activate-paper",
                no_order_receipt_output=None,
                host_ready_receipt=None,
            )
        )


def _host_handshake_fixture(
    tmp_path: Path,
    *,
    wall_clock=lambda: NOW,
):
    host_source = (
        Path(service_module.__file__).resolve().parent
        / "captured_paper_host_cutover.py"
    )
    executable = Path(sys.executable).resolve()
    manifest_path = tmp_path / "activation.json"
    manifest_path.write_text("{}", encoding="utf-8")
    candidate_root = tmp_path / "candidate"
    candidate_root.mkdir()
    stage0_path = (
        Path(service_module.__file__).resolve().parent
        / "captured_paper_isolated_stage0.py"
    )
    stage0_sha = hashlib.sha256(stage0_path.read_bytes()).hexdigest()
    service_cmdline = [str(executable), str(Path(service_module.__file__).resolve())]
    verified = SimpleNamespace(
        manifest_path=manifest_path.resolve(),
        activation_generation=GENERATION,
        manifest_sha256=SHA_A,
        expected_account_id=ACCOUNT,
        candidate_root=candidate_root.resolve(),
        expires_at=NOW + timedelta(minutes=5),
        source_paths={"captured_paper_host_cutover": host_source},
        source_hashes={
            "captured_paper_host_cutover": hashlib.sha256(
                host_source.read_bytes()
            ).hexdigest()
        },
        manifest={
            "cutover": {
                "stage0_path": str(stage0_path),
                "stage0_sha256": stage0_sha,
            }
        },
    )
    identity = {
        "service_pid": os.getpid(),
        "service_create_time_ns": 123_456_789,
        "service_executable_path": str(executable),
        "service_executable_sha256": hashlib.sha256(
            executable.read_bytes()
        ).hexdigest(),
        "service_cmdline_sha256": sha256_json(service_cmdline),
    }
    issuer_live: dict[str, object] = {}
    handshake = service_module._CapturedPaperHostActivationHandshake.prepare(
        ready_output=tmp_path / "host-ready.json",
        verified=verified,
        allowed_roots=(tmp_path,),
        wall_clock=wall_clock,
        process_probe=lambda: identity,
        issuer_process_probe=lambda _pid: dict(issuer_live),
        challenge_factory=lambda: SHA_C,
    )
    return (
        handshake,
        verified,
        identity,
        host_source,
        executable,
        service_cmdline,
        issuer_live,
    )


def _publish_matching_host_permit(
    handshake,
    *,
    verified,
    identity,
    host_source: Path,
    executable: Path,
    service_cmdline: list[str],
    issuer_live: dict[str, object],
) -> dict:
    journal_root = handshake.ready_path.parent / "journal"
    journal_root.mkdir()
    generation_root = journal_root / verified.activation_generation
    generation_root.mkdir()
    journal_path = generation_root / f"{verified.manifest_sha256}.jsonl"
    input_files: dict[str, Path] = {}
    for option in (
        "task-snapshot",
        "process-snapshot",
        "restore-plan",
        "candidate-task-template",
        "candidate-action",
    ):
        path = handshake.ready_path.parent / f"{option}.json"
        path.write_text("{}", encoding="utf-8")
        input_files[option] = path.resolve()
    issuer_cmdline = [
        str(executable),
        "-I",
        "-S",
        "-B",
        str(verified.manifest["cutover"]["stage0_path"]),
        "--manifest",
        str(verified.manifest_path),
        "--manifest-sha256",
        verified.manifest_sha256,
        "--candidate-root",
        str(verified.candidate_root),
        "--target-role",
        "captured_paper_host_cutover",
        "--target",
        str(host_source),
        "--target-sha256",
        hashlib.sha256(host_source.read_bytes()).hexdigest(),
        "--",
        "--mode",
        "Apply",
        "--manifest",
        str(verified.manifest_path),
        "--manifest-sha256",
        verified.manifest_sha256,
        "--candidate-root",
        str(verified.candidate_root),
        "--allow-read-root",
        str(handshake.ready_path.parent.resolve()),
        "--task-snapshot",
        str(input_files["task-snapshot"]),
        "--process-snapshot",
        str(input_files["process-snapshot"]),
        "--restore-plan",
        str(input_files["restore-plan"]),
        "--candidate-task-template",
        str(input_files["candidate-task-template"]),
        "--candidate-action",
        str(input_files["candidate-action"]),
        "--journal-root",
        str(journal_root.resolve()),
        "--confirm-fake-money-paper",
        "CUTOVER_FAKE_MONEY_ALPACA_PAPER",
    ]
    issuer = {
        "issuer_pid": os.getpid(),
        "issuer_create_time_ns": 987_654_321,
        "issuer_executable_path": str(executable),
        "issuer_executable_sha256": hashlib.sha256(
            executable.read_bytes()
        ).hexdigest(),
        "issuer_cmdline": issuer_cmdline,
        "issuer_cmdline_sha256": sha256_json(issuer_cmdline),
        "issuer_source_path": str(host_source),
        "issuer_source_sha256": hashlib.sha256(
            host_source.read_bytes()
        ).hexdigest(),
    }
    issuer_live.update(
        {
            key: value
            for key, value in issuer.items()
            if key not in {"issuer_source_path", "issuer_source_sha256"}
        }
    )
    transaction_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            "chili:captured-paper-cutover:"
            f"{verified.activation_generation}:{verified.manifest_sha256}",
        )
    )
    issued_at = NOW.isoformat().replace("+00:00", "Z")
    valid_until = (NOW + timedelta(seconds=20)).isoformat().replace(
        "+00:00", "Z"
    )
    authorization_payload = {
        "activation_generation": verified.activation_generation,
        "manifest_path": str(verified.manifest_path),
        "manifest_sha256": verified.manifest_sha256,
        "candidate_root": str(verified.candidate_root),
        "journal_root": str(journal_root.resolve()),
        "account_scope": "alpaca:paper",
        "expected_account_id": verified.expected_account_id,
        **identity,
        "service_cmdline": service_cmdline,
        "service_role": "candidate_service",
        "service_script_path": str(Path(service_module.__file__).resolve()),
        "service_script_sha256": hashlib.sha256(
            Path(service_module.__file__).resolve().read_bytes()
        ).hexdigest(),
        "challenge_sha256": SHA_C,
        "prepared_receipt_sha256": handshake._prepared_sha256,
        "issued_at": issued_at,
        "valid_until": valid_until,
        "permit_path": str(handshake.permit_path),
        **handshake._dispatch_lock_identity,
        **issuer,
        "live_cash_authorized": False,
        "real_money_authorized": False,
    }
    lane_sha256 = "7" * 64
    quiesced = {
        "schema_version": "chili.captured-paper-host-cutover-journal-event.v1",
        "transaction_id": transaction_id,
        "sequence": 1,
        "previous_event_sha256": "0" * 64,
        "event_type": "legacy_execution_lane_quiesced",
        "recorded_at": issued_at,
        "payload": {
            "legacy_execution_lane": {"state": "stopped"},
            "legacy_execution_lane_sha256": lane_sha256,
        },
    }
    quiesced["event_sha256"] = sha256_json(quiesced)
    quiet = {
        "schema_version": "chili.captured-paper-host-cutover-journal-event.v1",
        "transaction_id": transaction_id,
        "sequence": 2,
        "previous_event_sha256": quiesced["event_sha256"],
        "event_type": "legacy_paper_broker_quiet_horizon_completed",
        "recorded_at": issued_at,
        "payload": {
            "policy": "alpaca-paper-assumption-bound-quiet-horizon.v1",
            "assumption_bound": True,
            "live_cash_certification": False,
            "required_seconds": 30.0,
            "observed_monotonic_seconds": 30.0,
            "stabilized_probe_count": 2,
            "first_zero_at": issued_at,
            "last_zero_at": issued_at,
            "legacy_execution_lane_sha256": lane_sha256,
            "legacy_process_count": 0,
            "recreator_process_count": 0,
        },
    }
    quiet["event_sha256"] = sha256_json(quiet)
    authorization = {
        "schema_version": "chili.captured-paper-host-cutover-journal-event.v1",
        "transaction_id": transaction_id,
        "sequence": 3,
        "previous_event_sha256": quiet["event_sha256"],
        "event_type": "activation_permit_issued",
        "recorded_at": issued_at,
        "payload": authorization_payload,
    }
    authorization["event_sha256"] = sha256_json(authorization)
    journal_path.write_bytes(
        b"".join(
            json.dumps(
                event,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            + b"\n"
            for event in (quiesced, quiet, authorization)
        )
    )
    permit = {
        "schema_version": "chili.captured-paper-host-startup-permit.v1",
        "state": "ACTIVATION_PERMITTED",
        **authorization_payload,
        "journal_path": str(journal_path),
        "journal_transaction_id": transaction_id,
        "journal_authorization_sequence": 3,
        "journal_authorization_event_sha256": authorization["event_sha256"],
        "journal_authorization_event": authorization,
    }
    permit["permit_sha256"] = sha256_json(permit)
    _publish_canonical_json_once(handshake.permit_path, permit)
    return permit


def _append_matching_apply_completed(
    handshake,
    *,
    permit: dict,
    recorded_at: datetime = NOW + timedelta(seconds=1),
    payload_overrides: dict | None = None,
) -> dict:
    journal_path = Path(permit["journal_path"])
    rows = [
        json.loads(line)
        for line in journal_path.read_text(encoding="utf-8").splitlines()
        if line
    ]
    lane = {
        "schema_version": "chili.legacy-execution-lane-observation.v2",
        "state": "stopped",
        "recreator_tasks": [],
    }
    payload = {
        "postcondition": "one_unified_candidate_host",
        "activation_generation": handshake._verified.activation_generation,
        "manifest_sha256": handshake._verified.manifest_sha256,
        "account_scope": "alpaca:paper",
        "expected_account_id": handshake._verified.expected_account_id,
        "service_pid": handshake._identity["service_pid"],
        "service_create_time_ns": handshake._identity["service_create_time_ns"],
        "service_executable_path": handshake._identity["service_executable_path"],
        "service_executable_sha256": handshake._identity[
            "service_executable_sha256"
        ],
        "service_cmdline_sha256": handshake._identity["service_cmdline_sha256"],
        "legacy_task_count_disabled": 4,
        "legacy_process_count": 0,
        "prepared_receipt_sha256": handshake._prepared_sha256,
        "activation_permit_sha256": handshake._permit_sha256,
        "started_receipt_sha256": handshake._started_sha256,
        "active_start_authority_sha256": handshake._active_start_authority_body[
            "authority_sha256"
        ],
        "active_start_evidence_artifact_sha256": (
            handshake._active_start_evidence_artifact_sha256
        ),
        "host_quiet_horizon_event_sha256": (
            handshake._quiet_horizon_event_sha256
        ),
        "challenge_sha256": handshake._challenge_sha256,
        "legacy_execution_lane": lane,
        "legacy_execution_lane_sha256": sha256_json(lane),
        "paper_execution_committed": True,
        "live_cash_authorized": False,
        "real_money_authorized": False,
    }
    payload.update(payload_overrides or {})
    event = {
        "schema_version": "chili.captured-paper-host-cutover-journal-event.v1",
        "transaction_id": permit["journal_transaction_id"],
        "sequence": len(rows) + 1,
        "previous_event_sha256": rows[-1]["event_sha256"],
        "event_type": "apply_completed",
        "recorded_at": recorded_at.isoformat().replace("+00:00", "Z"),
        "payload": payload,
    }
    event["event_sha256"] = sha256_json(event)
    with journal_path.open("ab", buffering=0) as handle:
        handle.write(_canonical(event).encode("utf-8") + b"\n")
        handle.flush()
        os.fsync(handle.fileno())
    return event


def _published_started_handshake(tmp_path: Path, *, clock: dict[str, datetime]):
    (
        handshake,
        verified,
        identity,
        host_source,
        executable,
        service_cmdline,
        issuer_live,
    ) = _host_handshake_fixture(tmp_path, wall_clock=lambda: clock["now"])
    handshake.publish_prepared()
    permit = _publish_matching_host_permit(
        handshake,
        verified=verified,
        identity=identity,
        host_source=host_source,
        executable=executable,
        service_cmdline=service_cmdline,
        issuer_live=issuer_live,
    )
    handshake.await_and_consume_permit()
    authority = _active_start_authority_fixture(
        verified,
        permit_sha256=permit["permit_sha256"],
        quiet_horizon_sha256=handshake.quiet_horizon_event_sha256,
    )
    evidence = handshake.publish_active_start_evidence(authority)
    handshake.publish_started(
        health={
            "state": "active",
            "active_start_authority_consumed": True,
            "active_start_authority_sha256": authority["authority_sha256"],
            "active_start_evidence_artifact_sha256": evidence[
                "artifact_sha256"
            ],
        },
        active_start_authority=authority,
    )
    return handshake, permit, issuer_live


def test_two_phase_host_handshake_is_prepared_permit_started(
    tmp_path: Path,
) -> None:
    (
        handshake,
        verified,
        identity,
        host_source,
        executable,
        service_cmdline,
        issuer_live,
    ) = (
        _host_handshake_fixture(tmp_path)
    )
    prepared = handshake.publish_prepared()
    assert prepared["state"] == "PREPARED"
    assert prepared["workers_started"] is False
    assert prepared["paper_execution_started"] is False
    assert not handshake.started_path.exists()
    assert handshake.dispatch_lock_path.read_bytes() == b"0"
    assert all(
        prepared[key] == value
        for key, value in handshake._dispatch_lock_identity.items()
    )

    permit = _publish_matching_host_permit(
        handshake,
        verified=verified,
        identity=identity,
        host_source=host_source,
        executable=executable,
        service_cmdline=service_cmdline,
        issuer_live=issuer_live,
    )
    consumed = handshake.await_and_consume_permit()
    assert consumed["permit_sha256"] == permit["permit_sha256"]
    handshake.assert_consumed_permit_current()
    with handshake.hold_dispatch_authority():
        assert handshake.dispatch_lock_path.exists()

    class _BrokerBodyFailure(RuntimeError):
        pass

    with pytest.raises(_BrokerBodyFailure, match="broker lifecycle"):
        with handshake.hold_dispatch_authority():
            raise _BrokerBodyFailure("broker lifecycle")

    authority = _active_start_authority_fixture(
        verified,
        permit_sha256=permit["permit_sha256"],
        quiet_horizon_sha256=handshake.quiet_horizon_event_sha256,
    )
    evidence = handshake.publish_active_start_evidence(authority)
    started = handshake.publish_started(
        health={
            "state": "active",
            "active_start_authority_consumed": True,
            "active_start_authority_sha256": authority["authority_sha256"],
            "active_start_evidence_artifact_sha256": evidence[
                "artifact_sha256"
            ],
        },
        active_start_authority=authority,
    )
    assert started["state"] == "STARTED"
    assert started["activation_permit_sha256"] == permit["permit_sha256"]
    assert started["workers_started"] is True
    assert started["paper_execution_started"] is True
    assert started["active_start_evidence_artifact_sha256"] == evidence[
        "artifact_sha256"
    ]
    assert handshake.permit_path.exists()


def test_committed_host_authority_survives_startup_permit_expiry_but_not_revocation(
    tmp_path: Path,
) -> None:
    clock = {"now": NOW}
    handshake, permit, issuer_live = _published_started_handshake(
        tmp_path, clock=clock
    )
    committed = _append_matching_apply_completed(handshake, permit=permit)

    consumed = handshake.await_and_consume_apply_completed_authority()
    assert consumed["event_sha256"] == committed["event_sha256"]

    clock["now"] = NOW + timedelta(minutes=2)
    issuer_live.clear()
    handshake.assert_dispatch_authority_current()
    with handshake.hold_dispatch_authority():
        pass

    handshake.revocation_requested_path.write_text("{}", encoding="utf-8")
    with pytest.raises(CapturedAlpacaPaperServiceError, match="REVOKED"):
        handshake.assert_dispatch_authority_current()


def test_uncommitted_host_authority_expires_fail_closed(
    tmp_path: Path,
) -> None:
    clock = {"now": NOW}
    handshake, _permit, issuer_live = _published_started_handshake(
        tmp_path, clock=clock
    )
    clock["now"] = NOW + timedelta(seconds=21)
    issuer_live.clear()

    with pytest.raises(CapturedAlpacaPaperServiceError, match="PERMIT"):
        handshake.assert_dispatch_authority_current()
    with pytest.raises(CapturedAlpacaPaperServiceError):
        with handshake.hold_dispatch_authority():
            raise AssertionError("unreachable")


def test_committed_host_authority_rejects_later_or_missing_journal(
    tmp_path: Path,
) -> None:
    clock = {"now": NOW}
    handshake, permit, _issuer_live = _published_started_handshake(
        tmp_path, clock=clock
    )
    _append_matching_apply_completed(handshake, permit=permit)
    handshake.await_and_consume_apply_completed_authority()

    _append_matching_apply_completed(handshake, permit=permit)
    with pytest.raises(CapturedAlpacaPaperServiceError):
        handshake.assert_dispatch_authority_current()

    Path(permit["journal_path"]).unlink()
    with pytest.raises(CapturedAlpacaPaperServiceError):
        handshake.assert_dispatch_authority_current()


@pytest.mark.parametrize(
    "override",
    [
        {"expected_account_id": "00000000-0000-4000-8000-000000000000"},
        {"activation_generation": "00000000-0000-4000-8000-000000000000"},
        {"service_pid": 999_999},
        {"started_receipt_sha256": "0" * 64},
        {"active_start_authority_sha256": "0" * 64},
        {"live_cash_authorized": True},
    ],
)
def test_host_apply_commit_identity_forgery_is_rejected(
    tmp_path: Path,
    override: dict,
) -> None:
    clock = {"now": NOW}
    handshake, permit, _issuer_live = _published_started_handshake(
        tmp_path, clock=clock
    )
    _append_matching_apply_completed(
        handshake,
        permit=permit,
        payload_overrides=override,
    )

    with pytest.raises(CapturedAlpacaPaperServiceError, match="COMMIT_INVALID"):
        handshake.await_and_consume_apply_completed_authority()


def test_active_start_evidence_deletion_blocks_dispatch_before_commit(
    tmp_path: Path,
) -> None:
    clock = {"now": NOW}
    handshake, _permit, _issuer_live = _published_started_handshake(
        tmp_path, clock=clock
    )
    handshake.active_start_evidence_path.unlink()

    with pytest.raises(CapturedAlpacaPaperServiceError):
        handshake.assert_active_start_evidence_current()


def test_consumed_host_permit_expiry_blocks_post_read_worker_start(
    tmp_path: Path,
) -> None:
    clock = {"now": NOW}
    (
        handshake,
        verified,
        identity,
        host_source,
        executable,
        service_cmdline,
        issuer_live,
    ) = _host_handshake_fixture(tmp_path, wall_clock=lambda: clock["now"])
    handshake.publish_prepared()
    _publish_matching_host_permit(
        handshake,
        verified=verified,
        identity=identity,
        host_source=host_source,
        executable=executable,
        service_cmdline=service_cmdline,
        issuer_live=issuer_live,
    )
    handshake.await_and_consume_permit()
    clock["now"] = NOW + timedelta(seconds=21)

    with pytest.raises(
        CapturedAlpacaPaperServiceError,
        match="stale, mismatched, or untrusted",
    ):
        handshake.assert_consumed_permit_current()


def test_dispatch_authority_never_creates_or_repairs_missing_lock(
    tmp_path: Path,
) -> None:
    (
        handshake,
        verified,
        identity,
        host_source,
        executable,
        service_cmdline,
        issuer_live,
    ) = _host_handshake_fixture(tmp_path)
    handshake.publish_prepared()
    _publish_matching_host_permit(
        handshake,
        verified=verified,
        identity=identity,
        host_source=host_source,
        executable=executable,
        service_cmdline=service_cmdline,
        issuer_live=issuer_live,
    )
    handshake.await_and_consume_permit()
    handshake.dispatch_lock_path.unlink()

    with pytest.raises(
        CapturedAlpacaPaperServiceError,
        match="dispatch authority could not be acquired",
    ):
        with handshake.hold_dispatch_authority():
            raise AssertionError("missing lock must never authorize dispatch")
    assert not os.path.lexists(handshake.dispatch_lock_path)


def test_host_permit_expiring_during_issuer_verification_is_never_consumed(
    tmp_path: Path,
) -> None:
    clock = {"now": NOW}
    (
        handshake,
        verified,
        identity,
        host_source,
        executable,
        service_cmdline,
        issuer_live,
    ) = _host_handshake_fixture(
        tmp_path,
        wall_clock=lambda: clock["now"],
    )
    handshake.publish_prepared()
    _publish_matching_host_permit(
        handshake,
        verified=verified,
        identity=identity,
        host_source=host_source,
        executable=executable,
        service_cmdline=service_cmdline,
        issuer_live=issuer_live,
    )
    original_verify = handshake._verify_live_issuer_and_command

    def _verify_then_expire(*args, **kwargs):
        original_verify(*args, **kwargs)
        clock["now"] = NOW + timedelta(seconds=21)

    handshake._verify_live_issuer_and_command = _verify_then_expire

    with pytest.raises(
        CapturedAlpacaPaperServiceError,
        match="expired during verification",
    ):
        handshake.await_and_consume_permit()

    assert handshake._permit_sha256 is None
    assert not handshake.started_path.exists()


def test_host_permit_rejects_fabricated_embedded_event_not_in_durable_journal(
    tmp_path: Path,
) -> None:
    (
        handshake,
        verified,
        identity,
        host_source,
        executable,
        service_cmdline,
        issuer_live,
    ) = _host_handshake_fixture(tmp_path)
    handshake.publish_prepared()
    permit = _publish_matching_host_permit(
        handshake,
        verified=verified,
        identity=identity,
        host_source=host_source,
        executable=executable,
        service_cmdline=service_cmdline,
        issuer_live=issuer_live,
    )
    forged = dict(permit)
    forged_event = dict(forged["journal_authorization_event"])
    forged_event["recorded_at"] = (NOW + timedelta(microseconds=1)).isoformat().replace(
        "+00:00", "Z"
    )
    forged_event.pop("event_sha256")
    forged_event["event_sha256"] = sha256_json(forged_event)
    forged["journal_authorization_event"] = forged_event
    forged["journal_authorization_event_sha256"] = forged_event["event_sha256"]
    forged.pop("permit_sha256")
    forged["permit_sha256"] = sha256_json(forged)
    handshake.permit_path.write_bytes(
        json.dumps(
            forged, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    )

    with pytest.raises(CapturedAlpacaPaperServiceError, match="journal"):
        handshake.await_and_consume_permit()


def test_host_permit_rejects_dead_or_reused_issuer_process_identity(
    tmp_path: Path,
) -> None:
    (
        handshake,
        verified,
        identity,
        host_source,
        executable,
        service_cmdline,
        issuer_live,
    ) = _host_handshake_fixture(tmp_path)
    handshake.publish_prepared()
    _publish_matching_host_permit(
        handshake,
        verified=verified,
        identity=identity,
        host_source=host_source,
        executable=executable,
        service_cmdline=service_cmdline,
        issuer_live=issuer_live,
    )
    issuer_live["issuer_create_time_ns"] = int(
        issuer_live["issuer_create_time_ns"]
    ) + 1

    with pytest.raises(CapturedAlpacaPaperServiceError, match="issuer process"):
        handshake.await_and_consume_permit()


def test_host_handshake_rejects_preexisting_sibling_and_revocation(
    tmp_path: Path,
) -> None:
    ready = tmp_path / "host-ready.json"
    permit = ready.with_name(ready.name + ".permit.json")
    permit.write_text("{}", encoding="utf-8")
    with pytest.raises(CapturedAlpacaPaperServiceError, match="append-only"):
        service_module._CapturedPaperHostActivationHandshake.prepare(
            ready_output=ready,
            verified=SimpleNamespace(),
            allowed_roots=(tmp_path,),
        )
    permit.unlink()

    handshake, *_rest = _host_handshake_fixture(tmp_path)
    handshake.publish_prepared()
    requested = handshake.ready_path.with_name(
        handshake.ready_path.name + ".revocation-requested.json"
    )
    _publish_canonical_json_once(requested, {"state": "REVOCATION_REQUESTED"})
    with pytest.raises(CapturedAlpacaPaperServiceError, match="REVOKED"):
        handshake.assert_not_revoked()
    requested.unlink()
    _publish_canonical_json_once(handshake.revoked_path, {"state": "REVOKED"})
    with pytest.raises(CapturedAlpacaPaperServiceError, match="REVOKED"):
        handshake.assert_not_revoked()


def test_prepared_composition_closes_host_owner_and_store() -> None:
    calls: list[str] = []

    class _Supervisor:
        def close(self, *, join_timeout_seconds, quiesce_timeout_seconds):
            assert join_timeout_seconds > 0
            assert quiesce_timeout_seconds > 0
            calls.append("supervisor")
            return {"state": "stopped"}

    class _Store:
        def close(self):
            calls.append("store")

    composition = _CapturedPaperServiceComposition(
        supervisor=_Supervisor(),
        shared_capture_store=_Store(),
        adapter=object(),
        connection_generation_receipt={},
        phase_one_reconciliation_receipt={},
        restart_inventory_receipt={},
        database_engine=object(),
    )
    stopped = _close_composition(composition, supervisor_started=False)

    assert stopped["state"] == "stopped"
    assert calls == ["supervisor", "store"]


def test_shutdown_failure_never_closes_store_under_unjoined_workers() -> None:
    store_closed = False

    class _Supervisor:
        def close(self, **_kwargs):
            raise RuntimeError("worker still alive")

    class _Store:
        def close(self):
            nonlocal store_closed
            store_closed = True

    composition = _CapturedPaperServiceComposition(
        supervisor=_Supervisor(),
        shared_capture_store=_Store(),
        adapter=object(),
        connection_generation_receipt={},
        phase_one_reconciliation_receipt={},
        restart_inventory_receipt={},
        database_engine=object(),
    )
    with pytest.raises(
        CapturedAlpacaPaperServiceError,
        match="supervisor did not quiesce",
    ):
        _close_composition(composition, supervisor_started=True)

    assert store_closed is False


def test_active_failure_reports_order_and_external_state_as_unknown(
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    verified = SimpleNamespace()
    monkeypatch.setattr(
        service_module,
        "validate_offline_startup",
        lambda **_kwargs: (verified, object(), {}),
    )
    monkeypatch.setattr(
        service_module,
        "_load_pinned_runtime_modules",
        lambda _verified: {},
    )
    monkeypatch.setattr(
        service_module,
        "_build_service_composition",
        lambda **_kwargs: object(),
        raising=False,
    )
    monkeypatch.setattr(
        service_module,
        "_execute_active_service",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("after boundary")),
    )
    monkeypatch.setattr(
        service_module,
        "_CapturedPaperServiceSingleton",
        lambda: SimpleNamespace(acquire=lambda: None, close=lambda: None),
    )
    monkeypatch.setattr(
        service_module,
        "_issue_launcher_cutover_attestation",
        lambda **_kwargs: object(),
    )
    monkeypatch.setattr(
        service_module,
        "_assert_content_addressed_activation_entrypoints",
        lambda _verified: None,
    )
    monkeypatch.setattr(
        service_module,
        "_CapturedPaperHostActivationHandshake",
        SimpleNamespace(
                prepare=lambda **_kwargs: SimpleNamespace(
                    assert_dispatch_authority_current=lambda: None,
                    hold_dispatch_authority=nullcontext,
                )
        ),
    )

    exit_code = service_module.main(
        [
            "--mode",
            "activate-paper",
            "--manifest",
            r"D:\sealed\activation.json",
            "--manifest-sha256",
            SHA_A,
            "--candidate-root",
            r"D:\candidate",
            "--allow-read-root",
            r"D:\sealed",
            "--launcher-path",
            r"D:\candidate\launcher.ps1",
            "--launcher-sha256",
            SHA_B,
            "--host-ready-receipt",
            r"D:\sealed\host-ready.json",
        ]
    )
    report = json.loads(capfd.readouterr().out)

    assert exit_code == 2
    assert report["orders_submitted"] is None
    assert report["paper_execution_started"] is None
    assert report["provider_sockets_started"] is None
    assert report["database_connected"] is None
    assert report["broker_contacted"] is None
    assert report["live_cash_authorized"] is False


def _launcher_attestation_fixture(monkeypatch):
    root = Path(service_module.__file__).resolve().parents[1]
    service_path = Path(service_module.__file__).resolve()
    python_path = Path(sys.executable).resolve()
    launcher_path = root / "scripts" / "start-captured-alpaca-paper.ps1"
    stage0_path = root / "scripts" / "captured_paper_isolated_stage0.py"
    powershell_path = Path(
        service_module.shutil.which("powershell.exe")
        or service_module.shutil.which("pwsh.exe")
        or ""
    ).resolve(strict=True)
    read_roots = [str(root)]
    args = SimpleNamespace(
        mode="activate-paper",
        manifest=str(root / "sealed-activation.json"),
        manifest_sha256=SHA_A,
        candidate_root=str(root),
        launcher_path=str(launcher_path),
        launcher_sha256=hashlib.sha256(launcher_path.read_bytes()).hexdigest(),
        allow_read_root=read_roots,
        host_ready_receipt=str(root / "host-ready.json"),
    )
    projection = {
        "allowed_read_roots": read_roots,
        "candidate_root": str(root),
        "launcher_path": str(launcher_path),
        "python_executable_path": str(python_path),
        "python_executable_sha256": hashlib.sha256(
            python_path.read_bytes()
        ).hexdigest(),
        "service_path": str(service_path),
        "service_sha256": hashlib.sha256(service_path.read_bytes()).hexdigest(),
        "stage0_path": str(stage0_path),
        "stage0_sha256": hashlib.sha256(stage0_path.read_bytes()).hexdigest(),
        "singleton_name": "Global\\CHILI-Captured-Alpaca-PAPER-SINGLETON",
        "working_directory": str(root),
        "service_arguments": [
            "-B",
            str(service_path),
            "--host-ready-receipt",
            str(root / "host-ready.json"),
        ],
    }
    monkeypatch.setattr(
        service_module,
        "_launcher_projection",
        lambda *_args, **_kwargs: projection,
    )
    service_argv = [
        str(service_path),
        "--mode",
        "activate-paper",
        "--manifest",
        args.manifest,
        "--manifest-sha256",
        SHA_A,
        "--candidate-root",
        str(root),
        "--launcher-path",
        str(launcher_path),
        "--launcher-sha256",
        args.launcher_sha256,
        "--allow-read-root",
        str(root),
        "--host-ready-receipt",
        args.host_ready_receipt,
    ]
    roots_b64 = service_module.base64.b64encode(
        service_module._canonical_json_bytes(read_roots)
    ).decode("ascii")
    parent_cmdline = [
        str(powershell_path),
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(launcher_path),
        "-Mode",
        "ActivatePaper",
        "-PythonExecutable",
        str(python_path),
        "-CandidateRoot",
        str(root),
        "-ServiceScriptPath",
        str(service_path),
        "-Stage0ScriptPath",
        str(stage0_path),
        "-ManifestPath",
        args.manifest,
        "-ManifestSha256",
        SHA_A,
        "-AllowedReadRootsBase64",
        roots_b64,
    ]
    evidence = {
        "pid": os.getpid(),
        "process_create_time": 20.0,
        "parent_pid": os.getppid(),
        "parent_create_time": 10.0,
        "python_executable_path": str(python_path),
        "service_argv": service_argv,
        "working_directory": str(root),
        "parent_executable_path": str(powershell_path),
        "parent_cmdline": parent_cmdline,
        "cutover": {
            "candidate_task_name": "CHILI-Captured-Alpaca-PAPER",
            "candidate_task_enabled": True,
            "candidate_task_xml_sha256": SHA_A,
            "candidate_task_action_sha256": SHA_B,
            "legacy_task_enabled": {
                "CHILI-IQFeed-Depth-Bridge-Daily": False,
                "CHILI-IQFeed-Depth-Bridge-Logon": False,
                "CHILI-IQFeed-Trade-Bridge-Daily": False,
                "CHILI-IQFeed-Trade-Bridge-Logon": False,
            },
            "legacy_bridge_processes": [],
            "legacy_recreator_processes": [],
            "legacy_execution_lane": {
                "schema_version": "chili.legacy-execution-lane-observation.v2",
                "container_name": "chili-clean-recovery-momentum-exec",
                "container_id": SHA_C,
                "image_id": "sha256:" + SHA_A,
                "config_sha256": SHA_B,
                "execution_scope": (
                    "legacy:mixed-paper-config-live-masters-disabled"
                ),
                "scope_sha256": SHA_C,
                "recreator_tasks": [
                    {
                        "name": name,
                        "definition_sha256": SHA_A,
                        "action_sha256": SHA_B,
                        "source_chain_sha256": SHA_C,
                        "enabled": False,
                    }
                    for name in (
                        "CHILI-Docker-Socket-Guard",
                        "CHILI-Premarket-Readiness",
                        "CHILI-Premarket-Readiness-Recheck",
                        "CHILI-captured-paper-premarket-activation",
                        "CHILI-liveness-watchdog",
                    )
                ],
                "state": "stopped",
            },
        },
    }
    evidence["cutover"]["legacy_execution_lane_sha256"] = (
        sha256_json(evidence["cutover"]["legacy_execution_lane"])
    )
    verified = SimpleNamespace(
        activation_generation=GENERATION,
        manifest_sha256=SHA_B,
        launcher_sha256=args.launcher_sha256,
        expires_at=NOW + timedelta(minutes=5),
        manifest={"cutover": {"candidate_root": str(root)}},
    )
    return verified, args, evidence


def test_launcher_cutover_attestation_is_process_bound_and_one_shot(
    monkeypatch,
):
    verified, args, evidence = _launcher_attestation_fixture(monkeypatch)
    attestation = service_module._issue_launcher_cutover_attestation(
        verified=verified,
        args=args,
        process_probe=lambda: evidence,
        cutover_probe=lambda: evidence["cutover"],
        wall_clock=lambda: NOW,
    )

    receipt = attestation.consume(
        wall_clock=lambda: NOW + timedelta(seconds=1)
    )
    assert receipt["launcher_attestation_consumed"] is True
    with pytest.raises(
        CapturedAlpacaPaperServiceError,
        match="LAUNCH_ATTESTATION_ALREADY_CONSUMED",
    ):
        attestation.consume(wall_clock=lambda: NOW + timedelta(seconds=2))


def test_launcher_cutover_attestation_rejects_docker_identity_drift(
    monkeypatch,
) -> None:
    verified, args, evidence = _launcher_attestation_fixture(monkeypatch)
    attestation = service_module._issue_launcher_cutover_attestation(
        verified=verified,
        args=args,
        process_probe=lambda: evidence,
        cutover_probe=lambda: evidence["cutover"],
        wall_clock=lambda: NOW,
    )
    evidence["cutover"]["legacy_execution_lane"]["container_id"] = "f" * 64
    evidence["cutover"]["legacy_execution_lane_sha256"] = sha256_json(
        evidence["cutover"]["legacy_execution_lane"]
    )

    with pytest.raises(
        CapturedAlpacaPaperServiceError,
        match="HOST_CUTOVER_BINDING_DRIFT",
    ):
        attestation.consume(wall_clock=lambda: NOW + timedelta(seconds=1))


def test_launcher_cutover_attestation_rejects_runnable_docker_lane(
    monkeypatch,
) -> None:
    verified, args, evidence = _launcher_attestation_fixture(monkeypatch)
    evidence["cutover"]["legacy_execution_lane"]["state"] = "running"
    evidence["cutover"]["legacy_execution_lane_sha256"] = sha256_json(
        evidence["cutover"]["legacy_execution_lane"]
    )

    with pytest.raises(
        CapturedAlpacaPaperServiceError,
        match="legacy Docker execution lane identity is incomplete or runnable",
    ):
        service_module._issue_launcher_cutover_attestation(
            verified=verified,
            args=args,
            process_probe=lambda: evidence,
            cutover_probe=lambda: evidence["cutover"],
            wall_clock=lambda: NOW,
        )


def test_launcher_cutover_attestation_rejects_recreator_authority(
    monkeypatch,
) -> None:
    verified, args, evidence = _launcher_attestation_fixture(monkeypatch)
    evidence["cutover"]["legacy_execution_lane"]["recreator_tasks"][0][
        "enabled"
    ] = True
    evidence["cutover"]["legacy_execution_lane_sha256"] = sha256_json(
        evidence["cutover"]["legacy_execution_lane"]
    )

    with pytest.raises(
        CapturedAlpacaPaperServiceError, match="incomplete or runnable"
    ):
        service_module._issue_launcher_cutover_attestation(
            verified=verified,
            args=args,
            process_probe=lambda: evidence,
            cutover_probe=lambda: evidence["cutover"],
            wall_clock=lambda: NOW,
        )


def test_launcher_cutover_attestation_rejects_recreator_descendant(
    monkeypatch,
) -> None:
    verified, args, evidence = _launcher_attestation_fixture(monkeypatch)
    evidence["cutover"]["legacy_recreator_processes"] = [
        "1234:docker.exe:matched"
    ]

    with pytest.raises(
        CapturedAlpacaPaperServiceError, match="HOST_CUTOVER_INCOMPLETE"
    ):
        service_module._issue_launcher_cutover_attestation(
            verified=verified,
            args=args,
            process_probe=lambda: evidence,
            cutover_probe=lambda: evidence["cutover"],
            wall_clock=lambda: NOW,
        )


def test_launcher_cutover_attestation_identifies_late_bridge_process(
    monkeypatch,
) -> None:
    verified, args, evidence = _launcher_attestation_fixture(monkeypatch)
    evidence["cutover"]["legacy_bridge_processes"] = [
        "1234:iqfeed_trade_bridge.py"
    ]

    with pytest.raises(
        CapturedAlpacaPaperServiceError,
        match="HOST_CUTOVER_INCOMPLETE_LEGACY_BRIDGE",
    ):
        service_module._issue_launcher_cutover_attestation(
            verified=verified,
            args=args,
            process_probe=lambda: evidence,
            cutover_probe=lambda: evidence["cutover"],
            wall_clock=lambda: NOW,
        )


def test_launcher_cutover_attestation_rejects_direct_or_drifted_parent(
    monkeypatch,
):
    verified, args, evidence = _launcher_attestation_fixture(monkeypatch)
    evidence = dict(evidence)
    evidence["parent_cmdline"] = [
        evidence["parent_executable_path"],
        "-NoProfile",
    ]

    with pytest.raises(
        CapturedAlpacaPaperServiceError,
        match="PARENT_NOT_SEALED_LAUNCHER",
    ):
        service_module._issue_launcher_cutover_attestation(
            verified=verified,
            args=args,
            process_probe=lambda: evidence,
            cutover_probe=lambda: evidence["cutover"],
            wall_clock=lambda: NOW,
        )


def test_python_service_singleton_rejects_second_process_owner():
    name = f"Global\\CHILI-Captured-PAPER-TEST-{os.getpid()}-{id(object())}"
    first = service_module._CapturedPaperServiceSingleton(name)
    second = service_module._CapturedPaperServiceSingleton(name)
    first.acquire()
    try:
        with pytest.raises(
            CapturedAlpacaPaperServiceError,
            match="SERVICE_SINGLETON_HELD",
        ):
            second.acquire()
    finally:
        second.close()
        first.close()


def test_final_manifest_or_kill_expiry_after_provider_start_blocks_workers(
    monkeypatch,
):
    attested_verified, args, evidence = _launcher_attestation_fixture(monkeypatch)
    attestation = service_module._issue_launcher_cutover_attestation(
        verified=attested_verified,
        args=args,
        process_probe=lambda: evidence,
        cutover_probe=lambda: evidence["cutover"],
        wall_clock=lambda: NOW,
    )
    verified = SimpleNamespace(
        paper_order_submission_authorized=True,
        expected_account_id=ACCOUNT,
        activation_generation=GENERATION,
        manifest_sha256=SHA_A,
        receipt_hashes={"kill_switch": SHA_B},
        generated_at=NOW - timedelta(seconds=5),
    )
    reloads = []

    def reload_authority(*_args, **_kwargs):
        reloads.append("reload")
        if len(reloads) == 2:
            raise CapturedAlpacaPaperServiceError(
                "FINAL_ACTIVATION_REVALIDATION_FAILED",
                "kill authority expired",
            )
        return verified

    events = []

    class _Supervisor:
        def start_active(self, *, start_authority, provider_options=None):
            events.append("provider_and_runtime_started")
            start_authority.consume()
            events.append("worker_started")
            return {"state": "active"}

        def close(self, **_kwargs):
            events.append("closed")
            return {"state": "stopped"}

    class _Store:
        def close(self):
            events.append("store_closed")

    composition = _CapturedPaperServiceComposition(
        supervisor=_Supervisor(),
        shared_capture_store=_Store(),
        adapter=object(),
        connection_generation_receipt={},
        phase_one_reconciliation_receipt={},
        restart_inventory_receipt={},
        database_engine=object(),
    )
    host_handshake = object.__new__(
        service_module._CapturedPaperHostActivationHandshake
    )
    monkeypatch.setattr(
        service_module, "_reload_final_activation_authority", reload_authority
    )
    monkeypatch.setattr(
        service_module,
        "_paper_broker_snapshot",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        service_module,
        "_assert_composition_broker_generation",
        lambda *_args, **_kwargs: None,
    )

    with pytest.raises(
        CapturedAlpacaPaperServiceError,
        match="FINAL_ACTIVATION_REVALIDATION_FAILED",
    ):
        service_module._execute_active_service(
            verified=verified,
            composition=composition,
            allowed_read_roots=[Path(service_module.__file__).resolve().parents[1]],
            launcher_attestation=attestation,
            host_activation_handshake=host_handshake,
            stop_event=service_module.threading.Event(),
            wall_clock=lambda: NOW + timedelta(seconds=1),
        )

    assert reloads == ["reload", "reload"]
    assert events == [
        "provider_and_runtime_started",
        "closed",
        "store_closed",
    ]


def test_host_permit_is_consumed_before_any_worker_and_started_ack(
    monkeypatch,
) -> None:
    attested_verified, args, evidence = _launcher_attestation_fixture(monkeypatch)
    attestation = service_module._issue_launcher_cutover_attestation(
        verified=attested_verified,
        args=args,
        process_probe=lambda: evidence,
        cutover_probe=lambda: evidence["cutover"],
        wall_clock=lambda: NOW,
    )
    verified = SimpleNamespace(
        paper_order_submission_authorized=True,
        expected_account_id=ACCOUNT,
        activation_generation=GENERATION,
        manifest_sha256=SHA_A,
        receipt_hashes={"kill_switch": SHA_B},
        generated_at=NOW - timedelta(seconds=5),
    )
    events: list[str] = []
    handshake = object.__new__(
        service_module._CapturedPaperHostActivationHandshake
    )
    handshake.publish_prepared = lambda: events.append("prepared")
    handshake.await_and_consume_permit = lambda: (
        events.append("permit_consumed") or {"permit_sha256": SHA_C}
    )
    handshake.assert_not_revoked = lambda: None
    handshake.assert_consumed_permit_current = lambda: None
    handshake.assert_active_start_evidence_current = lambda: None
    handshake.publish_active_start_evidence = lambda _authority: (
        events.append("evidence_published")
        or {"authority_sha256": SHA_A, "artifact_sha256": SHA_B}
    )
    handshake._quiet_horizon_event_sha256 = SHA_B
    handshake.publish_started = lambda *, health, active_start_authority: (
        events.append("started_ack") or {"state": "STARTED"}
    )
    handshake.await_and_consume_apply_completed_authority = lambda: (
        events.append("apply_committed") or {"event_sha256": SHA_A}
    )

    class _Supervisor:
        def start_active(self, *, start_authority, provider_options=None):
            events.append("provider_runtime_ready")
            receipt = start_authority.consume()
            assert receipt["host_activation_permit_consumed"] is True
            events.append("worker_started")
            return {"state": "active"}

        def assert_healthy(self):
            events.append("health_confirmed")
            return {"state": "active"}

        def close(self, **_kwargs):
            events.append("closed")
            return {"state": "stopped"}

    class _Store:
        def close(self):
            events.append("store_closed")

    composition = _CapturedPaperServiceComposition(
        supervisor=_Supervisor(),
        shared_capture_store=_Store(),
        adapter=object(),
        connection_generation_receipt={},
        phase_one_reconciliation_receipt={},
        restart_inventory_receipt={},
        database_engine=object(),
    )
    monkeypatch.setattr(
        service_module,
        "_reload_final_activation_authority",
        lambda *_args, **_kwargs: verified,
    )
    monkeypatch.setattr(
        service_module,
        "_paper_broker_snapshot",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        service_module,
        "_assert_composition_broker_generation",
        lambda *_args, **_kwargs: None,
    )
    fixed_authority = _active_start_authority_fixture(
        verified,
        permit_sha256=SHA_C,
        quiet_horizon_sha256=SHA_B,
    )
    monkeypatch.setattr(
        service_module,
        "_paper_broker_quiet_fixed_point",
        lambda *_args, **_kwargs: fixed_authority["broker_fixed_point"],
    )
    monkeypatch.setattr(
        service_module,
        "_paper_kill_switch_snapshot",
        lambda *_args, **_kwargs: fixed_authority["final_kill_switch_query"],
    )
    stopped = service_module.threading.Event()
    stopped.set()
    service_module._execute_active_service(
        verified=verified,
        composition=composition,
        allowed_read_roots=(Path(service_module.__file__).resolve().parents[1],),
        launcher_attestation=attestation,
        host_activation_handshake=handshake,
        stop_event=stopped,
        wall_clock=lambda: NOW + timedelta(seconds=1),
    )

    assert events.index("permit_consumed") < events.index("worker_started")
    assert events.index("health_confirmed") < events.index("started_ack")
    assert events.index("started_ack") < events.index("apply_committed")


def test_post_permit_terminal_order_blocks_every_worker(monkeypatch) -> None:
    attested_verified, args, evidence = _launcher_attestation_fixture(monkeypatch)
    attestation = service_module._issue_launcher_cutover_attestation(
        verified=attested_verified,
        args=args,
        process_probe=lambda: evidence,
        cutover_probe=lambda: evidence["cutover"],
        wall_clock=lambda: NOW,
    )
    verified = SimpleNamespace(
        paper_order_submission_authorized=True,
        expected_account_id=ACCOUNT,
        activation_generation=GENERATION,
        manifest_sha256=SHA_A,
        receipt_hashes={"kill_switch": SHA_B},
        generated_at=NOW - timedelta(seconds=5),
    )
    events: list[str] = []
    handshake = object.__new__(
        service_module._CapturedPaperHostActivationHandshake
    )
    handshake.publish_prepared = lambda: events.append("prepared")
    handshake.await_and_consume_permit = lambda: (
        events.append("permit_consumed") or {"permit_sha256": SHA_C}
    )
    handshake.assert_not_revoked = lambda: None
    handshake.assert_consumed_permit_current = lambda: None
    handshake._quiet_horizon_event_sha256 = SHA_B

    class _Supervisor:
        def start_active(self, *, start_authority, provider_options=None):
            events.append("provider_runtime_ready")
            start_authority.consume()
            events.append("worker_started")
            return {"state": "active"}

        def close(self, **_kwargs):
            events.append("closed")
            return {"state": "stopped"}

    class _Store:
        def close(self):
            events.append("store_closed")

    composition = _CapturedPaperServiceComposition(
        supervisor=_Supervisor(),
        shared_capture_store=_Store(),
        adapter=object(),
        connection_generation_receipt={},
        phase_one_reconciliation_receipt={},
        restart_inventory_receipt={},
        database_engine=object(),
    )
    monkeypatch.setattr(
        service_module,
        "_reload_final_activation_authority",
        lambda *_args, **_kwargs: verified,
    )
    monkeypatch.setattr(
        service_module,
        "_paper_broker_snapshot",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        service_module,
        "_assert_composition_broker_generation",
        lambda *_args, **_kwargs: None,
    )

    def reject_transition(*_args, **_kwargs):
        events.append("transition_census")
        raise CapturedAlpacaPaperServiceError(
            "BROKER_RECENT_ORDER_TRANSITION_PRESENT",
            "terminal legacy order observed",
        )

    monkeypatch.setattr(
        service_module, "_paper_broker_quiet_fixed_point", reject_transition
    )

    with pytest.raises(
        CapturedAlpacaPaperServiceError,
        match="BROKER_RECENT_ORDER_TRANSITION_PRESENT",
    ):
        service_module._execute_active_service(
            verified=verified,
            composition=composition,
            allowed_read_roots=(
                Path(service_module.__file__).resolve().parents[1],
            ),
            launcher_attestation=attestation,
            host_activation_handshake=handshake,
            stop_event=service_module.threading.Event(),
            wall_clock=lambda: NOW,
        )

    assert events == [
        "provider_runtime_ready",
        "prepared",
        "permit_consumed",
        "transition_census",
        "closed",
        "store_closed",
    ]
    assert "worker_started" not in events


def test_composition_uses_measured_capacity_and_one_exact_adapter_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, list[tuple[tuple, dict]]] = {}
    instances: dict[str, list[object]] = {}
    code_body = {"schema_version": "test-code-build.v1", "files": []}
    code_sha = sha256_json(code_body)
    policy_snapshot = {"policy_version": "test-adaptive-policy.v1"}
    policy_sha = sha256_json(policy_snapshot)
    fenced_body = {
        "schema_version": "chili.captured-paper-fenced-prestart.v1",
        "verdict": "CAPTURED_ALPACA_PAPER_FENCED_PRESTART_REVALIDATED",
        "account_scope": "alpaca:paper",
        "expected_account_id": ACCOUNT,
        "runtime_generation": GENERATION,
        "baseline_restart_gate_receipt_sha256": SHA_A,
        "restart_gate_receipt_sha256": SHA_B,
        "admission_inventory_sha256": SHA_C,
        "initial_recovery_count": 0,
        "initial_recovery_inventory_sha256": SHA_A,
        "durable_admission_drift": False,
        "broker_inventory_flat": True,
        "paper_execution_only": True,
        "live_cash_authorized": False,
        "real_money_authorized": False,
    }
    monkeypatch.setattr(
        service_module,
        "_build_fenced_prestart_revalidation_receipt",
        lambda **_kwargs: {
            **fenced_body,
            "receipt_sha256": sha256_json(fenced_body),
        },
    )

    def recording_type(name: str, *, runtime: bool = False):
        class _Recorded:
            def __init__(self, *args, **kwargs):
                calls.setdefault(name, []).append((args, kwargs))
                instances.setdefault(name, []).append(self)
                if runtime:
                    self.runtime = object()

        return _Recorded

    managed = []

    class _ManagedWorker:
        def __init__(self, *, name, worker):
            self.name = name
            self.worker = worker
            managed.append(self)

    runtime_owner_type = recording_type("runtime_owner", runtime=True)
    supervisor_type = recording_type("supervisor")

    class service_fence_type:
        def __init__(self, *args, **kwargs):
            calls.setdefault("service_fence", []).append((args, kwargs))
            instances.setdefault("service_fence", []).append(self)

        def assert_held(self):
            return None
    transport_store_type = recording_type("transport_store")
    entry_transport_type = recording_type("entry_transport")
    transport_coordinator_type = recording_type("transport_coordinator")
    transport_worker_type = recording_type("transport_worker")
    post_commit_worker_type = recording_type("post_commit_worker")
    acceptance_type = recording_type("acceptance")
    class fill_capture_type(recording_type("fill_capture")):
        def complete_exit_post_commit(self, _request):
            return {"ok": True}

        def recover_exit_owner_inventory_bounded(self, **_kwargs):
            body = {
                "exit_owner_inventory_resolved": True,
                "exit_owner_recovery_bounded": True,
                "exit_owner_recovery_exhausted": False,
                "paper_order_submission_authorized": False,
                "live_cash_authorized": False,
                "real_money_authorized": False,
            }
            return {**body, "receipt_sha256": sha256_json(body)}

    exit_owner_worker_type = recording_type("exit_owner_worker")
    financial_breaker_type = recording_type("financial_breaker")
    initial_candidate_reader_type = recording_type("initial_candidate_reader")
    deferred_reader_type = recording_type("deferred_candidate_reader")
    class selection_worker_type:
        def __init__(self, *args, **kwargs):
            calls.setdefault("selection_worker", []).append((args, kwargs))
            instances.setdefault("selection_worker", []).append(self)

        def rollback_after_quiesce(self):
            body = {
                "schema_version": (
                    "chili.captured-paper-selection-runtime-rollback.v2"
                ),
                "account_scope": "alpaca:paper",
                "expected_account_id": ACCOUNT,
                "activation_generation": GENERATION,
                "variant_application_sha256": SHA_A,
                "variant_rollback_sha256": SHA_B,
                "target_variant_ids": [31, 32],
                "application_outcome": "rolled_back",
                "strategy_variants_deactivated": True,
                "paper_order_submission_authorized": False,
                "live_cash_authorized": False,
                "real_money_authorized": False,
            }
            return {
                **body,
                "runtime_rollback_sha256": sha256_json(body),
            }

    class _InitialControllerPolicy:
        def __init__(self, **kwargs):
            calls.setdefault("initial_controller_policy", []).append(((), kwargs))
            instances.setdefault("initial_controller_policy", []).append(self)

    class _InitialController:
        def __init__(self, **kwargs):
            calls.setdefault("initial_controller", []).append(((), kwargs))
            instances.setdefault("initial_controller", []).append(self)

        def admit(self, **_kwargs):
            return {"admitted": False}
    watch_store_type = recording_type("watch_store")
    watch_reader_type = recording_type("watch_reader")
    watch_coordinator_type = recording_type("watch_coordinator")
    watch_worker_type = recording_type("watch_worker")

    start_calls = []

    def start(**kwargs):
        start_calls.append(dict(kwargs))
        return True

    stop = lambda: True
    health_calls = []

    def healthy(**kwargs):
        health_calls.append(dict(kwargs))
        return True
    exit_transport_handler = lambda _request: {"ok": True}

    def build_exit_transport_handler(**kwargs):
        calls.setdefault("exit_transport_handler", []).append(((), kwargs))
        return exit_transport_handler

    runtime_modules = {
        "iqfeed_capture_host": SimpleNamespace(
            IqfeedCapturedPaperRuntimeOwner=runtime_owner_type
        ),
        "captured_paper_transport": SimpleNamespace(
            SqlAlchemyCapturedPaperTransportStore=transport_store_type,
            ExactAlpacaPaperEntryTransport=entry_transport_type,
            CapturedPaperTransportCoordinator=transport_coordinator_type,
        ),
        "captured_paper_transport_worker": SimpleNamespace(
            CapturedPaperTransportWorker=transport_worker_type
        ),
        "captured_paper_post_commit_worker": SimpleNamespace(
            CapturedPaperPostCommitWorker=post_commit_worker_type
        ),
        "captured_paper_positive_acceptance": SimpleNamespace(
            SqlAlchemyCapturedPaperPositiveAcceptanceRecorder=acceptance_type
        ),
        "captured_paper_fill_capture": SimpleNamespace(
            SqlAlchemyCapturedPaperFillCapture=fill_capture_type,
            CapturedPaperExitOwnerWorker=exit_owner_worker_type,
        ),
        "captured_paper_financial_breaker": SimpleNamespace(
            SqlAlchemyCapturedPaperFinancialBreakerIssuer=(
                financial_breaker_type
            )
        ),
        "captured_paper_initial_candidate_reader": SimpleNamespace(
            SqlAlchemyCapturedPaperInitialCandidateReader=(
                initial_candidate_reader_type
            )
        ),
        "captured_paper_selection_runtime": SimpleNamespace(
            DeferredCapturedPaperInitialCandidateReader=deferred_reader_type,
            CapturedPaperSelectionLifecycleWorker=selection_worker_type,
            CapturedPaperSelectionApplicationSetup=object,
            CapturedPaperSelectionRuntimeComponents=object,
        ),
        "captured_paper_selection_source": SimpleNamespace(
            SqlAlchemyCapturedViabilitySnapshotSource=object,
        ),
        "captured_paper_selection_queue": SimpleNamespace(
            CapturedPaperSelectionQueuePublisher=object,
            CapturedPaperSelectionQueueWriter=object,
            CapturedPaperSelectionQueueInputPort=object,
        ),
        "captured_paper_selection_producer": SimpleNamespace(
            CapturedPaperSelectionAuthority=object,
            CapturedPaperSelectionVariantBinding=object,
            CapturedPaperSelectionProducer=object,
        ),
        "captured_paper_variant_binding": SimpleNamespace(),
        "momentum_viability": SimpleNamespace(
            ViabilitySettingsProjection=SimpleNamespace(
                from_runtime=lambda _settings: object()
            )
        ),
        "replay_capture_contract": SimpleNamespace(CaptureRunIdentity=object),
        "replay_capture_runtime": SimpleNamespace(BoundedCaptureIngress=object),
        "app_db": SimpleNamespace(SessionLocal=lambda: None),
        "yf_session": SimpleNamespace(
            get_fundamentals_receipt=lambda _symbol: None
        ),
        "captured_paper_initial_controller": SimpleNamespace(
            CapturedPaperInitialAdmissionController=_InitialController,
            CapturedPaperInitialControllerPolicy=_InitialControllerPolicy,
        ),
        "captured_paper_fill_watch": SimpleNamespace(
            SqlAlchemyCapturedPaperCompletedFillWatchStore=watch_store_type,
            ExactAlpacaPaperCompletedFillWatchReader=watch_reader_type,
            CapturedPaperCompletedFillWatchCoordinator=watch_coordinator_type,
            CapturedPaperCompletedFillWatchWorker=watch_worker_type,
        ),
        "captured_paper_service_supervisor": SimpleNamespace(
            CapturedPaperManagedWorker=_ManagedWorker,
            CapturedPaperServiceSupervisor=supervisor_type,
        ),
        "captured_paper_service_fence": SimpleNamespace(
            CapturedPaperServiceFence=service_fence_type,
        ),
        "captured_paper_restart_inventory": SimpleNamespace(),
        "live_runner": SimpleNamespace(
            build_captured_paper_exit_transport_post_commit_handler=(
                build_exit_transport_handler
            )
        ),
        "live_runner_loop": SimpleNamespace(
            start_captured_paper_live_runner_loop=start,
            stop_live_runner_loop=stop,
            is_captured_paper_live_runner_loop_admission_ready=healthy,
        ),
    }
    adapter = object()
    store = object()

    class _Host:
        composition = SimpleNamespace(
            binding=SimpleNamespace(
                budget=SimpleNamespace(
                    derived_hot_symbol_capacity=7,
                    max_writer_threads=8,
                    max_queue_events=4096,
                    async_queue_bytes=8 * 1024 * 1024,
                )
            )
        )

        @staticmethod
        def captured_paper_config_sha256_for(_symbol):
            return SHA_A

    operational = SimpleNamespace(
        action_claim_lease_seconds=30,
        reconciliation_retry_delay_seconds=5,
    )
    prepared = _PreparedCapturedPaperCapture(
        preflight=SimpleNamespace(
            startup_process_instance_id=GENERATION,
            run_configuration={
                "writer_batch_events": 256,
                "writer_batch_bytes": 1024 * 1024,
                "writer_poll_seconds": 0.05,
                "writer_flush_interval_seconds": 0.25,
            },
        ),
        host=_Host(),
        shared_store=store,
        adapter=adapter,
        broker_snapshot={
            "connection_generation": "alpaca-paper-rest:" + "d" * 64,
            "connection_receipt": {"receipt_sha256": SHA_A},
        },
        policy_authority=_CapturedPaperPolicyAuthority(
            policy_receipt=SimpleNamespace(
                policy=SimpleNamespace(
                    context_data_max_age_seconds=60.0,
                    policy_sha256=policy_sha,
                ),
                to_settings_projection=lambda: {
                    "policy_snapshot": dict(policy_snapshot)
                },
            ),
            policy_spec=object(),
            operational_policy=operational,
            feature_flags={},
            feature_flags_sha256=SHA_A,
        ),
    )
    verified = SimpleNamespace(
        expected_account_id=ACCOUNT,
        code_build_sha256=code_sha,
        settings_projection_sha256=SHA_B,
        capture_receipt_sha256=SHA_C,
        activation_generation=GENERATION,
        manifest={
            "code_build": {**code_body, "code_build_sha256": code_sha}
        },
    )
    engine = object()
    factory = object()
    composition = _assemble_service_composition(
        verified=verified,
        prepared=prepared,
        phase_one_reconciliation_receipt={"receipt_sha256": SHA_A},
        restart_inventory_receipt={"receipt_sha256": SHA_B},
        production_material_factory=factory,
        runtime_modules=runtime_modules,
        settings=SimpleNamespace(
            chili_momentum_captured_paper_worker_idle_poll_seconds=0.25,
            chili_autotrader_user_id=41,
            chili_iqfeed_l1_authoritative_bridge_build=(
                "iqfeed-l1-exact-print-provenance-v3+sha256:0123456789abcdef"
            ),
            chili_momentum_captured_paper_trigger_max_attempts=3,
            chili_momentum_captured_paper_trigger_retry_delay_seconds=0.01,
            chili_momentum_captured_paper_trigger_future_tolerance_seconds=1.0,
            chili_momentum_captured_paper_trigger_exact_print_window_seconds=0.001,
            chili_tenbeat_entry_tilt_weight=0.0,
        ),
        database_engine=engine,
        assert_external_authority_current=lambda: None,
        acquire_external_dispatch_authority=nullcontext,
        wall_clock=lambda: NOW,
        monotonic_clock=lambda: 1.0,
    )

    owner_kwargs = calls["runtime_owner"][0][1]
    assert owner_kwargs["adapter_factory"]() is adapter
    assert owner_kwargs["decision_max_entries"] == 7
    assert owner_kwargs["admission_max_entries"] == 7
    assert owner_kwargs["production_material_factory"] is factory
    assert owner_kwargs["financial_breaker_issuer"] is instances[
        "financial_breaker"
    ][0]
    assert owner_kwargs["financial_breaker_clock"] is not None
    assert owner_kwargs["assert_service_fence_held"].__self__ is instances[
        "service_fence"
    ][0]
    assert owner_kwargs["allow_manual_staging"] is False
    assert "initial_candidate_reader" not in calls
    initial_kwargs = calls["initial_controller"][0][1]
    assert initial_kwargs["candidate_reader"] is instances[
        "deferred_candidate_reader"
    ][0]
    assert initial_kwargs["assert_service_fence_held"].__self__ is instances[
        "service_fence"
    ][0]
    assert calls["fill_capture"][0][1]["adapter"] is adapter
    assert calls["fill_capture"][0][1]["max_pending_reads"] == 7
    assert calls["financial_breaker"][0][0] == (engine,)
    assert calls["financial_breaker"][0][1]["observation_clock"] is not None
    assert calls["service_fence"][0][0] == (engine,)
    assert calls["supervisor"][0][1]["service_fence"] is instances[
        "service_fence"
    ][0]
    assert calls["transport_coordinator"][0][1][
        "financial_breaker_issuer"
    ] is instances["financial_breaker"][0]
    assert calls["entry_transport"][0][1]["adapter"] is adapter
    assert calls["watch_reader"][0][1]["adapter"] is adapter
    assert calls["transport_worker"][0][1]["recovery_limit"] == 7
    assert calls["post_commit_worker"][0][1]["owner"] is instances[
        "runtime_owner"
    ][0]
    assert calls["post_commit_worker"][0][1]["max_items_per_cycle"] == 7
    assert [item.name for item in managed] == [
        "post_commit",
        "transport",
        "later_fill",
        "exit_owner",
        "selection",
    ]
    selection_kwargs = calls["selection_worker"][0][1]
    assert selection_kwargs["shared_capture_runtime"] is store
    assert selection_kwargs["deferred_reader"] is instances[
        "deferred_candidate_reader"
    ][0]
    assert selection_kwargs["assert_service_fence_held"].__self__ is instances[
        "service_fence"
    ][0]
    supervisor_kwargs = calls["supervisor"][0][1]
    fenced_prestart = supervisor_kwargs["fenced_prestart_revalidate"]()
    fenced_body = dict(fenced_prestart)
    supplied_fenced_sha = fenced_body.pop("receipt_sha256")
    assert set(fenced_body) == {
        "schema_version",
        "verdict",
        "account_scope",
        "expected_account_id",
        "runtime_generation",
        "baseline_restart_gate_receipt_sha256",
        "restart_gate_receipt_sha256",
        "admission_inventory_sha256",
        "initial_recovery_count",
        "initial_recovery_inventory_sha256",
        "durable_admission_drift",
        "broker_inventory_flat",
        "paper_execution_only",
        "live_cash_authorized",
        "real_money_authorized",
    }
    assert sha256_json(fenced_body) == supplied_fenced_sha
    assert [
        item.name for item in supervisor_kwargs["active_pre_authority_workers"]
    ] == ["selection"]
    assert callable(supervisor_kwargs["post_quiesce_before_fence_release"])
    post_quiesce = supervisor_kwargs["post_quiesce_before_fence_release"]()
    assert post_quiesce["schema_version"] == (
        "chili.captured-paper-post-quiesce.v3"
    )
    assert post_quiesce["variant_application_sha256"] == SHA_A
    assert post_quiesce["variant_rollback_sha256"] == SHA_B
    assert post_quiesce["target_variant_ids"] == [31, 32]
    post_body = dict(post_quiesce)
    supplied_post_sha = post_body.pop("receipt_sha256")
    assert sha256_json(post_body) == supplied_post_sha
    assert supervisor_kwargs["live_loop_start"]() is True
    assert supervisor_kwargs["live_loop_health"]() is True
    expected_scope = {
        "expected_account_id": ACCOUNT,
        "runtime_generation": GENERATION,
        "execution_family": "alpaca_spot",
    }
    assert len(start_calls) == 1
    assert {
        key: start_calls[0][key] for key in expected_scope
    } == expected_scope
    assert start_calls[0]["captured_paper_symbol_admitter"].__self__ is instances[
        "initial_controller"
    ][0]
    assert len(health_calls) == 1
    assert {
        key: health_calls[0][key] for key in expected_scope
    } == expected_scope
    assert health_calls[0]["broker_connection_generation"] == (
        "alpaca-paper-rest:" + "d" * 64
    )
    assert health_calls[0]["captured_paper_exit_completion_handler"] is (
        start_calls[0]["captured_paper_exit_completion_handler"]
    )
    assert start_calls[0]["captured_paper_exit_transport_handler"] is (
        exit_transport_handler
    )
    assert health_calls[0]["captured_paper_exit_transport_handler"] is (
        exit_transport_handler
    )
    assert composition.shared_capture_store is store
    assert composition.adapter is adapter


def test_service_composition_wires_one_exit_owner_store_into_runner_reconciler_and_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One service-owned exit capability must back every completion path."""

    original_assemble = _assemble_service_composition
    observed: dict[str, object] = {
        "captures": [],
        "workers": [],
        "managed": [],
        "starts": [],
        "health": [],
    }

    def instrumented_assemble(*args, **kwargs):
        runtime_modules = kwargs["runtime_modules"]
        fill_module = runtime_modules["captured_paper_fill_capture"]
        original_capture_type = fill_module.SqlAlchemyCapturedPaperFillCapture
        original_managed_type = runtime_modules[
            "captured_paper_service_supervisor"
        ].CapturedPaperManagedWorker
        original_start = runtime_modules[
            "live_runner_loop"
        ].start_captured_paper_live_runner_loop
        original_health = runtime_modules[
            "live_runner_loop"
        ].is_captured_paper_live_runner_loop_admission_ready

        class _InstrumentedFillCapture(original_capture_type):
            def __init__(self, *capture_args, **capture_kwargs):
                super().__init__(*capture_args, **capture_kwargs)
                observed["captures"].append(self)

            def complete_exit_post_commit(self, _request):
                return {"ok": True}

        class _ExitOwnerWorker:
            def __init__(self, *worker_args, **worker_kwargs):
                observed["workers"].append((worker_args, worker_kwargs, self))

        def managed_worker(*managed_args, **managed_kwargs):
            item = original_managed_type(*managed_args, **managed_kwargs)
            observed["managed"].append(item)
            return item

        def start_probe(**start_kwargs):
            observed["starts"].append(dict(start_kwargs))
            return original_start(**start_kwargs)

        def health_probe(**health_kwargs):
            observed["health"].append(dict(health_kwargs))
            return original_health(**health_kwargs)

        fill_module.SqlAlchemyCapturedPaperFillCapture = _InstrumentedFillCapture
        fill_module.CapturedPaperExitOwnerWorker = _ExitOwnerWorker
        runtime_modules[
            "captured_paper_service_supervisor"
        ].CapturedPaperManagedWorker = managed_worker
        runtime_modules[
            "live_runner_loop"
        ].start_captured_paper_live_runner_loop = start_probe
        runtime_modules[
            "live_runner_loop"
        ].is_captured_paper_live_runner_loop_admission_ready = health_probe
        return original_assemble(*args, **kwargs)

    monkeypatch.setattr(
        sys.modules[__name__],
        "_assemble_service_composition",
        instrumented_assemble,
    )
    test_composition_uses_measured_capacity_and_one_exact_adapter_generation(
        monkeypatch
    )

    captures = observed["captures"]
    workers = observed["workers"]
    starts = observed["starts"]
    health = observed["health"]
    managed = observed["managed"]
    assert len(captures) == 1
    assert len(workers) == 1
    assert len(starts) == 1
    assert len(health) == 1
    worker_kwargs = workers[0][1]
    start_handler = starts[0]["captured_paper_exit_completion_handler"]
    health_handler = health[0]["captured_paper_exit_completion_handler"]
    start_transport_handler = starts[0][
        "captured_paper_exit_transport_handler"
    ]
    health_transport_handler = health[0][
        "captured_paper_exit_transport_handler"
    ]
    assert start_handler is health_handler
    assert start_handler.__self__ is captures[0]
    assert worker_kwargs["fill_capture"] is captures[0]
    assert worker_kwargs["expected_account_id"] == ACCOUNT
    assert worker_kwargs["runtime_generation"] == GENERATION
    assert worker_kwargs["execution_family"] == "alpaca_spot"
    assert start_transport_handler is health_transport_handler
    assert any(
        item.name == "exit_owner" and item.worker is workers[0][2]
        for item in managed
    )


def test_service_builder_owns_callback_free_factory_and_cleans_failed_prepare(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closes: list[str] = []

    class _Closable:
        def __init__(self, name):
            self.name = name

        def close(self):
            closes.append(self.name)

    adapter = object()
    host = _Closable("host")
    store = _Closable("store")
    prepared = _PreparedCapturedPaperCapture(
        preflight=SimpleNamespace(),
        host=host,
        shared_store=store,
        adapter=adapter,
        broker_snapshot={},
        policy_authority=_CapturedPaperPolicyAuthority(
            policy_receipt=SimpleNamespace(),
            policy_spec="policy-spec",
            operational_policy="operational-policy",
            feature_flags={},
            feature_flags_sha256=SHA_A,
        ),
    )
    verified = SimpleNamespace(settings_projection_sha256=SHA_A)
    monkeypatch.setattr(
        service_module,
        "_prepare_capture_components",
        lambda **_kwargs: prepared,
    )
    monkeypatch.setattr(
        service_module,
        "_verify_loaded_module_role",
        lambda *_args, **_kwargs: None,
    )

    observed: dict = {}

    def builder(**kwargs):
        observed.update(kwargs)
        raise RuntimeError("factory rejected")

    modules = {
        "captured_paper_production_provider": SimpleNamespace(
            build_live_fsm_captured_paper_service_material_factory=builder
        ),
        "app_db": SimpleNamespace(engine=object()),
    }
    with pytest.raises(RuntimeError, match="factory rejected"):
        _build_service_composition(
            verified=verified,
            projection={},
            runtime_modules=modules,
            allowed_read_roots=(r"D:\sealed",),
            assert_external_authority_current=lambda: None,
            acquire_external_dispatch_authority=nullcontext,
            wall_clock=lambda: NOW,
            monotonic_clock=lambda: 1.0,
        )

    assert observed["host"] is host
    assert observed["settings_projection_sha256"] == SHA_A
    assert observed["raw_adapter_factory"]() is adapter
    assert observed["policy_spec"] == "policy-spec"
    assert observed["operational_policy"] == "operational-policy"
    assert closes == ["host", "store"]


def test_service_builder_reconciles_phase_one_before_bracket_and_workers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    prepared = _PreparedCapturedPaperCapture(
        preflight=SimpleNamespace(),
        host=SimpleNamespace(),
        shared_store=SimpleNamespace(),
        adapter=SimpleNamespace(),
        broker_snapshot={},
        policy_authority=_CapturedPaperPolicyAuthority(
            policy_receipt=SimpleNamespace(),
            policy_spec="policy",
            operational_policy="operational",
            feature_flags={},
            feature_flags_sha256=SHA_A,
        ),
    )
    verified = SimpleNamespace(
        settings_projection_sha256=SHA_A,
        activation_generation=GENERATION,
    )
    monkeypatch.setattr(
        service_module, "_prepare_capture_components", lambda **_kwargs: prepared
    )
    monkeypatch.setattr(
        service_module, "_verify_loaded_module_role", lambda *_args, **_kwargs: None
    )

    def build_factory(**_kwargs):
        events.append("provider_factory")
        return object()

    def reconcile(_bind, **_kwargs):
        events.append("phase_one")
        return _phase_one_restart_receipt()

    def bracket(**_kwargs):
        events.append("restart_inventory")
        return {
            "disposition": "strict_flat_first_cutover",
            "recovery_required": False,
            "new_admissions_quarantined": False,
            "exposure_decreasing_only": False,
            "broker_inventory_flat": True,
            "receipt_sha256": SHA_B,
        }

    sentinel = object()

    def assemble(**_kwargs):
        events.append("assemble_workers")
        return sentinel

    monkeypatch.setattr(
        service_module, "_build_bracketed_restart_inventory_receipt", bracket
    )
    monkeypatch.setattr(service_module, "_assemble_service_composition", assemble)
    modules = {
        "captured_paper_production_provider": SimpleNamespace(
            build_live_fsm_captured_paper_service_material_factory=build_factory
        ),
        "captured_paper_phase_one_handoff": SimpleNamespace(
            reconcile_captured_paper_phase_one_after_restart=reconcile
        ),
        "captured_paper_restart_inventory": SimpleNamespace(),
        "app_db": SimpleNamespace(engine=object()),
    }

    result = _build_service_composition(
        verified=verified,
        projection={},
        runtime_modules=modules,
        allowed_read_roots=(r"D:\sealed",),
        assert_external_authority_current=lambda: None,
        acquire_external_dispatch_authority=nullcontext,
        wall_clock=lambda: NOW,
        monotonic_clock=lambda: 1.0,
    )

    assert result is sentinel
    assert events == [
        "provider_factory",
        "phase_one",
        "restart_inventory",
        "assemble_workers",
    ]


def test_service_builder_stops_owned_restart_before_worker_assembly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    prepared = _PreparedCapturedPaperCapture(
        preflight=SimpleNamespace(),
        host=SimpleNamespace(close=lambda: events.append("host_close")),
        shared_store=SimpleNamespace(close=lambda: events.append("store_close")),
        adapter=SimpleNamespace(),
        broker_snapshot={},
        policy_authority=_CapturedPaperPolicyAuthority(
            policy_receipt=SimpleNamespace(),
            policy_spec="policy",
            operational_policy="operational",
            feature_flags={},
            feature_flags_sha256=SHA_A,
        ),
    )
    verified = SimpleNamespace(
        settings_projection_sha256=SHA_A,
        activation_generation=GENERATION,
    )
    monkeypatch.setattr(
        service_module, "_prepare_capture_components", lambda **_kwargs: prepared
    )
    monkeypatch.setattr(
        service_module, "_verify_loaded_module_role", lambda *_args, **_kwargs: None
    )
    modules = {
        "captured_paper_production_provider": SimpleNamespace(
            build_live_fsm_captured_paper_service_material_factory=(
                lambda **_kwargs: object()
            )
        ),
        "captured_paper_phase_one_handoff": SimpleNamespace(
            reconcile_captured_paper_phase_one_after_restart=(
                lambda *_args, **_kwargs: _phase_one_restart_receipt()
            )
        ),
        "captured_paper_restart_inventory": SimpleNamespace(),
        "app_db": SimpleNamespace(engine=object()),
    }
    monkeypatch.setattr(
        service_module,
        "_build_bracketed_restart_inventory_receipt",
        lambda **_kwargs: {
            "disposition": "owned_restart_recovery",
            "recovery_required": True,
            "new_admissions_quarantined": True,
            "exposure_decreasing_only": True,
            "broker_inventory_flat": False,
            "receipt_sha256": SHA_B,
        },
    )
    monkeypatch.setattr(
        service_module,
        "_assemble_service_composition",
        lambda **_kwargs: events.append("assembled"),
    )

    with pytest.raises(
        CapturedAlpacaPaperServiceError, match="quarantined recovery"
    ):
        _build_service_composition(
            verified=verified,
            projection={},
            runtime_modules=modules,
            allowed_read_roots=(r"D:\sealed",),
            assert_external_authority_current=lambda: None,
            acquire_external_dispatch_authority=nullcontext,
            wall_clock=lambda: NOW,
            monotonic_clock=lambda: 1.0,
        )

    assert "assembled" not in events
    assert events == ["host_close", "store_close"]


def test_service_start_blocks_on_missing_migration_354_or_owner_inventory_unresolved_after_bounded_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A nominally-flat broker receipt cannot hide unresolved durable exits."""

    original_assemble = _assemble_service_composition

    def unresolved_owner_inventory_assemble(*args, **kwargs):
        fill_module = kwargs["runtime_modules"]["captured_paper_fill_capture"]
        original_capture_type = fill_module.SqlAlchemyCapturedPaperFillCapture

        class _UnresolvedFillCapture(original_capture_type):
            def recover_exit_owner_inventory_bounded(self, **_kwargs):
                body = {
                    "exit_owner_inventory_resolved": False,
                    "exit_owner_recovery_bounded": True,
                    "exit_owner_recovery_exhausted": True,
                    "paper_order_submission_authorized": False,
                    "live_cash_authorized": False,
                    "real_money_authorized": False,
                }
                return {**body, "receipt_sha256": sha256_json(body)}

        fill_module.SqlAlchemyCapturedPaperFillCapture = (
            _UnresolvedFillCapture
        )
        return original_assemble(*args, **kwargs)

    monkeypatch.setattr(
        sys.modules[__name__],
        "_assemble_service_composition",
        unresolved_owner_inventory_assemble,
    )
    with pytest.raises(
        CapturedAlpacaPaperServiceError,
        match="exit-owner inventory",
    ):
        test_composition_uses_measured_capacity_and_one_exact_adapter_generation(
            monkeypatch
        )


def test_database_schema_fence_rejects_future_unknown_migration() -> None:
    required_tables = (
        "captured_paper_post_commit_outbox",
        "captured_paper_post_commit_outbox_events",
        "captured_paper_completed_fill_watch",
        "captured_paper_completed_fill_watch_events",
        "alpaca_paper_fill_activities",
        "alpaca_paper_fill_query_observations",
        "alpaca_paper_post_settlement_fill_contradictions",
        "captured_paper_selection_frontiers",
        "captured_paper_selection_frontier_events",
        "captured_paper_selection_route_states",
        "captured_paper_variant_application_receipts",
        "captured_paper_variant_application_events",
    )

    class _Result:
        def __init__(self, rows):
            self.rows = rows

        def fetchall(self):
            return self.rows

    class _Connection:
        def __init__(self, versions, *, missing_tables=()):
            self.versions = versions
            self.missing_tables = frozenset(missing_tables)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, statement):
            if "schema_version" in statement:
                return _Result([(value,) for value in self.versions])
            return _Result(
                [
                    (name, name not in self.missing_tables)
                    for name in required_tables
                ]
            )

    class _Engine:
        def __init__(self, versions, *, missing_tables=()):
            self.versions = versions
            self.missing_tables = tuple(missing_tables)

        def connect(self):
            return _Connection(
                self.versions,
                missing_tables=self.missing_tables,
            )

    migrations = SimpleNamespace(
        MIGRATIONS=(("001", object()), ("002", object())),
        text=lambda value: value,
        _verify_migration_354_physical_contract=lambda _connection: None,
    )
    result = _verify_database_schema(
        _Engine(("001", "002")), migrations_module=migrations
    )
    assert result["latest_migration"] == "002"

    with pytest.raises(CapturedAlpacaPaperServiceError, match="exact code generation"):
        _verify_database_schema(
            _Engine(("001", "002", "future_999")),
            migrations_module=migrations,
        )

    with pytest.raises(CapturedAlpacaPaperServiceError, match="exact code generation"):
        _verify_database_schema(
            _Engine(
                ("001", "002"),
                missing_tables=("captured_paper_selection_route_states",),
            ),
            migrations_module=migrations,
        )


def test_policy_and_startup_evidence_bind_same_code_config_and_account() -> None:
    from app.config import settings
    from app.services.trading.momentum_neural.adaptive_risk_policy import (
        adaptive_risk_policy_settings_projection,
    )

    operational_names = (
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
    projection = {
        "schema_version": "chili.captured-paper-settings-projection.v1",
        "adaptive_risk_policy": adaptive_risk_policy_settings_projection(settings),
        "captured_paper_operational_policy": {
            name: getattr(settings, name) for name in operational_names
        },
        "settings_projection_sha256": SHA_A,
    }
    code_body = {
        "schema_version": "chili.captured-paper-code-build.v1",
        "artifacts": [],
    }
    code_sha = sha256_json(code_body)
    verified = SimpleNamespace(
        settings_projection_sha256=SHA_A,
        code_build_sha256=code_sha,
        expected_account_id=ACCOUNT,
        activation_generation=GENERATION,
        manifest_sha256=SHA_B,
        capture_receipt_sha256=SHA_C,
        manifest={
            "code_build": {**code_body, "code_build_sha256": code_sha}
        },
    )
    runtime_modules = {
        "adaptive_risk_policy": importlib.import_module(
            "app.services.trading.momentum_neural.adaptive_risk_policy"
        ),
        "captured_adaptive_risk_source": importlib.import_module(
            "app.services.trading.momentum_neural.captured_adaptive_risk_source"
        ),
        "captured_paper_admission": importlib.import_module(
            "app.services.trading.momentum_neural.captured_paper_admission"
        ),
    }
    authority = _build_policy_authority(
        verified=verified,
        projection=projection,
        runtime_modules=runtime_modules,
        settings=settings,
    )

    class _Bootstrap:
        CapturedPaperStartupEvidence = SimpleNamespace

    broker = {
        "account_equity": 100_000.0,
        "account_last_equity": 99_000.0,
        "account_buying_power": 400_000.0,
        "account_cash": 100_000.0,
        "broker_day_change": 1_000.0,
        "account_status": "ACTIVE",
        "account_blocked": False,
        "trading_blocked": False,
        "transfers_blocked": False,
        "trade_suspended_by_user": False,
        "account_retrieved_at": NOW.isoformat(),
        "connection_generation": "alpaca-paper-rest:" + "d" * 64,
        "connection_receipt_sha256": "d" * 64,
        "open_order_census_sha256": "e" * 64,
        "open_order_inventory_sha256": "f" * 64,
    }
    evidence = _build_startup_evidence(
        verified=verified,
        preflight=SimpleNamespace(
            startup_generation=7,
            startup_process_instance_id=GENERATION,
        ),
        broker_snapshot=broker,
        policy_authority=authority,
        bootstrap_module=_Bootstrap,
    )
    assert evidence.code_build == code_body
    assert sha256_json(evidence.code_build) == code_sha
    assert evidence.account_identity["account_id"] == ACCOUNT
    assert evidence.activation_generation == 7
