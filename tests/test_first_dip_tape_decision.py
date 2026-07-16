"""Pure contract tests for the exact-bound first-dip evidence authority."""

from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import pickle
from pathlib import Path

import pytest

import app.services.trading.momentum_neural.first_dip_tape_decision as decision_module
from app.services.trading.momentum_neural.first_dip_tape_decision import (
    FIRST_DIP_TAPE_DECISION_AUTHORITY_SCOPE,
    FIRST_DIP_TAPE_PURPOSE_PRE_RESERVATION,
    FirstDipTapeDecisionProviderError,
    FirstDipTapeDecisionReceipt,
    FirstDipTapeDecisionRequest,
    _first_dip_tape_receipt_binding_mismatch,
    _installed_exact_bound_test_first_dip_tape_decision_authority,
    _make_exact_bound_test_first_dip_tape_decision_authority,
    first_dip_tape_decision_debug,
    resolve_first_dip_tape_decision,
)
from app.services.trading.momentum_neural.first_dip_tape_policy import (
    FirstDipTapeEvaluation,
    FirstDipTapePolicy,
)
from tests.first_dip_test_support import captured_first_dip_detector_authority


DECISION_AT = datetime(2026, 7, 13, 12, 16, 29, 500000, tzinfo=timezone.utc)
POLICY = FirstDipTapePolicy(
    window_seconds=5.0,
    max_source_age_seconds=1.0,
    tick_rate_floor_pctile=0.25,
    minimum_prints=3,
)


def _request(
    *,
    symbol: str = "PLSM",
    decision_at: datetime = DECISION_AT,
    policy: FirstDipTapePolicy = POLICY,
) -> FirstDipTapeDecisionRequest:
    return FirstDipTapeDecisionRequest(
        symbol=symbol,
        decision_at=decision_at,
        policy=policy,
    )


def _typed_result(
    request: FirstDipTapeDecisionRequest,
    *,
    status: str = "valid_positive",
    reason: str | None = None,
    source_hashes: tuple[str, ...] = ("2" * 64, "3" * 64, "4" * 64),
    features: dict[str, float | int] | None = None,
    newest_age: float | None = 0.25,
) -> FirstDipTapeEvaluation:
    positive = status == "valid_positive"
    if features is None and status in {"valid_positive", "valid_negative"}:
        features = {
            "signed_tape_accel": 9.0 if positive else -9.0,
            "tick_rate": 7.0,
            "tick_rate_floor": 4.0,
            "n_ticks": len(source_hashes),
        }
    return FirstDipTapeEvaluation(
        symbol=request.symbol,
        decision_at=request.decision_at,
        read_id="typed-read-1",
        result_sha256="1" * 64,
        source_event_sha256s=source_hashes,
        policy_sha256=request.policy.policy_sha256,
        status=status,
        reason=reason
        or {
            "valid_positive": "first_dip_tape_confirmed",
            "valid_negative": "first_dip_tape_not_confirmed",
            "coverage_unavailable": "first_dip_tape_source_stale",
            "invalid": "first_dip_tape_source_from_future",
        }[status],
        confirmed=positive,
        features=features if status in {"valid_positive", "valid_negative"} else None,
        newest_source_age_seconds=newest_age,
    )


def _empty_negative(request: FirstDipTapeDecisionRequest) -> FirstDipTapeEvaluation:
    return FirstDipTapeEvaluation(
        symbol=request.symbol,
        decision_at=request.decision_at,
        read_id="typed-empty-read",
        result_sha256="1" * 64,
        source_event_sha256s=(),
        policy_sha256=request.policy.policy_sha256,
        status="valid_negative",
        reason="first_dip_tape_no_prints",
        confirmed=False,
        features=None,
        newest_source_age_seconds=None,
    )


def _authority(
    evaluation: FirstDipTapeEvaluation | None = None,
    *,
    request: FirstDipTapeDecisionRequest | None = None,
):
    request = request or _request()
    return _make_exact_bound_test_first_dip_tape_decision_authority(
        request=request,
        evaluation=evaluation or _typed_result(request),
    )


def _resolve(
    *,
    symbol: str = "plsm",
    purpose: str = decision_module.FIRST_DIP_TAPE_PURPOSE_DETECTOR,
):
    return resolve_first_dip_tape_decision(
        symbol=symbol,
        decision_at=DECISION_AT,
        policy=POLICY,
        purpose=purpose,
    )


def _resolve_with(authority):
    with _installed_exact_bound_test_first_dip_tape_decision_authority(authority):
        return _resolve()


def test_exact_bound_test_positive_is_never_runtime_bound_or_execution_authority():
    authority = _authority()
    result = _resolve_with(authority)

    assert result.status == "valid_positive"
    assert result.confirmed is True
    assert result.run_bound is False
    assert result.receipt is authority.receipt
    assert result.receipt.authority_source == "exact_bound_test"
    assert result.receipt.reservation_authority is False
    assert result.receipt.order_authority is False
    assert authority.request.symbol == "PLSM"
    assert authority.request.decision_at == DECISION_AT
    assert authority.request.policy is POLICY
    assert authority.request.authority_scope == FIRST_DIP_TAPE_DECISION_AUTHORITY_SCOPE


@pytest.mark.parametrize(
    "evaluation_factory,expected_status,expected_reason",
    [
        (
            lambda request: _typed_result(request, status="valid_negative"),
            "valid_negative",
            "first_dip_tape_not_confirmed",
        ),
        (
            _empty_negative,
            "valid_negative",
            "first_dip_tape_no_prints",
        ),
        (
            lambda request: _typed_result(
                request,
                status="valid_negative",
                reason="first_dip_tape_insufficient_prints",
                source_hashes=("2" * 64, "3" * 64),
                features={},
            ),
            "valid_negative",
            "first_dip_tape_insufficient_prints",
        ),
        (
            lambda request: _typed_result(
                request,
                status="valid_negative",
                reason="first_dip_tape_features_unavailable",
                features={},
            ),
            "valid_negative",
            "first_dip_tape_features_unavailable",
        ),
        (
            lambda request: _typed_result(
                request,
                status="coverage_unavailable",
                newest_age=POLICY.max_source_age_seconds + 0.001,
            ),
            "coverage_unavailable",
            "first_dip_tape_source_stale",
        ),
    ],
)
def test_exact_empty_thin_full_negative_and_stale_states(
    evaluation_factory,
    expected_status,
    expected_reason,
):
    request = _request()
    evaluation = evaluation_factory(request)
    # An empty dict is an explicit request for no features in this fixture.
    if evaluation.features == {}:
        evaluation = replace(evaluation, features=None)
    result = _resolve_with(_authority(evaluation, request=request))

    assert result.status == expected_status
    assert result.reason == expected_reason
    assert result.confirmed is False
    assert result.run_bound is False


@pytest.mark.parametrize(
    "evaluation",
    [
        _typed_result(_request(), newest_age=None),
        _typed_result(
            _request(),
            status="valid_negative",
            reason="first_dip_tape_no_prints",
        ),
        _typed_result(
            _request(),
            status="coverage_unavailable",
            newest_age=0.25,
        ),
        _typed_result(
            _request(),
            status="valid_negative",
            reason="first_dip_tape_not_confirmed",
            features={
                "signed_tape_accel": 1.0,
                "tick_rate": 7.0,
                "tick_rate_floor": 4.0,
                "n_ticks": 3,
            },
        ),
        _typed_result(
            _request(),
            features={
                "signed_tape_accel": "9.0",
                "tick_rate": "7.0",
                "tick_rate_floor": "4.0",
                "n_ticks": 3,
            },
        ),
        _typed_result(_request(), newest_age="0.25"),
    ],
)
def test_noncanonical_semantics_cannot_be_minted(evaluation):
    with pytest.raises(
        FirstDipTapeDecisionProviderError,
        match="authority evaluation is invalid",
    ):
        _authority(evaluation)


def test_uninstalled_default_and_context_reset_fail_closed_without_fallback():
    first = _resolve()
    installed = _resolve_with(_authority())
    reset = _resolve()

    assert installed.status == "valid_positive"
    assert installed.run_bound is False
    for result in (first, reset):
        assert result.status == "coverage_unavailable"
        assert result.reason == "first_dip_tape_decision_provider_missing"
        assert result.run_bound is False


def test_one_scope_can_ask_only_once_and_lineage_is_cross_context_one_shot():
    authority = _authority()
    with _installed_exact_bound_test_first_dip_tape_decision_authority(authority):
        first = _resolve()
        same_scope = _resolve()
    cross_scope = _resolve_with(authority)

    assert first.status == "valid_positive"
    assert first.run_bound is False
    assert same_scope.reason == "first_dip_tape_decision_provider_already_consumed"
    assert cross_scope.status == "invalid"
    assert cross_scope.reason == "first_dip_tape_decision_receipt_already_consumed"


def test_copied_context_cannot_outlive_revoked_authority_scope():
    authority = _authority()
    with _installed_exact_bound_test_first_dip_tape_decision_authority(authority):
        copied = copy_context()

    revoked = copied.run(_resolve)
    assert revoked.status == "coverage_unavailable"
    assert revoked.reason == "first_dip_tape_decision_provider_scope_revoked"
    assert authority.receipt._lineage.consumed is False

    reusable = _resolve_with(authority)
    assert reusable.status == "valid_positive"


def test_captured_detector_retention_sink_is_one_shot_and_revoked_in_copies():
    request = _request()
    authority = captured_first_dip_detector_authority(request)
    with (
        decision_module
        ._installed_captured_db_paper_first_dip_tape_decision_authority(
            authority
        )
    ):
        resolution = _resolve()
    opportunity = {
        "symbol": request.symbol,
        "trading_date": "2026-07-13",
        "setup_family": "first_dip_reclaim",
    }

    with (
        decision_module
        ._installed_captured_first_dip_detector_retention_provider(
            lambda _resolution, _opportunity: "a" * 64
        )
    ):
        copied = copy_context()
        retained = (
            decision_module
            ._retain_captured_first_dip_detector_for_opportunity(
                resolution,
                opportunity_key=opportunity,
            )
        )
        with pytest.raises(
            FirstDipTapeDecisionProviderError,
            match="already_consumed",
        ):
            copied.run(
                lambda: decision_module
                ._retain_captured_first_dip_detector_for_opportunity(
                    resolution,
                    opportunity_key=opportunity,
                )
            )

    assert retained == "a" * 64
    with pytest.raises(
        FirstDipTapeDecisionProviderError,
        match="scope_revoked",
    ):
        copied.run(
            lambda: decision_module
            ._retain_captured_first_dip_detector_for_opportunity(
                resolution,
                opportunity_key=opportunity,
            )
        )


def test_wrong_request_does_not_consume_lineage_and_correct_request_remains_reusable():
    authority = _authority()
    with _installed_exact_bound_test_first_dip_tape_decision_authority(authority):
        wrong = _resolve(symbol="OTHER")
    assert authority.receipt._lineage.consumed is False
    correct = _resolve_with(authority)

    assert wrong.status == "coverage_unavailable"
    assert wrong.reason == "first_dip_tape_decision_provider_error"
    assert authority.receipt._lineage.consumed is True
    assert correct.status == "valid_positive"


def test_lineage_lock_allows_only_one_concurrent_cross_context_consumer():
    authority = _authority()

    def consume():
        return _resolve_with(authority)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: consume(), range(2)))

    assert sum(result.status == "valid_positive" for result in results) == 1
    assert sum(
        result.reason == "first_dip_tape_decision_receipt_already_consumed"
        for result in results
    ) == 1


def test_receipt_copy_replace_lineage_and_pickle_contract():
    authority = _authority()
    receipt = authority.receipt
    shallow = copy.copy(receipt)
    deep = copy.deepcopy(receipt)
    noop_replace = replace(receipt)

    assert shallow is receipt
    assert deep is receipt
    assert noop_replace is not receipt
    assert noop_replace._lineage is receipt._lineage
    with pytest.raises(
        FirstDipTapeDecisionProviderError,
        match="lineage binding mismatch",
    ):
        replace(receipt, run_id="changed-run")
    with pytest.raises(TypeError, match="cannot be pickled"):
        pickle.dumps(receipt)
    with pytest.raises(TypeError, match="cannot be pickled"):
        pickle.dumps(authority)


def test_receipt_commit_cannot_predate_its_decision_clock():
    authority = _authority()
    before = DECISION_AT - timedelta(microseconds=1)

    with pytest.raises(
        FirstDipTapeDecisionProviderError,
        match="commit escapes its causal interval",
    ):
        replace(
            authority.expected_binding,
            receipt_committed_available_at=before,
        )
    with pytest.raises(
        FirstDipTapeDecisionProviderError,
        match="commit escapes its causal interval",
    ):
        replace(
            authority.receipt,
            receipt_committed_available_at=before,
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("run_id", "different-run"),
        ("authority_source", "sealed_replay"),
        ("generation", 2),
        ("identity_sha256", "f" * 64),
        ("decision_id", "different-decision"),
        ("decision_at", DECISION_AT - timedelta(microseconds=1)),
        ("input_prefix_sequence", 5),
        ("input_prefix_root_sha256", "f" * 64),
        ("decision_checkpoint_sha256", "f" * 64),
        ("final_capture_seal_sha256", "f" * 64),
        ("coverage_manifest_sha256", "f" * 64),
        ("coverage_grade_sha256", "f" * 64),
        ("stream_coverage_sha256", "f" * 64),
        ("read_receipt_sha256", "f" * 64),
        ("receipt_event_sha256", "f" * 64),
        ("receipt_event_sequence", 4),
        ("receipt_committed_available_at", DECISION_AT + timedelta(seconds=0.25)),
        ("source_frontier_sequence", 1),
        ("source_event_inventory_sha256", "f" * 64),
        ("watermark_event_at", DECISION_AT + timedelta(seconds=0.5)),
        ("watermark_emitted_available_at", DECISION_AT + timedelta(seconds=3)),
        ("evaluation_sha256", "f" * 64),
    ],
)
def test_every_active_boundary_binding_field_is_compared(field, value):
    authority = _authority()
    expected = replace(authority.expected_binding, **{field: value})

    assert (
        _first_dip_tape_receipt_binding_mismatch(authority.receipt, expected)
        == field
    )


def test_generic_issuer_and_callable_provider_install_surface_are_absent():
    assert not hasattr(decision_module, "_issue_first_dip_tape_decision_receipt")
    assert not hasattr(decision_module, "installed_first_dip_tape_decision_provider")
    assert not hasattr(decision_module, "FirstDipTapeDecisionProvider")
    with pytest.raises(
        FirstDipTapeDecisionProviderError,
        match="not an exact trusted source",
    ):
        with _installed_exact_bound_test_first_dip_tape_decision_authority(
            lambda request: request
        ):
            pass


def test_authority_issuers_and_installers_have_narrow_app_reachability():
    app_root = Path(decision_module.__file__).resolve().parents[3]
    uses: dict[str, set[str]] = {
        "_FIRST_DIP_TAPE_DECISION_AUTHORITY_ISSUER": set(),
        "_installed_sealed_replay_first_dip_tape_decision_authority": set(),
        "_make_exact_bound_test_first_dip_tape_decision_authority": set(),
        "_installed_exact_bound_test_first_dip_tape_decision_authority": set(),
    }
    for path in app_root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for name in uses:
            if name in text:
                uses[name].add(path.relative_to(app_root).as_posix())

    definition = "services/trading/momentum_neural/first_dip_tape_decision.py"
    replay = "services/trading/momentum_neural/replay_v3.py"
    capture_runtime = "services/trading/momentum_neural/replay_capture_runtime.py"
    assert uses["_FIRST_DIP_TAPE_DECISION_AUTHORITY_ISSUER"] == {
        definition,
        replay,
        capture_runtime,
    }
    assert uses["_installed_sealed_replay_first_dip_tape_decision_authority"] == {
        definition,
        replay,
    }
    assert uses["_make_exact_bound_test_first_dip_tape_decision_authority"] == {
        definition
    }
    assert uses[
        "_installed_exact_bound_test_first_dip_tape_decision_authority"
    ] == {definition}


def test_receipt_has_no_deserializer_and_audit_payload_has_no_secret():
    authority = _authority()
    resolution = _resolve_with(authority)
    receipt = authority.receipt
    audit = first_dip_tape_decision_debug(resolution)

    assert not hasattr(FirstDipTapeDecisionReceipt, "from_dict")
    with pytest.raises(ValueError, match="was not issued by the runtime"):
        replace(receipt, _verification_token=None, _verification_tag="")
    assert audit["run_bound"] is False
    assert audit["decision_receipt"]["authority_source"] == "exact_bound_test"
    assert audit["decision_receipt"]["run_bound"] is False
    assert audit["decision_receipt"]["binding_sha256"] == receipt.binding_sha256
    assert "_verification_tag" not in repr(audit)
    assert "_verification_token" not in repr(audit)


def test_accepted_receipt_features_are_deeply_immutable_and_digest_stable():
    authority = _authority()
    resolution = _resolve_with(authority)
    features = resolution.evaluation.features
    assert features is not None
    binding_before = resolution.receipt.binding_sha256
    debug_before = first_dip_tape_decision_debug(resolution)

    with pytest.raises(TypeError):
        features["n_ticks"] = 999  # type: ignore[index]

    assert resolution.receipt.binding_sha256 == binding_before
    assert first_dip_tape_decision_debug(resolution) == debug_before


def _runtime_authority(source: str):
    """Build a sealed mechanics fixture with a real consumed detector lineage."""

    if source != "sealed_replay":  # captured paper uses the live-runtime tests
        raise AssertionError(source)
    detector_request = _request()
    detector_evaluation = _typed_result(detector_request)
    detector_test = _authority(detector_evaluation, request=detector_request)
    detector_binding = replace(
        detector_test.expected_binding,
        authority_source="sealed_replay",
        run_id="sealed-replay:run-1",
        decision_id="sealed-replay:detector-1",
    )
    issuer = decision_module._FIRST_DIP_TAPE_DECISION_AUTHORITY_ISSUER
    detector_authority = issuer.issue_sealed_replay(
        detector_request,
        detector_binding,
        detector_evaluation,
        lambda: None,
    )
    with decision_module._installed_sealed_replay_first_dip_tape_decision_authority(
        detector_authority
    ):
        detector_resolution = _resolve()
    opportunity = "a" * 64
    prior = decision_module._prior_detector_reference_from_resolution(
        detector_resolution,
        opportunity_key_sha256=opportunity,
    )
    request = replace(
        detector_request,
        purpose=FIRST_DIP_TAPE_PURPOSE_PRE_RESERVATION,
    )
    evaluation = _typed_result(request)
    binding = replace(
        detector_binding,
        purpose=FIRST_DIP_TAPE_PURPOSE_PRE_RESERVATION,
        decision_id="sealed-replay:pre-reservation-1",
        adaptive_request_sha256="b" * 64,
        opportunity_key_sha256=opportunity,
        evaluation_sha256=evaluation.evaluation_sha256,
        prior_detector_reference=prior,
    )
    return issuer.issue_sealed_replay(
        request,
        binding,
        evaluation,
        lambda: None,
    )


def _runtime_resolution_and_envelope(source: str):
    authority = _runtime_authority(source)
    installer = (
        decision_module._installed_sealed_replay_first_dip_tape_decision_authority
        if source == "sealed_replay"
        else decision_module._installed_captured_db_paper_first_dip_tape_decision_authority
    )
    with installer(authority):
        resolution = _resolve(purpose=FIRST_DIP_TAPE_PURPOSE_PRE_RESERVATION)
    envelope = decision_module._prepare_first_dip_final_admission_envelope(
        resolution=resolution,
        execution_surface=source,
    )
    return authority, resolution, envelope


@pytest.mark.parametrize("surface", ["sealed_replay"])
def test_runtime_surfaces_mint_distinct_typed_final_admission_envelopes(surface):
    authority, resolution, envelope = _runtime_resolution_and_envelope(surface)

    final = decision_module._resolve_first_dip_final_admission(
        execution_surface=surface,
        envelope=envelope,
        expected=envelope.expectation,
    )

    assert resolution.run_bound is True
    assert resolution.receipt.authority_source == surface
    assert envelope.expectation.execution_surface == surface
    assert envelope.expectation.binding.to_dict() == (
        authority.expected_binding.to_dict()
    )
    assert final.admitted is True
    assert final.reason == "first_dip_final_admission_typed_receipt_verified"
    assert final.execution_surface == surface
    assert final.envelope_binding_sha256 == envelope.binding_sha256
    assert final.reservation_authority is False
    assert final.order_authority is False
    assert (
        decision_module._verify_first_dip_final_admission_resolution(
            final,
            require_admitted=True,
        )
        is final
    )


def test_replay_envelope_is_rejected_by_captured_db_paper_active_surface():
    _, _, replay_envelope = _runtime_resolution_and_envelope("sealed_replay")

    result = decision_module._resolve_first_dip_final_admission(
        execution_surface="captured_db_paper",
        envelope=replay_envelope,
        expected=replay_envelope.expectation,
    )

    assert result.admitted is False
    assert result.reason == "first_dip_final_admission_active_surface_mismatch"
    assert replay_envelope._lineage.consumed is False


def test_surface_specific_installers_reject_the_other_runtime_authority():
    replay = _runtime_authority("sealed_replay")

    with pytest.raises(
        FirstDipTapeDecisionProviderError,
        match="not an exact trusted source",
    ):
        with decision_module._installed_captured_db_paper_first_dip_tape_decision_authority(
            replay
        ):
            pass


def test_exact_bound_test_authority_cannot_mint_a_runtime_final_envelope():
    resolution = _resolve_with(_authority())

    with pytest.raises(
        FirstDipTapeDecisionProviderError,
        match="execution surface mismatch",
    ):
        decision_module._prepare_first_dip_final_admission_envelope(
            resolution=resolution,
            execution_surface="sealed_replay",
        )


def test_missing_or_serialized_debug_cannot_act_as_final_admission_provider():
    _, resolution, envelope = _runtime_resolution_and_envelope("sealed_replay")
    expected = envelope.expectation

    missing = decision_module._resolve_first_dip_final_admission(
        execution_surface="sealed_replay",
        envelope=None,
        expected=expected,
    )
    serialized = decision_module._resolve_first_dip_final_admission(
        execution_surface="sealed_replay",
        envelope={
            "first_dip_tape_confirmed": True,
            "decision": first_dip_tape_decision_debug(resolution),
            "envelope": envelope.to_audit_dict(),
        },
        expected=expected,
    )

    assert missing.admitted is False
    assert missing.reason == "first_dip_final_admission_provider_missing"
    assert serialized.admitted is False
    assert serialized.reason == "first_dip_final_admission_provider_unbound"
    assert not hasattr(decision_module._FirstDipFinalAdmissionEnvelope, "from_dict")
    assert "_verification_tag" not in repr(envelope.to_audit_dict())
    assert "_verification_token" not in repr(envelope.to_audit_dict())
    assert envelope._lineage.consumed is False


def _with_expected_binding(envelope, **changes):
    prior = envelope.expectation.binding.prior_detector_reference
    if prior is not None:
        prior_changes = {
            name: changes[name]
            for name in ("run_id", "generation", "symbol")
            if name in changes
        }
        if prior_changes:
            changes["prior_detector_reference"] = replace(prior, **prior_changes)
    binding = replace(envelope.expectation.binding, **changes)
    return replace(
        envelope.expectation,
        decision_at=binding.decision_at,
        evaluation_sha256=binding.evaluation_sha256,
        binding=binding,
    )


@pytest.mark.parametrize(
    "expected_mutation",
    [
        lambda envelope: _with_expected_binding(
            envelope,
            run_id="sealed_replay:other-run",
        ),
        lambda envelope: _with_expected_binding(envelope, generation=2),
        lambda envelope: replace(envelope.expectation, symbol="OTHER"),
        lambda envelope: replace(
            envelope.expectation,
            policy_sha256="f" * 64,
        ),
        lambda envelope: _with_expected_binding(
            envelope,
            evaluation_sha256="e" * 64,
        ),
        lambda envelope: _with_expected_binding(
            envelope,
            input_prefix_sequence=(
                envelope.expectation.binding.input_prefix_sequence + 1
            ),
            input_prefix_root_sha256="d" * 64,
        ),
        lambda envelope: _with_expected_binding(
            envelope,
            watermark_event_at=(
                envelope.expectation.binding.watermark_event_at
                + timedelta(microseconds=1)
            ),
            watermark_emitted_available_at=(
                envelope.expectation.binding.watermark_emitted_available_at
                + timedelta(microseconds=1)
            ),
        ),
        lambda envelope: _with_expected_binding(
            envelope,
            decision_at=(
                envelope.expectation.binding.decision_at
                + timedelta(seconds=1)
            ),
            boundary_attested_available_at=(
                envelope.expectation.binding.boundary_attested_available_at
                + timedelta(seconds=1)
            ),
            boundary_expires_at=(
                envelope.expectation.binding.boundary_expires_at
                + timedelta(seconds=1)
            ),
            receipt_committed_available_at=(
                envelope.expectation.binding.receipt_committed_available_at
                + timedelta(seconds=1)
            ),
            watermark_event_at=(
                envelope.expectation.binding.watermark_event_at
                + timedelta(seconds=1)
            ),
            watermark_emitted_available_at=(
                envelope.expectation.binding.watermark_emitted_available_at
                + timedelta(seconds=1)
            ),
        ),
    ],
    ids=[
        "run",
        "generation",
        "symbol",
        "policy",
        "evaluation",
        "capture-prefix",
        "watermark",
        "stale-decision-clock",
    ],
)
def test_final_admission_rejects_every_active_context_mismatch_without_consuming(
    expected_mutation,
):
    _, _, envelope = _runtime_resolution_and_envelope("sealed_replay")
    mismatched = expected_mutation(envelope)

    rejected = decision_module._resolve_first_dip_final_admission(
        execution_surface="sealed_replay",
        envelope=envelope,
        expected=mismatched,
    )
    retry = decision_module._resolve_first_dip_final_admission(
        execution_surface="sealed_replay",
        envelope=envelope,
        expected=envelope.expectation,
    )

    assert rejected.admitted is False
    assert rejected.reason.endswith("_mismatch")
    assert retry.admitted is True


def test_final_envelope_mutation_missing_tag_and_second_use_fail_closed():
    _, _, envelope = _runtime_resolution_and_envelope("sealed_replay")
    untagged = replace(envelope, _verification_tag="")

    unbound = decision_module._resolve_first_dip_final_admission(
        execution_surface="sealed_replay",
        envelope=untagged,
        expected=envelope.expectation,
    )
    accepted = decision_module._resolve_first_dip_final_admission(
        execution_surface="sealed_replay",
        envelope=envelope,
        expected=envelope.expectation,
    )
    reused = decision_module._resolve_first_dip_final_admission(
        execution_surface="sealed_replay",
        envelope=envelope,
        expected=envelope.expectation,
    )

    assert unbound.reason == "first_dip_final_admission_provider_unbound"
    assert accepted.admitted is True
    assert reused.admitted is False
    assert reused.reason == "first_dip_final_admission_envelope_already_consumed"


def test_final_resolution_cannot_be_caller_built_serialized_mutated_or_pickled():
    _, _, envelope = _runtime_resolution_and_envelope("sealed_replay")
    admitted = decision_module._resolve_first_dip_final_admission(
        execution_surface="sealed_replay",
        envelope=envelope,
        expected=envelope.expectation,
    )

    with pytest.raises(
        FirstDipTapeDecisionProviderError,
        match="was not issued by runtime",
    ):
        decision_module._FirstDipFinalAdmissionResolution(
            admitted=True,
            reason="first_dip_final_admission_typed_receipt_verified",
            execution_surface="sealed_replay",
            envelope_binding_sha256="a" * 64,
        )
    with pytest.raises(
        FirstDipTapeDecisionProviderError,
        match="not a runtime resolution",
    ):
        decision_module._verify_first_dip_final_admission_resolution(
            admitted.to_audit_dict(),
            require_admitted=True,
        )
    with pytest.raises(
        FirstDipTapeDecisionProviderError,
        match="(lineage is invalid|verification failed)",
    ):
        replace(admitted, envelope_binding_sha256="b" * 64)
    assert (
        decision_module._verify_first_dip_final_admission_resolution(
            admitted,
            require_admitted=True,
        )
        is admitted
    )
    with pytest.raises(
        FirstDipTapeDecisionProviderError,
        match="already consumed",
    ):
        decision_module._verify_first_dip_final_admission_resolution(
            admitted,
            require_admitted=True,
        )
    with pytest.raises(TypeError, match="cannot be pickled"):
        pickle.dumps(admitted)


def test_only_one_final_envelope_can_be_issued_per_pre_reservation_receipt():
    authority = _runtime_authority("sealed_replay")
    with decision_module._installed_sealed_replay_first_dip_tape_decision_authority(
        authority
    ):
        resolution = _resolve(purpose=FIRST_DIP_TAPE_PURPOSE_PRE_RESERVATION)
    first = decision_module._prepare_first_dip_final_admission_envelope(
        resolution=resolution,
        execution_surface="sealed_replay",
    )

    with pytest.raises(
        FirstDipTapeDecisionProviderError,
        match="already issued",
    ):
        decision_module._prepare_first_dip_final_admission_envelope(
            resolution=resolution,
            execution_surface="sealed_replay",
        )

    assert first._lineage.consumed is False


@pytest.mark.parametrize("status", ["valid_negative", "coverage_unavailable"])
def test_nonpositive_detector_evidence_cannot_mint_final_envelope(status):
    positive_authority = _runtime_authority("sealed_replay")
    request = positive_authority.request
    evaluation = _typed_result(
        request,
        status=status,
        newest_age=(
            POLICY.max_source_age_seconds + 0.001
            if status == "coverage_unavailable"
            else 0.25
        ),
    )
    binding = replace(
        positive_authority.expected_binding,
        evaluation_sha256=evaluation.evaluation_sha256,
    )
    authority = (
        decision_module._FIRST_DIP_TAPE_DECISION_AUTHORITY_ISSUER
        .issue_sealed_replay(request, binding, evaluation, lambda: None)
    )
    with decision_module._installed_sealed_replay_first_dip_tape_decision_authority(
        authority
    ):
        resolution = _resolve(purpose=FIRST_DIP_TAPE_PURPOSE_PRE_RESERVATION)

    with pytest.raises(
        FirstDipTapeDecisionProviderError,
        match="requires pre-reservation positive evidence",
    ):
        decision_module._prepare_first_dip_final_admission_envelope(
            resolution=resolution,
            execution_surface="sealed_replay",
        )
