from __future__ import annotations

from dataclasses import replace
from datetime import timezone

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.db import engine
from app.services.trading.momentum_neural import captured_paper_admission
from app.services.trading.momentum_neural import captured_paper_phase_one_handoff as phase_one
from tests.test_captured_paper_admission import (
    _inputs,
    _pre_reservation_authority,
    _seed_session,
)


UTC = timezone.utc


def _record(db, inputs, *, material="a" * 64):
    request = inputs.post_commit_request
    return phase_one.record_captured_paper_phase_one_handoff(
        db,
        request=request,
        material_sha256=material,
        executed_read_inventory=inputs.executed_read_inventory,
        captured_reads=inputs.predecision_captured_reads,
        active_input_attestation=inputs.active_input_attestation,
        candidate_sha256="b" * 64,
        bound_input_scope_sha256="c" * 64,
    )


def test_phase_one_record_is_transactional_and_restart_gap_has_no_side_effects(db):
    now = db.execute(
        text("SELECT clock_timestamp() - interval '200 ms'")
    ).scalar_one()
    inputs = _inputs(now=now, setup_family="primary_entry")
    receipt = _record(db, inputs)
    assert receipt.state == phase_one.STATE_PENDING
    assert receipt.executed_read_inventory_sha256 == (
        inputs.executed_read_inventory.inventory_sha256
    )
    assert receipt.executed_material_sha256
    db.commit()

    result = phase_one.reconcile_captured_paper_phase_one_after_restart(
        engine,
        activation_generation=inputs.post_commit_request.route_token.runtime_generation,
        limit=10,
    )

    assert result["decision_handoff_unavailable_count"] == 1
    assert result["outbox_committed_count"] == 0
    assert result["initial_pending_count"] == 1
    assert result["remaining_pending_count"] == 0
    assert result["reconciliation_complete"] is True
    assert result["phase_two_side_effects_inferred"] is False
    assert "orders_submitted" not in result
    assert "opportunities_consumed" not in result
    assert "risk_reserved" not in result
    state = db.execute(
        text(
            "SELECT state FROM captured_paper_phase_one_handoffs "
            "WHERE completion_sha256=:completion"
        ),
        {"completion": inputs.post_commit_request.completion_sha256},
    ).scalar_one()
    assert state == phase_one.STATE_DECISION_HANDOFF_UNAVAILABLE
    for table in (
        "adaptive_risk_opportunity_claims",
        "adaptive_risk_reservations",
        "broker_symbol_action_claims",
        "captured_paper_post_commit_outbox",
    ):
        assert db.execute(text(f"SELECT count(*) FROM {table}")).scalar_one() == 0


def test_exact_outbox_ack_is_idempotent_and_event_bound(db):
    now = db.execute(
        text("SELECT clock_timestamp() - interval '200 ms'")
    ).scalar_one()
    inputs = _inputs(now=now, setup_family="primary_entry")
    _seed_session(db, inputs.post_commit_request)
    _record(db, inputs)
    db.commit()

    captured_paper_admission.commit_captured_paper_admission(
        engine,
        inputs=inputs,
        phase_one_material_sha256="a" * 64,
        executed_read_inventory=inputs.executed_read_inventory,
        **_pre_reservation_authority(inputs),
    )
    first = phase_one.acknowledge_captured_paper_phase_one_handoff(
        engine,
        request=inputs.post_commit_request,
        material_sha256="a" * 64,
    )
    second = phase_one.acknowledge_captured_paper_phase_one_handoff(
        engine,
        request=inputs.post_commit_request,
        material_sha256="a" * 64,
    )

    assert first == second
    assert first.state == phase_one.STATE_OUTBOX_COMMITTED
    assert first.event_sequence == 2
    assert db.execute(
        text(
            "SELECT count(*) FROM captured_paper_phase_one_handoff_events "
            "WHERE completion_sha256=:completion"
        ),
        {"completion": inputs.post_commit_request.completion_sha256},
    ).scalar_one() == 2


def test_terminal_unavailable_completion_can_never_be_recorded_again(db):
    now = db.execute(
        text("SELECT clock_timestamp() - interval '200 ms'")
    ).scalar_one()
    inputs = _inputs(now=now, setup_family="primary_entry")
    _record(db, inputs)
    db.commit()
    phase_one.reconcile_captured_paper_phase_one_after_restart(
        engine,
        activation_generation=inputs.post_commit_request.route_token.runtime_generation,
        limit=10,
    )

    with pytest.raises(
        phase_one.CapturedPaperPhaseOneHandoffError,
        match="decision_handoff_unavailable",
    ):
        _record(db, inputs)
    db.rollback()
    assert db.execute(
        text("SELECT count(*) FROM captured_paper_post_commit_outbox")
    ).scalar_one() == 0


def test_phase_one_rejects_non_durable_read_result_before_insert(db):
    now = db.execute(
        text("SELECT clock_timestamp() - interval '200 ms'")
    ).scalar_one()
    inputs = _inputs(now=now, setup_family="primary_entry")
    broken_reads = (
        replace(
            inputs.predecision_captured_reads[0],
            coverage_gap_recorded=True,
        ),
        *inputs.predecision_captured_reads[1:],
    )

    with pytest.raises(
        phase_one.CapturedPaperPhaseOneHandoffError,
        match="captured_paper_executed_read_results_invalid",
    ):
        phase_one.record_captured_paper_phase_one_handoff(
            db,
            request=inputs.post_commit_request,
            material_sha256="a" * 64,
            executed_read_inventory=inputs.executed_read_inventory,
            captured_reads=broken_reads,
            active_input_attestation=inputs.active_input_attestation,
            candidate_sha256="b" * 64,
            bound_input_scope_sha256="c" * 64,
        )

    assert db.execute(
        text("SELECT count(*) FROM captured_paper_phase_one_handoffs")
    ).scalar_one() == 0

def test_restart_reconciliation_never_false_clears_above_limit(db):
    first = _inputs(
        now=db.execute(
            text("SELECT clock_timestamp() - interval '400 ms'")
        ).scalar_one(),
        setup_family="primary_entry",
    )
    second = _inputs(
        now=db.execute(
            text("SELECT clock_timestamp() - interval '300 ms'")
        ).scalar_one(),
        setup_family="primary_entry",
        binder_id="9746fa2f-c15a-45dc-ab95-6aca91508bf9",
        intent_generation="e39a6e7d-63c9-454e-97ec-f856b9e817be",
        decision_id="chili_ml_ACTU_41_atomic_2",
        completion_generation="d680fcec-58c6-4112-a973-197b3b29d8c5",
    )
    _record(db, first, material="a" * 64)
    phase_one.record_captured_paper_phase_one_handoff(
        db,
        request=second.post_commit_request,
        material_sha256="d" * 64,
        executed_read_inventory=second.executed_read_inventory,
        captured_reads=second.predecision_captured_reads,
        active_input_attestation=second.active_input_attestation,
        candidate_sha256="e" * 64,
        bound_input_scope_sha256="f" * 64,
    )
    db.commit()

    with pytest.raises(
        phase_one.CapturedPaperPhaseOneHandoffError,
        match="reconcile_limit_insufficient",
    ):
        phase_one.reconcile_captured_paper_phase_one_after_restart(
            engine,
            activation_generation=first.post_commit_request.route_token.runtime_generation,
            limit=1,
        )

    assert db.execute(
        text(
            "SELECT count(*) FROM captured_paper_phase_one_handoffs "
            "WHERE state='pending'"
        )
    ).scalar_one() == 2


def test_terminal_phase_one_identity_and_events_are_db_immutable(db):
    now = db.execute(
        text("SELECT clock_timestamp() - interval '200 ms'")
    ).scalar_one()
    inputs = _inputs(now=now, setup_family="primary_entry")
    _record(db, inputs)
    db.commit()
    phase_one.reconcile_captured_paper_phase_one_after_restart(
        engine,
        activation_generation=inputs.post_commit_request.route_token.runtime_generation,
        limit=10,
    )

    with pytest.raises(DBAPIError):
        db.execute(
            text(
                "UPDATE captured_paper_phase_one_handoffs "
                "SET material_sha256=:mutated WHERE completion_sha256=:completion"
            ),
            {
                "mutated": "f" * 64,
                "completion": inputs.post_commit_request.completion_sha256,
            },
        )
        db.commit()
    db.rollback()
    with pytest.raises(DBAPIError):
        db.execute(
            text(
                "DELETE FROM captured_paper_phase_one_handoff_events "
                "WHERE completion_sha256=:completion"
            ),
            {"completion": inputs.post_commit_request.completion_sha256},
        )
        db.commit()
    db.rollback()
