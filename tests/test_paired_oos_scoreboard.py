from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import inspect
import json

import pytest

from app.services.trading.momentum_neural import paired_oos_scoreboard as oos
from app.services.trading.momentum_neural import replay_v3


UTC = timezone.utc


def h(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def arm(arm_id: str) -> oos.ArmProvenanceV1:
    return oos.ArmProvenanceV1(
        arm_id=arm_id,
        build_sha256=h(f"{arm_id}:build"),
        variant_sha256=h(f"{arm_id}:variant"),
        config_sha256=h(f"{arm_id}:config"),
        feature_flags_sha256=h(f"{arm_id}:flags"),
        model_sha256=h(f"{arm_id}:model"),
        risk_policy_sha256=h(f"{arm_id}:risk"),
    )


def rules(**overrides: object) -> oos.AcceptanceRulesV1:
    values: dict[str, object] = {
        "minimum_paired_sessions": 2,
        "minimum_complete_folds": 1,
        "minimum_candidate_net_pnl_usd": 1.0,
        "minimum_candidate_expectancy_r": 0.1,
        "minimum_candidate_profit_factor": 1.0,
        "minimum_mean_session_net_pnl_delta_usd": 0.0,
        "minimum_mean_session_net_pnl_delta_r": 0.0,
        "minimum_bootstrap_lower_delta_usd": 0.0,
        "minimum_bootstrap_lower_delta_r": 0.0,
        "maximum_candidate_drawdown_r": 2.0,
        "maximum_drawdown_r_increase": 2.0,
        "maximum_candidate_worst_loss_r": 2.0,
        "maximum_worst_loss_r_increase": 2.0,
        "minimum_risk_utilization": 0.05,
        "maximum_risk_utilization": 0.5,
        "maximum_candidate_missed_winners": 0,
        "maximum_missed_winner_delta": 0,
        "maximum_candidate_false_positives": 0,
        "maximum_false_positive_delta": 0,
        "minimum_candidate_mfe_capture_ratio": 0.5,
        "minimum_mfe_capture_ratio_delta": 0.0,
        "maximum_candidate_giveback_fraction": 0.5,
        "maximum_giveback_fraction_delta": 0.0,
        "minimum_candidate_positive_folds": 1,
        "minimum_candidate_superior_folds": 1,
        "minimum_fold_net_pnl_delta_usd": 0.0,
        "confidence_level": 0.9,
        "bootstrap_resamples": 50,
        "random_seed": 731,
        "threshold_provenance_sha256": h("thresholds"),
        "confidence_method_sha256": h("paired-bootstrap"),
        "margin_policy_sha256": h("noninferiority-margins"),
    }
    values.update(overrides)
    return oos.AcceptanceRulesV1(**values)


def study_plan(
    *,
    acceptance_rules: oos.AcceptanceRulesV1 | None = None,
    ross_counts: tuple[int, int, int, int] = (1, 0, 0, 0),
) -> oos.OosStudyPlanV1:
    train = oos.SessionWindowV1("train", datetime(2026, 1, 1, 14, tzinfo=UTC), datetime(2026, 1, 1, 20, tzinfo=UTC))
    embargo = oos.SessionWindowV1("embargo", datetime(2026, 1, 2, 14, tzinfo=UTC), datetime(2026, 1, 2, 20, tzinfo=UTC))
    test_ross = oos.SessionWindowV1("test-ross", datetime(2026, 1, 3, 14, tzinfo=UTC), datetime(2026, 1, 3, 20, tzinfo=UTC))
    test_control = oos.SessionWindowV1("test-control", datetime(2026, 1, 4, 14, tzinfo=UTC), datetime(2026, 1, 4, 20, tzinfo=UTC))
    return oos.OosStudyPlanV1(
        study_id="paired-walk-forward-1",
        baseline_arm=arm("baseline"),
        candidate_arm=arm("candidate"),
        cost_policy=oos.CostPolicyV1(h("cost"), h("fees"), h("slip"), h("ask-bid"), h("cost-source")),
        acceptance_rules=acceptance_rules or rules(),
        session_windows=(train, embargo, test_ross, test_control),
        folds=(oos.OosFoldV1("fold-1", ("train",), ("embargo",), ("test-ross", "test-control")),),
        session_selector_sha256=h("frozen-selector"),
        negative_control_selector_sha256=h("frozen-controls"),
        start_state_policy_sha256=h("same-initial-state"),
        ross_authority_sha256=h(f"ross-authority-{'-'.join(map(str, ross_counts))}"),
        ross_certifiable_count=ross_counts[0],
        ross_diagnostic_only_count=ross_counts[1],
        ross_unavailable_count=ross_counts[2],
        ross_unresolved_count=ross_counts[3],
        label_join_policy_sha256=h("join-after-both-runs"),
        plan_provenance_sha256=h("plan-source"),
    )


def registered(plan: oos.OosStudyPlanV1) -> oos.RegisteredOosStudyPlanV1:
    return oos._register_oos_study_plan(
        plan,
        registration_identity_sha256=h("append-only-registry"),
        registered_at=datetime(2026, 1, 2, 21, tzinfo=UTC),
    )


def certified_replay_result(*, session: str, arm_id: str, release: str | None = None) -> replay_v3.ReplayResult:
    binding = replay_v3.ReplayV3RunBinding(
        identity_sha256=h(f"{session}:capture-identity"),
        final_capture_seal_sha256=h(f"{session}:final-seal"),
        manifest_sha256=h(f"{session}:manifest"),
        release_order_root_sha256=release or h(f"{session}:release-root"),
        decision_checkpoint_sha256=h(f"{session}:{arm_id}:checkpoint"),
        result_trace_sha256=h(f"{session}:{arm_id}:trace"),
        broker_lifecycle_root_sha256=h(f"{session}:{arm_id}:broker"),
        adapter_network_attempt_count=0,
        python_network_attempt_count=0,
        adapter_rejected_provider_request_count=0,
    )
    receipt = replay_v3.ReplayV3ExecutionReceipt(
        binding=binding,
        _verification_token=replay_v3._REPLAY_V3_EXECUTION_RECEIPT_TOKEN,
    )
    attestation = replay_v3.ReplayOsZeroEgressAttestation(
        run_binding_sha256=binding.run_binding_sha256,
        network_namespace="none",
        non_loopback_interfaces=(),
        non_loopback_routes=(),
        blocked_connect_ex=1,
        database_transport="unix_domain_socket",
        adapter_network_attempt_count=0,
        python_network_attempt_count=0,
        _verification_token=replay_v3._REPLAY_OS_ZERO_EGRESS_ATTESTATION_TOKEN,
    )
    return replay_v3.ReplayResult(
        certification_eligible=True,
        certification_failures=[],
        sealed_run_binding=binding,
        sealed_execution_receipt=receipt,
        os_zero_egress_attestation=attestation,
    )


def trade(
    trade_id: str,
    *,
    at: datetime,
    exit_price: float,
    mfe: float,
    symbol: str = "ROSS",
) -> oos.TradeLedgerRowV1:
    return oos.TradeLedgerRowV1(
        trade_id=trade_id,
        symbol=symbol,
        entry_at=at,
        exit_at=at + timedelta(minutes=1),
        quantity=10.0,
        entry_reference_ask=10.0,
        entry_fill_price=10.0,
        exit_reference_bid=exit_price,
        exit_fill_price=exit_price,
        planned_risk_usd=10.0,
        fees_usd=1.0,
        modeled_adverse_slippage_usd=1.0,
        executable_mfe_profit_usd=mfe,
        executable_mae_r=0.5,
        entry_favorable_broker_fill_sha256=None,
        exit_favorable_broker_fill_sha256=None,
        quote_path_through_exit=True,
    )


def ledger(
    plan: oos.OosStudyPlanV1,
    *,
    session_id: str,
    which_arm: str,
    trades: tuple[oos.TradeLedgerRowV1, ...],
    release: str | None = None,
) -> oos.ReplayV3BenchmarkLedgerV1:
    arm_spec = plan.baseline_arm if which_arm == "baseline" else plan.candidate_arm
    window = next(value for value in plan.session_windows if value.session_id == session_id)
    return oos._issue_diagnostic_replay_v3_benchmark_ledger(
        replay_result=certified_replay_result(session=session_id, arm_id=which_arm, release=release),
        session_id=session_id,
        session_starts_at=window.starts_at,
        session_ends_at=window.ends_at,
        arm=arm_spec,
        cost_policy=plan.cost_policy,
        initial_state_policy_sha256=plan.start_state_policy_sha256,
        initial_state_sha256=h(f"{session_id}:initial"),
        complete_session_root_sha256=h(f"{session_id}:complete"),
        decisions_root_sha256=h(f"{session_id}:{which_arm}:decisions"),
        intents_root_sha256=h(f"{session_id}:{which_arm}:intents"),
        risk_root_sha256=h(f"{session_id}:{which_arm}:risk"),
        fills_root_sha256=h(f"{session_id}:{which_arm}:fills"),
        fees_root_sha256=h(f"{session_id}:{which_arm}:fees"),
        equity_root_sha256=h(f"{session_id}:{which_arm}:equity"),
        quote_path_root_sha256=h(f"{session_id}:quote-path"),
        risk_budget_usd=100.0,
        trades=trades,
    )


def labels(plan: oos.OosStudyPlanV1) -> tuple[oos.CertifiedAfterFactLabelV1, ...]:
    ross = next(value for value in plan.session_windows if value.session_id == "test-ross")
    control = next(value for value in plan.session_windows if value.session_id == "test-control")
    return (
        oos._issue_certified_after_fact_label(
            label_id="ross-phase",
            session_id="test-ross",
            symbol="ROSS",
            starts_at=ross.starts_at + timedelta(minutes=5),
            ends_at=ross.starts_at + timedelta(minutes=30),
            expected_action="trade",
            cohort="ross_transferable",
            ross_authority_sha256=plan.ross_authority_sha256,
            label_evidence_sha256=h("ross-label-evidence"),
        ),
        oos._issue_certified_after_fact_label(
            label_id="negative-phase",
            session_id="test-control",
            symbol="CTRL",
            starts_at=control.starts_at + timedelta(minutes=5),
            ends_at=control.starts_at + timedelta(minutes=30),
            expected_action="reject",
            cohort="negative_control",
            ross_authority_sha256=plan.ross_authority_sha256,
            label_evidence_sha256=h("control-label-evidence"),
        ),
    )


def passing_ledgers(plan: oos.OosStudyPlanV1) -> tuple[oos.ReplayV3BenchmarkLedgerV1, ...]:
    ross_at = next(value for value in plan.session_windows if value.session_id == "test-ross").starts_at + timedelta(minutes=10)
    control_at = next(value for value in plan.session_windows if value.session_id == "test-control").starts_at + timedelta(minutes=10)
    return (
        ledger(plan, session_id="test-ross", which_arm="baseline", trades=(trade("b-win", at=ross_at, exit_price=13.0, mfe=100.0), trade("b-loss", at=ross_at + timedelta(minutes=3), exit_price=8.0, mfe=10.0))),
        ledger(plan, session_id="test-ross", which_arm="candidate", trades=(trade("c-win", at=ross_at, exit_price=20.0, mfe=100.0), trade("c-loss", at=ross_at + timedelta(minutes=3), exit_price=9.0, mfe=10.0))),
        ledger(plan, session_id="test-control", which_arm="baseline", trades=(trade("b-false-positive", at=control_at, exit_price=9.0, mfe=10.0, symbol="CTRL"),)),
        ledger(plan, session_id="test-control", which_arm="candidate", trades=()),
    )


def test_current_ross_zero_certified_sessions_is_unavailable_not_zero_performance() -> None:
    plan = study_plan(ross_counts=(0, 4, 2, 6))
    report = oos.build_paired_oos_scoreboard(registered(plan), ledgers=(), labels=())

    assert report.status == "unavailable"
    assert len(report.rows) == 2
    assert all(row.status == "unavailable" for row in report.rows)
    assert report.baseline is None
    assert report.candidate is None
    assert "ross_certifiable_labels_unavailable" in report.global_blockers
    assert "negative_control_labels_unavailable" in report.global_blockers
    assert oos.issue_oos_gate_receipt(report) is None


def test_zero_certified_authority_cannot_be_overridden_by_synthetic_label_rows() -> None:
    plan = study_plan(ross_counts=(0, 4, 2, 6))
    report = oos.build_paired_oos_scoreboard(
        registered(plan), ledgers=passing_ledgers(plan), labels=labels(plan)
    )

    assert all(row.status == "complete" for row in report.rows)
    assert report.status == "unavailable"
    assert "ross_certifiable_labels_unavailable" in report.global_blockers
    assert oos.issue_oos_gate_receipt(report) is None


def test_exact_paired_metrics_are_descriptive_but_cannot_mint_gate() -> None:
    plan = study_plan()
    report = oos.build_paired_oos_scoreboard(
        registered(plan), ledgers=passing_ledgers(plan), labels=labels(plan)
    )

    assert report.status == "unavailable"
    assert report.candidate is not None and report.baseline is not None
    assert report.candidate.trade_count == 2
    assert report.candidate.net_pnl_usd == pytest.approx(86.0)
    assert report.candidate.net_pnl_r == pytest.approx(8.6)
    assert report.candidate.expectancy_r == pytest.approx(4.3)
    assert report.candidate.profit_factor == pytest.approx(98.0 / 12.0)
    assert report.candidate.max_drawdown_usd == pytest.approx(12.0)
    assert report.candidate.max_drawdown_r == pytest.approx(1.2)
    assert report.candidate.mfe_capture_ratio == pytest.approx(98.0 / 110.0)
    assert report.candidate.mfe_giveback_usd == pytest.approx(24.0)
    assert report.candidate.missed_winners == 0
    assert report.candidate.false_positives == 0
    assert report.candidate.risk_utilization == pytest.approx(0.1)
    assert report.mean_session_net_pnl_delta_usd == pytest.approx(46.0)
    assert not report.failed_rules
    assert {
        "append_only_prospective_registration_receipt_unavailable",
        "sealed_replay_v3_counterfactual_ledger_receipt_unavailable",
        "sealed_replay_v3_reproduction_receipt_not_counterfactual_authority",
        "authoritative_after_fact_label_receipt_unavailable",
    }.issubset(report.global_blockers)
    assert all(
        value.evidence_grade == "DIAGNOSTIC_ONLY"
        for value in passing_ledgers(plan)
    )
    assert oos.issue_oos_gate_receipt(report) is None
    json.dumps(report.to_dict(), sort_keys=True)


def test_release_root_mismatch_makes_pair_unavailable_even_with_labels() -> None:
    plan = study_plan()
    values = list(passing_ledgers(plan))
    candidate = values[1]
    ross_at = next(value for value in plan.session_windows if value.session_id == "test-ross").starts_at + timedelta(minutes=10)
    values[1] = ledger(
        plan,
        session_id="test-ross",
        which_arm="candidate",
        release=h("different-release-root"),
        trades=(trade("c-win", at=ross_at, exit_price=20.0, mfe=100.0), trade("c-loss", at=ross_at + timedelta(minutes=3), exit_price=9.0, mfe=10.0)),
    )

    report = oos.build_paired_oos_scoreboard(registered(plan), ledgers=values, labels=labels(plan))

    row = next(value for value in report.rows if value.session_id == "test-ross")
    assert row.status == "unavailable"
    assert "paired_release_order_root_sha256_mismatch" in row.blockers
    assert row.baseline is None and row.candidate is None
    assert report.status == "unavailable"
    assert oos.issue_oos_gate_receipt(report) is None


def test_asymmetric_failure_is_retained_and_never_partially_scored() -> None:
    plan = study_plan()
    values: list[oos.TrustedLedger] = list(passing_ledgers(plan))
    values[3] = oos._issue_unavailable_replay_v3_ledger(
        session_id="test-control",
        arm=plan.candidate_arm,
        blockers=("sealed_capture_incomplete",),
    )

    report = oos.build_paired_oos_scoreboard(registered(plan), ledgers=values, labels=labels(plan))

    assert len(report.rows) == 2
    row = next(value for value in report.rows if value.session_id == "test-control")
    assert row.status == "unavailable"
    assert row.blockers == ("candidate:sealed_capture_incomplete",)
    assert row.baseline is None and row.candidate is None
    assert report.status == "unavailable"


def test_serialized_or_token_stripped_report_has_no_gate_authority() -> None:
    plan = study_plan()
    report = oos.build_paired_oos_scoreboard(registered(plan), ledgers=passing_ledgers(plan), labels=labels(plan))
    serialized = json.loads(json.dumps(report.to_dict()))
    stripped = replace(
        report,
        _registered_plan=None,
        _source_ledgers=(),
        _source_labels=(),
        _authority_token=None,
    )

    assert serialized["status"] == "unavailable"
    assert oos.issue_oos_gate_receipt(stripped) is None


def test_caller_made_ledger_cannot_retain_authority() -> None:
    plan = study_plan()
    values = list(passing_ledgers(plan))
    values[0] = replace(values[0], trades=())
    report = oos.build_paired_oos_scoreboard(
        registered(plan), ledgers=values, labels=labels(plan)
    )

    assert "untrusted_replay_ledger" in report.global_blockers
    assert report.status == "unavailable"
    assert oos.issue_oos_gate_receipt(report) is None


def test_better_than_executable_side_requires_exact_broker_fill_evidence() -> None:
    at = datetime(2026, 1, 3, 15, tzinfo=UTC)
    with pytest.raises(oos.OosScoreboardError, match="entry better than ask"):
        oos.TradeLedgerRowV1(
            "fill", "ROSS", at, at + timedelta(minutes=1), 10.0,
            10.0, 9.99, 11.0, 11.0, 10.0, 1.0, 1.0, 10.0, 0.5,
            None, None, True,
        )
    row = oos.TradeLedgerRowV1(
        "fill", "ROSS", at, at + timedelta(minutes=1), 10.0,
        10.0, 9.99, 11.0, 11.01, 10.0, 1.0, 1.0, 10.0, 0.5,
        h("entry-broker-fill"), h("exit-broker-fill"), True,
    )
    assert row.net_pnl_usd == pytest.approx((11.01 - 9.99) * 10.0 - 2.0)


def test_walk_forward_overlap_and_late_registration_fail_closed() -> None:
    plan = study_plan()
    windows = list(plan.session_windows)
    windows[1] = oos.SessionWindowV1(
        "embargo",
        windows[0].ends_at - timedelta(minutes=1),
        windows[0].ends_at + timedelta(hours=1),
    )
    with pytest.raises(oos.OosScoreboardError, match="train and embargo"):
        replace(plan, session_windows=tuple(windows))
    with pytest.raises(oos.OosScoreboardError, match="before the test frontier"):
        oos._register_oos_study_plan(
            plan,
            registration_identity_sha256=h("late-registry"),
            registered_at=datetime(2026, 1, 3, 14, tzinfo=UTC),
        )


def test_profit_factor_is_undefined_without_losses_and_cannot_pass_gate() -> None:
    plan = study_plan()
    values = list(passing_ledgers(plan))
    ross_at = next(value for value in plan.session_windows if value.session_id == "test-ross").starts_at + timedelta(minutes=10)
    values[1] = ledger(
        plan,
        session_id="test-ross",
        which_arm="candidate",
        trades=(trade("only-win", at=ross_at, exit_price=20.0, mfe=100.0),),
    )
    report = oos.build_paired_oos_scoreboard(registered(plan), ledgers=values, labels=labels(plan))

    assert report.candidate is not None
    assert report.candidate.profit_factor is None
    assert "absolute.profit_factor_defined" in report.failed_rules
    assert report.status == "unavailable"
    assert oos.issue_oos_gate_receipt(report) is None


def test_caller_supplied_roots_backdated_registration_and_labels_never_authorize() -> None:
    plan = study_plan()
    # Every authority-like value in these helpers is syntactically valid and
    # deliberately caller supplied.  Even an otherwise passing scoreboard must
    # disclose the missing owner-minted receipts and remain non-authoritative.
    report = oos.build_paired_oos_scoreboard(
        registered(plan),
        ledgers=passing_ledgers(plan),
        labels=labels(plan),
    )

    assert not report.failed_rules
    assert report.status == "unavailable"
    assert "sealed_replay_v3_counterfactual_ledger_receipt_unavailable" in (
        report.global_blockers
    )
    assert oos.issue_oos_gate_receipt(report) is None


def test_gate_receipt_cannot_be_constructed_before_authority_integration() -> None:
    with pytest.raises(oos.OosScoreboardError, match="authority integration"):
        oos.OosGateReceipt(
            plan_sha256=h("plan"),
            registration_receipt_sha256=h("registration"),
            scoreboard_sha256=h("scoreboard"),
            passed_rules=("synthetic",),
            _authority_token=None,
        )


def test_rules_have_no_numeric_constructor_defaults() -> None:
    signature = inspect.signature(oos.AcceptanceRulesV1)
    assert all(parameter.default is inspect.Parameter.empty for parameter in signature.parameters.values())


def test_uncertified_replay_result_cannot_mint_ledger() -> None:
    plan = study_plan()
    result = certified_replay_result(session="test-ross", arm_id="candidate")
    result.certification_eligible = False
    window = next(value for value in plan.session_windows if value.session_id == "test-ross")
    with pytest.raises(oos.OosScoreboardError, match="certification eligible"):
        oos._issue_diagnostic_replay_v3_benchmark_ledger(
            replay_result=result,
            session_id="test-ross",
            session_starts_at=window.starts_at,
            session_ends_at=window.ends_at,
            arm=plan.candidate_arm,
            cost_policy=plan.cost_policy,
            initial_state_policy_sha256=plan.start_state_policy_sha256,
            initial_state_sha256=h("initial"),
            complete_session_root_sha256=h("complete"),
            decisions_root_sha256=h("decisions"),
            intents_root_sha256=h("intents"),
            risk_root_sha256=h("risk"),
            fills_root_sha256=h("fills"),
            fees_root_sha256=h("fees"),
            equity_root_sha256=h("equity"),
            quote_path_root_sha256=h("quote"),
            risk_budget_usd=100.0,
            trades=(),
        )


def test_reproduction_receipt_cannot_use_authority_like_ledger_issuer() -> None:
    with pytest.raises(
        oos.OosScoreboardError,
        match="reproduction is not counterfactual benchmark authority",
    ):
        oos._issue_replay_v3_benchmark_ledger()
