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
    _paper_kill_switch_snapshot,
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

    def __init__(self, *, orders: list[dict] | None = None) -> None:
        self.orders = list(orders or [])
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


def _verified_stub() -> SimpleNamespace:
    return SimpleNamespace(
        expected_account_id=ACCOUNT,
        activation_generation=GENERATION,
        manifest_sha256=SHA_A,
    )


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
    authorization = {
        "schema_version": "chili.captured-paper-host-cutover-journal-event.v1",
        "transaction_id": transaction_id,
        "sequence": 1,
        "previous_event_sha256": "0" * 64,
        "event_type": "activation_permit_issued",
        "recorded_at": issued_at,
        "payload": authorization_payload,
    }
    authorization["event_sha256"] = sha256_json(authorization)
    journal_path.write_bytes(
        json.dumps(
            authorization,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )
    permit = {
        "schema_version": "chili.captured-paper-host-startup-permit.v1",
        "state": "ACTIVATION_PERMITTED",
        **authorization_payload,
        "journal_path": str(journal_path),
        "journal_transaction_id": transaction_id,
        "journal_authorization_sequence": 1,
        "journal_authorization_event_sha256": authorization["event_sha256"],
        "journal_authorization_event": authorization,
    }
    permit["permit_sha256"] = sha256_json(permit)
    _publish_canonical_json_once(handshake.permit_path, permit)
    return permit


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
    with handshake.hold_dispatch_authority():
        assert handshake.dispatch_lock_path.exists()

    class _BrokerBodyFailure(RuntimeError):
        pass

    with pytest.raises(_BrokerBodyFailure, match="broker lifecycle"):
        with handshake.hold_dispatch_authority():
            raise _BrokerBodyFailure("broker lifecycle")

    started = handshake.publish_started(health={"state": "active"})
    assert started["state"] == "STARTED"
    assert started["activation_permit_sha256"] == permit["permit_sha256"]
    assert started["workers_started"] is True
    assert started["paper_execution_started"] is True
    assert handshake.permit_path.exists()


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
                assert_not_revoked=lambda: None,
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
        },
    }
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
        def start_active(self, *, start_authority):
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
    handshake.publish_started = lambda *, health: (
        events.append("started_ack") or {"state": "STARTED"}
    )

    class _Supervisor:
        def start_active(self, *, start_authority):
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


def test_composition_uses_measured_capacity_and_one_exact_adapter_generation() -> None:
    calls: dict[str, list[tuple[tuple, dict]]] = {}
    instances: dict[str, list[object]] = {}

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
    fill_capture_type = recording_type("fill_capture")
    financial_breaker_type = recording_type("financial_breaker")
    initial_candidate_reader_type = recording_type("initial_candidate_reader")

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
            SqlAlchemyCapturedPaperFillCapture=fill_capture_type
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
                budget=SimpleNamespace(derived_hot_symbol_capacity=7)
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
        preflight=SimpleNamespace(startup_process_instance_id=GENERATION),
        host=_Host(),
        shared_store=store,
        adapter=adapter,
        broker_snapshot={
            "connection_generation": "alpaca-paper-rest:" + "d" * 64,
            "connection_receipt": {"receipt_sha256": SHA_A},
        },
        policy_authority=_CapturedPaperPolicyAuthority(
            policy_receipt=SimpleNamespace(
                policy=SimpleNamespace(context_data_max_age_seconds=60.0)
            ),
            policy_spec=object(),
            operational_policy=operational,
            feature_flags={},
            feature_flags_sha256=SHA_A,
        ),
    )
    verified = SimpleNamespace(
        expected_account_id=ACCOUNT,
        code_build_sha256=SHA_A,
        settings_projection_sha256=SHA_B,
        capture_receipt_sha256=SHA_C,
        activation_generation=GENERATION,
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
    assert calls["initial_candidate_reader"][0][0] == (engine,)
    initial_kwargs = calls["initial_controller"][0][1]
    assert initial_kwargs["candidate_reader"] is instances[
        "initial_candidate_reader"
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
    ]
    supervisor_kwargs = calls["supervisor"][0][1]
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
    assert health_calls == [expected_scope]
    assert composition.shared_capture_store is store
    assert composition.adapter is adapter


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


def test_database_schema_fence_rejects_future_unknown_migration() -> None:
    required_tables = (
        "captured_paper_post_commit_outbox",
        "captured_paper_post_commit_outbox_events",
        "captured_paper_completed_fill_watch",
        "captured_paper_completed_fill_watch_events",
        "alpaca_paper_fill_activities",
        "alpaca_paper_fill_query_observations",
        "alpaca_paper_post_settlement_fill_contradictions",
    )

    class _Result:
        def __init__(self, rows):
            self.rows = rows

        def fetchall(self):
            return self.rows

    class _Connection:
        def __init__(self, versions):
            self.versions = versions

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, statement):
            if "schema_version" in statement:
                return _Result([(value,) for value in self.versions])
            return _Result([(name, True) for name in required_tables])

    class _Engine:
        def __init__(self, versions):
            self.versions = versions

        def connect(self):
            return _Connection(self.versions)

    migrations = SimpleNamespace(
        MIGRATIONS=(("001", object()), ("002", object())),
        text=lambda value: value,
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
