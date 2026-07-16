from __future__ import annotations

import ast
from datetime import UTC, date, datetime
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import uuid

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError

from app import migrations
from app.db import Base, engine
from app.models.trading import (
    AdaptiveRiskDecisionPacket,
    AdaptiveRiskReservation,
    AlpacaPaperAccountSettlementHead,
    AlpacaPaperCycleSettlement,
)
from app.services.trading.momentum_neural import alpaca_cycle_settlement as module
from app.services.trading.momentum_neural.alpaca_cycle_settlement import (
    AlpacaCycleSettlementIntegrityError,
    cycle_settlement_content_payload,
    new_zero_settlement_head,
    verify_cycle_settlement_content,
    verify_settlement_head_content,
)
from app.services.trading.momentum_neural.adaptive_risk_reservation import (
    AdaptiveRiskPendingSettlement,
    AdaptiveRiskReservationStore,
    ImmutableAccountRiskSnapshot,
)
from app.services.trading.momentum_neural.alpaca_fill_activity import (
    query_pending_alpaca_paper_cycle_settlements,
)


def _hash(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


class _RecordingConnection:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self.commits = 0

    def execute(self, statement):
        self.statements.append(str(statement))
        return None

    def commit(self) -> None:
        self.commits += 1


def test_migrations_follow_fill_capture_and_install_atomic_fail_closed_guards(
    monkeypatch,
) -> None:
    tables = {
        "adaptive_risk_reservations",
        "adaptive_risk_decision_packets",
        "alpaca_paper_fill_activities",
    }
    monkeypatch.setattr(migrations, "_tables", lambda _conn: tables)
    conn = _RecordingConnection()
    migrations._migration_329_alpaca_paper_cycle_settlement_foundation(conn)
    sql = "\n".join(conn.statements).lower()
    assert "flat_pending_settlement" in sql
    assert "fee_usd numeric(28, 10) not null" in sql
    assert "cycle settlement fill chain is incomplete or non-authoritative" in sql
    assert "cycle settlement/head/reservation must commit atomically" in sql
    assert "deferrable initially deferred" in sql
    assert "before update or delete on alpaca_paper_cycle_settlements" in sql
    assert "before truncate on alpaca_paper_cycle_settlements" in sql
    assert "before delete on alpaca_paper_account_settlement_heads" in sql
    assert "before truncate on alpaca_paper_account_settlement_heads" in sql
    assert conn.commits == 1

    ids = [migration_id for migration_id, _fn in migrations.MIGRATIONS]
    assert ids.index("329_alpaca_paper_cycle_settlement_foundation") == (
        ids.index("328_alpaca_paper_fill_capture_authority_repair") + 1
    )
    assert ids.index("330_alpaca_paper_cycle_settlement_repair") == (
        ids.index("329_alpaca_paper_cycle_settlement_foundation") + 1
    )
    assert ids.index(
        "331_alpaca_paper_cycle_settlement_redteam_hardening"
    ) == ids.index("330_alpaca_paper_cycle_settlement_repair") + 1
    assert ids.index(
        "332_alpaca_paper_fill_boundary_drift_hardening"
    ) == ids.index("331_alpaca_paper_cycle_settlement_redteam_hardening") + 1


def test_redteam_hardening_rejects_retained_self_attestation_and_reinstalls_guards(
    monkeypatch,
) -> None:
    complete = {
        "adaptive_risk_reservations",
        "adaptive_risk_decision_packets",
        "alpaca_paper_fill_activities",
        "alpaca_paper_account_settlement_heads",
        "alpaca_paper_cycle_settlements",
    }
    monkeypatch.setattr(migrations, "_tables", lambda _conn: complete)
    conn = _RecordingConnection()
    migrations._migration_331_alpaca_paper_cycle_settlement_redteam_hardening(
        conn
    )
    sql = "\n".join(conn.statements).lower()
    assert "requires sealed offline" in sql
    assert "verification; online migration will not bless it" in sql
    assert "fill chain is gapped or has a wrong predecessor" in sql
    assert "before insert or update on adaptive_risk_reservations" in sql
    assert "before insert on alpaca_paper_fill_activities" in sql
    assert "new fill cannot append after alpaca cycle became flat" in sql
    assert "update of state" not in sql
    assert conn.commits == 1


def test_fill_boundary_drift_hardening_locks_audit_and_restores_exact_v1(
    monkeypatch,
) -> None:
    complete = {
        "adaptive_risk_reservations",
        "adaptive_risk_decision_packets",
        "alpaca_paper_fill_activities",
        "alpaca_paper_account_settlement_heads",
        "alpaca_paper_cycle_settlements",
    }
    monkeypatch.setattr(migrations, "_tables", lambda _conn: complete)
    conn = _RecordingConnection()

    migrations._migration_332_alpaca_paper_fill_boundary_drift_hardening(
        conn
    )

    sql = "\n".join(conn.statements).lower()
    assert "lock table" in sql
    assert "adaptive_risk_reservations" in sql
    assert "in share row exclusive mode" in sql
    assert "capture_authority_status = 'unverified'" in sql
    assert "from pg_attribute attribute_row" in sql
    assert "attribute_row.attnotnull" in sql
    assert "required not null columns drifted" in sql
    assert "fill-boundary migration will not bless it" in sql
    assert "drop constraint if exists ck_alpaca_paper_fill_capture_authority" in sql
    assert "capture_authority_status = 'unverified'" in sql
    assert "before update or delete on alpaca_paper_fill_activities" in sql
    assert "before truncate on alpaca_paper_fill_activities" in sql
    assert "conrelid = 'alpaca_paper_fill_activities'::regclass" in sql
    assert "trigger_row.tgrelid" in sql
    assert "function_row.proname = expected.function_name" in sql
    assert "trigger_row.tgenabled = 'o'" in sql
    assert "trigger_row.tgtype = expected.trigger_type" in sql
    assert "is not true" in sql
    assert "fill chain is gapped or has a wrong predecessor" in sql
    assert conn.commits == 1


def test_repair_refuses_missing_schema_and_never_rewrites_retained_facts(
    monkeypatch,
) -> None:
    monkeypatch.setattr(migrations, "_tables", lambda _conn: set())
    with pytest.raises(RuntimeError, match="foundation is incomplete"):
        migrations._migration_330_alpaca_paper_cycle_settlement_repair(
            _RecordingConnection()
        )

    complete = {
        "adaptive_risk_reservations",
        "adaptive_risk_decision_packets",
        "alpaca_paper_fill_activities",
        "alpaca_paper_account_settlement_heads",
        "alpaca_paper_cycle_settlements",
    }
    monkeypatch.setattr(migrations, "_tables", lambda _conn: complete)
    conn = _RecordingConnection()
    migrations._migration_330_alpaca_paper_cycle_settlement_repair(conn)
    sql = "\n".join(conn.statements).lower()
    assert "append-only evidence will not be rewritten" in sql
    assert "alter column fee_usd set not null" in sql
    assert "update alpaca_paper_cycle_settlements set" not in sql
    assert "update alpaca_paper_account_settlement_heads set" not in sql
    assert conn.commits == 1


def test_models_register_exact_terminal_and_nullable_pending_fee_contract() -> None:
    reservation_checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in AdaptiveRiskReservation.__table__.constraints
        if constraint.name
    }
    assert "flat_pending_settlement" in reservation_checks[
        "ck_adaptive_risk_reservation_state"
    ]
    assert "closed_at IS NULL" in reservation_checks[
        "ck_adaptive_risk_reservation_settlement_state"
    ]

    settlement_constraints = {
        constraint.name
        for constraint in AlpacaPaperCycleSettlement.__table__.constraints
        if constraint.name
    }
    assert {
        "uq_alpaca_paper_cycle_settlement_reservation",
        "uq_alpaca_paper_cycle_settlement_sequence",
        "ck_alpaca_paper_cycle_settlement_authority",
        "ck_alpaca_paper_cycle_settlement_values",
        "ck_alpaca_paper_cycle_settlement_lineage",
    } <= settlement_constraints
    assert AlpacaPaperCycleSettlement.__table__.columns.fee_usd.nullable is False
    # Unknown fees are retained as NULL in source facts, never coerced into a
    # terminal settlement row.
    from app.models.trading import AlpacaPaperFillActivity

    assert AlpacaPaperFillActivity.__table__.columns.fee_usd.nullable is True
    assert AlpacaPaperAccountSettlementHead.__table__.columns[
        "last_settlement_sha256"
    ].nullable is True


def _settlement_row() -> AlpacaPaperCycleSettlement:
    observed = datetime(2026, 7, 15, 13, 5, tzinfo=UTC)
    row = AlpacaPaperCycleSettlement(
        settlement_sha256="0" * 64,
        settlement_schema_version=module.SETTLEMENT_SCHEMA_VERSION,
        settlement_authority_status="sealed_verified",
        reservation_id=uuid.UUID("00000000-0000-0000-0000-000000000329"),
        decision_packet_sha256=_hash("packet"),
        reservation_request_sha256=_hash("request"),
        account_scope="alpaca:paper",
        account_identity_sha256=_hash("account"),
        account_snapshot_sha256=_hash("snapshot"),
        broker_connection_generation="alpaca-paper-generation-329",
        execution_family="alpaca_spot",
        broker_environment="paper",
        position_direction="long",
        symbol="VEEE",
        trading_date=date(2026, 7, 15),
        setup_family="momentum_breakout",
        terminal_sequence=1,
        previous_account_settlement_sha256=None,
        source_fill_count=2,
        terminal_fill_sequence=2,
        terminal_fill_event_sha256=_hash("terminal-fill"),
        fill_chain_root_sha256=_hash("terminal-fill"),
        flat_evidence_sha256=_hash("flat-proof"),
        capture_authority_status="verified",
        capture_authority_receipt_sha256=_hash("capture-receipt"),
        provider_event_clock_status="authoritative",
        provider_client_order_id_status="authoritative",
        exit_order_ownership_status="authoritative",
        fee_status="authoritative",
        fee_evidence_root_sha256=_hash("fee-root"),
        entry_quantity=Decimal("10"),
        exit_quantity=Decimal("10"),
        entry_cost_usd=Decimal("25"),
        exit_proceeds_usd=Decimal("30"),
        gross_realized_pnl_usd=Decimal("5"),
        fee_usd=Decimal("0.02"),
        net_realized_pnl_usd=Decimal("4.98"),
        settlement_policy_sha256=_hash("settlement-policy"),
        effective_config_sha256=_hash("config"),
        code_build_sha256=_hash("build"),
        feature_flags_sha256=_hash("flags"),
        settlement_content_canonical_json="{}",
        settlement_content_sha256="0" * 64,
        closed_observed_at=observed,
        closed_available_at=observed,
    )
    payload = cycle_settlement_content_payload(row)
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    content_sha = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    settlement_sha = hashlib.sha256(
        (
            f"{module.SETTLEMENT_HASH_DOMAIN}|genesis|{content_sha}"
        ).encode("utf-8")
    ).hexdigest()
    row.settlement_content_canonical_json = canonical
    row.settlement_content_sha256 = content_sha
    row.settlement_sha256 = settlement_sha
    return row


def test_content_hashes_bind_every_terminal_value_but_do_not_mint_authority() -> None:
    head = new_zero_settlement_head(account_identity_sha256=_hash("account"))
    verify_settlement_head_content(head)
    head.cumulative_fee_usd = Decimal("0.01")
    with pytest.raises(AlpacaCycleSettlementIntegrityError, match="head SHA-256"):
        verify_settlement_head_content(head)

    settlement = _settlement_row()
    verify_cycle_settlement_content(settlement)
    settlement.net_realized_pnl_usd = Decimal("999")
    with pytest.raises(AlpacaCycleSettlementIntegrityError, match="canonical JSON"):
        verify_cycle_settlement_content(settlement)


def test_settlement_module_has_no_direct_network_or_activation_side_effect() -> None:
    source_path = Path(module.__file__).resolve()
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not any(
        value == banned or value.startswith(banned + ".")
        for value in imported
        for banned in ("alpaca", "requests", "httpx", "urllib", "socket")
    )
    # Production wiring now exists deliberately, but importing this module
    # still performs no broker read, runner activation, or transaction commit.
    assert "commit(" not in source_path.read_text(encoding="utf-8")


def _packet_and_filled_reservation(*, suffix: str):
    packet_sha = _hash(f"packet:{suffix}")
    request_sha = _hash(f"request:{suffix}")
    account_identity = _hash(f"account:{suffix}")
    packet = AdaptiveRiskDecisionPacket(
        decision_packet_sha256=packet_sha,
        reservation_request_sha256=request_sha,
        decision_id=f"decision:{suffix}",
        account_scope="alpaca:paper",
        symbol="VEEE",
        trading_date=date(2026, 7, 15),
        setup_family="momentum_breakout",
        correlation_cluster="equity:momentum",
        client_order_id=f"client:{suffix}",
        execution_surface="alpaca_paper",
        execution_family="alpaca_spot",
        broker_environment="paper",
        account_identity_sha256=account_identity,
        account_snapshot_sha256=_hash(f"snapshot:{suffix}"),
        account_snapshot_generation=f"snapshot-generation:{suffix}",
        policy_sha256=_hash(f"policy:{suffix}"),
        input_sha256=_hash(f"input:{suffix}"),
        economic_input_sha256=_hash(f"economic-input:{suffix}"),
        economic_resolution_sha256=_hash(f"economic-resolution:{suffix}"),
        effective_config_sha256=_hash(f"config:{suffix}"),
        code_build_sha256=_hash(f"build:{suffix}"),
        feature_flags_sha256=_hash(f"flags:{suffix}"),
        capture_prefix_root_sha256=_hash(f"capture:{suffix}"),
        evidence_sha256=_hash(f"evidence:{suffix}"),
        reservation_ledger_sha256=_hash(f"ledger:{suffix}"),
        resolved_quantity_shares=10,
        structural_stop=Decimal("2"),
        entry_limit_price=Decimal("2.50"),
        resolver_valid=True,
        admission_accepted=True,
        rejection_reasons_json=[],
        account_snapshot_json={"fixture": suffix},
        decision_packet_json={"fixture": suffix},
    )
    reservation = AdaptiveRiskReservation(
        reservation_id=uuid.uuid5(uuid.NAMESPACE_URL, suffix),
        decision_packet_sha256=packet_sha,
        opportunity_claim_id=None,
        account_scope="alpaca:paper",
        symbol="VEEE",
        trading_date=date(2026, 7, 15),
        setup_family="momentum_breakout",
        correlation_cluster="equity:momentum",
        state="filled",
        planned_quantity_shares=10,
        cumulative_filled_quantity_shares=10,
        open_quantity_shares=10,
        planned_structural_risk_usd=Decimal("5"),
        planned_gross_notional_usd=Decimal("25"),
        planned_buying_power_impact_usd=Decimal("25"),
        pending_structural_risk_usd=Decimal("0"),
        pending_gross_notional_usd=Decimal("0"),
        pending_buying_power_impact_usd=Decimal("0"),
        open_structural_risk_usd=Decimal("5"),
        open_gross_notional_usd=Decimal("25"),
        open_buying_power_impact_usd=Decimal("25"),
        broker_order_id=f"order:{suffix}",
        broker_source="alpaca",
        broker_connection_generation=f"broker-generation:{suffix}",
        last_broker_observed_at=datetime(2026, 7, 15, 13, tzinfo=UTC),
        last_broker_available_at=datetime(2026, 7, 15, 13, tzinfo=UTC),
        last_source_event_content_sha256=_hash(f"fill-proof:{suffix}"),
        event_sequence=1,
        last_event_sha256=_hash(f"reservation-event:{suffix}"),
        version=1,
    )
    return packet, reservation, account_identity


def test_zero_dimension_pending_settlement_blocks_account_admission(db) -> None:
    suffix = f"pending-settlement-{uuid.uuid4()}"
    packet, reservation, account_identity = _packet_and_filled_reservation(
        suffix=suffix
    )
    now = datetime.now(UTC)
    snapshot = ImmutableAccountRiskSnapshot(
        snapshot_id=f"snapshot:{suffix}",
        source="alpaca:account-v2",
        provider_generation=f"provider:{suffix}",
        account_scope="alpaca:paper",
        execution_family="alpaca_spot",
        broker_environment="paper",
        venue="alpaca",
        account_identity_sha256=account_identity,
        observed_at=now,
        available_at=now,
        equity_usd=100_000.0,
        buying_power_usd=400_000.0,
        broker_day_change_usd=0.0,
        local_realized_pnl_usd=0.0,
        pending_policy_buying_power_reflected_usd=0.0,
    )
    db.add_all((packet, reservation))
    db.flush()
    reservation.state = "flat_pending_settlement"
    reservation.open_quantity_shares = 0
    reservation.open_structural_risk_usd = Decimal("0")
    reservation.open_gross_notional_usd = Decimal("0")
    reservation.open_buying_power_impact_usd = Decimal("0")
    db.flush()

    store = AdaptiveRiskReservationStore(db.get_bind())
    with pytest.raises(AdaptiveRiskPendingSettlement) as caught:
        store.lock_admission_snapshot(
            account_scope="alpaca:paper",
            symbol="NEXT",
            correlation_cluster="equity:momentum",
            account_snapshot=snapshot,
            session=db,
        )

    exc = caught.value
    assert exc.reason == "adaptive_risk_pending_cycle_settlement"
    assert exc.provenance["pending_count"] == 1
    assert exc.provenance["ledger_sha256"] == (
        exc.locked_snapshot.ledger_sha256
    )
    pending = exc.locked_snapshot.ledger_payload["pending_settlements"]
    assert pending == [
        {
            "reservation_id": str(reservation.reservation_id),
            "decision_packet_sha256": packet.decision_packet_sha256,
            "symbol": "VEEE",
            "trading_date": "2026-07-15",
            "setup_family": "momentum_breakout",
            "state": "flat_pending_settlement",
            "cumulative_filled_quantity_shares": 10,
            "last_broker_available_at": (
                reservation.last_broker_available_at.isoformat().replace(
                    "+00:00", "Z"
                )
            ),
            "last_source_event_content_sha256": (
                reservation.last_source_event_content_sha256
            ),
            "version": 1,
        }
    ]
    coverage = query_pending_alpaca_paper_cycle_settlements(
        db,
        account_scope="alpaca:paper",
        account_identity_sha256=account_identity,
    )
    assert len(coverage) == 1
    assert coverage[0].reservation_id == reservation.reservation_id
    assert coverage[0].pending is True
    assert "entry_fill_missing" in coverage[0].pending_reasons


def test_postgres_schema_parity_and_terminal_transitions_fail_closed() -> None:
    # This test deliberately avoids the shared destructive db fixture.  It uses
    # one rollback-only transaction in the dedicated *_test database.
    Base.metadata.create_all(bind=engine)
    migrations.run_migrations(engine)
    # Re-run the idempotent repair body explicitly so this test also exercises
    # its empty-schema-safe ADD COLUMN path after schema_version is recorded.
    with engine.connect() as repair_conn:
        migrations._migration_330_alpaca_paper_cycle_settlement_repair(
            repair_conn
        )
    suffix = f"cycle-foundation-{uuid.uuid4()}"
    packet, reservation, account_identity = _packet_and_filled_reservation(
        suffix=suffix
    )
    head = new_zero_settlement_head(account_identity_sha256=account_identity)

    with engine.connect() as conn:
        transaction = conn.begin()
        try:
            state_constraint = conn.execute(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conrelid = "
                    "'adaptive_risk_reservations'::regclass "
                    "AND conname = 'ck_adaptive_risk_reservation_state'"
                )
            ).scalar_one()
            assert "exposure_quarantined" in state_constraint
            reservation_guard = conn.execute(
                text(
                    "SELECT pg_get_functiondef(oid) FROM pg_proc "
                    "WHERE proname = "
                    "'chili_guard_alpaca_reservation_settlement_state'"
                )
            ).scalar_one()
            assert "quarantined Alpaca exposure is fail-closed" in (
                reservation_guard
            )

            conn.execute(
                AdaptiveRiskDecisionPacket.__table__.insert(),
                {
                    column.name: getattr(packet, column.name)
                    for column in AdaptiveRiskDecisionPacket.__table__.columns
                    if column.name != "created_at"
                },
            )
            conn.execute(
                AdaptiveRiskReservation.__table__.insert(),
                {
                    column.name: getattr(reservation, column.name)
                    for column in AdaptiveRiskReservation.__table__.columns
                    if column.name not in {"created_at", "updated_at"}
                },
            )
            conn.execute(
                AlpacaPaperAccountSettlementHead.__table__.insert(),
                {
                    column.name: getattr(head, column.name)
                    for column in AlpacaPaperAccountSettlementHead.__table__.columns
                    if column.name not in {"created_at", "updated_at"}
                },
            )

            nested = conn.begin_nested()
            with pytest.raises(DBAPIError):
                conn.execute(
                    text(
                        "UPDATE adaptive_risk_reservations "
                        "SET state = 'closed', open_quantity_shares = 0, "
                        "open_structural_risk_usd = 0, "
                        "open_gross_notional_usd = 0, "
                        "open_buying_power_impact_usd = 0, closed_at = :at "
                        "WHERE reservation_id = :reservation_id"
                    ),
                    {
                        "at": datetime(2026, 7, 15, 13, 5, tzinfo=UTC),
                        "reservation_id": reservation.reservation_id,
                    },
                )
            nested.rollback()

            # OLD is exact Alpaca PAPER.  Changing the scope in the same
            # statement must not bypass the terminal transition guard.
            nested = conn.begin_nested()
            with pytest.raises(DBAPIError):
                conn.execute(
                    text(
                        "UPDATE adaptive_risk_reservations "
                        "SET account_scope = 'alpaca:paper:bypass', "
                        "state = 'closed', open_quantity_shares = 0, "
                        "open_structural_risk_usd = 0, "
                        "open_gross_notional_usd = 0, "
                        "open_buying_power_impact_usd = 0, closed_at = :at "
                        "WHERE reservation_id = :reservation_id"
                    ),
                    {
                        "at": datetime(2026, 7, 15, 13, 5, tzinfo=UTC),
                        "reservation_id": reservation.reservation_id,
                    },
                )
            nested.rollback()

            conn.execute(
                text(
                    "UPDATE adaptive_risk_reservations "
                    "SET state = 'flat_pending_settlement', "
                    "open_quantity_shares = 0, open_structural_risk_usd = 0, "
                    "open_gross_notional_usd = 0, "
                    "open_buying_power_impact_usd = 0 "
                    "WHERE reservation_id = :reservation_id"
                ),
                {"reservation_id": reservation.reservation_id},
            )
            state = conn.execute(
                text(
                    "SELECT state FROM adaptive_risk_reservations "
                    "WHERE reservation_id = :reservation_id"
                ),
                {"reservation_id": reservation.reservation_id},
            ).scalar_one()
            assert state == "flat_pending_settlement"

            nested = conn.begin_nested()
            with pytest.raises(DBAPIError):
                conn.execute(
                    text(
                        "UPDATE adaptive_risk_reservations SET symbol = 'PLSM' "
                        "WHERE reservation_id = :reservation_id"
                    ),
                    {"reservation_id": reservation.reservation_id},
                )
            nested.rollback()

            nested = conn.begin_nested()
            with pytest.raises(DBAPIError):
                conn.execute(
                    text(
                        "UPDATE adaptive_risk_reservations "
                        "SET state = 'closed', closed_at = :at, "
                        "last_broker_available_at = :at, "
                        "last_source_event_content_sha256 = :proof "
                        "WHERE reservation_id = :reservation_id"
                    ),
                    {
                        "at": datetime(2026, 7, 15, 13, 5, tzinfo=UTC),
                        "proof": _hash(f"flat:{suffix}"),
                        "reservation_id": reservation.reservation_id,
                    },
                )
            nested.rollback()

            nested = conn.begin_nested()
            with pytest.raises(DBAPIError):
                conn.execute(
                    text(
                        "UPDATE alpaca_paper_account_settlement_heads "
                        "SET settled_cycle_sequence = 1, version = 2, "
                        "last_settlement_sha256 = :sha, last_settled_at = :at, "
                        "updated_at = :at, head_content_sha256 = :head "
                        "WHERE account_scope = 'alpaca:paper' "
                        "AND account_identity_sha256 = :identity"
                    ),
                    {
                        "sha": _hash(f"invented-settlement:{suffix}"),
                        "head": _hash(f"invented-head:{suffix}"),
                        "at": datetime(2026, 7, 15, 13, 5, tzinfo=UTC),
                        "identity": account_identity,
                    },
                )
            nested.rollback()

            columns = {
                item["name"]: item
                for item in inspect(conn).get_columns(
                    "alpaca_paper_cycle_settlements"
                )
            }
            assert columns["fee_usd"]["nullable"] is False
            assert columns["flat_evidence_sha256"]["nullable"] is False
        finally:
            transaction.rollback()
