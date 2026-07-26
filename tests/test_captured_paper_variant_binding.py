from __future__ import annotations

import copy
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.models.trading import MomentumStrategyVariant
from app.services.trading.momentum_neural.captured_paper_initial_admission import (
    captured_paper_initial_variant_sha256,
)
from app.services.trading.momentum_neural.captured_paper_variant_binding import (
    BINDING_META_KEY,
    CapturedPaperVariantBindingAuthority,
    CapturedPaperVariantBindingError,
    apply_captured_paper_variant_bindings,
    assert_committed_captured_paper_variant_application,
    assert_rolled_back_captured_paper_variant_application,
    load_captured_paper_variant_application_receipt_by_generation,
    plan_captured_paper_variant_bindings,
    recover_stale_captured_paper_variant_bindings,
    record_captured_paper_variant_application_receipt,
    resolve_intended_canonical_source_variant_ids,
    rollback_captured_paper_variant_bindings,
)
from app.services.trading.momentum_neural.evolution import (
    maybe_kill_underperforming_variant,
    maybe_publish_refined_variant,
)
from app.services.trading.momentum_neural.persistence import (
    active_variant_for_family,
)
from app.services.trading.momentum_neural.viable_query import (
    _hot_variants_by_family_version,
)
from app.services.trading.momentum_neural.variants import iter_momentum_families


_T0 = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)
_MANIFEST_SHA256 = "4" * 64


def _authority(
    *,
    generation: str | None = None,
    bound_at: datetime = _T0,
) -> CapturedPaperVariantBindingAuthority:
    return CapturedPaperVariantBindingAuthority(
        expected_account_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        activation_generation=(generation or str(uuid.uuid4())),
        policy_sha256="1" * 64,
        settings_projection_sha256="2" * 64,
        code_build_sha256="3" * 64,
        bound_at=bound_at,
    )


def _source(
    db,
    *,
    family: str,
    version: int = 3,
    variant_key: str | None = None,
    active: bool = True,
    parent_variant_id: int | None = None,
    params: dict | None = None,
) -> MomentumStrategyVariant:
    at = _T0.replace(tzinfo=None) - timedelta(days=1)
    row = MomentumStrategyVariant(
        family=family,
        variant_key=variant_key or family,
        version=version,
        label=f"{family} strategy",
        params_json=copy.deepcopy(
            params
            or {
                "entry_style": "breakout",
                "nested": {"threshold": 0.73, "windows": [1, 3, 5]},
            }
        ),
        is_active=active,
        execution_family="coinbase_spot",
        parent_variant_id=parent_variant_id,
        refinement_meta_json={"research_origin": "shared-strategy"},
        created_at=at,
        updated_at=at,
    )
    db.add(row)
    db.flush()
    return row


def _clone_for(db, source: MomentumStrategyVariant) -> MomentumStrategyVariant:
    return (
        db.query(MomentumStrategyVariant)
        .filter(
            MomentumStrategyVariant.family == source.family,
            MomentumStrategyVariant.variant_key
            == f"captured_paper:{source.family}",
            MomentumStrategyVariant.version == source.version,
        )
        .one()
    )


def _record(db, application):
    return record_captured_paper_variant_application_receipt(
        db,
        application=application,
        activation_manifest_sha256=_MANIFEST_SHA256,
    )


def test_plan_is_deterministic_and_rejects_replay_v3_sources(db) -> None:
    source = _source(db, family="impulse_breakout")
    replay = _source(
        db,
        family="replay_family",
        variant_key="replay_v3_deadbeef",
    )
    authority = _authority(generation="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")

    first = plan_captured_paper_variant_bindings(
        db, authority=authority, source_variant_ids=[source.id]
    )
    second = plan_captured_paper_variant_bindings(
        db, authority=authority, source_variant_ids=[source.id]
    )

    assert first.to_dict() == second.to_dict()
    assert first.plan_sha256 == second.plan_sha256
    assert [item.target_variant_key for item in first.items] == [
        "captured_paper:impulse_breakout"
    ]
    assert all("replay_v3" not in item.target_variant_key for item in first.items)

    with pytest.raises(CapturedPaperVariantBindingError) as rejected:
        plan_captured_paper_variant_bindings(
            db, authority=authority, source_variant_ids=[replay.id]
        )
    assert rejected.value.code == "SOURCE_INVALID"


def test_apply_clones_exact_strategy_and_is_idempotent(db) -> None:
    source = _source(
        db,
        family="micro_pullback_continuation",
        params={
            "entry_style": "micro_pullback",
            "setup": {"minimum_score": 0.81, "tags": ["front_side"]},
        },
    )
    source_before = captured_paper_initial_variant_sha256(source)
    params_before = copy.deepcopy(source.params_json)
    authority = _authority(generation="cccccccc-cccc-4ccc-8ccc-cccccccccccc")
    plan = plan_captured_paper_variant_bindings(
        db, authority=authority, source_variant_ids=[source.id]
    )

    first = apply_captured_paper_variant_bindings(db, plan=plan)
    clone = _clone_for(db, source)

    assert first.items[0].action == "created"
    assert first.items[0].source_variant_sha256 == source_before
    assert first.items[0].target_after_sha256 == (
        captured_paper_initial_variant_sha256(clone)
    )
    assert clone.is_active is True
    assert clone.execution_family == "alpaca_spot"
    assert clone.parent_variant_id == source.id
    assert clone.params_json == params_before == source.params_json
    assert captured_paper_initial_variant_sha256(source) == source_before
    marker = clone.refinement_meta_json[BINDING_META_KEY]
    assert marker["account_scope"] == "alpaca:paper"
    assert marker["expected_account_id"] == authority.expected_account_id
    assert marker["activation_generation"] == authority.activation_generation
    assert marker["source_variant_sha256"] == source_before
    assert marker["strategy_params_overridden"] is False
    assert marker["live_cash_authorized"] is False
    assert marker["real_money_authorized"] is False
    serialized_params = json.dumps(clone.params_json, sort_keys=True).lower()
    assert all(
        token not in serialized_params
        for token in ("$50", "$250", "one_symbol", "paper_risk_override")
    )

    repeated = apply_captured_paper_variant_bindings(db, plan=plan)
    assert repeated.items[0].action == "already_applied"
    assert repeated.items[0].target_variant_id == clone.id
    assert repeated.items[0].target_after_sha256 == first.items[0].target_after_sha256
    assert (
        db.query(MomentumStrategyVariant)
        .filter(
            MomentumStrategyVariant.variant_key
            == "captured_paper:micro_pullback_continuation"
        )
        .count()
        == 1
    )

    # Regenerating the same semantic request is also a no-write success.
    regenerated = plan_captured_paper_variant_bindings(
        db, authority=authority, source_variant_ids=[source.id]
    )
    reapplied = apply_captured_paper_variant_bindings(db, plan=regenerated)
    assert regenerated.items[0].target_state == "already_applied"
    assert reapplied.items[0].action == "already_applied"
    assert reapplied.items[0].target_after_sha256 == first.items[0].target_after_sha256


def test_active_clone_with_extra_binding_field_is_not_accepted(db) -> None:
    source = _source(db, family="strict_binding_provenance")
    authority = _authority()
    plan = plan_captured_paper_variant_bindings(
        db, authority=authority, source_variant_ids=[source.id]
    )
    apply_captured_paper_variant_bindings(db, plan=plan)
    clone = _clone_for(db, source)
    changed_meta = copy.deepcopy(clone.refinement_meta_json)
    changed_meta[BINDING_META_KEY]["paper_risk_override"] = "$50"
    clone.refinement_meta_json = changed_meta
    db.flush()

    with pytest.raises(CapturedPaperVariantBindingError) as rejected:
        plan_captured_paper_variant_bindings(
            db, authority=authority, source_variant_ids=[source.id]
        )
    assert rejected.value.code == "TARGET_ACTIVE_CONFLICT"


@pytest.mark.parametrize("drift", ["parent", "version"])
def test_apply_fails_closed_on_source_parent_or_version_drift(db, drift: str) -> None:
    parent = _source(db, family=f"parent_{drift}")
    source = _source(db, family=f"source_{drift}")
    plan = plan_captured_paper_variant_bindings(
        db,
        authority=_authority(),
        source_variant_ids=[source.id],
    )

    if drift == "parent":
        source.parent_variant_id = parent.id
    else:
        source.version += 1
    db.flush()

    with pytest.raises(CapturedPaperVariantBindingError) as rejected:
        apply_captured_paper_variant_bindings(db, plan=plan)
    assert rejected.value.code == "SOURCE_DRIFT"
    assert (
        db.query(MomentumStrategyVariant)
        .filter(
            MomentumStrategyVariant.variant_key
            == f"captured_paper:{source.family}"
        )
        .count()
        == 0
    )


def test_apply_rejects_active_sibling_version_created_after_plan(db) -> None:
    source = _source(db, family="version_bound_breakout", version=4)
    plan = plan_captured_paper_variant_bindings(
        db,
        authority=_authority(),
        source_variant_ids=[source.id],
    )
    at = _T0.replace(tzinfo=None) - timedelta(hours=1)
    sibling = MomentumStrategyVariant(
        family=source.family,
        variant_key=f"captured_paper:{source.family}",
        version=3,
        label="old active PAPER route",
        params_json=copy.deepcopy(source.params_json),
        is_active=True,
        execution_family="alpaca_spot",
        parent_variant_id=source.id,
        refinement_meta_json={},
        created_at=at,
        updated_at=at,
    )
    db.add(sibling)
    db.flush()

    with pytest.raises(CapturedPaperVariantBindingError) as rejected:
        apply_captured_paper_variant_bindings(db, plan=plan)
    assert rejected.value.code == "TARGET_ACTIVE_CONFLICT"
    assert (
        db.query(MomentumStrategyVariant)
        .filter(
            MomentumStrategyVariant.family == source.family,
            MomentumStrategyVariant.variant_key
            == f"captured_paper:{source.family}",
            MomentumStrategyVariant.version == source.version,
        )
        .count()
        == 0
    )


def test_rollback_deactivates_only_exact_receipt_bound_clones(db) -> None:
    source = _source(db, family="breakout_reclaim")
    authority = _authority(generation="dddddddd-dddd-4ddd-8ddd-dddddddddddd")
    plan = plan_captured_paper_variant_bindings(
        db, authority=authority, source_variant_ids=[source.id]
    )
    application = apply_captured_paper_variant_bindings(db, plan=plan)
    _record(db, application)

    receipt = rollback_captured_paper_variant_bindings(
        db,
        application=application,
        rolled_back_at=_T0 + timedelta(minutes=1),
    )
    clone = _clone_for(db, source)

    assert receipt["account_scope"] == "alpaca:paper"
    assert receipt["activation_generation"] == authority.activation_generation
    assert receipt["items"][0]["deactivated"] is True
    assert receipt["live_cash_authorized"] is False
    assert clone.is_active is False
    assert source.is_active is True
    assert clone.parent_variant_id == source.id
    assert clone.params_json == source.params_json

    # A fresh generation may safely reuse the inactive, structurally exact clone.
    next_authority = _authority(
        generation="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
        bound_at=_T0 + timedelta(minutes=2),
    )
    next_plan = plan_captured_paper_variant_bindings(
        db, authority=next_authority, source_variant_ids=[source.id]
    )
    assert next_plan.items[0].target_state == "inactive_reusable"
    reapplied = apply_captured_paper_variant_bindings(db, plan=next_plan)
    assert reapplied.items[0].action == "reactivated"
    assert _clone_for(db, source).is_active is True


def test_migration_352_receipt_and_append_only_transition_round_trip(db) -> None:
    from app import migrations

    migration = (
        "352_captured_paper_variant_application_append_only",
        migrations._migration_352_captured_paper_variant_application_append_only,
    )
    assert migration in migrations.MIGRATIONS
    migrations._migration_352_captured_paper_variant_application_append_only(
        db.connection()
    )
    migrations._migration_352_captured_paper_variant_application_append_only(
        db.connection()
    )

    source = _source(db, family="durable_application_receipt")
    authority = _authority(
        generation="abababab-abab-4bab-8bab-abababababab"
    )
    application = apply_captured_paper_variant_bindings(
        db,
        plan=plan_captured_paper_variant_bindings(
            db, authority=authority, source_variant_ids=[source.id]
        ),
    )
    recorded = _record(db, application)
    assert recorded.status == "applied"
    assert_committed_captured_paper_variant_application(
        db,
        application=application,
        activation_manifest_sha256=_MANIFEST_SHA256,
    )
    loaded = load_captured_paper_variant_application_receipt_by_generation(
        db,
        expected_account_id=authority.expected_account_id,
        activation_generation=authority.activation_generation,
        activation_manifest_sha256=_MANIFEST_SHA256,
    )
    assert loaded is not None
    assert loaded.application.to_dict() == application.to_dict()
    assert loaded.version == 1

    rollback_captured_paper_variant_bindings(
        db,
        application=application,
        rolled_back_at=_T0 + timedelta(minutes=1),
    )
    terminal = assert_rolled_back_captured_paper_variant_application(
        db, application=application
    )
    assert terminal.status == "rolled_back"
    assert terminal.version == 2

    with pytest.raises(DBAPIError):
        with db.begin_nested():
            db.execute(
                text(
                    "UPDATE captured_paper_variant_application_receipts "
                    "SET plan_sha256=:forged WHERE id=:receipt_id"
                ),
                {"forged": "f" * 64, "receipt_id": recorded.receipt_id},
            )
    with pytest.raises(DBAPIError):
        with db.begin_nested():
            db.execute(
                text(
                    "UPDATE captured_paper_variant_application_events "
                    "SET detail_canonical_json='{}' WHERE application_id=:receipt_id"
                ),
                {"receipt_id": recorded.receipt_id},
            )


def test_rollback_refuses_any_after_hash_mismatch_without_partial_deactivation(db) -> None:
    first_source = _source(db, family="ema_reclaim_continuation")
    second_source = _source(db, family="vwap_reclaim_continuation", version=4)
    plan = plan_captured_paper_variant_bindings(
        db,
        authority=_authority(),
        source_variant_ids=[first_source.id, second_source.id],
    )
    application = apply_captured_paper_variant_bindings(db, plan=plan)
    _record(db, application)
    first_clone = _clone_for(db, first_source)
    second_clone = _clone_for(db, second_source)
    second_clone.label = "tampered after apply"
    db.flush()

    with pytest.raises(CapturedPaperVariantBindingError) as rejected:
        rollback_captured_paper_variant_bindings(
            db,
            application=application,
            rolled_back_at=_T0 + timedelta(minutes=1),
        )
    assert rejected.value.code == "ROLLBACK_TARGET_DRIFT"
    assert first_clone.is_active is True
    assert second_clone.is_active is True


def test_intended_source_resolution_has_no_paper_only_family_allowlist(db) -> None:
    expected = tuple(iter_momentum_families())
    rows = [
        _source(db, family=family.family_id, version=family.version)
        for family in expected
    ]

    assert resolve_intended_canonical_source_variant_ids(db) == tuple(
        sorted(row.id for row in rows)
    )

    rows[0].is_active = False
    db.flush()
    with pytest.raises(CapturedPaperVariantBindingError) as rejected:
        resolve_intended_canonical_source_variant_ids(db)
    assert rejected.value.code == "SOURCE_TAXONOMY_MISMATCH"


def test_reserved_clone_is_invisible_to_generic_readers_and_mutators(db) -> None:
    source = _source(db, family="reserved_clone_isolation", version=7)
    authority = _authority(
        generation="55555555-5555-4555-8555-555555555555"
    )
    application = apply_captured_paper_variant_bindings(
        db,
        plan=plan_captured_paper_variant_bindings(
            db,
            authority=authority,
            source_variant_ids=[source.id],
        ),
    )
    clone = _clone_for(db, source)

    assert active_variant_for_family(db, source.family).id == source.id
    hot = _hot_variants_by_family_version(
        db,
        [{"family_id": source.family, "family_version": source.version}],
    )
    assert hot[(source.family, source.version)].id == source.id
    assert maybe_kill_underperforming_variant(
        db, variant_id=clone.id
    ) == {"ok": True, "skipped": "reserved_captured_paper_variant"}
    assert maybe_publish_refined_variant(
        db, variant_id=clone.id
    ) == {"ok": True, "skipped": "reserved_captured_paper_variant"}
    assert clone.is_active is True
    assert application.items[0].target_variant_id == clone.id


def test_recovery_deactivates_only_prior_generation_and_retains_exact_current(db) -> None:
    old_source = _source(db, family="old_generation_family")
    old_authority = _authority(
        generation="11111111-1111-4111-8111-111111111111",
        bound_at=_T0 - timedelta(minutes=2),
    )
    old_application = apply_captured_paper_variant_bindings(
        db,
        plan=plan_captured_paper_variant_bindings(
            db,
            authority=old_authority,
            source_variant_ids=[old_source.id],
        ),
    )
    _record(db, old_application)
    old_target_id = old_application.items[0].target_variant_id

    current_source = _source(db, family="current_generation_family")
    current_authority = _authority(
        generation="22222222-2222-4222-8222-222222222222",
        bound_at=_T0,
    )
    current_application = apply_captured_paper_variant_bindings(
        db,
        plan=plan_captured_paper_variant_bindings(
            db,
            authority=current_authority,
            source_variant_ids=[current_source.id],
        ),
    )
    _record(db, current_application)
    current_target_id = current_application.items[0].target_variant_id

    receipt = recover_stale_captured_paper_variant_bindings(
        db,
        authority=current_authority,
        recovered_at=_T0 + timedelta(seconds=1),
    )

    assert [row["target_variant_id"] for row in receipt["recovered"]] == [
        old_target_id
    ]
    assert [
        row["target_variant_id"]
        for row in receipt["retained_current_generation"]
    ] == [current_target_id]
    assert receipt["paper_order_submission_authorized"] is False
    assert receipt["live_cash_authorized"] is False
    assert db.get(MomentumStrategyVariant, old_target_id).is_active is False
    assert db.get(MomentumStrategyVariant, current_target_id).is_active is True
    old_terminal = load_captured_paper_variant_application_receipt_by_generation(
        db,
        expected_account_id=old_authority.expected_account_id,
        activation_generation=old_authority.activation_generation,
        activation_manifest_sha256=_MANIFEST_SHA256,
    )
    current_durable = load_captured_paper_variant_application_receipt_by_generation(
        db,
        expected_account_id=current_authority.expected_account_id,
        activation_generation=current_authority.activation_generation,
        activation_manifest_sha256=_MANIFEST_SHA256,
    )
    assert old_terminal is not None
    assert old_terminal.status == "recovered_stale"
    assert old_terminal.version == 2
    assert current_durable is not None
    assert current_durable.status == "applied"
    assert current_durable.version == 1


def test_recovery_refuses_unreceipted_active_clone_without_deactivation(db) -> None:
    source = _source(db, family="unreceipted_generation_family")
    stale_authority = _authority(
        generation="33333333-3333-4333-8333-333333333333",
        bound_at=_T0 - timedelta(minutes=2),
    )
    application = apply_captured_paper_variant_bindings(
        db,
        plan=plan_captured_paper_variant_bindings(
            db,
            authority=stale_authority,
            source_variant_ids=[source.id],
        ),
    )
    target_id = application.items[0].target_variant_id

    with pytest.raises(CapturedPaperVariantBindingError) as rejected:
        recover_stale_captured_paper_variant_bindings(
            db,
            authority=_authority(
                generation="44444444-4444-4444-8444-444444444444"
            ),
            recovered_at=_T0 + timedelta(seconds=1),
        )

    assert rejected.value.code == "RECOVERY_RECEIPT_UNAVAILABLE"
    assert db.get(MomentumStrategyVariant, target_id).is_active is True
