from __future__ import annotations

import ast
from copy import copy
from dataclasses import FrozenInstanceError, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import inspect
from pathlib import Path
from types import SimpleNamespace
import threading
import uuid

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app import migrations
from app.db import engine
from app.models.trading import (
    AdaptiveRiskDecisionPacket,
    AdaptiveRiskReservation,
    AdaptiveRiskReservationEvent,
    AlpacaPaperAccountSettlementHead,
    AlpacaPaperFillActivity,
    AlpacaPaperFillObservationActivity,
    AlpacaPaperFillObservationPage,
    AlpacaPaperFillPageObject,
    AlpacaPaperFillQueryObservation,
    AlpacaPaperPostSettlementFillContradiction,
)
from app.services.trading.momentum_neural import alpaca_fill_activity as capture_module
from app.services.trading.momentum_neural.alpaca_fill_activity import (
    ALPACA_PAPER_ENTRY_FILL_HANDOFF_SCHEMA_VERSION,
    AlpacaFillActivityConflict,
    AlpacaFillActivityCorruption,
    AlpacaFillActivityError,
    AlpacaPaperEntryFillHandoffProof,
    AlpacaPaperFillCycleBinding,
    PreparedAlpacaPaperFillBatch,
    append_alpaca_paper_fill_activity,
    append_prepared_alpaca_paper_fill_batch,
    evaluate_alpaca_paper_cycle_settlement,
    prepare_alpaca_paper_fill_activity,
    prepare_verified_alpaca_paper_fill_activity,
    publish_prepared_alpaca_paper_entry_fill_batch,
    publish_prepared_alpaca_paper_post_settlement_fill_batch,
    read_verified_alpaca_paper_exit_fill_batch,
    read_verified_alpaca_paper_fill_batch,
    verify_alpaca_paper_entry_fill_handoff,
    verify_alpaca_paper_fill_activity_chain,
    verify_alpaca_paper_fill_activity_row,
)
from app.services.trading.momentum_neural.adaptive_risk_reservation import (
    AdaptiveExitOwnerReceipt,
    AdaptiveExitOwnerTransportBinding,
    AdaptiveReservationStateConflict,
    AdaptiveRiskReservationStore,
    DurableOrderLifecycleEvidence,
)
from app.services.trading.momentum_neural.alpaca_cycle_settlement import (
    new_zero_settlement_head,
    settle_flat_alpaca_paper_cycle,
)
from app.services.trading.momentum_neural.alpaca_paper_identity import (
    alpaca_paper_account_identity_sha256,
)
from app.services.trading.momentum_neural.alpaca_orphan_claims import (
    acquire_action_claim,
    advance_owner_transport,
    lease_owner_transport,
    read_action_claim,
    resolve_owner_transport_terminal,
)
from app.services.trading.venue import alpaca_spot
from app.services.trading.venue.alpaca_spot import AlpacaSpotAdapter


UTC = timezone.utc
ACCOUNT_ID = "paper-account-activity-test"


def _hash(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _cycle(**overrides) -> AlpacaPaperFillCycleBinding:
    values = {
        "reservation_id": uuid.UUID("12345678-1234-5678-1234-567812345678"),
        "decision_packet_sha256": _hash("decision"),
        "reservation_request_sha256": _hash("request"),
        "account_scope": "alpaca:paper",
        "account_identity_sha256": _hash(ACCOUNT_ID),
        "account_snapshot_sha256": _hash("account-snapshot"),
        "account_snapshot_generation": "account-generation-7",
        "broker_connection_generation": "broker-connection-7",
        "execution_family": "alpaca_spot",
        "position_direction": "long",
        "cycle_client_order_id": "chili-cycle-entry-7",
        "entry_provider_order_id": "alpaca-entry-order-7",
        "symbol": "VEEE",
    }
    values.update(overrides)
    return AlpacaPaperFillCycleBinding(**values)


def _activity(
    *,
    side: str = "buy",
    activity_id: str = "activity-entry-1",
    order_id: str | None = None,
    price: float = 2.50,
    transaction_time: str = "2026-07-15T13:00:00Z",
    **extra,
) -> dict:
    if order_id is None:
        order_id = (
            "alpaca-entry-order-7" if side == "buy" else "alpaca-exit-order-7"
        )
    payload = {
        "id": activity_id,
        "account_id": ACCOUNT_ID,
        "activity_type": "FILL",
        "transaction_time": transaction_time,
        "type": "fill",
        "price": price,
        "qty": 10.0,
        "side": side,
        "symbol": "VEEE",
        "leaves_qty": 0.0,
        "order_id": order_id,
        "cum_qty": 10.0,
        "order_status": "filled",
    }
    payload.update(extra)
    return payload


def _provider_order(activity: dict, cid: str) -> dict:
    return {
        "id": activity["order_id"],
        "client_order_id": cid,
        "symbol": activity["symbol"],
        "side": activity["side"],
        "status": "filled",
    }


def _fee_evidence(activity: dict, fee: str = "0.01") -> dict:
    return {
        "provider_activity_id": activity["id"],
        "provider_order_id": activity["order_id"],
        "fee_usd": fee,
        "currency": "USD",
        "source": "caller-supplied-test-mapping",
    }


def _prepare_unavailable(
    activity: dict | None = None,
    *,
    cycle: AlpacaPaperFillCycleBinding | None = None,
    received_at: datetime | None = None,
    available_at: datetime | None = None,
):
    activity = activity or _activity()
    return prepare_alpaca_paper_fill_activity(
        cycle or _cycle(),
        provider_activity=activity,
        received_at=received_at or datetime(2026, 7, 15, 13, 0, 1, tzinfo=UTC),
        available_at=available_at or datetime(2026, 7, 15, 13, 0, 2, tzinfo=UTC),
        provider_event_clock_status="provider_unavailable",
        provider_client_order_id_status="provider_unavailable",
        fee_status="provider_unavailable",
    )


def _row(prepared, *, sequence: int = 1, previous: str | None = None):
    return AlpacaPaperFillActivity(
        **prepared.model_kwargs(
            sequence=sequence,
            previous_event_sha256=previous,
        )
    )


def test_cycle_binding_is_explicitly_alpaca_spot_long_only() -> None:
    with pytest.raises(AlpacaFillActivityError, match="alpaca_spot"):
        _cycle(execution_family="alpaca_short")
    with pytest.raises(AlpacaFillActivityError, match="long-only"):
        _cycle(position_direction="short")
    with pytest.raises(AlpacaFillActivityError, match="alpaca:paper"):
        _cycle(account_scope="alpaca:live")


def test_entry_fill_handoff_proof_is_canonical_typed_and_source_bound() -> None:
    source_sha = _hash("fill-source")
    observation_sha = _hash("fill-observation")
    proof = AlpacaPaperEntryFillHandoffProof(
        schema_version=ALPACA_PAPER_ENTRY_FILL_HANDOFF_SCHEMA_VERSION,
        publication_kind="active_cycle_fill",
        reservation_id=_cycle().reservation_id,
        decision_packet_sha256=_cycle().decision_packet_sha256,
        account_scope="alpaca:paper",
        account_identity_sha256=_cycle().account_identity_sha256,
        client_order_id=_cycle().cycle_client_order_id,
        broker_order_id=_cycle().entry_provider_order_id,
        broker_connection_generation=(
            _cycle().broker_connection_generation
        ),
        observation_sha256=observation_sha,
        durability_kind="committed_alpaca_paper_fill",
        source_record_table="alpaca_paper_fill_activities",
        source_record_id=source_sha,
        terminal_evidence_sha256=_hash("terminal-evidence"),
        immutable_fill_identity_sha256=_hash("immutable-fill"),
        cumulative_filled_quantity_shares=7,
        lifecycle_provider_event_id=(
            f"alpaca-fill:{source_sha}:observation:{observation_sha}"
        ),
        lifecycle_event_sha256=_hash("lifecycle-event"),
        lifecycle_event_sequence=4,
        resulting_reservation_state="partially_filled",
        observed_at=datetime(2026, 7, 15, 13, 0, tzinfo=UTC),
        available_at=datetime(2026, 7, 15, 13, 0, 1, tzinfo=UTC),
    )
    canonical = proof.to_canonical_json()
    rebuilt = AlpacaPaperEntryFillHandoffProof.from_canonical_json(canonical)

    assert rebuilt == proof
    assert rebuilt.proof_canonical_json == canonical
    assert rebuilt.proof_sha256 == hashlib.sha256(
        canonical.encode("utf-8")
    ).hexdigest()
    assert rebuilt.to_payload()["proof_sha256"] == rebuilt.proof_sha256
    with pytest.raises(FrozenInstanceError):
        rebuilt.lifecycle_event_sequence = 5
    with pytest.raises(AlpacaFillActivityError, match="source binding"):
        replace(rebuilt, observation_sha256=_hash("another-observation"))
    with pytest.raises(AlpacaFillActivityError, match="not canonical"):
        AlpacaPaperEntryFillHandoffProof.from_canonical_json(
            canonical.replace(":", ": ", 1)
        )


def test_trade_activity_requires_exact_account_symbol_and_nonnull_leaves() -> None:
    bad_account = _activity(account_id="another-paper-account")
    with pytest.raises(AlpacaFillActivityConflict, match="account id"):
        _prepare_unavailable(bad_account)

    bad_symbol = _activity(symbol="PLSM")
    with pytest.raises(AlpacaFillActivityConflict, match="symbol"):
        _prepare_unavailable(bad_symbol)

    missing_leaves = _activity()
    missing_leaves.pop("leaves_qty")
    with pytest.raises(AlpacaFillActivityError, match="leaves_qty"):
        _prepare_unavailable(missing_leaves)
    with pytest.raises(AlpacaFillActivityError, match="leaves_qty"):
        _prepare_unavailable(_activity(leaves_qty=None))


def test_entry_order_is_reservation_bound_and_exit_cannot_alias_it() -> None:
    with pytest.raises(AlpacaFillActivityConflict, match="reservation-owned"):
        _prepare_unavailable(_activity(order_id="forged-entry-order"))

    exit_alias = _activity(
        side="sell",
        activity_id="activity-exit-alias",
        order_id=_cycle().entry_provider_order_id,
        transaction_time="2026-07-15T13:05:00Z",
    )
    with pytest.raises(AlpacaFillActivityConflict, match="cannot alias"):
        _prepare_unavailable(
            exit_alias,
            received_at=datetime(2026, 7, 15, 13, 5, 1, tzinfo=UTC),
            available_at=datetime(2026, 7, 15, 13, 5, 2, tzinfo=UTC),
        )

    exit_activity = _activity(
        side="sell",
        activity_id="activity-exit-1",
        transaction_time="2026-07-15T13:05:00Z",
    )
    prepared = _prepare_unavailable(
        exit_activity,
        received_at=datetime(2026, 7, 15, 13, 5, 1, tzinfo=UTC),
        available_at=datetime(2026, 7, 15, 13, 5, 2, tzinfo=UTC),
    )
    assert prepared.order_role == "exit"
    assert prepared.order_ownership_status == "unverified"


def test_transaction_clock_is_distinct_and_cannot_proxy_event_clock() -> None:
    activity = _activity(event_time="2026-07-15T12:59:59Z")
    mapped = prepare_alpaca_paper_fill_activity(
        _cycle(),
        provider_activity=activity,
        received_at=datetime(2026, 7, 15, 13, 0, 1, tzinfo=UTC),
        available_at=datetime(2026, 7, 15, 13, 0, 2, tzinfo=UTC),
        provider_event_clock_status="unverified_mapping",
        provider_event_clock_field="event_time",
        provider_client_order_id_status="provider_unavailable",
        fee_status="provider_unavailable",
    )
    assert mapped.provider_event_at != mapped.provider_transaction_at
    assert mapped.capture_authority_status == "unverified"

    with pytest.raises(AlpacaFillActivityError, match="cannot proxy"):
        prepare_alpaca_paper_fill_activity(
            _cycle(),
            provider_activity=_activity(),
            received_at=datetime(2026, 7, 15, 13, 0, 1, tzinfo=UTC),
            available_at=datetime(2026, 7, 15, 13, 0, 2, tzinfo=UTC),
            provider_event_clock_status="unverified_mapping",
            provider_event_clock_field="transaction_time",
            provider_client_order_id_status="provider_unavailable",
            fee_status="provider_unavailable",
        )
    with pytest.raises(AlpacaFillActivityError, match="absent"):
        prepare_alpaca_paper_fill_activity(
            _cycle(),
            provider_activity=_activity(),
            received_at=datetime(2026, 7, 15, 13, 0, 1, tzinfo=UTC),
            available_at=datetime(2026, 7, 15, 13, 0, 2, tzinfo=UTC),
            provider_event_clock_status="unverified_mapping",
            provider_event_clock_field="fabricated_event_at",
            provider_client_order_id_status="provider_unavailable",
            fee_status="provider_unavailable",
        )


def test_unknown_fee_never_accepts_numeric_zero() -> None:
    with pytest.raises(AlpacaFillActivityError, match="never zero"):
        prepare_alpaca_paper_fill_activity(
            _cycle(),
            provider_activity=_activity(),
            received_at=datetime(2026, 7, 15, 13, 0, 1, tzinfo=UTC),
            available_at=datetime(2026, 7, 15, 13, 0, 2, tzinfo=UTC),
            provider_event_clock_status="provider_unavailable",
            provider_client_order_id_status="provider_unavailable",
            fee_status="provider_unavailable",
            fee_usd=0,
        )


def test_forged_mapping_shapes_remain_unverified_and_cannot_settle() -> None:
    entry_activity = _activity(event_time="2026-07-15T12:59:59Z")
    entry = prepare_alpaca_paper_fill_activity(
        _cycle(),
        provider_activity=entry_activity,
        received_at=datetime(2026, 7, 15, 13, 0, 1, tzinfo=UTC),
        # Caller can choose this value in v1; the capture is therefore unverified.
        available_at=datetime(2026, 7, 15, 13, 0, 1, 1, tzinfo=UTC),
        provider_event_clock_status="unverified_mapping",
        provider_event_clock_field="event_time",
        provider_client_order_id_status="unverified_mapping",
        provider_order=_provider_order(
            entry_activity, _cycle().cycle_client_order_id
        ),
        fee_status="unverified_mapping",
        fee_usd="0.01",
        fee_evidence=_fee_evidence(entry_activity, "0.01"),
    )
    entry_row = _row(entry)

    exit_activity = _activity(
        side="sell",
        activity_id="activity-exit-forged",
        price=3.00,
        transaction_time="2026-07-15T13:05:00Z",
        event_time="2026-07-15T13:04:59Z",
    )
    exit_prepared = prepare_alpaca_paper_fill_activity(
        _cycle(),
        provider_activity=exit_activity,
        received_at=datetime(2026, 7, 15, 13, 5, 1, tzinfo=UTC),
        available_at=datetime(2026, 7, 15, 13, 5, 1, 1, tzinfo=UTC),
        provider_event_clock_status="unverified_mapping",
        provider_event_clock_field="event_time",
        provider_client_order_id_status="unverified_mapping",
        provider_order=_provider_order(exit_activity, "forged-exit-cid"),
        fee_status="unverified_mapping",
        fee_usd="0.02",
        fee_evidence=_fee_evidence(exit_activity, "0.02"),
    )
    exit_row = _row(
        exit_prepared,
        sequence=2,
        previous=entry_row.event_sha256,
    )
    coverage = evaluate_alpaca_paper_cycle_settlement(
        reservation_id=_cycle().reservation_id,
        rows=[entry_row, exit_row],
        expected_entry_quantity=10,
    )
    assert coverage.status == "pending"
    assert "capture_authority_unverified" in coverage.pending_reasons
    assert "fee_truth_unavailable" in coverage.pending_reasons
    assert "provider_event_clock_unavailable" in coverage.pending_reasons
    assert "provider_client_order_id_unavailable" in coverage.pending_reasons
    assert "exit_order_ownership_unverified" in coverage.pending_reasons
    assert coverage.gross_realized_pnl_usd == Decimal("5.0000000000")
    assert coverage.fees_usd is None
    assert coverage.net_realized_pnl_usd is None


def test_forged_fee_and_order_mappings_fail_internal_binding() -> None:
    activity = _activity(event_time="2026-07-15T12:59:59Z")
    wrong_fee = _fee_evidence(activity, "999.00")
    with pytest.raises(AlpacaFillActivityConflict, match="fee evidence"):
        prepare_alpaca_paper_fill_activity(
            _cycle(),
            provider_activity=activity,
            received_at=datetime(2026, 7, 15, 13, 0, 1, tzinfo=UTC),
            available_at=datetime(2026, 7, 15, 13, 0, 2, tzinfo=UTC),
            provider_event_clock_status="provider_unavailable",
            provider_client_order_id_status="provider_unavailable",
            fee_status="unverified_mapping",
            fee_usd="0.01",
            fee_evidence=wrong_fee,
        )

    wrong_order = _provider_order(activity, _cycle().cycle_client_order_id)
    wrong_order["id"] = "another-order"
    with pytest.raises(AlpacaFillActivityConflict, match="does not own"):
        prepare_alpaca_paper_fill_activity(
            _cycle(),
            provider_activity=activity,
            received_at=datetime(2026, 7, 15, 13, 0, 1, tzinfo=UTC),
            available_at=datetime(2026, 7, 15, 13, 0, 2, tzinfo=UTC),
            provider_event_clock_status="provider_unavailable",
            provider_client_order_id_status="unverified_mapping",
            provider_order=wrong_order,
            fee_status="provider_unavailable",
        )


def test_canonical_payload_hash_and_lineage_detect_mutation_and_reparse() -> None:
    prepared_a = _prepare_unavailable(_activity(extra_b=2, extra_a=1))
    reordered = _activity(extra_a=1, extra_b=2)
    prepared_b = _prepare_unavailable(reordered)
    assert prepared_a.provider_payload_sha256 == prepared_b.provider_payload_sha256
    assert prepared_a.record_content_sha256 == prepared_b.record_content_sha256

    row = _row(prepared_a)
    verify_alpaca_paper_fill_activity_row(row)
    row.price = Decimal("9.0000000000")
    with pytest.raises(AlpacaFillActivityCorruption, match="typed fill"):
        verify_alpaca_paper_fill_activity_row(row)

    row = _row(prepared_a)
    row.provider_payload_canonical_json += " "
    with pytest.raises(AlpacaFillActivityCorruption, match="content hash mismatch"):
        verify_alpaca_paper_fill_activity_row(row)

    row = _row(prepared_a)
    row.capture_authority_status = "verified"
    with pytest.raises(AlpacaFillActivityCorruption, match="capture_authority"):
        verify_alpaca_paper_fill_activity_row(row)


def test_canonical_input_rejects_non_json_sdk_objects_and_noncausal_availability() -> None:
    activity = _activity(extra_datetime=datetime.now(UTC))
    with pytest.raises(AlpacaFillActivityError, match="JSON-compatible"):
        _prepare_unavailable(activity)

    with pytest.raises(AlpacaFillActivityError, match="not causal"):
        _prepare_unavailable(
            _activity(),
            received_at=datetime(2026, 7, 15, 13, 0, 1, tzinfo=UTC),
            available_at=datetime(2026, 7, 15, 13, 0, 0, tzinfo=UTC),
        )


def test_chain_rejects_gaps_and_predecessor_mutation() -> None:
    first = _row(_prepare_unavailable())
    exit_activity = _activity(
        side="sell",
        activity_id="activity-exit-chain",
        transaction_time="2026-07-15T13:05:00Z",
    )
    exit_prepared = _prepare_unavailable(
        exit_activity,
        received_at=datetime(2026, 7, 15, 13, 5, 1, tzinfo=UTC),
        available_at=datetime(2026, 7, 15, 13, 5, 2, tzinfo=UTC),
    )
    second = _row(exit_prepared, sequence=2, previous=first.event_sha256)
    verify_alpaca_paper_fill_activity_chain([second, first])

    gap = _row(exit_prepared, sequence=3, previous=first.event_sha256)
    with pytest.raises(AlpacaFillActivityCorruption, match="gap"):
        verify_alpaca_paper_fill_activity_chain([first, gap])

    second.previous_event_sha256 = _hash("forged-previous")
    with pytest.raises(AlpacaFillActivityCorruption, match="predecessor"):
        verify_alpaca_paper_fill_activity_chain([first, second])


def test_binding_from_rows_checks_packet_snapshot_and_broker_generation() -> None:
    cycle = _cycle()
    packet = SimpleNamespace(
        decision_packet_sha256=cycle.decision_packet_sha256,
        reservation_request_sha256=cycle.reservation_request_sha256,
        account_scope=cycle.account_scope,
        symbol=cycle.symbol,
        resolver_valid=True,
        admission_accepted=True,
        execution_surface="alpaca_paper",
        execution_family="alpaca_spot",
        broker_environment="paper",
        account_identity_sha256=cycle.account_identity_sha256,
        account_snapshot_sha256=cycle.account_snapshot_sha256,
        account_snapshot_generation=cycle.account_snapshot_generation,
        client_order_id=cycle.cycle_client_order_id,
        account_snapshot_json={
            "account_scope": cycle.account_scope,
            "execution_family": "alpaca_spot",
            "broker_environment": "paper",
            "venue": "alpaca",
            "account_identity_sha256": cycle.account_identity_sha256,
            "provider_generation": cycle.account_snapshot_generation,
            "snapshot_sha256": cycle.account_snapshot_sha256,
        },
    )
    reservation = SimpleNamespace(
        reservation_id=cycle.reservation_id,
        decision_packet_sha256=cycle.decision_packet_sha256,
        account_scope=cycle.account_scope,
        symbol=cycle.symbol,
        broker_connection_generation=cycle.broker_connection_generation,
        broker_order_id=cycle.entry_provider_order_id,
    )
    assert AlpacaPaperFillCycleBinding.from_rows(reservation, packet) == cycle

    reservation.symbol = "PLSM"
    with pytest.raises(AlpacaFillActivityConflict, match="symbol"):
        AlpacaPaperFillCycleBinding.from_rows(reservation, packet)


class _RecordingConnection:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self.commits = 0

    def execute(self, statement):
        self.statements.append(str(statement))
        return None

    def commit(self) -> None:
        self.commits += 1


def test_migration_327_is_unverified_append_only_including_truncate(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        migrations,
        "_tables",
        lambda _conn: {
            "adaptive_risk_decision_packets",
            "adaptive_risk_reservations",
        },
    )
    conn = _RecordingConnection()
    migrations._migration_327_alpaca_paper_fill_activity_capture(conn)
    sql = "\n".join(conn.statements).lower()
    assert "capture_authority_status = 'unverified'" in sql
    assert "unverified_mapping" in sql
    assert "position_direction = 'long'" in sql
    assert "provider_order_id <> entry_provider_order_id" in sql
    assert "before update or delete" in sql
    assert "before truncate" in sql
    assert "for each statement" in sql
    assert "fee_usd numeric(28, 10) null" in sql
    assert "fee_usd numeric(28, 10) not null" not in sql
    assert conn.commits == 1
    ids = [migration_id for migration_id, _fn in migrations.MIGRATIONS]
    assert ids.index("327_alpaca_paper_fill_activity_capture") == (
        ids.index("326_adaptive_opportunity_scope_constraint") + 1
    )
    assert ids.index("328_alpaca_paper_fill_capture_authority_repair") == (
        ids.index("327_alpaca_paper_fill_activity_capture") + 1
    )


def test_migration_328_never_rewrites_interim_append_only_rows(monkeypatch) -> None:
    monkeypatch.setattr(
        migrations,
        "_tables",
        lambda _conn: {"alpaca_paper_fill_activities"},
    )
    conn = _RecordingConnection()
    migrations._migration_328_alpaca_paper_fill_capture_authority_repair(conn)
    sql = "\n".join(conn.statements).lower()
    assert "add column if not exists capture_authority_status" in sql
    assert "select 1 from alpaca_paper_fill_activities limit 1" in sql
    assert "append-only evidence will not be rewritten" in sql
    assert "update alpaca_paper_fill_activities" not in sql
    assert "alter column capture_authority_status set not null" in sql
    assert "before truncate" in sql
    assert conn.commits == 1


def test_migration_332_rejects_contiguous_forged_authoritative_fill() -> None:
    with engine.connect() as conn:
        transaction = conn.begin()
        db = Session(bind=conn, expire_on_commit=False)
        try:
            conn.execute(
                text(
                    "ALTER TABLE alpaca_paper_fill_activities DROP CONSTRAINT "
                    "ck_alpaca_paper_fill_capture_authority"
                )
            )
            reservation, packet = _persist_cycle_rows(db)
            cycle = AlpacaPaperFillCycleBinding.from_rows(reservation, packet)
            prepared = _prepare_unavailable(cycle=cycle)
            forged = prepared.model_kwargs(
                sequence=1,
                previous_event_sha256=None,
            )
            forged["capture_authority_status"] = "authoritative"
            conn.execute(AlpacaPaperFillActivity.__table__.insert(), forged)

            with pytest.raises(
                DBAPIError,
                match="not exact permanently-unverified v1 evidence",
            ):
                migrations._migration_332_alpaca_paper_fill_boundary_drift_hardening(
                    conn
                )
        finally:
            db.close()
            if transaction.is_active:
                transaction.rollback()


def test_migration_332_rejects_retained_null_and_not_null_drift() -> None:
    with engine.connect() as conn:
        transaction = conn.begin()
        db = Session(bind=conn, expire_on_commit=False)
        try:
            conn.execute(
                text(
                    "ALTER TABLE alpaca_paper_fill_activities "
                    "ALTER COLUMN capture_authority_status DROP NOT NULL"
                )
            )
            reservation, packet = _persist_cycle_rows(db)
            cycle = AlpacaPaperFillCycleBinding.from_rows(reservation, packet)
            prepared = _prepare_unavailable(cycle=cycle)
            retained_null = prepared.model_kwargs(
                sequence=1,
                previous_event_sha256=None,
            )
            retained_null["capture_authority_status"] = None
            conn.execute(
                AlpacaPaperFillActivity.__table__.insert(),
                retained_null,
            )

            with pytest.raises(
                DBAPIError,
                match="required NOT NULL columns drifted",
            ):
                migrations._migration_332_alpaca_paper_fill_boundary_drift_hardening(
                    conn
                )
        finally:
            db.close()
            if transaction.is_active:
                transaction.rollback()


def test_migration_332_rejects_retained_gapped_fill_chain() -> None:
    with engine.connect() as conn:
        transaction = conn.begin()
        db = Session(bind=conn, expire_on_commit=False)
        try:
            conn.execute(
                text(
                    "DROP TRIGGER IF EXISTS "
                    "trg_alpaca_paper_fill_activity_cycle_guard "
                    "ON alpaca_paper_fill_activities"
                )
            )
            reservation, packet = _persist_cycle_rows(db)
            cycle = AlpacaPaperFillCycleBinding.from_rows(reservation, packet)
            prepared = _prepare_unavailable(cycle=cycle)
            conn.execute(
                AlpacaPaperFillActivity.__table__.insert(),
                prepared.model_kwargs(
                    sequence=2,
                    previous_event_sha256=_hash("missing-predecessor"),
                ),
            )

            with pytest.raises(
                DBAPIError,
                match="gapped or has a wrong predecessor",
            ):
                migrations._migration_332_alpaca_paper_fill_boundary_drift_hardening(
                    conn
                )
        finally:
            db.close()
            if transaction.is_active:
                transaction.rollback()


def test_migration_332_restores_fill_append_only_triggers() -> None:
    with engine.connect() as conn:
        conn.execute(
            text(
                "DROP TRIGGER IF EXISTS "
                "trg_alpaca_paper_fill_activity_append_only "
                "ON alpaca_paper_fill_activities"
            )
        )
        conn.execute(
            text(
                "DROP TRIGGER IF EXISTS "
                "trg_alpaca_paper_fill_activity_no_truncate "
                "ON alpaca_paper_fill_activities"
            )
        )
        conn.execute(
            text(
                "DROP TRIGGER IF EXISTS "
                "trg_alpaca_paper_fill_activity_cycle_guard "
                "ON alpaca_paper_fill_activities"
            )
        )
        migrations._migration_332_alpaca_paper_fill_boundary_drift_hardening(
            conn
        )
        restored = {
            row[0]: (row[1], row[2], row[3])
            for row in conn.execute(
                text(
                    "SELECT trigger_row.tgname, function_row.proname, "
                    "trigger_row.tgenabled, trigger_row.tgtype "
                    "FROM pg_trigger trigger_row "
                    "JOIN pg_proc function_row "
                    "ON function_row.oid = trigger_row.tgfoid "
                    "WHERE trigger_row.tgrelid = "
                    "'alpaca_paper_fill_activities'::regclass "
                    "AND NOT trigger_row.tgisinternal"
                )
            )
        }
        reservation_state_constraint = conn.execute(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conrelid = "
                "'adaptive_risk_reservations'::regclass "
                "AND conname = 'ck_adaptive_risk_reservation_state'"
            )
        ).scalar_one()
        reservation_guard = conn.execute(
            text(
                "SELECT pg_get_functiondef(oid) FROM pg_proc "
                "WHERE proname = "
                "'chili_guard_alpaca_reservation_settlement_state'"
            )
        ).scalar_one()
    assert restored["trg_alpaca_paper_fill_activity_append_only"] == (
        "chili_reject_alpaca_fill_activity_mutation",
        "O",
        27,
    )
    assert restored["trg_alpaca_paper_fill_activity_no_truncate"] == (
        "chili_reject_alpaca_fill_activity_mutation",
        "O",
        34,
    )
    assert restored["trg_alpaca_paper_fill_activity_cycle_guard"] == (
        "chili_guard_alpaca_fill_activity_insert",
        "O",
        7,
    )
    assert "exposure_quarantined" in reservation_state_constraint
    assert "quarantined Alpaca exposure is fail-closed" in reservation_guard


def test_migration_332_replaces_wrong_event_cycle_guard() -> None:
    with engine.connect() as conn:
        transaction = conn.begin()
        try:
            conn.execute(
                text(
                    "DROP TRIGGER IF EXISTS "
                    "trg_alpaca_paper_fill_activity_cycle_guard "
                    "ON alpaca_paper_fill_activities"
                )
            )
            conn.execute(
                text(
                    "CREATE TRIGGER trg_alpaca_paper_fill_activity_cycle_guard "
                    "AFTER UPDATE ON alpaca_paper_fill_activities "
                    "FOR EACH ROW EXECUTE FUNCTION "
                    "chili_guard_alpaca_fill_activity_insert()"
                )
            )

            migrations._migration_332_alpaca_paper_fill_boundary_drift_hardening(
                conn
            )
            restored = conn.execute(
                text(
                    "SELECT function_row.proname, trigger_row.tgenabled, "
                    "trigger_row.tgtype "
                    "FROM pg_trigger trigger_row "
                    "JOIN pg_proc function_row "
                    "ON function_row.oid = trigger_row.tgfoid "
                    "WHERE trigger_row.tgrelid = "
                    "'alpaca_paper_fill_activities'::regclass "
                    "AND trigger_row.tgname = "
                    "'trg_alpaca_paper_fill_activity_cycle_guard' "
                    "AND NOT trigger_row.tgisinternal"
                )
            ).one()
            assert restored == (
                "chili_guard_alpaca_fill_activity_insert",
                "O",
                7,
            )
        finally:
            if transaction.is_active:
                transaction.rollback()


def test_migration_332_lock_blocks_concurrent_fill_writer(monkeypatch) -> None:
    complete = {
        "adaptive_risk_reservations",
        "adaptive_risk_reservation_events",
        "adaptive_risk_decision_packets",
        "alpaca_paper_fill_activities",
        "alpaca_paper_account_settlement_heads",
        "alpaca_paper_cycle_settlements",
    }
    monkeypatch.setattr(migrations, "_tables", lambda _conn: complete)
    lock_acquired = threading.Event()
    release_migration = threading.Event()
    failures: list[BaseException] = []

    class _PausingConnection:
        def __init__(self, raw):
            self.raw = raw

        def execute(self, statement, *args, **kwargs):
            result = self.raw.execute(statement, *args, **kwargs)
            if "LOCK TABLE" in str(statement).upper():
                lock_acquired.set()
                if not release_migration.wait(timeout=10):
                    raise RuntimeError("migration lock test release timed out")
            return result

        def commit(self):
            return self.raw.commit()

    def run_migration() -> None:
        try:
            with engine.connect() as raw:
                migrations._migration_332_alpaca_paper_fill_boundary_drift_hardening(
                    _PausingConnection(raw)
                )
        except BaseException as exc:  # surfaced in the owning test thread below
            failures.append(exc)

    worker = threading.Thread(target=run_migration, daemon=True)
    worker.start()
    assert lock_acquired.wait(timeout=10)
    try:
        with engine.connect() as contender:
            transaction = contender.begin()
            contender.execute(text("SET LOCAL lock_timeout = '250ms'"))
            with pytest.raises(DBAPIError):
                contender.execute(
                    text(
                        "LOCK TABLE alpaca_paper_fill_activities "
                        "IN ROW EXCLUSIVE MODE"
                    )
                )
            transaction.rollback()
    finally:
        release_migration.set()
        worker.join(timeout=15)

    assert not worker.is_alive()
    assert failures == []


def test_model_registers_versioned_content_addressed_capture_contract() -> None:
    constraints = {
        constraint.name
        for constraint in AlpacaPaperFillActivity.__table__.constraints
        if constraint.name
    }
    assert {
        "uq_alpaca_paper_fill_provider_activity",
        "uq_alpaca_paper_fill_cycle_sequence",
        "ck_alpaca_paper_fill_capture_authority",
        "ck_alpaca_paper_fill_strategy_scope",
        "ck_alpaca_paper_fill_fee_truth",
        "ck_alpaca_paper_fill_lineage",
    } <= constraints
    columns = AlpacaPaperFillActivity.__table__.columns
    assert columns.fee_usd.nullable is True
    assert columns.provider_event_at.nullable is True
    assert columns.provider_client_order_id.nullable is True
    assert columns.provider_transaction_at.nullable is False
    assert columns.entry_provider_order_id.nullable is False


def test_capture_module_has_no_direct_network_or_activation_side_effect() -> None:
    source_path = Path(capture_module.__file__).resolve()
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not any(
        module == banned or module.startswith(banned + ".")
        for module in imported
        for banned in ("alpaca", "requests", "httpx", "urllib", "socket")
    )
    # The explicit runtime seam is now wired. Importing this module still
    # starts no client/service and commits no transaction by itself.
    assert "SessionLocal" not in source


def _persist_cycle_rows(
    db,
    cycle: AlpacaPaperFillCycleBinding | None = None,
    *,
    unbound: bool = False,
):
    cycle = cycle or _cycle()
    snapshot = {
        "account_scope": cycle.account_scope,
        "execution_family": "alpaca_spot",
        "broker_environment": "paper",
        "venue": "alpaca",
        "account_identity_sha256": cycle.account_identity_sha256,
        "provider_generation": cycle.account_snapshot_generation,
        "snapshot_sha256": cycle.account_snapshot_sha256,
    }
    packet = AdaptiveRiskDecisionPacket(
        decision_packet_sha256=cycle.decision_packet_sha256,
        reservation_request_sha256=cycle.reservation_request_sha256,
        decision_id=cycle.cycle_client_order_id,
        account_scope=cycle.account_scope,
        symbol=cycle.symbol,
        trading_date=date(2026, 7, 15),
        setup_family="momentum_breakout",
        correlation_cluster="equity:momentum",
        client_order_id=cycle.cycle_client_order_id,
        execution_surface="alpaca_paper",
        execution_family="alpaca_spot",
        broker_environment="paper",
        account_identity_sha256=cycle.account_identity_sha256,
        account_snapshot_sha256=cycle.account_snapshot_sha256,
        account_snapshot_generation=cycle.account_snapshot_generation,
        policy_sha256=_hash("policy"),
        input_sha256=_hash("input"),
        economic_input_sha256=_hash("economic-input"),
        economic_resolution_sha256=_hash("economic-resolution"),
        effective_config_sha256=_hash("config"),
        code_build_sha256=_hash("build"),
        feature_flags_sha256=_hash("flags"),
        capture_prefix_root_sha256=_hash("capture-prefix"),
        evidence_sha256=_hash("evidence"),
        reservation_ledger_sha256=_hash("ledger"),
        resolved_quantity_shares=10,
        structural_stop=Decimal("2.00"),
        entry_limit_price=Decimal("2.50"),
        resolver_valid=True,
        admission_accepted=True,
        rejection_reasons_json=[],
        account_snapshot_json=snapshot,
        decision_packet_json={"schema_version": "test"},
    )
    db.add(packet)
    db.flush()
    reservation = AdaptiveRiskReservation(
        reservation_id=cycle.reservation_id,
        decision_packet_sha256=cycle.decision_packet_sha256,
        opportunity_claim_id=None,
        account_scope=cycle.account_scope,
        symbol=cycle.symbol,
        trading_date=date(2026, 7, 15),
        setup_family="momentum_breakout",
        correlation_cluster="equity:momentum",
        state="reserved" if unbound else "filled",
        planned_quantity_shares=10,
        cumulative_filled_quantity_shares=0 if unbound else 10,
        open_quantity_shares=0 if unbound else 10,
        planned_structural_risk_usd=Decimal("5.00"),
        planned_gross_notional_usd=Decimal("25.00"),
        planned_buying_power_impact_usd=Decimal("25.00"),
        pending_structural_risk_usd=(
            Decimal("5.00") if unbound else Decimal("0")
        ),
        pending_gross_notional_usd=(
            Decimal("25.00") if unbound else Decimal("0")
        ),
        pending_buying_power_impact_usd=(
            Decimal("25.00") if unbound else Decimal("0")
        ),
        open_structural_risk_usd=(
            Decimal("0") if unbound else Decimal("5.00")
        ),
        open_gross_notional_usd=(
            Decimal("0") if unbound else Decimal("25.00")
        ),
        open_buying_power_impact_usd=(
            Decimal("0") if unbound else Decimal("25.00")
        ),
        broker_order_id=(None if unbound else cycle.entry_provider_order_id),
        broker_source=None if unbound else "alpaca",
        broker_connection_generation=(
            None if unbound else cycle.broker_connection_generation
        ),
        last_broker_observed_at=(
            None
            if unbound
            else datetime(2026, 7, 15, 13, 0, tzinfo=UTC)
        ),
        last_broker_available_at=(
            None
            if unbound
            else datetime(2026, 7, 15, 13, 0, 1, tzinfo=UTC)
        ),
        last_source_event_content_sha256=(
            None if unbound else _hash("entry-order-observation")
        ),
        event_sequence=0,
        last_event_sha256=None,
        version=1,
    )
    db.add(reservation)
    db.flush()
    return reservation, packet


def test_db_append_is_idempotent_allows_late_booking_and_blocks_mutation(db) -> None:
    with db.begin():
        reservation, packet = _persist_cycle_rows(db)
        cycle = AlpacaPaperFillCycleBinding.from_rows(reservation, packet)
        prepared = _prepare_unavailable(cycle=cycle)
        first = append_alpaca_paper_fill_activity(db, prepared)
        retry = append_alpaca_paper_fill_activity(db, prepared)
        assert first.created is True
        assert retry.created is False
        assert first.row.id == retry.row.id

        changed = _activity(price=2.60)
        conflicting = _prepare_unavailable(changed, cycle=cycle)
        with pytest.raises(AlpacaFillActivityConflict, match="reused"):
            append_alpaca_paper_fill_activity(db, conflicting)

        exit_activity = _activity(
            side="sell",
            activity_id="activity-exit-after-flat",
            transaction_time="2026-07-15T13:05:00Z",
        )
        exit_prepared = _prepare_unavailable(
            exit_activity,
            cycle=cycle,
            received_at=datetime(2026, 7, 15, 13, 5, 1, tzinfo=UTC),
            available_at=datetime(2026, 7, 15, 13, 5, 2, tzinfo=UTC),
        )
        reservation.state = "flat_pending_settlement"
        reservation.open_quantity_shares = 0
        reservation.open_structural_risk_usd = Decimal("0")
        reservation.open_gross_notional_usd = Decimal("0")
        reservation.open_buying_power_impact_usd = Decimal("0")
        db.flush()

        # A read-only retry is harmless. Provider activities may arrive after
        # flat proof while settlement is pending; the new row remains
        # diagnostic-only and cannot certify settlement.
        assert append_alpaca_paper_fill_activity(db, prepared).created is False
        late = append_alpaca_paper_fill_activity(db, exit_prepared)
        assert late.created is True
        coverage = evaluate_alpaca_paper_cycle_settlement(
            reservation_id=reservation.reservation_id,
            rows=[first.row, late.row],
            expected_entry_quantity=10,
        )
        assert coverage.pending is True
        assert "capture_authority_unverified" in coverage.pending_reasons

        nested = db.begin_nested()
        with pytest.raises(DBAPIError):
            db.execute(
                AlpacaPaperFillActivity.__table__.insert(),
                exit_prepared.model_kwargs(
                    sequence=2,
                    previous_event_sha256=first.row.event_sha256,
                ),
            )
        nested.rollback()

    assert db.scalar(select(func.count(AlpacaPaperFillActivity.id))) == 2
    db.rollback()

    statements = (
        "UPDATE alpaca_paper_fill_activities SET price = 99",
        "DELETE FROM alpaca_paper_fill_activities",
        "TRUNCATE alpaca_paper_fill_activities",
    )
    for statement in statements:
        with engine.connect() as conn:
            transaction = conn.begin()
            with pytest.raises(DBAPIError):
                conn.execute(text(statement))
            transaction.rollback()


VERIFIED_ACCOUNT_ID = "ae7b7443-9a5f-4db2-8e58-9b872f5015cf"


def _verified_cycle() -> AlpacaPaperFillCycleBinding:
    return _cycle(
        account_identity_sha256=alpaca_paper_account_identity_sha256(
            VERIFIED_ACCOUNT_ID
        )
    )


def _verified_activity(
    *,
    activity_id: str,
    transaction_time: str,
    quantity: str,
    cumulative_quantity: str,
    leaves_quantity: str,
    trade_type: str,
    order_id: str = "alpaca-entry-order-7",
) -> dict:
    return {
        "id": activity_id,
        "account_id": VERIFIED_ACCOUNT_ID,
        "activity_type": "FILL",
        "transaction_time": transaction_time,
        "type": trade_type,
        "price": "2.5000000000",
        "qty": quantity,
        "side": "buy",
        "symbol": "VEEE",
        "leaves_qty": leaves_quantity,
        "order_id": order_id,
        "cum_qty": cumulative_quantity,
        "order_status": "filled" if leaves_quantity == "0.0000000000" else "partially_filled",
    }


def _verified_fee(activity: dict) -> dict:
    return {
        "schema_version": "chili.alpaca-paper-equity-fee-contract.v1",
        "provider_activity_id": activity["id"],
        "provider_order_id": activity["order_id"],
        "fee_usd": "0.0000000000",
        "currency": "USD",
        "broker_environment": "paper",
        "asset_class": "us_equity",
        "basis": "alpaca_paper_does_not_account_for_regulatory_fees",
        "source": "https://docs.alpaca.markets/us/docs/paper-trading",
    }


def _verified_raw_batch(
    *,
    activities: list[dict] | None = None,
    order_id: str = "alpaca-entry-order-7",
    client_order_id: str = "chili-cycle-entry-7",
) -> dict:
    activities = activities or [
        _verified_activity(
            activity_id="verified-fill-1",
            transaction_time="2026-07-15T13:00:00Z",
            quantity="10.0000000000",
            cumulative_quantity="10.0000000000",
            leaves_quantity="0.0000000000",
            trade_type="fill",
            order_id=order_id,
        )
    ]
    return {
        "readable": True,
        "complete": True,
        "provider_order": {
            "id": order_id,
            "client_order_id": client_order_id,
            "account_id": VERIFIED_ACCOUNT_ID,
            "symbol": "VEEE",
            "side": "buy",
            "status": "filled",
            "qty": "10.0000000000",
            "filled_qty": "10.0000000000",
            "asset_class": "us_equity",
            "created_at": "2026-07-15T12:59:00Z",
        },
        "query_after": "2026-07-15T00:00:00Z",
        "query_until": "2026-07-15T13:00:30Z",
        "received_at": datetime(2026, 7, 15, 13, 0, 31, tzinfo=UTC),
        "available_at": datetime(2026, 7, 15, 13, 0, 32, tzinfo=UTC),
        "activities": [
            {
                "provider_activity": activity,
                "fee_usd": "0.0000000000",
                "fee_evidence": _verified_fee(activity),
            }
            for activity in activities
        ],
    }


def _verified_adapter(
    raw_batch: dict,
    monkeypatch,
    *,
    account_id: str = VERIFIED_ACCOUNT_ID,
):
    adapter = AlpacaSpotAdapter()
    adapter._bound_account_id = account_id
    calls: list[str] = []

    class _Order:
        def model_dump(self, mode: str = "json"):
            assert mode == "json"
            return dict(raw_batch["provider_order"])

    class _Client:
        def get_order_by_id(self, order_id: str):
            calls.append(order_id)
            return _Order()

        def _request(self, *_args, **_kwargs):
            return [
                dict(item["provider_activity"])
                for item in raw_batch.get("activities", [])
            ]

    client = _Client()
    monkeypatch.setattr(alpaca_spot, "_trading_client", lambda: client)
    monkeypatch.setitem(alpaca_spot._clients, "trading:paper", client)
    monkeypatch.setitem(
        alpaca_spot._clients, "trading:observed_account_id", account_id
    )
    monkeypatch.setitem(alpaca_spot._clients, "trading:fingerprint", "b" * 64)
    monkeypatch.setattr(alpaca_spot, "_paper", lambda: True)
    monkeypatch.setattr(alpaca_spot, "_require_paper_posture", lambda: None)
    return adapter, calls


def _two_fill_verified_batch(monkeypatch) -> PreparedAlpacaPaperFillBatch:
    activities = [
        _verified_activity(
            activity_id="verified-partial-1",
            transaction_time="2026-07-15T13:00:00Z",
            quantity="4.0000000000",
            cumulative_quantity="4.0000000000",
            leaves_quantity="6.0000000000",
            trade_type="partial_fill",
        ),
        _verified_activity(
            activity_id="verified-final-2",
            transaction_time="2026-07-15T13:00:01Z",
            quantity="6.0000000000",
            cumulative_quantity="10.0000000000",
            leaves_quantity="0.0000000000",
            trade_type="fill",
        ),
    ]
    adapter, _calls = _verified_adapter(
        _verified_raw_batch(activities=activities), monkeypatch
    )
    return read_verified_alpaca_paper_fill_batch(
        adapter,
        cycle=_verified_cycle(),
        provider_order_id="alpaca-entry-order-7",
        expected_client_order_id="chili-cycle-entry-7",
    )


def _partial_verified_entry_batch(monkeypatch) -> PreparedAlpacaPaperFillBatch:
    partial = _verified_activity(
        activity_id="verified-partial-1",
        transaction_time="2026-07-15T13:00:00Z",
        quantity="4.0000000000",
        cumulative_quantity="4.0000000000",
        leaves_quantity="6.0000000000",
        trade_type="partial_fill",
    )
    raw = _verified_raw_batch(activities=[partial])
    raw["provider_order"]["status"] = "partially_filled"
    raw["provider_order"]["filled_qty"] = "4.0000000000"
    adapter, _calls = _verified_adapter(raw, monkeypatch)
    return read_verified_alpaca_paper_fill_batch(
        adapter,
        cycle=_verified_cycle(),
        provider_order_id="alpaca-entry-order-7",
        expected_client_order_id="chili-cycle-entry-7",
    )


def _verified_exit_batch(
    monkeypatch,
    receipt: AdaptiveExitOwnerReceipt,
    *,
    quantities: tuple[int, ...] = (4,),
    order_quantity: int = 10,
    first_activity_number: int = 1,
    transaction_second_start: int | None = None,
) -> PreparedAlpacaPaperFillBatch:
    cumulative = 0
    activities: list[dict] = []
    for offset, quantity in enumerate(quantities):
        cumulative += quantity
        transaction_second = (
            first_activity_number + offset
            if transaction_second_start is None
            else transaction_second_start + offset
        )
        activity = _verified_activity(
            activity_id=f"verified-exit-{first_activity_number + offset}",
            transaction_time=(
                f"2026-07-15T13:05:{transaction_second:02d}Z"
            ),
            quantity=f"{quantity}.0000000000",
            cumulative_quantity=f"{cumulative}.0000000000",
            leaves_quantity=f"{order_quantity - cumulative}.0000000000",
            trade_type=(
                "fill" if cumulative == order_quantity else "partial_fill"
            ),
            order_id=receipt.provider_order_id,
        )
        activity["side"] = "sell"
        activity["price"] = "3.0000000000"
        activities.append(activity)
    raw_fixture = _verified_raw_batch(
        activities=activities,
        order_id=receipt.provider_order_id,
        client_order_id=receipt.provider_client_order_id,
    )
    raw_fixture["provider_order"].update(
        {
            "side": "sell",
            "qty": f"{order_quantity}.0000000000",
            "filled_qty": f"{cumulative}.0000000000",
            "status": (
                "filled" if cumulative == order_quantity else "partially_filled"
            ),
        }
    )
    adapter, _calls = _verified_adapter(raw_fixture, monkeypatch)
    return read_verified_alpaca_paper_exit_fill_batch(
        adapter,
        cycle=_verified_cycle(),
        exit_owner_receipt=receipt,
    )


def _verified_entry_batch(
    monkeypatch,
    *,
    quantities: tuple[int, ...],
    order_quantity: int = 10,
) -> PreparedAlpacaPaperFillBatch:
    cumulative = 0
    activities: list[dict] = []
    for offset, quantity in enumerate(quantities):
        cumulative += quantity
        activities.append(
            _verified_activity(
                activity_id=(
                    "verified-partial-1"
                    if offset == 0
                    else f"verified-entry-late-{offset + 1}"
                ),
                transaction_time=f"2026-07-15T13:00:{offset:02d}Z",
                quantity=f"{quantity}.0000000000",
                cumulative_quantity=f"{cumulative}.0000000000",
                leaves_quantity=f"{order_quantity - cumulative}.0000000000",
                trade_type=(
                    "fill" if cumulative == order_quantity else "partial_fill"
                ),
            )
        )
    raw = _verified_raw_batch(activities=activities)
    raw["provider_order"].update(
        {
            "qty": f"{order_quantity}.0000000000",
            "filled_qty": f"{cumulative}.0000000000",
            "status": (
                "filled" if cumulative == order_quantity else "partially_filled"
            ),
        }
    )
    adapter, _calls = _verified_adapter(raw, monkeypatch)
    return read_verified_alpaca_paper_fill_batch(
        adapter,
        cycle=_verified_cycle(),
        provider_order_id="alpaca-entry-order-7",
        expected_client_order_id="chili-cycle-entry-7",
    )


def _historical_verified_exit_batch(monkeypatch) -> PreparedAlpacaPaperFillBatch:
    """Build a complete pre-354 observation without retaining an owner event."""

    cycle = _verified_cycle()
    exit_order_id = "alpaca-historical-exit-order-7"
    exit_cid = "chili-historical-exit-7"
    binding = _exit_owner_binding(
        cycle,
        exit_client_order_id=exit_cid,
        order_quantity=4,
        generation=99,
    )
    observed_at = datetime(2026, 7, 15, 13, 3, 59, tzinfo=UTC)
    unretained_receipt = AdaptiveExitOwnerReceipt(
        receipt_sha256=_hash("pre-354-owner-not-retained"),
        event_type="alpaca_exit_owner_submitted",
        reservation_id=cycle.reservation_id,
        sequence=1,
        transport_started_event_sha256=_hash("pre-354-start-not-retained"),
        binding=binding,
        provider_order_id=exit_order_id,
        provider_client_order_id=exit_cid,
        provider_status="accepted",
        provider_cumulative_quantity=0,
        observer_claim_token=binding.transport_claim_token,
        observer_session_id=binding.transport_owner_session_id,
        observer_generation=binding.transport_owner_generation,
        observer_runtime_generation=binding.transport_runtime_generation,
        observer_connection_generation=binding.transport_connection_generation,
        effective_at=observed_at,
        available_at=observed_at,
    )
    return _verified_exit_batch(
        monkeypatch,
        unretained_receipt,
        quantities=(4,),
        order_quantity=4,
        first_activity_number=4,
        transaction_second_start=0,
    )


def test_fill_batch_io_signatures_are_strictly_split_and_immutable(monkeypatch) -> None:
    reader_parameters = inspect.signature(
        read_verified_alpaca_paper_fill_batch
    ).parameters
    appender_parameters = inspect.signature(
        append_prepared_alpaca_paper_fill_batch
    ).parameters
    publisher_parameters = inspect.signature(
        publish_prepared_alpaca_paper_entry_fill_batch
    ).parameters
    assert "session" not in reader_parameters
    assert "db" not in reader_parameters
    assert "adapter" not in appender_parameters
    assert "provider_order_id" not in appender_parameters
    assert tuple(publisher_parameters) == ("session", "batch")
    reader_tree = ast.parse(inspect.getsource(read_verified_alpaca_paper_fill_batch))
    appender_tree = ast.parse(
        inspect.getsource(append_prepared_alpaca_paper_fill_batch)
    )
    publisher_tree = ast.parse(
        inspect.getsource(publish_prepared_alpaca_paper_entry_fill_batch)
    )
    assert not {
        node.id
        for node in ast.walk(reader_tree)
        if isinstance(node, ast.Name)
    } & {"Session", "session", "select"}
    assert "adapter" not in {
        node.id
        for node in ast.walk(appender_tree)
        if isinstance(node, ast.Name)
    }
    assert "adapter" not in {
        node.id
        for node in ast.walk(publisher_tree)
        if isinstance(node, ast.Name)
    }

    adapter, calls = _verified_adapter(_verified_raw_batch(), monkeypatch)
    batch = read_verified_alpaca_paper_fill_batch(
        adapter,
        cycle=_verified_cycle(),
        provider_order_id="alpaca-entry-order-7",
        expected_client_order_id="chili-cycle-entry-7",
    )
    assert calls == ["alpaca-entry-order-7"]
    assert batch.batch_content_sha256 == batch.computed_batch_content_sha256()
    with pytest.raises(FrozenInstanceError):
        batch.provider_order_id = "mutated"  # type: ignore[misc]


def test_prepared_batch_mutation_or_hash_mismatch_rejects_before_db_access(
    monkeypatch,
) -> None:
    batch = _two_fill_verified_batch(monkeypatch)

    class _NoDatabaseTouch:
        def __getattribute__(self, name):
            raise AssertionError(f"database touched before batch verification: {name}")

    mutated_order = replace(batch, provider_order_id="forged-order")
    with pytest.raises(AlpacaFillActivityCorruption, match="payload changed"):
        append_prepared_alpaca_paper_fill_batch(
            _NoDatabaseTouch(), mutated_order  # type: ignore[arg-type]
        )

    forged_hash = replace(batch, batch_content_sha256=_hash("forged-batch"))
    with pytest.raises(AlpacaFillActivityCorruption, match="content hash"):
        append_prepared_alpaca_paper_fill_batch(
            _NoDatabaseTouch(), forged_hash  # type: ignore[arg-type]
        )


def test_verified_batch_reader_rejects_account_oid_and_cid_mismatch(monkeypatch) -> None:
    wrong_account = "8c3bf816-ae5e-4f2e-9f22-c391a9c9f17a"
    adapter, calls = _verified_adapter(
        _verified_raw_batch(), monkeypatch, account_id=wrong_account
    )
    with pytest.raises(AlpacaFillActivityConflict, match="differs from adaptive"):
        read_verified_alpaca_paper_fill_batch(
            adapter,
            cycle=_verified_cycle(),
            provider_order_id="alpaca-entry-order-7",
            expected_client_order_id="chili-cycle-entry-7",
        )
    assert calls == []

    raw = _verified_raw_batch()
    raw["provider_order"]["id"] = "forged-order"
    adapter, _calls = _verified_adapter(raw, monkeypatch)
    with pytest.raises(AlpacaFillActivityError, match="incomplete or unreadable"):
        read_verified_alpaca_paper_fill_batch(
            adapter,
            cycle=_verified_cycle(),
            provider_order_id="alpaca-entry-order-7",
            expected_client_order_id="chili-cycle-entry-7",
        )

    adapter, _calls = _verified_adapter(
        _verified_raw_batch(client_order_id="forged-cid"), monkeypatch
    )
    with pytest.raises(AlpacaFillActivityConflict, match="durable order owner"):
        read_verified_alpaca_paper_fill_batch(
            adapter,
            cycle=_verified_cycle(),
            provider_order_id="alpaca-entry-order-7",
            expected_client_order_id="chili-cycle-entry-7",
        )


def test_verified_batch_reader_binds_exact_order_and_fill_economics(monkeypatch) -> None:
    projected = _verified_raw_batch()
    projected["provider_order"]["filled_qty"] = "9.0000000000"
    adapter, _calls = _verified_adapter(projected, monkeypatch)
    with pytest.raises(
        AlpacaFillActivityConflict,
        match="projection differs from complete activities",
    ):
        read_verified_alpaca_paper_fill_batch(
            adapter,
            cycle=_verified_cycle(),
            provider_order_id="alpaca-entry-order-7",
            expected_client_order_id="chili-cycle-entry-7",
        )

    activities = [
        _verified_activity(
            activity_id="verified-partial-1",
            transaction_time="2026-07-15T13:00:00Z",
            quantity="4.0000000000",
            cumulative_quantity="4.0000000000",
            leaves_quantity="6.0000000000",
            trade_type="partial_fill",
        ),
        _verified_activity(
            activity_id="verified-final-2",
            transaction_time="2026-07-15T13:00:01Z",
            quantity="6.0000000000",
            cumulative_quantity="9.0000000000",
            leaves_quantity="1.0000000000",
            trade_type="fill",
        ),
    ]
    adapter, _calls = _verified_adapter(
        _verified_raw_batch(activities=activities), monkeypatch
    )
    with pytest.raises(
        AlpacaFillActivityConflict,
        match="cumulative sequence is not exact",
    ):
        read_verified_alpaca_paper_fill_batch(
            adapter,
            cycle=_verified_cycle(),
            provider_order_id="alpaca-entry-order-7",
            expected_client_order_id="chili-cycle-entry-7",
        )


def _reset_for_entry_fill(
    reservation: AdaptiveRiskReservation,
    *,
    state: str = "submitted",
) -> None:
    reservation.state = state
    reservation.cumulative_filled_quantity_shares = 0
    reservation.open_quantity_shares = 0
    reservation.open_structural_risk_usd = Decimal("0")
    reservation.open_gross_notional_usd = Decimal("0")
    reservation.open_buying_power_impact_usd = Decimal("0")
    reservation.first_fill_at = None
    reservation.event_sequence = 0
    reservation.last_event_sha256 = None
    reservation.lifecycle_contradiction_source_state = None
    reservation.lifecycle_contradiction_at = None
    reservation.lifecycle_contradiction_evidence_sha256 = None
    if state == "released":
        reservation.pending_structural_risk_usd = Decimal("0")
        reservation.pending_gross_notional_usd = Decimal("0")
        reservation.pending_buying_power_impact_usd = Decimal("0")
        reservation.release_reason = "confirmed_zero_fill"
        reservation.released_at = datetime(
            2026, 7, 15, 13, 0, 5, tzinfo=UTC
        )
    else:
        reservation.pending_structural_risk_usd = Decimal(
            reservation.planned_structural_risk_usd
        )
        reservation.pending_gross_notional_usd = Decimal(
            reservation.planned_gross_notional_usd
        )
        reservation.pending_buying_power_impact_usd = Decimal(
            reservation.planned_buying_power_impact_usd
        )
        reservation.release_reason = None
        reservation.released_at = None


def _rest_projection_fill_evidence(
    batch: PreparedAlpacaPaperFillBatch,
) -> DurableOrderLifecycleEvidence:
    return DurableOrderLifecycleEvidence(
        event_kind="cumulative_fill",
        durability_kind="authoritative_broker_event",
        provider_event_id=f"alpaca-rest:cumulative_fill:{_hash('projection')}",
        broker_source="alpaca",
        connection_generation=batch.cycle.broker_connection_generation,
        account_scope=batch.cycle.account_scope,
        execution_family=batch.cycle.execution_family,
        broker_environment="paper",
        account_identity_sha256=batch.cycle.account_identity_sha256,
        client_order_id=batch.expected_client_order_id,
        broker_order_id=batch.provider_order_id,
        observed_at=datetime(2026, 7, 15, 13, 0, 1, tzinfo=UTC),
        available_at=datetime(2026, 7, 15, 13, 0, 2, tzinfo=UTC),
        event_content_sha256=_hash("projection"),
        cumulative_filled_quantity=10,
        source_record_table="alpaca_rest_order_observations",
        source_record_id=f"{batch.provider_order_id}:{_hash('projection')}",
        order_status="filled",
    )


def _terminal_entry_or_position_evidence(
    batch: PreparedAlpacaPaperFillBatch,
    *,
    event_at: datetime,
    event_kind: str,
    order_status: str,
    cumulative: int,
    label: str,
    remaining_open_quantity: int | None = None,
) -> DurableOrderLifecycleEvidence:
    content_sha = _hash(label)
    return DurableOrderLifecycleEvidence(
        event_kind=event_kind,
        durability_kind="authoritative_broker_event",
        provider_event_id=f"alpaca-test:{label}:{content_sha}",
        broker_source="alpaca",
        connection_generation=batch.cycle.broker_connection_generation,
        account_scope=batch.cycle.account_scope,
        execution_family=batch.cycle.execution_family,
        broker_environment="paper",
        account_identity_sha256=batch.cycle.account_identity_sha256,
        client_order_id=batch.expected_client_order_id,
        broker_order_id=batch.provider_order_id,
        observed_at=event_at,
        available_at=event_at,
        event_content_sha256=content_sha,
        cumulative_filled_quantity=cumulative,
        source_record_table="alpaca_test_lifecycle_observations",
        source_record_id=content_sha,
        order_status=order_status,
        remaining_open_quantity=remaining_open_quantity,
    )


def _exit_owner_binding(
    cycle: AlpacaPaperFillCycleBinding,
    *,
    exit_client_order_id: str = "chili-cycle-exit-7",
    order_quantity: int = 10,
    generation: int = 1,
) -> AdaptiveExitOwnerTransportBinding:
    return AdaptiveExitOwnerTransportBinding(
        reservation_id=cycle.reservation_id,
        expected_account_id=VERIFIED_ACCOUNT_ID,
        account_identity_sha256=cycle.account_identity_sha256,
        decision_packet_sha256=cycle.decision_packet_sha256,
        symbol=cycle.symbol,
        entry_client_order_id=cycle.cycle_client_order_id,
        exit_client_order_id=exit_client_order_id,
        order_request={
            "account_scope": "alpaca:paper",
            "alpaca_account_id": VERIFIED_ACCOUNT_ID,
            "base_size": str(order_quantity),
            "client_order_id": exit_client_order_id,
            "order_type": "market",
            "position_intent": "sell_to_close",
            "product_id": cycle.symbol,
            "side": "sell",
            "time_in_force": "day",
        },
        transport_claim_token="exit-owner-claim-1",
        transport_owner_session_id=7001,
        transport_owner_generation=generation,
        transport_owner_kind="ordinary_exit",
        transport_lease_id=f"exit-owner-lease-{generation}",
        transport_runtime_generation=f"paper-runtime-{generation}",
        transport_connection_generation=f"paper-connection-{generation}",
    )


def _append_exit_owner_receipt(
    db,
    store: AdaptiveRiskReservationStore,
    cycle: AlpacaPaperFillCycleBinding,
    *,
    provider_order_id: str = "alpaca-exit-order-7",
    exit_client_order_id: str = "chili-cycle-exit-7",
    order_quantity: int = 10,
    generation: int = 1,
) -> AdaptiveExitOwnerReceipt:
    if db.get(
        AlpacaPaperAccountSettlementHead,
        (cycle.account_scope, cycle.account_identity_sha256),
    ) is None:
        db.add(
            new_zero_settlement_head(
                account_identity_sha256=cycle.account_identity_sha256
            )
        )
        db.flush()
    binding = _exit_owner_binding(
        cycle,
        exit_client_order_id=exit_client_order_id,
        order_quantity=order_quantity,
        generation=generation,
    )
    started_at = datetime.now(UTC)
    claim_context = {
        "symbol": binding.symbol,
        "claim_token": binding.transport_claim_token,
        "owner_session_id": binding.transport_owner_session_id,
        "account_scope": binding.account_scope,
        "alpaca_account_id": binding.expected_account_id,
    }
    acquired = acquire_action_claim(
        db,
        symbol=binding.symbol,
        action="entry",
        claim_token=binding.transport_claim_token,
        owner_session_id=binding.transport_owner_session_id,
        client_order_id=binding.entry_client_order_id,
        metadata={
            "alpaca_account_id": binding.expected_account_id,
            "order_request": {
                "alpaca_account_id": binding.expected_account_id,
                "client_order_id": binding.entry_client_order_id,
                "product_id": binding.symbol,
                "side": "buy",
            },
        },
        account_scope=binding.account_scope,
    )
    assert acquired["ok"] is True
    leased = lease_owner_transport(
        db,
        **claim_context,
        transport_kind=binding.transport_owner_kind,
        client_order_id=binding.exit_client_order_id,
        order_request=dict(binding.order_request),
        lease_token=binding.transport_lease_id,
        exit_owner_store=store,
        exit_owner_binding=binding,
        exit_owner_effective_at=started_at,
        exit_owner_available_at=started_at,
    )
    assert leased["ok"] is True
    # Production commits the transport-start marker before any broker I/O.
    # This fixture may be nested inside a larger rollback-only migration test,
    # so force just that deferred invariant now and defer subsequent events.
    db.execute(text(
        "SET CONSTRAINTS trg_adaptive_exit_owner_event_head IMMEDIATE"
    ))
    db.execute(text(
        "SET CONSTRAINTS trg_adaptive_exit_owner_event_head DEFERRED"
    ))
    outcome_at = started_at + timedelta(milliseconds=1)
    assert advance_owner_transport(
        db,
        **claim_context,
        client_order_id=binding.exit_client_order_id,
        lease_token=binding.transport_lease_id,
        phase="submitted",
        broker_order_id=provider_order_id,
        metadata={"provider_status": "accepted"},
        exit_owner_store=store,
        exit_owner_effective_at=outcome_at,
        exit_owner_available_at=outcome_at,
        provider_status="accepted",
        provider_cumulative_quantity=0,
        observer_claim_token=binding.transport_claim_token,
        observer_session_id=binding.transport_owner_session_id,
        observer_generation=binding.transport_owner_generation,
        observer_runtime_generation=binding.transport_runtime_generation,
        observer_connection_generation=binding.transport_connection_generation,
    ) is True
    readable, claim = read_action_claim(
        db,
        symbol=binding.symbol,
        account_scope=binding.account_scope,
    )
    assert readable and claim is not None
    receipt_sha = claim["metadata"]["owner_transport"][
        "exit_owner_last_event_sha256"
    ]
    return store.load_exit_owner_receipt(
        receipt_sha,
        reservation_id=cycle.reservation_id,
        session=db,
    )


def _prepare_settled_partial_cycle_with_owner(
    db,
    monkeypatch,
    *,
    include_replacement_owner: bool = False,
):
    initial_batch = _partial_verified_entry_batch(monkeypatch)
    store = AdaptiveRiskReservationStore(engine)
    with db.begin():
        reservation, _packet = _persist_cycle_rows(db, initial_batch.cycle)
        _reset_for_entry_fill(reservation)
        db.flush()
        publish_prepared_alpaca_paper_entry_fill_batch(db, initial_batch)
        store.finalize_filled_entry_remainder(
            reservation.reservation_id,
            evidence=_terminal_entry_or_position_evidence(
                initial_batch,
                event_at=db.execute(text("SELECT clock_timestamp()")).scalar_one(),
                event_kind="filled_entry_terminal",
                order_status="canceled",
                cumulative=4,
                label="partial-entry-canceled-v2",
            ),
            session=db,
        )
        owner_receipt = _append_exit_owner_receipt(
            db,
            store,
            initial_batch.cycle,
            order_quantity=4,
        )

    first_recorded_exit = 1 if include_replacement_owner else 2
    initial_exit_batch = _verified_exit_batch(
        monkeypatch,
        owner_receipt,
        quantities=(first_recorded_exit,),
        order_quantity=4,
    )
    with db.begin():
        append_prepared_alpaca_paper_fill_batch(db, initial_exit_batch)
        terminal_at = datetime.now(UTC)
        initial_binding = owner_receipt.binding
        assert resolve_owner_transport_terminal(
            db,
            symbol=initial_binding.symbol,
            claim_token=initial_binding.transport_claim_token,
            owner_session_id=initial_binding.transport_owner_session_id,
            client_order_id=initial_binding.exit_client_order_id,
            broker_order_id=owner_receipt.provider_order_id,
            broker_order_status="canceled",
            filled_size=first_recorded_exit,
            remaining_quantity=4 - first_recorded_exit,
            account_scope=initial_binding.account_scope,
            alpaca_account_id=initial_binding.expected_account_id,
            exit_owner_store=store,
            exit_owner_effective_at=terminal_at,
            exit_owner_available_at=terminal_at,
            observer_claim_token=initial_binding.transport_claim_token,
            observer_session_id=initial_binding.transport_owner_session_id,
            observer_generation=initial_binding.transport_owner_generation,
            observer_runtime_generation=(
                initial_binding.transport_runtime_generation
            ),
            observer_connection_generation=(
                initial_binding.transport_connection_generation
            ),
        ) is True
        # Model a legitimate cancel/replace ladder: every request is bounded by
        # the then-known remainder.  A later page may still reveal a fill from a
        # canceled predecessor, which is the signed-quarantine case under test.
        db.execute(text(
            "SET CONSTRAINTS trg_adaptive_exit_owner_event_head IMMEDIATE"
        ))
        db.execute(text(
            "SET CONSTRAINTS trg_adaptive_exit_owner_event_head DEFERRED"
        ))
        replacement_receipt = _append_exit_owner_receipt(
            db,
            store,
            initial_batch.cycle,
            provider_order_id="alpaca-exit-order-replacement-7",
            exit_client_order_id="chili-cycle-exit-replacement-7",
            order_quantity=4 - first_recorded_exit,
            generation=2,
        )
        replacement_recorded_exit = (
            0 if include_replacement_owner else 4 - first_recorded_exit
        )
        if replacement_recorded_exit:
            replacement_batch = _verified_exit_batch(
                monkeypatch,
                replacement_receipt,
                quantities=(replacement_recorded_exit,),
                order_quantity=4 - first_recorded_exit,
                first_activity_number=11,
                transaction_second_start=0,
            )
            append_prepared_alpaca_paper_fill_batch(db, replacement_batch)
        if include_replacement_owner:
            # The provider acknowledgement is a distinct production commit
            # before a later terminal reconciliation observes cancellation.
            db.execute(text(
                "SET CONSTRAINTS trg_adaptive_exit_owner_event_head IMMEDIATE"
            ))
            db.execute(text(
                "SET CONSTRAINTS trg_adaptive_exit_owner_event_head DEFERRED"
            ))
            replacement_binding = replacement_receipt.binding
            replacement_terminal_at = max(
                datetime.now(UTC),
                replacement_receipt.available_at,
            ) + timedelta(milliseconds=1)
            assert resolve_owner_transport_terminal(
                db,
                symbol=replacement_binding.symbol,
                claim_token=replacement_binding.transport_claim_token,
                owner_session_id=(
                    replacement_binding.transport_owner_session_id
                ),
                client_order_id=replacement_binding.exit_client_order_id,
                broker_order_id=replacement_receipt.provider_order_id,
                broker_order_status="canceled",
                filled_size=replacement_recorded_exit,
                remaining_quantity=(
                    4 - first_recorded_exit - replacement_recorded_exit
                ),
                account_scope=replacement_binding.account_scope,
                alpaca_account_id=replacement_binding.expected_account_id,
                exit_owner_store=store,
                exit_owner_effective_at=replacement_terminal_at,
                exit_owner_available_at=replacement_terminal_at,
                observer_claim_token=(
                    replacement_binding.transport_claim_token
                ),
                observer_session_id=(
                    replacement_binding.transport_owner_session_id
                ),
                observer_generation=(
                    replacement_binding.transport_owner_generation
                ),
                observer_runtime_generation=(
                    replacement_binding.transport_runtime_generation
                ),
                observer_connection_generation=(
                    replacement_binding.transport_connection_generation
                ),
            ) is True
    with db.begin():
        if include_replacement_owner:
            final_receipt = _append_exit_owner_receipt(
                db,
                store,
                initial_batch.cycle,
                provider_order_id="alpaca-exit-order-final-7",
                exit_client_order_id="chili-cycle-exit-final-7",
                order_quantity=3,
                generation=3,
            )
            final_batch = _verified_exit_batch(
                monkeypatch,
                final_receipt,
                quantities=(3,),
                order_quantity=3,
                first_activity_number=21,
                transaction_second_start=3,
            )
            append_prepared_alpaca_paper_fill_batch(db, final_batch)
        store.close_open_exposure(
            initial_batch.cycle.reservation_id,
            evidence=_terminal_entry_or_position_evidence(
                initial_batch,
                event_at=db.execute(text("SELECT clock_timestamp()")).scalar_one(),
                event_kind="position_flat",
                order_status="flat",
                cumulative=4,
                remaining_open_quantity=0,
                label="position-flat-before-v2-contradiction",
            ),
            session=db,
        )
        settled = settle_flat_alpaca_paper_cycle(
            db,
            reservation_id=initial_batch.cycle.reservation_id,
        )
        settlement_sha = settled.row.settlement_sha256
        settled_pnl = Decimal(settled.row.net_realized_pnl_usd)
        settled_fill_hashes = tuple(
            db.scalars(
                select(AlpacaPaperFillActivity.event_sha256)
                .where(
                    AlpacaPaperFillActivity.reservation_id
                    == initial_batch.cycle.reservation_id
                )
                .order_by(AlpacaPaperFillActivity.sequence)
            )
        )
    return {
        "cycle": initial_batch.cycle,
        "entry_batch": initial_batch,
        "owner_receipt": owner_receipt,
        "replacement_receipt": replacement_receipt,
        "settlement_sha256": settlement_sha,
        "settled_pnl": settled_pnl,
        "settled_fill_hashes": settled_fill_hashes,
    }


def _prepare_historical_settled_cycle_without_owner(db, monkeypatch):
    """Create the exact pre-354 settled shape without reserved owner events."""

    initial_batch = _partial_verified_entry_batch(monkeypatch)
    historical_exit = _historical_verified_exit_batch(monkeypatch)
    store = AdaptiveRiskReservationStore(engine)
    with db.begin():
        reservation, _packet = _persist_cycle_rows(db, initial_batch.cycle)
        _reset_for_entry_fill(reservation)
        db.flush()
        publish_prepared_alpaca_paper_entry_fill_batch(db, initial_batch)
        store.finalize_filled_entry_remainder(
            reservation.reservation_id,
            evidence=_terminal_entry_or_position_evidence(
                initial_batch,
                event_at=db.execute(text("SELECT clock_timestamp()")).scalar_one(),
                event_kind="filled_entry_terminal",
                order_status="canceled",
                cumulative=4,
                label="historical-partial-entry-canceled",
            ),
            session=db,
        )
        append_prepared_alpaca_paper_fill_batch(db, historical_exit)
        store.close_open_exposure(
            reservation.reservation_id,
            evidence=_terminal_entry_or_position_evidence(
                initial_batch,
                event_at=db.execute(text("SELECT clock_timestamp()")).scalar_one(),
                event_kind="position_flat",
                order_status="flat",
                cumulative=4,
                remaining_open_quantity=0,
                label="historical-position-flat",
            ),
            session=db,
        )
        settled = settle_flat_alpaca_paper_cycle(
            db,
            reservation_id=reservation.reservation_id,
        )
        settlement_sha256 = settled.row.settlement_sha256
        db.flush()
        before = {
            field_name: getattr(reservation, field_name)
            for field_name in (
                "state",
                "cumulative_filled_quantity_shares",
                "open_quantity_shares",
                "pending_structural_risk_usd",
                "pending_gross_notional_usd",
                "pending_buying_power_impact_usd",
                "open_structural_risk_usd",
                "open_gross_notional_usd",
                "open_buying_power_impact_usd",
                "last_broker_observed_at",
                "last_broker_available_at",
                "last_source_event_content_sha256",
                "event_sequence",
                "last_event_sha256",
                "version",
                "updated_at",
                "lifecycle_contradiction_source_state",
                "lifecycle_contradiction_at",
                "lifecycle_contradiction_evidence_sha256",
            )
        }
    return {
        "cycle": initial_batch.cycle,
        "entry_batch": initial_batch,
        "settlement_sha256": settlement_sha256,
        "reservation_before_projection": before,
    }


def _migration_354_physical_fingerprint(conn) -> str:
    """Hash the exact migration-owned catalog objects for reapply tests."""

    names = (
        "ck_adaptive_risk_reservation_quantities",
        "ck_adaptive_risk_reservation_contradiction",
        "fk_alpaca_ps_fill_exit_owner_receipt",
        "uq_alpaca_ps_fill_batch_ordinal",
        "ck_alpaca_post_settlement_fill_schema",
        "ck_alpaca_post_settlement_fill_scope",
        "ck_alpaca_post_settlement_fill_lineage",
        "ck_alpaca_post_settlement_fill_values",
        "ck_alpaca_post_settlement_fill_clocks",
        "ck_alpaca_post_settlement_fill_fee",
        "ck_alpaca_post_settlement_fill_hashes",
    )
    catalog: list[tuple[str, ...]] = []
    catalog.extend(
        ("column", str(row[0]), str(row[1]), str(row[2]), str(row[3]))
        for row in conn.execute(
            text(
                "SELECT table_name, column_name, data_type, "
                "       COALESCE(character_maximum_length::text, '') "
                "FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND ((table_name = 'adaptive_risk_reservations' "
                "      AND column_name = "
                "          'post_settlement_net_position_quantity_shares') "
                " OR (table_name = "
                "      'alpaca_paper_post_settlement_fill_contradictions' "
                "     AND column_name = ANY(:columns))) "
                "ORDER BY table_name, column_name"
            ),
            {
                "columns": [
                    "exit_owner_receipt_sha256",
                    "provider_client_order_id",
                    "projection_prior_net_position_quantity_shares",
                    "projected_net_position_quantity_shares",
                ]
            },
        )
    )
    catalog.extend(
        ("constraint", str(row[0]), " ".join(str(row[1]).split()))
        for row in conn.execute(
            text(
                "SELECT constraint_row.conname, "
                "       pg_get_constraintdef(constraint_row.oid) "
                "FROM pg_constraint AS constraint_row "
                "JOIN pg_class AS table_row "
                "  ON table_row.oid = constraint_row.conrelid "
                "JOIN pg_namespace AS namespace "
                "  ON namespace.oid = table_row.relnamespace "
                "WHERE namespace.nspname = current_schema() "
                "AND constraint_row.conname = ANY(:names) "
                "ORDER BY conname"
            ),
            {"names": list(names)},
        )
    )
    catalog.extend(
        ("trigger", str(row[0]), " ".join(str(row[1]).split()))
        for row in conn.execute(
            text(
                "SELECT tgname, pg_get_triggerdef(oid) FROM pg_trigger "
                "WHERE NOT tgisinternal AND (tgname LIKE "
                "'trg_adaptive_exit_owner_%' OR tgname = "
                "'trg_adaptive_reservation_event_head' OR tgname = "
                "'trg_adaptive_risk_reservation_events_append_only' OR "
                "tgname = "
                "'trg_adaptive_risk_reservation_events_no_truncate' OR "
                "tgname LIKE 'trg_alpaca_post_settlement_%' OR tgname = "
                "'trg_alpaca_reservation_settlement_state') "
                "ORDER BY tgname"
            )
        )
    )
    catalog.extend(
        ("index", str(row[0]), " ".join(str(row[1]).split()))
        for row in conn.execute(
            text(
                "SELECT index_relation.relname, "
                "       pg_get_indexdef(index_relation.oid) "
                "FROM pg_class AS index_relation "
                "JOIN pg_namespace AS namespace "
                "  ON namespace.oid = index_relation.relnamespace "
                "WHERE namespace.nspname = current_schema() "
                "AND index_relation.relname = "
                "    'ix_alpaca_ps_fill_order_sequence'"
            )
        )
    )
    catalog.extend(
        (
            "function",
            str(row[0]),
            str(row[1]),
            " ".join(str(row[2]).split()),
        )
        for row in conn.execute(
            text(
                "SELECT function_row.proname, "
                "       pg_get_function_identity_arguments(function_row.oid), "
                "       pg_get_functiondef(function_row.oid) "
                "FROM pg_proc AS function_row "
                "JOIN pg_namespace AS namespace "
                "  ON namespace.oid = function_row.pronamespace "
                "WHERE namespace.nspname = current_schema() "
                "AND function_row.proname = ANY(:names) "
                "ORDER BY function_row.proname, "
                "         pg_get_function_identity_arguments(function_row.oid)"
            ),
            {
                "names": [
                    "chili_reject_adaptive_append_only_mutation",
                    "chili_guard_adaptive_exit_owner_event_insert",
                    "chili_require_adaptive_exit_owner_event_head",
                    "chili_require_adaptive_reservation_event_head",
                    "chili_guard_alpaca_reservation_settlement_state",
                    "chili_guard_alpaca_post_settlement_fill_insert",
                    "chili_require_alpaca_post_settlement_fill_projection",
                    "chili_require_alpaca_post_settlement_batch_complete",
                    "chili_reject_alpaca_post_settlement_fill_mutation",
                ]
            },
        )
    )
    return hashlib.sha256(repr(catalog).encode("utf-8")).hexdigest()


def _migration_354_retained_data_fingerprint(conn, reservation_id: uuid.UUID) -> str:
    """Hash row bytes/identities that historical repair must never rewrite."""

    retained = tuple(
        tuple(row)
        for row in conn.execute(
            text(
                "SELECT ctid::text, xmin::text, to_jsonb(row_data)::text "
                "FROM alpaca_paper_post_settlement_fill_contradictions "
                "AS row_data WHERE reservation_id = :reservation_id "
                "ORDER BY contradiction_sequence"
            ),
            {"reservation_id": reservation_id},
        )
    )
    reservation = conn.execute(
        text(
            "SELECT ctid::text, xmin::text, to_jsonb(row_data)::text "
            "FROM adaptive_risk_reservations AS row_data "
            "WHERE reservation_id = :reservation_id"
        ),
        {"reservation_id": reservation_id},
    ).one()
    return hashlib.sha256(
        repr((retained, tuple(reservation))).encode("utf-8")
    ).hexdigest()


def test_migration_354_upgrades_retained_v1_preserves_row_identity_and_bridges_first_v2_sell(
    db,
    monkeypatch,
) -> None:
    historical = _prepare_historical_settled_cycle_without_owner(db, monkeypatch)
    late_buy = _verified_entry_batch(
        monkeypatch,
        quantities=(4, 2),
        order_quantity=10,
    )
    with db.begin():
        created = publish_prepared_alpaca_paper_entry_fill_batch(db, late_buy)
        db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        assert len(created.contradiction_sha256s) == 1
        v2_sha = created.contradiction_sha256s[0]

    with engine.connect() as conn:
        transaction = conn.begin()
        historical_db = Session(bind=conn, expire_on_commit=False)
        try:
            row = historical_db.get(
                AlpacaPaperPostSettlementFillContradiction,
                v2_sha,
            )
            assert row is not None
            v1_values = {
                field_name: getattr(row, field_name)
                for field_name in (
                    capture_module._POST_SETTLEMENT_CONTRADICTION_BODY_FIELDS
                )
            }
            v1_values["contradiction_schema_version"] = (
                capture_module.LEGACY_POST_SETTLEMENT_FILL_CONTRADICTION_SCHEMA_VERSION
            )
            v1_body = capture_module._post_settlement_contradiction_content_body(
                v1_values
            )
            v1_json, v1_sha = capture_module._canonical_json_text(
                v1_body,
                "retained_v1_post_settlement_fill_contradiction",
            )

            # Recreate the exact historical v1 lifecycle shape.  This is test
            # downgrade scaffolding only; all DDL and data changes roll back.
            for table_name, trigger_name in (
                (
                    "alpaca_paper_post_settlement_fill_contradictions",
                    "trg_alpaca_post_settlement_fill_immutable",
                ),
                (
                    "adaptive_risk_reservation_events",
                    "trg_adaptive_risk_reservation_events_append_only",
                ),
                (
                    "adaptive_risk_reservations",
                    "trg_adaptive_reservation_event_head",
                ),
                (
                    "adaptive_risk_reservations",
                    "trg_alpaca_post_settlement_fill_projection",
                ),
                (
                    "adaptive_risk_reservations",
                    "trg_alpaca_reservation_settlement_state",
                ),
            ):
                conn.execute(text(
                    f'DROP TRIGGER IF EXISTS "{trigger_name}" '
                    f'ON "{table_name}"'
                ))
            conn.execute(
                text(
                    "DELETE FROM adaptive_risk_reservation_events "
                    "WHERE reservation_id = :reservation_id "
                    "AND broker_event_id = :broker_event_id"
                ),
                {
                    "reservation_id": historical["cycle"].reservation_id,
                    "broker_event_id": f"alpaca-post-settlement-fill:{v2_sha}",
                },
            )
            conn.execute(
                text(
                    "UPDATE alpaca_paper_post_settlement_fill_contradictions "
                    "SET contradiction_sha256 = :v1_sha, "
                    "contradiction_schema_version = :v1_schema, "
                    "contradiction_content_canonical_json = :v1_json, "
                    "contradiction_content_sha256 = :v1_sha, "
                    "exit_owner_receipt_sha256 = NULL, "
                    "provider_client_order_id = NULL, "
                    "projection_prior_net_position_quantity_shares = NULL, "
                    "projected_net_position_quantity_shares = NULL "
                    "WHERE contradiction_sha256 = :v2_sha"
                ),
                {
                    "v1_sha": v1_sha,
                    "v1_schema": (
                        capture_module.LEGACY_POST_SETTLEMENT_FILL_CONTRADICTION_SCHEMA_VERSION
                    ),
                    "v1_json": v1_json,
                    "v2_sha": v2_sha,
                },
            )
            before = historical["reservation_before_projection"]
            conn.execute(
                text(
                    "UPDATE adaptive_risk_reservations SET "
                    "state = :state, "
                    "cumulative_filled_quantity_shares = :cumulative, "
                    "open_quantity_shares = :open_quantity, "
                    "post_settlement_net_position_quantity_shares = NULL, "
                    "pending_structural_risk_usd = :pending_risk, "
                    "pending_gross_notional_usd = :pending_gross, "
                    "pending_buying_power_impact_usd = :pending_bp, "
                    "open_structural_risk_usd = :open_risk, "
                    "open_gross_notional_usd = :open_gross, "
                    "open_buying_power_impact_usd = :open_bp, "
                    "last_broker_observed_at = :observed_at, "
                    "last_broker_available_at = :available_at, "
                    "last_source_event_content_sha256 = :source_sha, "
                    "event_sequence = :event_sequence, "
                    "last_event_sha256 = :event_sha, version = :version, "
                    "updated_at = :updated_at, "
                    "lifecycle_contradiction_source_state = :contradiction_state, "
                    "lifecycle_contradiction_at = :contradiction_at, "
                    "lifecycle_contradiction_evidence_sha256 = "
                    ":contradiction_sha "
                    "WHERE reservation_id = :reservation_id"
                ),
                {
                    "reservation_id": historical["cycle"].reservation_id,
                    "state": before["state"],
                    "cumulative": before["cumulative_filled_quantity_shares"],
                    "open_quantity": before["open_quantity_shares"],
                    "pending_risk": before["pending_structural_risk_usd"],
                    "pending_gross": before["pending_gross_notional_usd"],
                    "pending_bp": before["pending_buying_power_impact_usd"],
                    "open_risk": before["open_structural_risk_usd"],
                    "open_gross": before["open_gross_notional_usd"],
                    "open_bp": before["open_buying_power_impact_usd"],
                    "observed_at": before["last_broker_observed_at"],
                    "available_at": before["last_broker_available_at"],
                    "source_sha": before["last_source_event_content_sha256"],
                    "event_sequence": before["event_sequence"],
                    "event_sha": before["last_event_sha256"],
                    "version": before["version"],
                    "updated_at": before["updated_at"],
                    "contradiction_state": before[
                        "lifecycle_contradiction_source_state"
                    ],
                    "contradiction_at": before["lifecycle_contradiction_at"],
                    "contradiction_sha": before[
                        "lifecycle_contradiction_evidence_sha256"
                    ],
                },
            )
            historical_db.expire_all()
            legacy_row = historical_db.get(
                AlpacaPaperPostSettlementFillContradiction,
                v1_sha,
            )
            assert legacy_row is not None
            capture_module.verify_alpaca_paper_post_settlement_fill_contradiction_row(
                legacy_row
            )
            store = AdaptiveRiskReservationStore(engine)
            legacy_state = store.apply_cumulative_fill(
                historical["cycle"].reservation_id,
                evidence=DurableOrderLifecycleEvidence(
                    event_kind="cumulative_fill",
                    durability_kind=(
                        capture_module.POST_SETTLEMENT_FILL_CONTRADICTION_DURABILITY_KIND
                    ),
                    provider_event_id=(
                        f"alpaca-post-settlement-fill:{v1_sha}"
                    ),
                    broker_source="alpaca",
                    connection_generation=legacy_row.broker_connection_generation,
                    account_scope=legacy_row.account_scope,
                    execution_family=legacy_row.execution_family,
                    broker_environment=legacy_row.broker_environment,
                    account_identity_sha256=legacy_row.account_identity_sha256,
                    client_order_id=legacy_row.expected_client_order_id,
                    broker_order_id=legacy_row.broker_order_id,
                    observed_at=legacy_row.provider_transaction_at,
                    available_at=legacy_row.provider_available_at,
                    event_content_sha256=v1_sha,
                    cumulative_filled_quantity=int(
                        legacy_row.broker_observed_cumulative_quantity
                    ),
                    source_record_table=(
                        "alpaca_paper_post_settlement_fill_contradictions"
                    ),
                    source_record_id=v1_sha,
                    order_status="partially_filled",
                ),
                session=historical_db,
            )
            historical_db.flush()
            assert legacy_state.post_settlement_net_position_quantity_shares is None
            assert legacy_state.open_quantity_shares == 2
            frozen_row = conn.execute(
                text(
                    "SELECT ctid::text, xmin::text, contradiction_sha256, "
                    "contradiction_content_canonical_json, "
                    "contradiction_content_sha256, created_at "
                    "FROM alpaca_paper_post_settlement_fill_contradictions "
                    "WHERE contradiction_sha256 = :v1_sha"
                ),
                {"v1_sha": v1_sha},
            ).one()

            conn.execute(text(
                "DROP TRIGGER IF EXISTS "
                "trg_adaptive_exit_owner_event_insert "
                "ON adaptive_risk_reservation_events"
            ))
            conn.execute(text(
                "ALTER TABLE adaptive_risk_reservations DROP COLUMN "
                "post_settlement_net_position_quantity_shares CASCADE"
            ))
            conn.execute(text(
                "ALTER TABLE alpaca_paper_post_settlement_fill_contradictions "
                "DROP COLUMN exit_owner_receipt_sha256 CASCADE, "
                "DROP COLUMN provider_client_order_id CASCADE, "
                "DROP COLUMN projection_prior_net_position_quantity_shares "
                "CASCADE, DROP COLUMN "
                "projected_net_position_quantity_shares CASCADE"
            ))
            migrations._install_and_verify_migration_354_contract(
                conn,
                allow_registered_repair=False,
            )
            assert conn.execute(
                text(
                    "SELECT ctid::text, xmin::text, contradiction_sha256, "
                    "contradiction_content_canonical_json, "
                    "contradiction_content_sha256, created_at "
                    "FROM alpaca_paper_post_settlement_fill_contradictions "
                    "WHERE contradiction_sha256 = :v1_sha"
                ),
                {"v1_sha": v1_sha},
            ).one() == frozen_row

            historical_db.expunge_all()
            retained = historical_db.get(
                AlpacaPaperPostSettlementFillContradiction,
                v1_sha,
            )
            assert retained is not None
            capture_module.verify_alpaca_paper_post_settlement_fill_contradiction_row(
                retained
            )
            replayed_v1 = store.apply_post_settlement_fill_contradiction(
                historical["cycle"].reservation_id,
                contradiction_sha256=v1_sha,
                session=historical_db,
            )
            assert replayed_v1.post_settlement_net_position_quantity_shares is None
            assert replayed_v1.open_quantity_shares == 2

            owner = _append_exit_owner_receipt(
                historical_db,
                store,
                historical["cycle"],
                provider_order_id="alpaca-first-v2-sell-after-v1",
                exit_client_order_id="chili-first-v2-sell-after-v1",
                order_quantity=2,
                generation=9,
            )
            first_v2_sell = _verified_exit_batch(
                monkeypatch,
                owner,
                quantities=(1,),
                order_quantity=2,
                first_activity_number=41,
            )
            bridged = publish_prepared_alpaca_paper_post_settlement_fill_batch(
                historical_db,
                first_v2_sell,
            )
            conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
            terminal = historical_db.get(
                AlpacaPaperPostSettlementFillContradiction,
                bridged.contradiction_sha256s[-1],
            )
            assert terminal.projection_prior_net_position_quantity_shares == 2
            assert terminal.projected_net_position_quantity_shares == 1
            assert terminal.prior_recorded_cumulative_quantity == 0
            assert bridged.reservation_state.cumulative_filled_quantity_shares == 6
            assert bridged.reservation_state.open_quantity_shares == 1
        finally:
            historical_db.close()
            transaction.rollback()


def test_migration_354_partial_schema_is_completed_or_rejected_before_mutation(
    db,
) -> None:
    with db.begin():
        reservation, _packet = _persist_cycle_rows(db, _verified_cycle())
        reservation_id = reservation.reservation_id

    # A correctly named marker is not authority when another required object
    # is missing.  The current-contract branch verifies and never self-attests.
    with engine.connect() as conn:
        transaction = conn.begin()
        try:
            conn.execute(text(
                "ALTER TABLE alpaca_paper_post_settlement_fill_contradictions "
                "DROP CONSTRAINT ck_alpaca_post_settlement_fill_values"
            ))
            conn.execute(text(
                "DROP TRIGGER trg_adaptive_exit_owner_event_insert "
                "ON adaptive_risk_reservation_events"
            ))
            conn.execute(text(
                "CREATE TRIGGER trg_adaptive_exit_owner_event_insert "
                "BEFORE INSERT ON adaptive_risk_reservation_events "
                "FOR EACH ROW EXECUTE FUNCTION "
                "chili_guard_adaptive_exit_owner_event_insert()"
            ))
            with pytest.raises(
                RuntimeError,
                match="constraints missing/misbound",
            ):
                migrations._install_and_verify_migration_354_contract(
                    conn,
                    allow_registered_repair=False,
                )
            assert conn.execute(text(
                "SELECT count(*) FROM pg_constraint WHERE conname = "
                "'ck_alpaca_post_settlement_fill_values'"
            )).scalar_one() == 0
        finally:
            transaction.rollback()

    # A wrong physical type fails before the installer can add or replace any
    # other member of the contract.
    with engine.connect() as conn:
        transaction = conn.begin()
        try:
            conn.execute(text(
                "ALTER TABLE alpaca_paper_post_settlement_fill_contradictions "
                "DROP COLUMN projected_net_position_quantity_shares CASCADE"
            ))
            conn.execute(text(
                "ALTER TABLE alpaca_paper_post_settlement_fill_contradictions "
                "ADD COLUMN projected_net_position_quantity_shares TEXT NULL"
            ))
            before_columns = migrations._columns(
                conn,
                "alpaca_paper_post_settlement_fill_contradictions",
            )
            with pytest.raises(
                RuntimeError,
                match="partial column contract differs",
            ):
                migrations._install_and_verify_migration_354_contract(
                    conn,
                    allow_registered_repair=False,
                )
            assert migrations._columns(
                conn,
                "alpaca_paper_post_settlement_fill_contradictions",
            ) == before_columns
        finally:
            transaction.rollback()

    # A compatible partial schema is still untrusted once any retained new
    # field is populated; no missing column may be added before adjudication.
    with engine.connect() as conn:
        transaction = conn.begin()
        try:
            conn.execute(text(
                "DROP TRIGGER trg_adaptive_exit_owner_event_insert "
                "ON adaptive_risk_reservation_events"
            ))
            conn.execute(text(
                "ALTER TABLE alpaca_paper_post_settlement_fill_contradictions "
                "DROP COLUMN projected_net_position_quantity_shares CASCADE"
            ))
            conn.execute(text(
                "ALTER TABLE adaptive_risk_reservations "
                "DROP CONSTRAINT ck_adaptive_risk_reservation_quantities, "
                "DROP CONSTRAINT ck_adaptive_risk_reservation_contradiction"
            ))
            conn.execute(text(
                "ALTER TABLE adaptive_risk_reservations DISABLE TRIGGER USER"
            ))
            conn.execute(
                text(
                    "UPDATE adaptive_risk_reservations SET "
                    "post_settlement_net_position_quantity_shares = 1 "
                    "WHERE reservation_id = :reservation_id"
                ),
                {"reservation_id": reservation_id},
            )
            with pytest.raises(
                RuntimeError,
                match="non-null pre-contract state",
            ):
                migrations._install_and_verify_migration_354_contract(
                    conn,
                    allow_registered_repair=False,
                )
            assert "projected_net_position_quantity_shares" not in (
                migrations._columns(
                    conn,
                    "alpaca_paper_post_settlement_fill_contradictions",
                )
            )
        finally:
            transaction.rollback()

    # A reserved-prefix event is likewise quarantined before any partial DDL.
    with engine.connect() as conn:
        transaction = conn.begin()
        try:
            conn.execute(text(
                "DROP TRIGGER trg_adaptive_exit_owner_event_insert "
                "ON adaptive_risk_reservation_events"
            ))
            conn.execute(text(
                "ALTER TABLE alpaca_paper_post_settlement_fill_contradictions "
                "DROP COLUMN projected_net_position_quantity_shares CASCADE"
            ))
            conn.execute(
                text(
                    "INSERT INTO adaptive_risk_reservation_events "
                    "(reservation_id, sequence, event_type, "
                    " previous_event_sha256, event_sha256, broker_event_id, "
                    " payload_json, effective_at) VALUES "
                    "(:reservation_id, 1, 'alpaca_exit_owner_spoofed', "
                    " NULL, :event_sha256, NULL, '{}'::jsonb, "
                    " clock_timestamp())"
                ),
                {
                    "reservation_id": reservation_id,
                    "event_sha256": _hash("spoofed-owner-namespace"),
                },
            )
            with pytest.raises(
                RuntimeError,
                match="reserved owner-event namespace is not empty",
            ):
                migrations._install_and_verify_migration_354_contract(
                    conn,
                    allow_registered_repair=False,
                )
            assert "projected_net_position_quantity_shares" not in (
                migrations._columns(
                    conn,
                    "alpaca_paper_post_settlement_fill_contradictions",
                )
            )
        finally:
            transaction.rollback()

    # Exact nullable/NULL partial additions are the sole completion case.
    with engine.connect() as conn:
        transaction = conn.begin()
        try:
            conn.execute(text(
                "DROP TRIGGER trg_adaptive_exit_owner_event_insert "
                "ON adaptive_risk_reservation_events"
            ))
            conn.execute(text(
                "ALTER TABLE alpaca_paper_post_settlement_fill_contradictions "
                "DROP COLUMN provider_client_order_id CASCADE, "
                "DROP COLUMN projected_net_position_quantity_shares CASCADE"
            ))
            migrations._install_and_verify_migration_354_contract(
                conn,
                allow_registered_repair=False,
            )
            migrations._verify_migration_354_physical_contract(conn)
            assert set(
                migrations._MIGRATION_354_COLUMN_CONTRACT[
                    "alpaca_paper_post_settlement_fill_contradictions"
                ]
            ) <= migrations._columns(
                conn,
                "alpaca_paper_post_settlement_fill_contradictions",
            )
        finally:
            transaction.rollback()


def test_migration_354_survives_legacy_repair_reapply(
    db,
    monkeypatch,
) -> None:
    frozen = _prepare_settled_partial_cycle_with_owner(db, monkeypatch)
    late_sell = _verified_exit_batch(
        monkeypatch,
        frozen["owner_receipt"],
        quantities=(2, 2),
        order_quantity=4,
    )
    with db.begin():
        publication = publish_prepared_alpaca_paper_post_settlement_fill_batch(
            db,
            late_sell,
        )
        db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        retained_hashes = tuple(publication.contradiction_sha256s)
        assert publication.reservation_state.post_settlement_net_position_quantity_shares == -2

    repair_functions = (
        migrations._migration_323_adaptive_db_paper_atomic_lifecycle,
        migrations._migration_335_adaptive_late_fill_exposure_quarantine,
        migrations._migration_343_alpaca_post_settlement_fill_contradictions,
    )
    with engine.connect() as conn:
        migrations._verify_migration_354_physical_contract(conn)
        expected_fingerprint = _migration_354_physical_fingerprint(conn)
        expected_data_fingerprint = _migration_354_retained_data_fingerprint(
            conn,
            frozen["cycle"].reservation_id,
        )
        for repair in repair_functions:
            repair(conn)
            migrations._verify_migration_354_physical_contract(conn)
            assert (
                _migration_354_physical_fingerprint(conn)
                == expected_fingerprint
            ), repair.__name__
            assert _migration_354_retained_data_fingerprint(
                conn,
                frozen["cycle"].reservation_id,
            ) == expected_data_fingerprint
            actual_hashes = tuple(
                conn.execute(
                    text(
                        "SELECT contradiction_sha256 FROM "
                        "alpaca_paper_post_settlement_fill_contradictions "
                        "WHERE reservation_id = :reservation_id "
                        "ORDER BY contradiction_sequence"
                    ),
                    {"reservation_id": frozen["cycle"].reservation_id},
                ).scalars()
            )
            assert actual_hashes == retained_hashes

    # The signed-zero projection is still v2 authority.  A legacy repair may
    # not coerce it back to the old unsigned/open-quantity interpretation.
    late_buy = _verified_entry_batch(
        monkeypatch,
        quantities=(4, 2),
        order_quantity=10,
    )
    with db.begin():
        zeroed = publish_prepared_alpaca_paper_entry_fill_batch(db, late_buy)
        db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        assert zeroed.reservation_state.post_settlement_net_position_quantity_shares == 0
        assert zeroed.reservation_state.open_quantity_shares == 0
    with engine.connect() as conn:
        expected_zero_fingerprint = _migration_354_retained_data_fingerprint(
            conn,
            frozen["cycle"].reservation_id,
        )
        migrations._migration_343_alpaca_post_settlement_fill_contradictions(conn)
        migrations._verify_migration_354_physical_contract(conn)
        assert _migration_354_retained_data_fingerprint(
            conn,
            frozen["cycle"].reservation_id,
        ) == expected_zero_fingerprint
        assert conn.execute(
            text(
                "SELECT post_settlement_net_position_quantity_shares "
                "FROM adaptive_risk_reservations "
                "WHERE reservation_id = :reservation_id"
            ),
            {"reservation_id": frozen["cycle"].reservation_id},
        ).scalar_one() == 0

    # A registered/current database may be repaired; a registered database
    # with a missing column must hard-fail rather than infer a partial install.
    with engine.connect() as conn:
        transaction = conn.begin()
        try:
            conn.execute(text(
                "ALTER TABLE alpaca_paper_post_settlement_fill_contradictions "
                "DROP COLUMN projected_net_position_quantity_shares CASCADE"
            ))
            with pytest.raises(
                RuntimeError,
                match="registered migration 354 column contract is incomplete",
            ):
                migrations._reassert_354_if_present(conn)
        finally:
            transaction.rollback()


def test_entry_fill_publication_is_atomic_and_replay_idempotent(
    db, monkeypatch
) -> None:
    batch = _two_fill_verified_batch(monkeypatch)
    with db.begin():
        reservation, _packet = _persist_cycle_rows(db, batch.cycle)
        _reset_for_entry_fill(reservation)
        db.flush()

        first = publish_prepared_alpaca_paper_entry_fill_batch(db, batch)
        assert first.capture.created_count == 2
        assert first.handoff_proof is not None
        assert (
            verify_alpaca_paper_entry_fill_handoff(
                db,
                first.handoff_proof,
            )
            == first.handoff_proof
        )
        assert first.reservation_state.state == "filled"
        assert first.reservation_state.cumulative_filled_quantity_shares == 10
        assert first.reservation_state.open_quantity_shares == 10
        assert first.reservation_state.pending_structural_risk_usd == 0
        assert db.scalar(select(func.count(AlpacaPaperFillActivity.id))) == 2
        assert (
            db.scalar(
                select(func.count(AdaptiveRiskReservationEvent.id)).where(
                    AdaptiveRiskReservationEvent.event_type
                    == "cumulative_fill_advanced"
                )
            )
            == 1
        )

        replayed = publish_prepared_alpaca_paper_entry_fill_batch(db, batch)
        assert replayed.capture.created_count == 0
        assert replayed.reservation_state == first.reservation_state
        assert replayed.handoff_proof == first.handoff_proof
        assert (
            db.scalar(
                select(func.count(AdaptiveRiskReservationEvent.id)).where(
                    AdaptiveRiskReservationEvent.event_type
                    == "cumulative_fill_advanced"
                )
            )
            == 1
        )
        with pytest.raises(
            AlpacaFillActivityError,
            match="source binding",
        ):
            replace(
                first.handoff_proof,
                observation_sha256=_hash("wrong-observation"),
            )
        tampered = replace(
            first.handoff_proof,
            terminal_evidence_sha256=_hash("wrong-evidence"),
        )
        with pytest.raises(
            AlpacaFillActivityConflict,
            match="durable identity changed",
        ):
            verify_alpaca_paper_entry_fill_handoff(db, tampered)


def test_positive_fill_atomically_bootstraps_unbound_broker_identity(
    db,
    monkeypatch,
) -> None:
    batch = _two_fill_verified_batch(monkeypatch)
    with db.begin():
        reservation, _packet = _persist_cycle_rows(
            db,
            batch.cycle,
            unbound=True,
        )

        result = publish_prepared_alpaca_paper_entry_fill_batch(db, batch)

        assert result.handoff_proof is not None
        assert result.reservation_state.state == "filled"
        assert result.reservation_state.broker_source == "alpaca"
        assert (
            result.reservation_state.broker_connection_generation
            == batch.cycle.broker_connection_generation
        )
        assert (
            result.reservation_state.broker_order_id
            == batch.provider_order_id
        )
        assert result.reservation_state.cumulative_filled_quantity_shares == 10
        assert result.reservation_state.event_sequence == 1
        assert (
            verify_alpaca_paper_entry_fill_handoff(
                db,
                result.handoff_proof,
            )
            == result.handoff_proof
        )


def test_empty_fill_observation_never_binds_unbound_reservation(
    db,
    monkeypatch,
) -> None:
    raw = _verified_raw_batch()
    raw["activities"] = []
    raw["provider_order"]["status"] = "new"
    raw["provider_order"]["filled_qty"] = "0.0000000000"
    adapter, _calls = _verified_adapter(raw, monkeypatch)
    batch = read_verified_alpaca_paper_fill_batch(
        adapter,
        cycle=_verified_cycle(),
        provider_order_id="alpaca-entry-order-7",
        expected_client_order_id="chili-cycle-entry-7",
    )
    assert batch.activities == ()

    with db.begin():
        reservation, _packet = _persist_cycle_rows(
            db,
            batch.cycle,
            unbound=True,
        )

        result = publish_prepared_alpaca_paper_entry_fill_batch(db, batch)

        assert result.capture is None
        assert result.handoff_proof is None
        assert result.reservation_state.state == "reserved"
        assert result.reservation_state.broker_order_id is None
        assert result.reservation_state.event_sequence == 0
        assert db.scalar(select(func.count(AlpacaPaperFillActivity.id))) == 0
        assert (
            db.scalar(
                select(
                    func.count(
                        AlpacaPaperFillQueryObservation.observation_sha256
                    )
                )
            )
            == 0
        )


def test_later_query_metadata_deduplicates_same_immutable_execution(
    db, monkeypatch
) -> None:
    raw = _verified_raw_batch()
    adapter, _calls = _verified_adapter(raw, monkeypatch)
    first_batch = read_verified_alpaca_paper_fill_batch(
        adapter,
        cycle=_verified_cycle(),
        provider_order_id="alpaca-entry-order-7",
        expected_client_order_id="chili-cycle-entry-7",
    )
    raw["provider_order"]["status"] = "canceled"
    adapter, _calls = _verified_adapter(raw, monkeypatch)
    later_batch = read_verified_alpaca_paper_fill_batch(
        adapter,
        cycle=_verified_cycle(),
        provider_order_id="alpaca-entry-order-7",
        expected_client_order_id="chili-cycle-entry-7",
    )
    assert (
        first_batch.activities[0].immutable_fill_identity_sha256
        == later_batch.activities[0].immutable_fill_identity_sha256
    )
    assert (
        first_batch.activities[0].record_content_sha256
        != later_batch.activities[0].record_content_sha256
    )

    with db.begin():
        _persist_cycle_rows(db, first_batch.cycle)
        first = append_prepared_alpaca_paper_fill_batch(db, first_batch)
        later = append_prepared_alpaca_paper_fill_batch(db, later_batch)
        assert first.created_count == 1
        assert later.created_count == 0
        assert first.event_sha256s == later.event_sha256s
        assert db.scalar(select(func.count(AlpacaPaperFillActivity.id))) == 1
        assert (
            db.scalar(
                select(
                    func.count(
                        AlpacaPaperFillQueryObservation.observation_sha256
                    )
                )
            )
            == 2
        )
        assert (
            db.scalar(
                select(
                    func.count(
                        AlpacaPaperFillObservationActivity.observation_sha256
                    )
                )
            )
            == 2
        )


def test_fill_publication_replays_older_exact_observation_after_newer_poll(
    db, monkeypatch
) -> None:
    raw = _verified_raw_batch()
    adapter, _calls = _verified_adapter(raw, monkeypatch)
    first_batch = read_verified_alpaca_paper_fill_batch(
        adapter,
        cycle=_verified_cycle(),
        provider_order_id="alpaca-entry-order-7",
        expected_client_order_id="chili-cycle-entry-7",
    )
    raw["provider_order"]["status"] = "canceled"
    adapter, _calls = _verified_adapter(raw, monkeypatch)
    later_batch = read_verified_alpaca_paper_fill_batch(
        adapter,
        cycle=_verified_cycle(),
        provider_order_id="alpaca-entry-order-7",
        expected_client_order_id="chili-cycle-entry-7",
    )

    with db.begin():
        reservation, _packet = _persist_cycle_rows(db, first_batch.cycle)
        _reset_for_entry_fill(reservation)
        db.flush()
        first = publish_prepared_alpaca_paper_entry_fill_batch(db, first_batch)
        later = publish_prepared_alpaca_paper_entry_fill_batch(db, later_batch)
        replayed = publish_prepared_alpaca_paper_entry_fill_batch(db, first_batch)
        assert first.reservation_state.state == "filled"
        assert later.reservation_state == first.reservation_state
        assert replayed.reservation_state == first.reservation_state
        assert (
            db.scalar(
                select(func.count(AdaptiveRiskReservationEvent.id)).where(
                    AdaptiveRiskReservationEvent.event_type
                    == "cumulative_fill_advanced"
                )
            )
            == 1
        )
        assert (
            db.scalar(
                select(func.count(AdaptiveRiskReservationEvent.id)).where(
                    AdaptiveRiskReservationEvent.event_type
                    == "fill_observation_no_advance"
                )
            )
            == 1
        )
        assert (
            db.scalar(
                select(
                    func.count(
                        AlpacaPaperFillQueryObservation.observation_sha256
                    )
                )
            )
            == 2
        )


def test_rest_projection_cannot_advance_alpaca_fill_watermark(
    db, monkeypatch
) -> None:
    batch = _two_fill_verified_batch(monkeypatch)
    with db.begin():
        reservation, _packet = _persist_cycle_rows(db, batch.cycle)
        _reset_for_entry_fill(reservation)
        db.flush()
        store = AdaptiveRiskReservationStore(engine)
        with pytest.raises(
            AdaptiveReservationStateConflict,
            match="lacks committed fill authority",
        ):
            store.apply_cumulative_fill(
                reservation.reservation_id,
                evidence=_rest_projection_fill_evidence(batch),
                session=db,
            )
        retained = db.get(AdaptiveRiskReservation, reservation.reservation_id)
        assert retained.state == "submitted"
        assert retained.cumulative_filled_quantity_shares == 0
        assert db.scalar(select(func.count(AdaptiveRiskReservationEvent.id))) == 0
        assert db.scalar(select(func.count(AlpacaPaperFillActivity.id))) == 0


def test_entry_fill_publication_rolls_back_append_when_watermark_fails(
    db, monkeypatch
) -> None:
    batch = _two_fill_verified_batch(monkeypatch)
    original = AdaptiveRiskReservationStore.apply_cumulative_fill

    def _fail_watermark(*_args, **_kwargs):
        raise AdaptiveReservationStateConflict("injected watermark failure")

    with db.begin():
        reservation, _packet = _persist_cycle_rows(
            db,
            batch.cycle,
            unbound=True,
        )
        monkeypatch.setattr(
            AdaptiveRiskReservationStore,
            "apply_cumulative_fill",
            _fail_watermark,
        )
        with pytest.raises(
            AdaptiveReservationStateConflict,
            match="injected watermark failure",
        ):
            publish_prepared_alpaca_paper_entry_fill_batch(db, batch)
        assert db.scalar(select(func.count(AlpacaPaperFillActivity.id))) == 0
        assert (
            db.scalar(
                select(
                    func.count(
                        AlpacaPaperFillQueryObservation.observation_sha256
                    )
                )
            )
            == 0
        )
        retained = db.get(AdaptiveRiskReservation, reservation.reservation_id)
        assert retained.state == "reserved"
        assert retained.cumulative_filled_quantity_shares == 0
        assert retained.broker_source is None
        assert retained.broker_connection_generation is None
        assert retained.broker_order_id is None
        assert retained.last_broker_observed_at is None
        assert retained.last_broker_available_at is None
        assert retained.last_source_event_content_sha256 is None

        monkeypatch.setattr(
            AdaptiveRiskReservationStore,
            "apply_cumulative_fill",
            original,
        )
        published = publish_prepared_alpaca_paper_entry_fill_batch(db, batch)
        assert published.reservation_state.state == "filled"
        assert published.capture.created_count == 2


def test_late_fill_after_release_is_durably_quarantined_and_idempotent(
    db, monkeypatch
) -> None:
    batch = _two_fill_verified_batch(monkeypatch)
    with db.begin():
        reservation, _packet = _persist_cycle_rows(db, batch.cycle)
        _reset_for_entry_fill(reservation, state="released")
        db.flush()

        first = publish_prepared_alpaca_paper_entry_fill_batch(db, batch)
        assert first.reservation_state.state == "exposure_quarantined"
        assert first.reservation_state.cumulative_filled_quantity_shares == 10
        assert first.reservation_state.open_quantity_shares == 10
        assert first.reservation_state.open_structural_risk_usd == Decimal("5")
        assert first.reservation_state.lifecycle_contradiction_source_state == "released"
        assert (
            db.scalar(
                select(func.count(AdaptiveRiskReservationEvent.id)).where(
                    AdaptiveRiskReservationEvent.event_type
                    == "late_cumulative_fill_quarantined"
                )
            )
            == 1
        )

        replayed = publish_prepared_alpaca_paper_entry_fill_batch(db, batch)
        assert replayed.capture.created_count == 0
        assert replayed.reservation_state == first.reservation_state
        assert (
            db.scalar(select(func.count(AdaptiveRiskReservationEvent.id)))
            == 1
        )


def test_post_settlement_late_sell_binds_oid_owner_and_projects_signed_exposure_without_rewriting_settlement(
    db,
    monkeypatch,
) -> None:
    frozen = _prepare_settled_partial_cycle_with_owner(db, monkeypatch)
    late_batch = _verified_exit_batch(
        monkeypatch,
        frozen["owner_receipt"],
        quantities=(2, 2),
        order_quantity=4,
    )

    with db.begin():
        published = publish_prepared_alpaca_paper_post_settlement_fill_batch(
            db,
            late_batch,
        )
        db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        assert published.handoff_proof is None
        assert len(published.contradiction_sha256s) == 1
        assert published.reservation_state.state == "exposure_quarantined"
        assert published.reservation_state.cumulative_filled_quantity_shares == 4
        assert (
            published.reservation_state.post_settlement_net_position_quantity_shares
            == -2
        )
        assert published.reservation_state.open_quantity_shares == 2
        assert published.reservation_state.open_structural_risk_usd == Decimal("1")
        row = db.get(
            AlpacaPaperPostSettlementFillContradiction,
            published.contradiction_sha256s[0],
        )
        assert row.side == "sell"
        assert row.order_role == "exit"
        assert row.exit_owner_receipt_sha256 == (
            frozen["owner_receipt"].receipt_sha256
        )
        assert row.provider_client_order_id == (
            frozen["owner_receipt"].provider_client_order_id
        )
        aliased_cid = copy(row)
        aliased_cid.provider_client_order_id = row.expected_client_order_id
        with pytest.raises(
            capture_module.AlpacaFillActivityCorruption,
            match="role authority changed",
        ):
            capture_module.verify_alpaca_paper_post_settlement_fill_contradiction_row(
                aliased_cid
            )
        assert row.prior_recorded_cumulative_quantity == Decimal("2")
        assert row.broker_observed_cumulative_quantity == Decimal("4")
        assert row.projection_prior_net_position_quantity_shares == 0
        assert row.projected_net_position_quantity_shares == -2

        settlement = db.get(
            capture_module.AlpacaPaperCycleSettlement,
            frozen["settlement_sha256"],
        )
        assert Decimal(settlement.net_realized_pnl_usd) == frozen["settled_pnl"]
        assert tuple(
            db.scalars(
                select(AlpacaPaperFillActivity.event_sha256)
                .where(
                    AlpacaPaperFillActivity.reservation_id
                    == frozen["cycle"].reservation_id
                )
                .order_by(AlpacaPaperFillActivity.sequence)
            )
        ) == frozen["settled_fill_hashes"]

        replayed = publish_prepared_alpaca_paper_post_settlement_fill_batch(
            db,
            late_batch,
        )
        assert replayed.contradiction_sha256s == ()
        assert replayed.reservation_state == published.reservation_state
        assert db.scalar(
            select(func.count()).select_from(
                AlpacaPaperPostSettlementFillContradiction
            )
        ) == 1


def test_post_settlement_late_buy_and_sell_return_signed_net_to_zero_with_exact_costs(
    db,
    monkeypatch,
) -> None:
    frozen = _prepare_settled_partial_cycle_with_owner(db, monkeypatch)
    late_sell = _verified_exit_batch(
        monkeypatch,
        frozen["owner_receipt"],
        quantities=(2, 2),
        order_quantity=4,
    )
    late_buy = _verified_entry_batch(
        monkeypatch,
        quantities=(4, 2),
        order_quantity=10,
    )

    with db.begin():
        sold = publish_prepared_alpaca_paper_post_settlement_fill_batch(
            db,
            late_sell,
        )
        db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        assert sold.reservation_state.post_settlement_net_position_quantity_shares == -2

    with db.begin():
        bought = publish_prepared_alpaca_paper_entry_fill_batch(db, late_buy)
        db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        assert bought.reservation_state.cumulative_filled_quantity_shares == 6
        assert bought.reservation_state.post_settlement_net_position_quantity_shares == 0
        assert bought.reservation_state.open_quantity_shares == 0
        assert bought.reservation_state.open_structural_risk_usd == 0
        assert bought.reservation_state.open_gross_notional_usd == 0
        assert bought.reservation_state.open_buying_power_impact_usd == 0
        rows = list(
            db.scalars(
                select(AlpacaPaperPostSettlementFillContradiction)
                .order_by(
                    AlpacaPaperPostSettlementFillContradiction.contradiction_sequence
                )
            )
        )
        assert [row.side for row in rows] == ["sell", "buy"]
        assert [Decimal(row.fee_usd) for row in rows] == [Decimal("0"), Decimal("0")]
        assert rows[1].previous_contradiction_sha256 == rows[0].contradiction_sha256
        assert rows[1].projection_prior_net_position_quantity_shares == -2
        assert rows[1].projected_net_position_quantity_shares == 0
        assert bought.handoff_proof is None
        replayed = publish_prepared_alpaca_paper_entry_fill_batch(db, late_buy)
        assert replayed.contradiction_sha256s == ()
        assert replayed.handoff_proof is None
        settlement = db.get(
            capture_module.AlpacaPaperCycleSettlement,
            frozen["settlement_sha256"],
        )
        assert Decimal(settlement.net_realized_pnl_usd) == frozen["settled_pnl"]


def test_post_settlement_replacement_owners_use_order_local_cumulative_and_available_lineage(
    db,
    monkeypatch,
) -> None:
    frozen = _prepare_settled_partial_cycle_with_owner(
        db,
        monkeypatch,
        include_replacement_owner=True,
    )
    first_batch = _verified_exit_batch(
        monkeypatch,
        frozen["owner_receipt"],
        quantities=(1, 2),
        order_quantity=4,
    )
    with db.begin():
        first = publish_prepared_alpaca_paper_post_settlement_fill_batch(
            db,
            first_batch,
        )
        db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        assert first.reservation_state.post_settlement_net_position_quantity_shares == -2

    replacement_receipt = frozen["replacement_receipt"]
    assert replacement_receipt is not None
    replacement_batch = _verified_exit_batch(
        monkeypatch,
        replacement_receipt,
        quantities=(1,),
        order_quantity=3,
        first_activity_number=11,
        transaction_second_start=0,
    )

    with db.begin():
        second = publish_prepared_alpaca_paper_post_settlement_fill_batch(
            db,
            replacement_batch,
        )
        db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        assert second.reservation_state.post_settlement_net_position_quantity_shares == -3
        rows = list(
            db.scalars(
                select(AlpacaPaperPostSettlementFillContradiction)
                .order_by(
                    AlpacaPaperPostSettlementFillContradiction.contradiction_sequence
                )
            )
        )
        assert len(rows) == 2
        assert rows[0].prior_recorded_cumulative_quantity == Decimal("1")
        assert rows[0].broker_observed_cumulative_quantity == Decimal("3")
        assert rows[1].provider_order_id == replacement_receipt.provider_order_id
        assert rows[1].prior_recorded_cumulative_quantity == Decimal("0")
        assert rows[1].broker_observed_cumulative_quantity == Decimal("1")
        assert rows[1].provider_transaction_at < rows[0].provider_transaction_at
        assert rows[1].provider_available_at >= rows[0].provider_available_at
        assert rows[1].previous_contradiction_sha256 == rows[0].contradiction_sha256


def test_post_settlement_sell_batch_is_all_or_none_idempotent_and_owner_fail_closed(
    db,
    monkeypatch,
) -> None:
    frozen = _prepare_settled_partial_cycle_with_owner(db, monkeypatch)
    batch = _verified_exit_batch(
        monkeypatch,
        frozen["owner_receipt"],
        quantities=(2, 1, 1),
        order_quantity=4,
    )
    original_apply = AdaptiveRiskReservationStore.apply_post_settlement_fill_contradiction

    def fail_projection(*_args, **_kwargs):
        raise AdaptiveReservationStateConflict("injected signed projection failure")

    with db.begin():
        monkeypatch.setattr(
            AdaptiveRiskReservationStore,
            "apply_post_settlement_fill_contradiction",
            fail_projection,
        )
        with pytest.raises(
            AdaptiveReservationStateConflict,
            match="injected signed projection failure",
        ):
            publish_prepared_alpaca_paper_post_settlement_fill_batch(db, batch)
        assert db.scalar(
            select(func.count()).select_from(
                AlpacaPaperPostSettlementFillContradiction
            )
        ) == 0

        monkeypatch.setattr(
            AdaptiveRiskReservationStore,
            "apply_post_settlement_fill_contradiction",
            original_apply,
        )
        published = publish_prepared_alpaca_paper_post_settlement_fill_batch(
            db,
            batch,
        )
        db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        assert len(published.contradiction_sha256s) == 2
        replayed = publish_prepared_alpaca_paper_post_settlement_fill_batch(
            db,
            batch,
        )
        assert replayed.contradiction_sha256s == ()
        assert db.scalar(
            select(func.count()).select_from(
                AlpacaPaperPostSettlementFillContradiction
            )
        ) == 2

    original_load = AdaptiveRiskReservationStore.load_exit_owner_receipt

    def fail_owner(*_args, **_kwargs):
        raise AdaptiveReservationStateConflict("injected owner receipt failure")

    with db.begin():
        reservation = db.scalar(
            select(AdaptiveRiskReservation)
            .where(
                AdaptiveRiskReservation.reservation_id
                == batch.cycle.reservation_id
            )
            .with_for_update()
        )
        assert reservation is not None
        packet = db.get(
            AdaptiveRiskDecisionPacket,
            reservation.decision_packet_sha256,
        )
        settlement = db.scalar(
            select(capture_module.AlpacaPaperCycleSettlement)
            .where(
                capture_module.AlpacaPaperCycleSettlement.reservation_id
                == reservation.reservation_id
            )
            .with_for_update()
        )
        assert packet is not None and settlement is not None
        monkeypatch.setattr(
            AdaptiveRiskReservationStore,
            "load_exit_owner_receipt",
            fail_owner,
        )
        with pytest.raises(
            AlpacaFillActivityConflict,
            match="lacks its prelocked owner receipt",
        ):
            capture_module._publish_post_settlement_fill_contradictions(
                db,
                batch,
                reservation=reservation,
                packet=packet,
                settlement=settlement,
                exit_owner_receipt=None,
            )
        with pytest.raises(
            AdaptiveReservationStateConflict,
            match="injected owner receipt failure",
        ):
            publish_prepared_alpaca_paper_post_settlement_fill_batch(db, batch)
        assert db.scalar(
            select(func.count()).select_from(
                AlpacaPaperPostSettlementFillContradiction
            )
        ) == 2
        monkeypatch.setattr(
            AdaptiveRiskReservationStore,
            "load_exit_owner_receipt",
            original_load,
        )


def test_prepared_batch_appends_missing_suffix_and_retries_idempotently(
    db, monkeypatch
) -> None:
    batch = _two_fill_verified_batch(monkeypatch)
    with db.begin():
        _persist_cycle_rows(db, batch.cycle)
        prefix = append_alpaca_paper_fill_activity(db, batch.activities[0])
        assert prefix.created is True

        first = append_prepared_alpaca_paper_fill_batch(db, batch)
        assert first.observed_count == 2
        assert first.created_count == 1
        assert first.event_sha256s[0] == prefix.row.event_sha256
        assert db.scalar(select(func.count(AlpacaPaperFillActivity.id))) == 2
        assert (
            db.scalar(
                select(
                    func.count(AlpacaPaperFillQueryObservation.observation_sha256)
                )
            )
            == 1
        )
        assert (
            db.scalar(
                select(func.count(AlpacaPaperFillPageObject.page_object_sha256))
            )
            == 1
        )
        assert (
            db.scalar(
                select(
                    func.count(AlpacaPaperFillObservationPage.observation_sha256)
                )
            )
            == 1
        )
        assert (
            db.scalar(
                select(
                    func.count(
                        AlpacaPaperFillObservationActivity.observation_sha256
                    )
                )
            )
            == 2
        )

        retried = append_prepared_alpaca_paper_fill_batch(db, batch)
        assert retried.observed_count == 2
        assert retried.created_count == 0
        assert retried.event_sha256s == first.event_sha256s
        assert db.scalar(select(func.count(AlpacaPaperFillActivity.id))) == 2


def test_prepared_batch_publication_is_all_or_none_savepoint(
    db, monkeypatch
) -> None:
    batch = _two_fill_verified_batch(monkeypatch)
    original = capture_module._append_alpaca_paper_fill_activity_under_locked_cycle
    calls = 0

    def _fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise AlpacaFillActivityError("injected second activity failure")
        return original(*args, **kwargs)

    with db.begin():
        _persist_cycle_rows(db, batch.cycle)
        monkeypatch.setattr(
            capture_module,
            "_append_alpaca_paper_fill_activity_under_locked_cycle",
            _fail_second,
        )
        with pytest.raises(
            AlpacaFillActivityError, match="injected second activity failure"
        ):
            append_prepared_alpaca_paper_fill_batch(db, batch)
        assert db.scalar(select(func.count(AlpacaPaperFillActivity.id))) == 0
        assert (
            db.scalar(
                select(
                    func.count(AlpacaPaperFillQueryObservation.observation_sha256)
                )
            )
            == 0
        )
        assert (
            db.scalar(
                select(func.count(AlpacaPaperFillPageObject.page_object_sha256))
            )
            == 0
        )
        assert (
            db.scalar(
                select(
                    func.count(AlpacaPaperFillObservationPage.observation_sha256)
                )
            )
            == 0
        )
        assert (
            db.scalar(
                select(
                    func.count(
                        AlpacaPaperFillObservationActivity.observation_sha256
                    )
                )
            )
            == 0
        )

        monkeypatch.setattr(
            capture_module,
            "_append_alpaca_paper_fill_activity_under_locked_cycle",
            original,
        )
        published = append_prepared_alpaca_paper_fill_batch(db, batch)
        assert published.observed_count == 2
        assert published.created_count == 2
        assert db.scalar(select(func.count(AlpacaPaperFillActivity.id))) == 2
        assert (
            db.scalar(
                select(
                    func.count(AlpacaPaperFillQueryObservation.observation_sha256)
                )
            )
            == 1
        )
        assert (
            db.scalar(
                select(
                    func.count(
                        AlpacaPaperFillObservationActivity.observation_sha256
                    )
                )
            )
            == 2
        )
