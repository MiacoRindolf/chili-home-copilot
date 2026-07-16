from __future__ import annotations

from sqlalchemy import text

from app import migrations
from app.db import Base, engine
from app.models.trading import (
    AdaptiveRiskDecisionPacket,
    AdaptiveRiskReservation,
    AlpacaPaperBuyingPowerReflectionItem,
    AlpacaPaperBuyingPowerReflectionReceipt,
)


class _RecordingConnection:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self.commits = 0

    def execute(self, statement):
        self.statements.append(str(statement))
        return None

    def commit(self) -> None:
        self.commits += 1


def test_migration_321_repairs_every_modeled_lifecycle_column(monkeypatch) -> None:
    monkeypatch.setattr(
        migrations,
        "_tables",
        lambda _conn: {
            "adaptive_risk_decision_packets",
            "adaptive_risk_reservations",
        },
    )
    conn = _RecordingConnection()

    migrations._migration_321_adaptive_risk_lifecycle_schema_repair(conn)

    sql = "\n".join(conn.statements).lower()
    lifecycle_columns = {
        "broker_source",
        "broker_connection_generation",
        "last_broker_observed_at",
        "last_broker_available_at",
        "last_source_event_content_sha256",
    }
    model_columns = set(AdaptiveRiskReservation.__table__.columns.keys())
    assert lifecycle_columns <= model_columns
    for column in lifecycle_columns:
        assert f"add column if not exists {column}" in sql
    assert "reservation_request_sha256" in set(
        AdaptiveRiskDecisionPacket.__table__.columns.keys()
    )
    assert "add column if not exists reservation_request_sha256" in sql
    assert "drop constraint if exists ck_adaptive_risk_reservation_lifecycle_binding" in sql
    assert "add constraint ck_adaptive_risk_reservation_lifecycle_binding" in sql
    assert conn.commits == 1


def test_request_hash_repairs_have_new_registry_ids_after_320() -> None:
    ids = [migration_id for migration_id, _fn in migrations.MIGRATIONS]
    lifecycle_index = ids.index("320_adaptive_risk_lifecycle_evidence")
    schema_repair_index = ids.index("321_adaptive_risk_lifecycle_schema_repair")
    request_hash_index = ids.index("322_adaptive_risk_request_hash_schema_repair")
    assert schema_repair_index == lifecycle_index + 1
    assert request_hash_index == schema_repair_index + 1


def test_migration_322_enforces_request_hash_for_new_packets(monkeypatch) -> None:
    monkeypatch.setattr(
        migrations,
        "_tables",
        lambda _conn: {"adaptive_risk_decision_packets"},
    )
    conn = _RecordingConnection()

    migrations._migration_322_adaptive_risk_request_hash_schema_repair(conn)

    sql = "\n".join(conn.statements).lower()
    assert "add column if not exists reservation_request_sha256" in sql
    assert "alter column reservation_request_sha256 set not null" in sql
    assert "ck_adaptive_risk_packet_request_hash_present" in sql
    assert "reservation_request_sha256 is null" in sql
    assert conn.commits == 1


def test_migration_323_fences_lifecycle_ids_and_partial_heat(monkeypatch) -> None:
    monkeypatch.setattr(
        migrations,
        "_tables",
        lambda _conn: {
            "adaptive_risk_reservations",
            "trading_automation_simulated_fills",
        },
    )
    conn = _RecordingConnection()

    migrations._migration_323_adaptive_db_paper_atomic_lifecycle(conn)

    sql = "\n".join(conn.statements).lower()
    assert "add column if not exists open_quantity_shares" in sql
    assert "open_quantity_shares <= cumulative_filled_quantity_shares" in sql
    assert "uq_tasf_adaptive_lifecycle_event" in sql
    assert "adaptive_risk_lifecycle_event_id" in sql
    ids = [migration_id for migration_id, _fn in migrations.MIGRATIONS]
    assert ids.index("323_adaptive_db_paper_atomic_lifecycle") == (
        ids.index("322_adaptive_risk_request_hash_schema_repair") + 1
    )
    assert conn.commits == 1


def test_migration_324_makes_adaptive_canonical_fills_append_only(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        migrations,
        "_tables",
        lambda _conn: {"trading_automation_simulated_fills"},
    )
    conn = _RecordingConnection()

    migrations._migration_324_adaptive_db_paper_fill_immutability(conn)

    sql = "\n".join(conn.statements).lower()
    assert "chili_prevent_adaptive_sim_fill_mutation" in sql
    assert "trg_adaptive_sim_fill_append_only" in sql
    assert "before update or delete" in sql
    assert "adaptive_risk_lifecycle_event_id" in sql
    assert "adaptive db-paper lifecycle fills are append-only" in sql
    ids = [migration_id for migration_id, _fn in migrations.MIGRATIONS]
    assert ids.index("324_adaptive_db_paper_fill_immutability") == (
        ids.index("323_adaptive_db_paper_atomic_lifecycle") + 1
    )
    assert conn.commits == 1


def test_migration_325_makes_opportunity_claim_first_dip_only(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        migrations,
        "_tables",
        lambda _conn: {"adaptive_risk_reservations"},
    )
    conn = _RecordingConnection()

    migrations._migration_325_first_dip_only_opportunity_claims(conn)

    sql = "\n".join(conn.statements).lower()
    assert "alter column opportunity_claim_id drop not null" in sql
    assert "set opportunity_claim_id = null" in sql
    assert "setup_family <> 'first_dip_reclaim'" in sql
    assert AdaptiveRiskReservation.__table__.c.opportunity_claim_id.nullable is True
    ids = [migration_id for migration_id, _fn in migrations.MIGRATIONS]
    assert ids.index("325_first_dip_only_opportunity_claims") == (
        ids.index("324_adaptive_db_paper_fill_immutability") + 1
    )
    assert conn.commits == 1


def test_migration_326_enforces_first_dip_only_claim_scope(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        migrations,
        "_tables",
        lambda _conn: {"adaptive_risk_reservations"},
    )
    conn = _RecordingConnection()

    migrations._migration_326_adaptive_opportunity_scope_constraint(conn)

    sql = "\n".join(conn.statements).lower()
    assert "ck_adaptive_risk_reservation_opportunity_scope" in sql
    assert "setup_family = 'first_dip_reclaim'" in sql
    assert "opportunity_claim_id is not null" in sql
    assert "setup_family <> 'first_dip_reclaim'" in sql
    assert "opportunity_claim_id is null" in sql
    ids = [migration_id for migration_id, _fn in migrations.MIGRATIONS]
    assert ids.index("326_adaptive_opportunity_scope_constraint") == (
        ids.index("325_first_dip_only_opportunity_claims") + 1
    )
    assert conn.commits == 1


def test_migration_335_installs_irreversible_late_fill_quarantine(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        migrations,
        "_tables",
        lambda _conn: {
            "adaptive_risk_reservations",
            "adaptive_risk_reservation_events",
        },
    )
    conn = _RecordingConnection()

    migrations._migration_335_adaptive_late_fill_exposure_quarantine(conn)

    sql = "\n".join(conn.statements).lower()
    model_columns = set(AdaptiveRiskReservation.__table__.columns.keys())
    assert {
        "lifecycle_contradiction_source_state",
        "lifecycle_contradiction_at",
        "lifecycle_contradiction_evidence_sha256",
    } <= model_columns
    assert "add column if not exists lifecycle_contradiction_source_state" in sql
    assert "state = 'exposure_quarantined'" in sql
    assert "cumulative_filled_quantity_shares > 0" in sql
    assert "quarantined alpaca exposure is fail-closed" in sql
    assert "new.open_quantity_shares <= old.open_quantity_shares" in sql
    ids = [migration_id for migration_id, _fn in migrations.MIGRATIONS]
    assert ids.index("335_adaptive_late_fill_exposure_quarantine") == (
        ids.index("334_iqfeed_host_bridge_schema_ownership") + 1
    )


def test_migration_341_binds_aggregate_buying_power_reflection(
    monkeypatch,
) -> None:
    model_columns = {
        "alpaca_paper_bp_reflection_receipts": set(
            AlpacaPaperBuyingPowerReflectionReceipt.__table__.columns.keys()
        ),
        "alpaca_paper_bp_reflection_items": set(
            AlpacaPaperBuyingPowerReflectionItem.__table__.columns.keys()
        ),
    }
    monkeypatch.setattr(
        migrations,
        "_tables",
        lambda _conn: {
            "adaptive_risk_reservations",
            "captured_paper_post_commit_outbox",
        },
    )
    monkeypatch.setattr(
        migrations,
        "_columns",
        lambda _conn, table_name: model_columns[table_name],
    )
    monkeypatch.setattr(
        migrations,
        "_reassert_adaptive_late_fill_quarantine_if_present",
        lambda _conn: None,
    )
    conn = _RecordingConnection()

    migrations._migration_341_alpaca_paper_buying_power_reflection(conn)

    sql = "\n".join(conn.statements).lower()
    assert "census_a_adapter_connection_generation =" in sql
    assert "census_b_adapter_connection_generation" in sql
    assert "provider_client_order_id = client_order_id" in sql
    assert "provider_limit_price = local_entry_limit_price" in sql
    assert "provider_quantity_shares =" in sql
    assert "provider_filled_quantity_shares" in sql
    assert "local_action_claim_phase = 'claimed'" in sql
    assert "transport_indeterminate" in sql
    assert "reject_alpaca_bp_reflection_mutation" in sql
    assert "before truncate" in sql
    ids = [migration_id for migration_id, _fn in migrations.MIGRATIONS]
    assert ids.index("341_alpaca_paper_buying_power_reflection") == (
        ids.index("340_alpaca_fill_query_observation_ledger") + 1
    )
    assert conn.commits == 1


def test_migration_341_reassert_executes_full_335_body_on_real_schema() -> None:
    # Livehead-audit gap G1: every migration-repair suite above drives the
    # migration with a recording fake, and the 341 test additionally stubbed
    # _reassert_adaptive_late_fill_quarantine_if_present to a no-op — so the
    # forward-call of the FULL migration-335 body (constraint re-validation
    # against existing rows + %ROWTYPE guard-function CREATE) never executed
    # any SQL anywhere in the tree. This test runs the real, unstubbed
    # migration 341 against the dedicated *_test database in one rollback-only
    # transaction, so inverted trigger comparisons, invalid DDL, or a
    # constraint-vs-existing-rows abort can no longer stay green.
    Base.metadata.create_all(bind=engine)
    migrations.run_migrations(engine)

    with engine.connect() as conn:
        transaction = conn.begin()
        try:
            quarantine_columns = {
                "lifecycle_contradiction_source_state",
                "lifecycle_contradiction_at",
                "lifecycle_contradiction_evidence_sha256",
            }
            assert quarantine_columns <= set(
                migrations._columns(conn, "adaptive_risk_reservations")
            )

            # The reassert helper must take its real branch (columns present)
            # and re-run the entire 335 body without error.
            migrations._reassert_adaptive_late_fill_quarantine_if_present(conn)

            state_constraint = conn.execute(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conrelid = 'adaptive_risk_reservations'::regclass "
                    "AND conname = 'ck_adaptive_risk_reservation_state'"
                )
            ).scalar_one()
            assert "exposure_quarantined" in state_constraint
            contradiction_constraint = conn.execute(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conrelid = 'adaptive_risk_reservations'::regclass "
                    "AND conname = "
                    "'ck_adaptive_risk_reservation_contradiction'"
                )
            ).scalar_one()
            assert contradiction_constraint

            # And the full migration 341 must be idempotent + reassert-clean
            # on the real current schema (it forward-calls the helper at its
            # tail; under the fakes that call was doubly unreachable).
            migrations._migration_341_alpaca_paper_buying_power_reflection(
                conn
            )

            guard_function = conn.execute(
                text(
                    "SELECT pg_get_functiondef(oid) FROM pg_proc "
                    "WHERE proname = "
                    "'chili_guard_alpaca_reservation_settlement_state'"
                )
            ).scalar_one()
            assert "quarantined Alpaca exposure is fail-closed" in (
                guard_function
            )
        finally:
            transaction.rollback()
