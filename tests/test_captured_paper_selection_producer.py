from __future__ import annotations

import ast
import copy
import hashlib
import threading
import time
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from app.models.captured_paper_selection_frontier import (
    CapturedPaperSelectionFrontier,
    CapturedPaperSelectionFrontierEvent,
    CapturedPaperSelectionRouteState,
)
from app.models.trading import MomentumStrategyVariant, MomentumSymbolViability
from app.services.trading.momentum_neural import (
    captured_paper_selection_producer as producer_module,
)
from app.services.trading.momentum_neural.captured_paper_selection_producer import (
    PROVENANCE_KEY,
    CapturedPaperSelectionAuthority,
    CapturedPaperSelectionBatch,
    CapturedPaperSelectionObservation,
    CapturedPaperSelectionProducer,
    CapturedPaperSelectionProducerError,
    CapturedPaperSelectionProviderUnavailable,
    CapturedPaperSelectionQueueUnavailable,
    CapturedPaperSelectionRouteStateUpdate,
    CapturedPaperSelectionVariantBinding,
    apply_captured_paper_selection_batch,
    ensure_captured_paper_selection_frontier,
)
from app.services.trading.momentum_neural.captured_paper_variant_binding import (
    CapturedPaperVariantBindingAuthority,
    apply_captured_paper_variant_bindings,
    plan_captured_paper_variant_bindings,
    record_captured_paper_variant_application_receipt,
    rollback_captured_paper_variant_bindings,
)


UTC = timezone.utc
T0 = datetime(2026, 7, 18, 14, 30, tzinfo=UTC)
ACCOUNT_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
GEN_A = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
GEN_B = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
MANIFEST_SHA256 = "d" * 64


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _source(db, *, family: str = "captured_tape_breakout"):
    at = T0.replace(tzinfo=None) - timedelta(days=1)
    row = MomentumStrategyVariant(
        family=family,
        variant_key=family,
        version=3,
        label=f"{family} intended policy",
        params_json={
            "setup": "front_side_breakout",
            "adaptive_sizing": True,
            "minimum_score": 0.73,
        },
        is_active=True,
        execution_family="coinbase_spot",
        refinement_meta_json={"policy_surface": "shared_replay_paper"},
        created_at=at,
        updated_at=at,
    )
    db.add(row)
    db.flush()
    return row


def _binding_authority(generation: str, *, at: datetime = T0):
    return CapturedPaperVariantBindingAuthority(
        expected_account_id=ACCOUNT_ID,
        activation_generation=generation,
        policy_sha256=_digest("policy"),
        settings_projection_sha256=_digest("settings"),
        code_build_sha256=_digest("build"),
        bound_at=at,
    )


def _bind(db, source, generation: str, *, at: datetime = T0):
    binding_authority = _binding_authority(generation, at=at)
    plan = plan_captured_paper_variant_bindings(
        db,
        authority=binding_authority,
        source_variant_ids=[source.id],
    )
    application = apply_captured_paper_variant_bindings(db, plan=plan)
    record_captured_paper_variant_application_receipt(
        db,
        application=application,
        activation_manifest_sha256=MANIFEST_SHA256,
    )
    item = application.items[0]
    authority = CapturedPaperSelectionAuthority(
        expected_account_id=ACCOUNT_ID,
        activation_generation=generation,
        policy_sha256=binding_authority.policy_sha256,
        settings_projection_sha256=(
            binding_authority.settings_projection_sha256
        ),
        code_build_sha256=binding_authority.code_build_sha256,
        variant_bindings=(
            CapturedPaperSelectionVariantBinding(
                variant_id=item.target_variant_id,
                family=item.family,
                version=item.version,
                variant_key=item.target_variant_key,
                target_after_sha256=item.target_after_sha256,
            ),
        ),
    )
    return binding_authority, application, authority


def _observation(
    authority: CapturedPaperSelectionAuthority,
    *,
    sequence: int = 1,
    symbol: str = "ACTU",
    score: float = 0.84,
):
    return CapturedPaperSelectionObservation(
        source_sequence=sequence,
        source_event_at=T0 + timedelta(milliseconds=sequence),
        source_available_at=T0 + timedelta(milliseconds=sequence + 1),
        symbol=symbol,
        variant_id=authority.variant_ids[0],
        viability_score=score,
        paper_eligible=True,
        live_eligible=True,
        regime_snapshot_json={"regime": "momentum"},
        execution_readiness_json={
            "spread_bps": 22.0,
            "structural_stop_distance": 0.19,
            "liquidity_score": 0.91,
        },
        explain_json={"setup": "front_side_breakout"},
        evidence_window_json={
            "tape_from_sequence": sequence,
            "tape_through_sequence": sequence,
        },
        correlation_id=f"capture-{sequence}-{symbol.lower()}",
    )


def _eligible_route_update(observation: CapturedPaperSelectionObservation):
    return CapturedPaperSelectionRouteStateUpdate(
        source_sequence=observation.source_sequence,
        source_event_at=observation.source_event_at,
        source_available_at=observation.source_available_at,
        symbol=observation.symbol,
        variant_id=observation.variant_id,
        state="eligible",
        evidence_sha256=observation.observation_sha256,
        bundle_sha256=_digest(f"bundle-{observation.source_sequence}"),
        scoring_authority_sha256=_digest(
            f"scoring-{observation.source_sequence}"
        ),
        score_result_sha256=_digest(f"result-{observation.source_sequence}"),
    )


def _batch(
    authority: CapturedPaperSelectionAuthority,
    frontier,
    *,
    sequence: int = 1,
    symbol: str = "ACTU",
):
    observation = _observation(
        authority,
        sequence=sequence,
        symbol=symbol,
    )
    return CapturedPaperSelectionBatch(
        authority_sha256=authority.authority_sha256,
        expected_frontier=frontier,
        source_name="captured_iqfeed_tape",
        source_generation="dddddddd-dddd-4ddd-8ddd-dddddddddddd",
        queue_receipt_sha256=_digest(f"queue-{sequence}"),
        coverage_receipt_sha256=_digest(f"coverage-{sequence}"),
        source_sequence_from=frontier.last_source_sequence,
        source_sequence_through=sequence,
        watermark_at=observation.source_event_at,
        read_at=observation.source_available_at,
        observations=(observation,),
        route_state_updates=(_eligible_route_update(observation),),
    )


def _setup(db, *, generation: str = GEN_A):
    source = _source(db)
    binding_authority, application, authority = _bind(
        db, source, generation
    )
    frontier = ensure_captured_paper_selection_frontier(
        db,
        authority=authority,
        initialized_at=T0,
    )
    return source, binding_authority, application, authority, frontier


def test_batch_upsert_and_frontier_cas_commit_together(db) -> None:
    _source_row, _binding, _application, authority, frontier = _setup(db)
    batch = _batch(authority, frontier)

    result = apply_captured_paper_selection_batch(
        db,
        authority=authority,
        batch=batch,
        recorded_at=T0 + timedelta(seconds=1),
    )

    viability = (
        db.query(MomentumSymbolViability)
        .filter(MomentumSymbolViability.symbol == "ACTU")
        .one()
    )
    event = db.query(CapturedPaperSelectionFrontierEvent).one()
    assert result.status == "applied"
    assert result.viability_upserts == 1
    assert result.route_state_upserts == 1
    assert result.frontier.last_source_sequence == 1
    assert result.frontier.status == "ready"
    assert event.event_type == "batch_applied"
    assert event.batch_sha256 == batch.batch_sha256
    assert event.next_frontier_sha256 == result.frontier.frontier_sha256
    provenance = viability.execution_readiness_json[PROVENANCE_KEY]
    assert provenance["account_scope"] == "alpaca:paper"
    assert provenance["expected_account_id"] == ACCOUNT_ID
    assert provenance["activation_generation"] == GEN_A
    assert provenance["variant_id"] == authority.variant_ids[0]
    assert provenance["policy_sha256"] == authority.policy_sha256
    assert provenance["batch_sha256"] == batch.batch_sha256
    assert provenance["paper_only_strategy_override"] is False
    assert provenance["live_cash_authorized"] is False
    assert viability.paper_eligible is True
    assert viability.live_eligible is True
    route_state = db.query(CapturedPaperSelectionRouteState).one()
    assert route_state.state == "eligible"
    assert route_state.latest_source_sequence == 1
    assert route_state.evidence_sha256 == provenance["observation_sha256"]


def test_crash_rollback_then_restart_is_atomic_and_idempotent(db) -> None:
    _source_row, _binding, _application, authority, frontier = _setup(db)
    db.commit()
    batch = _batch(authority, frontier)

    apply_captured_paper_selection_batch(
        db,
        authority=authority,
        batch=batch,
        recorded_at=T0 + timedelta(seconds=1),
    )
    db.rollback()  # process crash before the caller-owned commit

    durable_frontier = db.query(CapturedPaperSelectionFrontier).one()
    assert durable_frontier.last_source_sequence == 0
    assert db.query(MomentumSymbolViability).count() == 0
    assert db.query(CapturedPaperSelectionRouteState).count() == 0
    assert db.query(CapturedPaperSelectionFrontierEvent).count() == 0

    first = apply_captured_paper_selection_batch(
        db,
        authority=authority,
        batch=batch,
        recorded_at=T0 + timedelta(seconds=2),
    )
    db.commit()
    repeated = apply_captured_paper_selection_batch(
        db,
        authority=authority,
        batch=batch,
        recorded_at=T0 + timedelta(seconds=3),
    )
    assert first.idempotent is False
    assert repeated.idempotent is True
    assert repeated.viability_upserts == 0
    assert repeated.route_state_upserts == 0
    assert db.query(MomentumSymbolViability).count() == 1
    assert db.query(CapturedPaperSelectionRouteState).count() == 1
    assert db.query(CapturedPaperSelectionFrontierEvent).count() == 1


@pytest.mark.parametrize(
    "mutation",
    (
        "scope",
        "freshness",
        "regime",
        "readiness",
        "explain",
        "evidence",
        "source_node",
        "correlation",
        "recorded_clock",
    ),
)
def test_idempotent_replay_verifies_full_stored_observation_material(
    db,
    mutation: str,
) -> None:
    _source_row, _binding, _application, authority, frontier = _setup(db)
    batch = _batch(authority, frontier)
    apply_captured_paper_selection_batch(
        db,
        authority=authority,
        batch=batch,
        recorded_at=T0 + timedelta(seconds=1),
    )
    db.commit()
    row = db.query(MomentumSymbolViability).one()
    if mutation == "scope":
        row.scope = "forged"
    elif mutation == "freshness":
        row.freshness_ts = row.freshness_ts + timedelta(microseconds=1)
    elif mutation == "regime":
        row.regime_snapshot_json = {"regime": "forged"}
    elif mutation == "readiness":
        row.execution_readiness_json = {
            **dict(row.execution_readiness_json),
            "forged": True,
        }
    elif mutation == "explain":
        row.explain_json = {**dict(row.explain_json), "forged": True}
    elif mutation == "evidence":
        row.evidence_window_json = {
            **dict(row.evidence_window_json),
            "forged": True,
        }
    elif mutation == "source_node":
        row.source_node_id = "forged_selection_producer"
    elif mutation == "correlation":
        row.correlation_id = "forged-correlation"
    elif mutation == "recorded_clock":
        row.updated_at = row.updated_at + timedelta(microseconds=1)
    else:  # pragma: no cover - parametrization authority
        raise AssertionError(mutation)
    db.commit()

    with pytest.raises(CapturedPaperSelectionProducerError) as rejected:
        apply_captured_paper_selection_batch(
            db,
            authority=authority,
            batch=batch,
            recorded_at=T0 + timedelta(seconds=2),
        )
    assert rejected.value.code == "IDEMPOTENCY_DRIFT"


def test_route_clock_regression_rolls_back_mixed_route_batch(db) -> None:
    _source_row, _binding, _application, authority, frontier = _setup(db)
    first_batch = _batch(authority, frontier, sequence=1, symbol="ACTU")
    first = apply_captured_paper_selection_batch(
        db,
        authority=authority,
        batch=first_batch,
        recorded_at=T0 + timedelta(seconds=1),
    )
    regressed = replace(
        _observation(authority, sequence=2, symbol="ACTU", score=0.99),
        source_event_at=T0,
        source_available_at=T0 + timedelta(milliseconds=1),
    )
    newer_other = _observation(
        authority,
        sequence=3,
        symbol="SAFE",
        score=0.91,
    )
    mixed = CapturedPaperSelectionBatch(
        authority_sha256=authority.authority_sha256,
        expected_frontier=first.frontier,
        source_name="captured_iqfeed_tape",
        source_generation="dddddddd-dddd-4ddd-8ddd-dddddddddddd",
        queue_receipt_sha256=_digest("regressed-route-queue"),
        coverage_receipt_sha256=_digest("regressed-route-coverage"),
        source_sequence_from=1,
        source_sequence_through=3,
        watermark_at=newer_other.source_event_at,
        read_at=newer_other.source_available_at,
        observations=(regressed, newer_other),
        route_state_updates=(
            _eligible_route_update(regressed),
            _eligible_route_update(newer_other),
        ),
    )

    with pytest.raises(CapturedPaperSelectionProducerError) as rejected:
        apply_captured_paper_selection_batch(
            db,
            authority=authority,
            batch=mixed,
            recorded_at=T0 + timedelta(seconds=2),
        )
    assert rejected.value.code == "ROUTE_STATE_CAS_CONFLICT"
    assert (
        db.query(CapturedPaperSelectionRouteState)
        .filter(CapturedPaperSelectionRouteState.symbol == "ACTU")
        .one()
        .latest_source_sequence
        == 1
    )
    assert (
        db.query(MomentumSymbolViability)
        .filter(MomentumSymbolViability.symbol == "ACTU")
        .one()
        .viability_score
        == pytest.approx(first_batch.observations[0].viability_score)
    )
    assert (
        db.query(MomentumSymbolViability)
        .filter(MomentumSymbolViability.symbol == "SAFE")
        .count()
        == 0
    )


def test_stale_frontier_batch_fails_without_partial_viability(db) -> None:
    _source_row, _binding, _application, authority, frontier = _setup(db)
    first = _batch(authority, frontier, sequence=1, symbol="ACTU")
    stale = _batch(authority, frontier, sequence=1, symbol="NEXT")
    apply_captured_paper_selection_batch(
        db,
        authority=authority,
        batch=first,
        recorded_at=T0 + timedelta(seconds=1),
    )

    with pytest.raises(CapturedPaperSelectionProducerError) as rejected:
        apply_captured_paper_selection_batch(
            db,
            authority=authority,
            batch=stale,
            recorded_at=T0 + timedelta(seconds=2),
        )
    assert rejected.value.code == "FRONTIER_CAS_CONFLICT"
    assert (
        db.query(MomentumSymbolViability)
        .filter(MomentumSymbolViability.symbol == "NEXT")
        .count()
        == 0
    )
    assert (
        db.query(CapturedPaperSelectionRouteState)
        .filter(CapturedPaperSelectionRouteState.symbol == "NEXT")
        .count()
        == 0
    )


def test_ensure_and_apply_use_frontier_then_variant_lock_order(db) -> None:
    """A restart ensure cannot hold variants while waiting on an apply."""

    _source_row, _binding, _application, authority, _frontier = _setup(db)
    db.commit()
    SessionLocal = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    applying = SessionLocal()
    waiter_started = threading.Event()
    waiter_done = threading.Event()
    waiter_pid: list[int] = []
    waiter_errors: list[BaseException] = []

    def ensure_on_second_session() -> None:
        session = SessionLocal()
        try:
            with session.begin():
                waiter_pid.append(
                    int(session.execute(text("SELECT pg_backend_pid()" )).scalar_one())
                )
                waiter_started.set()
                ensure_captured_paper_selection_frontier(
                    session,
                    authority=authority,
                    initialized_at=T0 + timedelta(seconds=2),
                )
        except BaseException as exc:  # captured for main-thread assertion
            waiter_errors.append(exc)
        finally:
            session.close()
            waiter_done.set()

    transaction = applying.begin()
    try:
        producer_module._locked_frontier(applying, authority)
        worker = threading.Thread(
            target=ensure_on_second_session,
            name="selection-ensure-lock-order",
            daemon=True,
        )
        worker.start()
        assert waiter_started.wait(timeout=5.0)
        deadline = time.monotonic() + 5.0
        waiting_on_lock = False
        while time.monotonic() < deadline:
            # Reuse the lock-owning transaction as the observer.  The guarded
            # test engine intentionally permits only two concurrent pooled
            # connections: one for this owner and one for the waiter.  A third
            # observer checkout would test pool exhaustion instead of lock
            # ordering.
            wait_type = applying.execute(
                text(
                    "SELECT wait_event_type FROM pg_stat_activity "
                    "WHERE pid=:pid"
                ),
                {"pid": waiter_pid[0]},
            ).scalar_one_or_none()
            if wait_type == "Lock":
                waiting_on_lock = True
                break
            time.sleep(0.01)
        assert waiting_on_lock, "ensure session never waited on held frontier"

        # Under the old reverse order, the waiter already held this variant
        # lock and PostgreSQL detected a frontier<->variant deadlock here.
        producer_module._validate_bound_variants(
            applying,
            authority,
            lock=True,
        )
        transaction.commit()
        assert waiter_done.wait(timeout=5.0)
        worker.join(timeout=1.0)
        assert waiter_errors == []
    finally:
        if transaction.is_active:
            transaction.rollback()
        applying.close()


@pytest.mark.parametrize(
    ("exception_type", "reason"),
    [
        (CapturedPaperSelectionProviderUnavailable, "provider_unavailable"),
        (CapturedPaperSelectionQueueUnavailable, "queue_unavailable"),
    ],
)
def test_provider_or_queue_failure_records_gap_without_advancing_or_writing(
    db,
    exception_type,
    reason,
) -> None:
    _source_row, _binding, _application, authority, _frontier = _setup(db)
    db.commit()
    SessionLocal = sessionmaker(bind=db.get_bind(), expire_on_commit=False)

    class UnavailablePort:
        network_fallback_allowed = False
        broker_access_allowed = False
        mutation_allowed = False

        def read_batch(self, *, frontier, authority):
            raise exception_type("redacted local capture failure")

    producer = CapturedPaperSelectionProducer(
        session_factory=SessionLocal,
        authority=authority,
        input_port=UnavailablePort(),
        wall_clock=lambda: T0 + timedelta(seconds=4),
    )
    result = producer.tick()

    db.expire_all()
    durable = db.query(CapturedPaperSelectionFrontier).one()
    event = db.query(CapturedPaperSelectionFrontierEvent).one()
    assert result.status == "gap"
    assert result.frontier.status == "gap"
    assert durable.last_source_sequence == 0
    assert durable.gap_count == 1
    assert event.event_type == "coverage_gap"
    assert event.batch_sha256 is None
    assert event.gap_sha256 == result.gap_sha256
    assert reason in event.detail_canonical_json
    assert db.query(MomentumSymbolViability).count() == 0


def test_generation_rotation_rejects_old_authority_and_accepts_new(db) -> None:
    source, binding_a, application_a, authority_a, frontier_a = _setup(db)
    batch_a = _batch(authority_a, frontier_a)
    apply_captured_paper_selection_batch(
        db,
        authority=authority_a,
        batch=batch_a,
        recorded_at=T0 + timedelta(seconds=1),
    )
    db.commit()

    rollback_captured_paper_variant_bindings(
        db,
        application=application_a,
        rolled_back_at=T0 + timedelta(seconds=2),
    )
    db.commit()
    _binding_b, _application_b, authority_b = _bind(
        db,
        source,
        GEN_B,
        at=T0 + timedelta(seconds=3),
    )
    frontier_b = ensure_captured_paper_selection_frontier(
        db,
        authority=authority_b,
        initialized_at=T0 + timedelta(seconds=3),
    )
    batch_b = _batch(authority_b, frontier_b)

    with pytest.raises(CapturedPaperSelectionProducerError) as old_rejected:
        apply_captured_paper_selection_batch(
            db,
            authority=authority_a,
            batch=batch_a,
            recorded_at=T0 + timedelta(seconds=4),
        )
    assert old_rejected.value.code == "VARIANT_AUTHORITY_DRIFT"

    applied_b = apply_captured_paper_selection_batch(
        db,
        authority=authority_b,
        batch=batch_b,
        recorded_at=T0 + timedelta(seconds=5),
    )
    viability = db.query(MomentumSymbolViability).one()
    provenance = viability.execution_readiness_json[PROVENANCE_KEY]
    assert applied_b.frontier.activation_generation == GEN_B
    assert provenance["activation_generation"] == GEN_B
    assert db.query(CapturedPaperSelectionFrontier).count() == 2


def test_db_guard_refuses_unreceipted_frontier_update(db) -> None:
    _source_row, _binding, _application, _authority, _frontier = _setup(db)
    db.flush()

    with pytest.raises(Exception):
        with db.begin_nested():
            db.execute(
                text(
                    "UPDATE captured_paper_selection_frontiers "
                    "SET status='gap' WHERE status='ready'"
                )
            )
    db.rollback()


def test_migration_350_is_registered_idempotent_and_installs_guards(db) -> None:
    from app import migrations

    migration = (
        "350_captured_paper_selection_frontier",
        migrations._migration_350_captured_paper_selection_frontier,
    )
    assert migration in migrations.MIGRATIONS
    migration_ids = [row[0] for row in migrations.MIGRATIONS]
    assert migration_ids.count(migration[0]) == 1
    assert migration_ids.index(migration[0]) > migration_ids.index(
        "348_captured_paper_executed_read_inventory"
    )

    # Rehearsal owns the exact production migration function and proves that a
    # restart can safely re-enter it on the dedicated test database.
    migrations._migration_350_captured_paper_selection_frontier(db.connection())
    migrations._migration_350_captured_paper_selection_frontier(db.connection())

    tables = {
        row[0]
        for row in db.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = current_schema() "
                "AND table_name IN "
                "('captured_paper_selection_frontiers', "
                "'captured_paper_selection_frontier_events')"
            )
        )
    }
    assert tables == {
        "captured_paper_selection_frontiers",
        "captured_paper_selection_frontier_events",
    }
    triggers = {
        row[0]
        for row in db.execute(
            text(
                "SELECT tgname FROM pg_trigger "
                "WHERE NOT tgisinternal AND tgname IN "
                "('trg_captured_paper_selection_frontier_guard', "
                "'trg_captured_paper_selection_event_immutable')"
            )
        )
    }
    assert triggers == {
        "trg_captured_paper_selection_frontier_guard",
        "trg_captured_paper_selection_event_immutable",
    }

    _source_row, _binding, _application, authority, frontier = _setup(db)
    result = apply_captured_paper_selection_batch(
        db,
        authority=authority,
        batch=_batch(authority, frontier),
        recorded_at=T0 + timedelta(seconds=1),
    )
    db.flush()
    assert result.frontier.event_sequence == 1
    assert db.query(CapturedPaperSelectionFrontierEvent).count() == 1

    with pytest.raises(DBAPIError) as immutable_authority:
        with db.begin_nested():
            db.execute(
                text(
                    "UPDATE captured_paper_selection_frontiers "
                    "SET authority_sha256 = :forged WHERE id = :frontier_id"
                ),
                {
                    "forged": _digest("forged-authority"),
                    "frontier_id": frontier.frontier_id,
                },
            )
    assert (
        getattr(immutable_authority.value.orig, "sqlstate", None)
        or getattr(immutable_authority.value.orig, "pgcode", None)
    ) == "23514"

    with pytest.raises(DBAPIError) as immutable_event:
        with db.begin_nested():
            db.execute(
                text(
                    "UPDATE captured_paper_selection_frontier_events "
                    "SET detail_canonical_json = '{}' WHERE frontier_id = :frontier_id"
                ),
                {"frontier_id": frontier.frontier_id},
            )
    assert (
        getattr(immutable_event.value.orig, "sqlstate", None)
        or getattr(immutable_event.value.orig, "pgcode", None)
    ) == "23514"


def test_migration_353_route_state_schema_and_cas_guards(db) -> None:
    from app import migrations

    migration = (
        "353_captured_paper_selection_route_state",
        migrations._migration_353_captured_paper_selection_route_state,
    )
    assert migration in migrations.MIGRATIONS
    migration_ids = [row[0] for row in migrations.MIGRATIONS]
    assert migration_ids.count(migration[0]) == 1
    assert migration_ids.index(migration[0]) > migration_ids.index(
        "352_captured_paper_variant_application_append_only"
    )
    migrations._migration_353_captured_paper_selection_route_state(
        db.connection()
    )
    migrations._migration_353_captured_paper_selection_route_state(
        db.connection()
    )

    columns = {
        row[0]
        for row in db.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema=current_schema() "
                "AND table_name='captured_paper_selection_route_states'"
            )
        )
    }
    assert columns == {
        "id",
        "account_scope",
        "expected_account_id",
        "activation_generation",
        "execution_family",
        "authority_sha256",
        "symbol",
        "variant_id",
        "latest_source_sequence",
        "state",
        "evidence_sha256",
        "batch_sha256",
        "source_event_at",
        "source_available_at",
        "version",
        "state_sha256",
        "created_at",
        "updated_at",
    }
    constraints = {
        row[0]
        for row in db.execute(
            text(
                "SELECT conname FROM pg_constraint "
                "WHERE conrelid='captured_paper_selection_route_states'::regclass"
            )
        )
    }
    assert {
        "uq_captured_paper_selection_route_state",
        "ck_captured_paper_selection_route_state_route",
        "ck_captured_paper_selection_route_state_value",
        "ck_captured_paper_selection_route_state_counters",
        "ck_captured_paper_selection_route_state_clocks",
        "ck_captured_paper_selection_route_state_hashes",
    }.issubset(constraints)
    assert db.execute(
        text(
            "SELECT to_regclass("
            "'ix_captured_paper_selection_route_state_symbol')"
        )
    ).scalar_one() == "ix_captured_paper_selection_route_state_symbol"
    assert db.execute(
        text(
            "SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal "
            "AND tgname='trg_captured_paper_selection_route_state_guard'"
        )
    ).scalar_one() == 1
    # The migration must verify the complete physical contract, not just names.
    migrations._verify_migration_353_route_state_physical_contract(
        db.connection()
    )

    _source_row, _binding, _application, authority, frontier = _setup(db)
    first = apply_captured_paper_selection_batch(
        db,
        authority=authority,
        batch=_batch(authority, frontier, sequence=1),
        recorded_at=T0 + timedelta(seconds=1),
    )
    second = apply_captured_paper_selection_batch(
        db,
        authority=authority,
        batch=_batch(authority, first.frontier, sequence=2),
        recorded_at=T0 + timedelta(seconds=2),
    )
    db.flush()
    route_state = db.query(CapturedPaperSelectionRouteState).one()
    assert second.frontier.last_source_sequence == 2
    assert route_state.latest_source_sequence == 2
    assert route_state.version == 2

    def rejected(sql: str, parameters: dict | None = None) -> None:
        with pytest.raises(DBAPIError) as error:
            with db.begin_nested():
                db.execute(text(sql), parameters or {"row_id": route_state.id})
        assert (
            getattr(error.value.orig, "sqlstate", None)
            or getattr(error.value.orig, "pgcode", None)
        ) == "23514"

    rejected(
        "DELETE FROM captured_paper_selection_route_states WHERE id=:row_id"
    )
    rejected(
        "UPDATE captured_paper_selection_route_states SET "
        "authority_sha256=:forged, version=version+1, "
        "latest_source_sequence=latest_source_sequence+1, "
        "updated_at=updated_at+interval '1 microsecond' WHERE id=:row_id",
        {"row_id": route_state.id, "forged": _digest("forged-route-authority")},
    )
    rejected(
        "UPDATE captured_paper_selection_route_states SET version=version+1, "
        "updated_at=updated_at+interval '1 microsecond' WHERE id=:row_id"
    )
    rejected(
        "UPDATE captured_paper_selection_route_states SET version=version+2, "
        "latest_source_sequence=latest_source_sequence+1, "
        "updated_at=updated_at+interval '1 microsecond' WHERE id=:row_id"
    )
    rejected(
        "UPDATE captured_paper_selection_route_states SET version=version+1, "
        "latest_source_sequence=latest_source_sequence+1, state_sha256='bad', "
        "updated_at=updated_at+interval '1 microsecond' WHERE id=:row_id"
    )
    rejected(
        "UPDATE captured_paper_selection_route_states SET version=version+1, "
        "latest_source_sequence=latest_source_sequence+1, "
        "source_available_at=source_event_at-interval '1 microsecond', "
        "updated_at=updated_at+interval '1 microsecond' WHERE id=:row_id"
    )
    rejected(
        "UPDATE captured_paper_selection_route_states SET version=version+1, "
        "latest_source_sequence=latest_source_sequence+1, "
        "source_event_at=source_event_at-interval '1 microsecond', "
        "updated_at=updated_at+interval '1 microsecond' WHERE id=:row_id"
    )


@pytest.mark.parametrize(
    "physical_defect",
    ("missing_allocator_and_pk", "nullable_state", "missing_variant_fk"),
)
def test_migration_353_rejects_all_column_partial_physical_schemas(
    db,
    physical_defect: str,
) -> None:
    from app import migrations

    schema = f"mig353_partial_{uuid.uuid4().hex[:12]}"
    connection = db.get_bind().connect()
    try:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        connection.commit()
        connection.execute(text(f'SET search_path TO "{schema}", public'))
        connection.commit()
        id_definition = (
            "BIGINT NOT NULL"
            if physical_defect == "missing_allocator_and_pk"
            else "BIGSERIAL PRIMARY KEY"
        )
        state_definition = (
            "VARCHAR(24)"
            if physical_defect == "nullable_state"
            else "VARCHAR(24) NOT NULL"
        )
        variant_definition = (
            "INTEGER NOT NULL REFERENCES public.momentum_strategy_variants(id) "
            "ON DELETE RESTRICT"
            if physical_defect != "missing_variant_fk"
            else "INTEGER NOT NULL"
        )
        connection.execute(text(f"""
            CREATE TABLE captured_paper_selection_route_states (
                id {id_definition},
                account_scope VARCHAR(32) NOT NULL,
                expected_account_id VARCHAR(36) NOT NULL,
                activation_generation VARCHAR(36) NOT NULL,
                execution_family VARCHAR(32) NOT NULL,
                authority_sha256 VARCHAR(64) NOT NULL,
                symbol VARCHAR(36) NOT NULL,
                variant_id {variant_definition},
                latest_source_sequence BIGINT NOT NULL,
                state {state_definition},
                evidence_sha256 VARCHAR(64) NOT NULL,
                batch_sha256 VARCHAR(64) NOT NULL,
                source_event_at TIMESTAMPTZ NOT NULL,
                source_available_at TIMESTAMPTZ NOT NULL,
                version BIGINT NOT NULL DEFAULT 1,
                state_sha256 VARCHAR(64) NOT NULL,
                created_at TIMESTAMPTZ NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL
            )
        """))
        connection.commit()

        with pytest.raises(RuntimeError, match="route-state"):
            migrations._migration_353_captured_paper_selection_route_state(
                connection
            )
        connection.rollback()
    finally:
        try:
            connection.execute(text("SET search_path TO public"))
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            connection.commit()
        finally:
            connection.close()


def test_module_has_no_execution_or_broker_imports_and_migration_is_registered() -> None:
    module_path = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "services"
        / "trading"
        / "momentum_neural"
        / "captured_paper_selection_producer.py"
    )
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
    forbidden = ("live_runner", "dispatcher", "alpaca_spot", "broker")
    assert not any(token in name for name in imported for token in forbidden)

    from app.migrations import MIGRATIONS

    ids = [version_id for version_id, _migration in MIGRATIONS]
    assert ids.count("350_captured_paper_selection_frontier") == 1
    assert ids.count("353_captured_paper_selection_route_state") == 1


def test_policy_parity_and_reserved_provenance_are_fail_closed(db) -> None:
    _source_row, _binding, _application, authority, _frontier = _setup(db)
    with pytest.raises(CapturedPaperSelectionProducerError) as parity:
        CapturedPaperSelectionObservation(
            source_sequence=1,
            source_event_at=T0,
            source_available_at=T0 + timedelta(milliseconds=1),
            symbol="ACTU",
            variant_id=authority.variant_ids[0],
            viability_score=0.8,
            paper_eligible=True,
            live_eligible=False,
            regime_snapshot_json={},
            execution_readiness_json={"spread_bps": 20},
            explain_json={},
            evidence_window_json={"from": 1, "through": 1},
            correlation_id="parity-mismatch",
        )
    assert parity.value.code == "POLICY_PARITY_MISMATCH"

    reserved = copy.deepcopy(_observation(authority).execution_readiness_json)
    reserved[PROVENANCE_KEY] = {"paper_risk_override": 50}
    with pytest.raises(CapturedPaperSelectionProducerError) as hidden:
        CapturedPaperSelectionObservation(
            source_sequence=1,
            source_event_at=T0,
            source_available_at=T0 + timedelta(milliseconds=1),
            symbol="ACTU",
            variant_id=authority.variant_ids[0],
            viability_score=0.8,
            paper_eligible=True,
            live_eligible=True,
            regime_snapshot_json={},
            execution_readiness_json=reserved,
            explain_json={},
            evidence_window_json={"from": 1, "through": 1},
            correlation_id="hidden-override",
        )
    assert hidden.value.code == "CONTRACT_INVALID"


def test_post_construction_payload_mutation_and_duplicate_route_are_rejected(db) -> None:
    _source_row, _binding, _application, authority, frontier = _setup(db)
    batch = _batch(authority, frontier)
    batch.observations[0].execution_readiness_json["spread_bps"] = 999

    with pytest.raises(CapturedPaperSelectionProducerError) as mutated:
        apply_captured_paper_selection_batch(
            db,
            authority=authority,
            batch=batch,
            recorded_at=T0 + timedelta(seconds=1),
        )
    assert mutated.value.code == "CONTRACT_INVALID"
    assert db.query(MomentumSymbolViability).count() == 0

    first = _observation(authority, sequence=1)
    second = _observation(authority, sequence=2)
    with pytest.raises(CapturedPaperSelectionProducerError) as duplicate:
        CapturedPaperSelectionBatch(
            authority_sha256=authority.authority_sha256,
            expected_frontier=frontier,
            source_name="captured_iqfeed_tape",
            source_generation="dddddddd-dddd-4ddd-8ddd-dddddddddddd",
            queue_receipt_sha256=_digest("duplicate-queue"),
            coverage_receipt_sha256=_digest("duplicate-coverage"),
            source_sequence_from=0,
            source_sequence_through=2,
            watermark_at=second.source_event_at,
            read_at=second.source_available_at,
            observations=(first, second),
            route_state_updates=(
                _eligible_route_update(first),
                _eligible_route_update(second),
            ),
        )
    assert duplicate.value.code == "CONTRACT_INVALID"
