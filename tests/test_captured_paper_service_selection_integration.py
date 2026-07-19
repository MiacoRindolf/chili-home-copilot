from __future__ import annotations

import copy
from datetime import datetime, timezone
from types import SimpleNamespace

from app import db as app_db
from app.config import settings as runtime_settings
from app.services.yf_session import (
    FundamentalsProviderState,
    FundamentalsReceipt,
    FundamentalsReceiptOrigin,
    FundamentalsReceiptStatus,
)
from app.models.trading import (
    BrainGraphNode,
    BrainNodeState,
    MomentumStrategyVariant,
    MomentumSymbolViability,
)
from app.services.trading.momentum_neural import (
    captured_paper_initial_candidate_reader,
    captured_paper_selection_producer,
    captured_paper_selection_queue,
    captured_paper_selection_runtime,
    captured_paper_selection_source,
    captured_paper_variant_binding,
    replay_capture_contract,
    replay_capture_runtime,
    viability,
)
from app.services.trading.momentum_neural.adaptive_risk_policy import (
    build_adaptive_risk_policy_from_settings,
)
from app.services.trading.momentum_neural.context import (
    build_momentum_regime_context,
)
from app.services.trading.momentum_neural.replay_capture_runtime import (
    CaptureBudgetPolicy,
    CaptureResourceBinding,
    CaptureResourceMeasurement,
    SharedCaptureAdmissionBudget,
    SharedCaptureStoreRuntime,
)
from app.services.trading.momentum_neural.variants import (
    iter_momentum_families,
)
from scripts.captured_alpaca_paper_service import (
    _CapturedPaperPolicyAuthority,
    _PreparedCapturedPaperCapture,
    _assemble_service_composition,
)
from scripts.captured_paper_activation_contract import sha256_json


UTC = timezone.utc
ACCOUNT_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
ACTIVATION_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
HUB_NODE_ID = "nm_momentum_crypto_intel"


def _naive(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(tzinfo=None)


def _seed_complete_selection_universe(db, *, now: datetime) -> None:
    context = build_momentum_regime_context(
        now=now,
        atr_pct=0.03,
        meta={
            "ross_scores": {"ACTU": 0.88},
            "ross_signals": {
                "ACTU": {
                    "rvol": 8.0,
                    "daily_change_pct": 32.0,
                    "float_shares": 2_500_000.0,
                    "squeeze_fuel_rank_pct": 0.91,
                }
            },
            "spread_regime": "tight",
            "liquidity_regime": "hot",
            "rolling_range_state": "compression",
            "breakout_continuity": "holding",
        },
    )
    regime = context.to_public_dict()
    correlation_id = "captured-paper-service-integration"
    node = db.get(BrainGraphNode, HUB_NODE_ID)
    if node is None:
        node = BrainGraphNode(
            id=HUB_NODE_ID,
            domain="trading",
            graph_version=1,
            node_type="momentum_intel",
            layer=1,
            label="Momentum viability hub",
            enabled=True,
            created_at=_naive(now),
            updated_at=_naive(now),
        )
        db.add(node)
        db.flush()
    state_payload = {
        "symbols_evaluated": ["ACTU"],
        "last_tick_utc": now.isoformat(),
        "correlation_id": correlation_id,
        "regime": copy.deepcopy(regime),
    }
    state = db.get(BrainNodeState, HUB_NODE_ID)
    if state is None:
        state = BrainNodeState(
            node_id=HUB_NODE_ID,
            activation_score=0.9,
            confidence=0.9,
            local_state=state_payload,
            last_activated_at=_naive(now),
            updated_at=_naive(now),
        )
        db.add(state)
    else:
        state.activation_score = 0.9
        state.confidence = 0.9
        state.local_state = state_payload
        state.last_activated_at = _naive(now)
        state.updated_at = _naive(now)
    for family in iter_momentum_families():
        source = MomentumStrategyVariant(
            family=family.family_id,
            variant_key=family.family_id,
            version=family.version,
            label=family.label,
            params_json={
                "entry_style": family.entry_style,
                "stop_logic": family.default_stop_logic,
                "exit_logic": family.default_exit_logic,
            },
            is_active=True,
            execution_family="alpaca_spot",
            refinement_meta_json={"policy_surface": "replay_and_paper"},
            created_at=_naive(now),
            updated_at=_naive(now),
        )
        db.add(source)
        db.flush()
        db.add(
            MomentumSymbolViability(
                symbol="ACTU",
                scope="symbol",
                variant_id=int(source.id),
                viability_score=0.84,
                paper_eligible=True,
                live_eligible=True,
                freshness_ts=_naive(now),
                regime_snapshot_json=copy.deepcopy(regime),
                execution_readiness_json={
                    "spread_bps": 18.0,
                    "ofi": 0.55,
                    "micro_price_edge": 7.0,
                    "trade_flow": 0.62,
                    "product_tradable": True,
                },
                explain_json={"setup": family.family_id},
                evidence_window_json={"coverage": "derived_snapshot"},
                source_node_id=HUB_NODE_ID,
                correlation_id=correlation_id,
                created_at=_naive(now),
                updated_at=_naive(now),
            )
        )
    db.commit()


def _resource_binding(now: datetime) -> CaptureResourceBinding:
    measurement = CaptureResourceMeasurement(
        measured_at=now,
        sample_seconds=5,
        total_memory_bytes=256_000_000,
        available_memory_bytes=192_000_000,
        disk_free_bytes=2_000_000_000,
        average_cpu_percent=20,
        sustained_append_bytes_per_second=20_000_000,
        fsync_p95_milliseconds=5,
        logical_cpu_count=8,
        host_fingerprint_sha256=sha256_json({"host": "integration"}),
    )
    policy = CaptureBudgetPolicy(
        memory_reserve_bytes=32_000_000,
        disk_reserve_bytes=100_000_000,
        capture_fraction_of_memory_headroom=0.50,
        ring_fraction_of_capture_memory=0.25,
        queue_fraction_of_capture_memory=0.25,
        capture_fraction_of_disk_headroom=0.50,
        capture_fraction_of_measured_write_bandwidth=0.25,
        max_average_cpu_percent=80,
        capture_fraction_of_cpu_headroom=0.90,
        calibrated_hot_symbol_bytes=100_000,
        max_queue_events=256,
        max_ring_events=256,
        max_gap_keys=64,
        raw_retention_days=3,
        derived_retention_days=90,
        pressure_cpu_enter_percent=75,
        pressure_cpu_exit_percent=60,
        pressure_memory_enter_margin_bytes=1_000_000,
        pressure_memory_exit_margin_bytes=2_000_000,
        pressure_disk_enter_margin_bytes=1_000_000,
        pressure_disk_exit_margin_bytes=2_000_000,
        pressure_write_latency_enter_milliseconds=100,
        pressure_write_latency_exit_milliseconds=25,
        pressure_enter_samples=3,
        pressure_recovery_samples=3,
        pressure_sample_max_age_seconds=5,
        store_owner_lease_seconds=60,
        store_owner_heartbeat_seconds=10,
    )
    binding = CaptureResourceBinding.resolve(measurement, policy)
    assert binding.budget.max_writer_threads > 1
    return binding


class _Recorded:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs


class _RuntimeOwner(_Recorded):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.runtime = SimpleNamespace(
            account_scope="alpaca:paper",
            expected_account_id=ACCOUNT_ID,
            runtime_generation=ACTIVATION_ID,
        )


class _InitialController(_Recorded):
    def admit(self, **_kwargs):
        return {"admitted": False}


class _Fence(_Recorded):
    def assert_held(self) -> None:
        return None


class _Managed:
    def __init__(self, *, name, worker):
        self.name = name
        self.worker = worker


class _CommitAckFaultSessionFactory:
    """Raise only after the selected real test transaction has committed."""

    def __init__(self) -> None:
        self.fail_next_commit_ack = False
        self.injected_failures = 0

    def __call__(self):
        session = app_db.SessionLocal()
        original_commit = session.commit

        def commit() -> None:
            original_commit()
            if self.fail_next_commit_ack:
                self.fail_next_commit_ack = False
                self.injected_failures += 1
                raise TimeoutError("synthetic commit acknowledgement loss")

        session.commit = commit  # type: ignore[method-assign]
        return session


def _fake_modules(*, fundamentals_calls: list[str]):
    return {
        "iqfeed_capture_host": SimpleNamespace(
            IqfeedCapturedPaperRuntimeOwner=_RuntimeOwner
        ),
        "captured_paper_transport": SimpleNamespace(
            SqlAlchemyCapturedPaperTransportStore=_Recorded,
            ExactAlpacaPaperEntryTransport=_Recorded,
            CapturedPaperTransportCoordinator=_Recorded,
        ),
        "captured_paper_transport_worker": SimpleNamespace(
            CapturedPaperTransportWorker=_Recorded
        ),
        "captured_paper_post_commit_worker": SimpleNamespace(
            CapturedPaperPostCommitWorker=_Recorded
        ),
        "captured_paper_positive_acceptance": SimpleNamespace(
            SqlAlchemyCapturedPaperPositiveAcceptanceRecorder=_Recorded
        ),
        "captured_paper_fill_capture": SimpleNamespace(
            SqlAlchemyCapturedPaperFillCapture=_Recorded
        ),
        "captured_paper_financial_breaker": SimpleNamespace(
            SqlAlchemyCapturedPaperFinancialBreakerIssuer=_Recorded
        ),
        "captured_paper_initial_candidate_reader": (
            captured_paper_initial_candidate_reader
        ),
        "captured_paper_selection_runtime": captured_paper_selection_runtime,
        "captured_paper_selection_source": captured_paper_selection_source,
        "captured_paper_selection_queue": captured_paper_selection_queue,
        "captured_paper_selection_producer": captured_paper_selection_producer,
        "captured_paper_variant_binding": captured_paper_variant_binding,
        "momentum_viability": viability,
        "replay_capture_contract": replay_capture_contract,
        "replay_capture_runtime": replay_capture_runtime,
        "app_db": app_db,
        "yf_session": SimpleNamespace(
            get_fundamentals_receipt=lambda symbol: (
                fundamentals_calls.append(symbol)
                or FundamentalsReceipt(
                    symbol=symbol,
                    status=FundamentalsReceiptStatus.FRESH_DATA,
                    provider_state=FundamentalsProviderState.AVAILABLE,
                    origin=FundamentalsReceiptOrigin.NETWORK,
                    observed_at=datetime.now(UTC),
                    data={"short_name": "Actuate Therapeutics Inc."},
                    cache_ttl_seconds=300.0,
                )
            )
        ),
        "captured_paper_initial_controller": SimpleNamespace(
            CapturedPaperInitialAdmissionController=_InitialController,
            CapturedPaperInitialControllerPolicy=_Recorded,
        ),
        "captured_paper_fill_watch": SimpleNamespace(
            SqlAlchemyCapturedPaperCompletedFillWatchStore=_Recorded,
            ExactAlpacaPaperCompletedFillWatchReader=_Recorded,
            CapturedPaperCompletedFillWatchCoordinator=_Recorded,
            CapturedPaperCompletedFillWatchWorker=_Recorded,
        ),
        "captured_paper_service_supervisor": SimpleNamespace(
            CapturedPaperManagedWorker=_Managed,
            CapturedPaperServiceSupervisor=_Recorded,
        ),
        "captured_paper_service_fence": SimpleNamespace(
            CapturedPaperServiceFence=_Fence,
        ),
        "live_runner_loop": SimpleNamespace(
            start_captured_paper_live_runner_loop=lambda **_kwargs: True,
            stop_live_runner_loop=lambda: True,
            is_captured_paper_live_runner_loop_admission_ready=(
                lambda **_kwargs: True
            ),
        ),
    }


def test_real_service_selection_lifecycle_primes_reads_and_rolls_back(
    db,
    tmp_path,
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    _seed_complete_selection_universe(db, now=now)
    binding = _resource_binding(now)
    admission = SharedCaptureAdmissionBudget.from_resource_binding(binding)
    shared = SharedCaptureStoreRuntime.create(
        tmp_path / "selection-store",
        resource_binding=binding,
        shared_admission_budget=admission,
    )
    code_body = {"schema_version": "test.service-code.v1", "files": []}
    code_sha = sha256_json(code_body)
    settings_sha = sha256_json(
        {"schema_version": "test.service-settings.v1", "policy": "intended"}
    )
    policy_receipt = build_adaptive_risk_policy_from_settings(runtime_settings)
    policy = policy_receipt.policy
    host = SimpleNamespace(
        composition=SimpleNamespace(binding=binding),
        captured_paper_config_sha256_for=lambda _symbol: settings_sha,
    )
    prepared = _PreparedCapturedPaperCapture(
        preflight=SimpleNamespace(
            startup_process_instance_id=ACTIVATION_ID,
            run_configuration={
                "writer_batch_events": min(64, binding.budget.max_queue_events),
                "writer_batch_bytes": min(
                    1024 * 1024, binding.budget.async_queue_bytes
                ),
                "writer_poll_seconds": 0.001,
                "writer_flush_interval_seconds": 0.001,
            },
        ),
        host=host,
        shared_store=shared,
        adapter=object(),
        broker_snapshot={
            "connection_generation": "alpaca-paper-rest:" + "d" * 64,
            "connection_receipt": {"receipt_sha256": "e" * 64},
        },
        policy_authority=_CapturedPaperPolicyAuthority(
            policy_receipt=policy_receipt,
            policy_spec=object(),
            operational_policy=SimpleNamespace(
                action_claim_lease_seconds=30,
                reconciliation_retry_delay_seconds=5,
            ),
            feature_flags={},
            feature_flags_sha256=policy.policy_sha256,
        ),
    )
    verified = SimpleNamespace(
        expected_account_id=ACCOUNT_ID,
        code_build_sha256=code_sha,
        settings_projection_sha256=settings_sha,
            capture_receipt_sha256="f" * 64,
            activation_generation=ACTIVATION_ID,
            manifest_sha256="9" * 64,
            manifest={
            "code_build": {**code_body, "code_build_sha256": code_sha}
        },
    )
    settings = SimpleNamespace(
        chili_momentum_captured_paper_worker_idle_poll_seconds=0.01,
        chili_autotrader_user_id=41,
        chili_iqfeed_l1_authoritative_bridge_build=(
            "iqfeed-l1-exact-print-provenance-v3+sha256:0123456789abcdef"
        ),
        chili_momentum_captured_paper_trigger_max_attempts=3,
        chili_momentum_captured_paper_trigger_retry_delay_seconds=0.01,
        chili_momentum_captured_paper_trigger_future_tolerance_seconds=1.0,
        chili_momentum_captured_paper_trigger_exact_print_window_seconds=0.001,
        chili_tenbeat_entry_tilt_weight=0.0,
    )
    fundamentals_calls: list[str] = []
    modules = _fake_modules(fundamentals_calls=fundamentals_calls)
    commit_ack_factory = _CommitAckFaultSessionFactory()
    commit_ack_factory.fail_next_commit_ack = True
    modules["app_db"] = SimpleNamespace(SessionLocal=commit_ack_factory)
    composition = _assemble_service_composition(
        verified=verified,
        prepared=prepared,
        phase_one_reconciliation_receipt={"receipt_sha256": "1" * 64},
        restart_inventory_receipt={"receipt_sha256": "2" * 64},
        production_material_factory=object(),
        runtime_modules=modules,
        settings=settings,
        database_engine=app_db.engine,
        assert_external_authority_current=lambda: None,
        acquire_external_dispatch_authority=lambda: None,
        wall_clock=lambda: datetime.now(UTC),
    )
    supervisor_kwargs = composition.supervisor.kwargs
    managed = supervisor_kwargs["active_pre_authority_workers"]
    assert [item.name for item in managed] == ["selection"]
    worker = managed[0].worker
    rollback = None
    try:
        worker.start()
        health = worker.health()
        assert health["ready"] is True
        assert health["fatal"] is False
        assert fundamentals_calls == ["ACTU"]
        read = worker.deferred_reader.read_candidates(
            user_id=41,
            symbol="ACTU",
            decision_at=datetime.now(UTC),
        )
        assert len(read.rows) == len(tuple(iter_momentum_families()))
        assert all(row.viability.paper_eligible for row in read.rows)
        assert all(
            row.variant.variant_key.startswith("captured_paper:")
            for row in read.rows
        )

        worker.close(join_timeout_seconds=5.0)
        commit_ack_factory.fail_next_commit_ack = True
        rollback = supervisor_kwargs["post_quiesce_before_fence_release"]()
        assert rollback["schema_version"] == (
            "chili.captured-paper-post-quiesce.v3"
        )
        assert rollback["strategy_variants_deactivated"] is True
        assert rollback["paper_order_submission_authorized"] is False
        assert rollback["live_cash_authorized"] is False
        assert rollback["real_money_authorized"] is False
        remaining = (
            db.query(MomentumStrategyVariant)
            .filter(
                MomentumStrategyVariant.variant_key.like("captured_paper:%"),
                MomentumStrategyVariant.is_active.is_(True),
            )
            .count()
        )
        assert remaining == 0
        assert commit_ack_factory.injected_failures == 2
    finally:
        worker_health = worker.health()
        if worker_health["running"]:
            worker.close(join_timeout_seconds=5.0)
            worker_health = worker.health()
        if (
            rollback is None
            and worker.application is not None
            and worker_health["quiesced"]
        ):
            supervisor_kwargs["post_quiesce_before_fence_release"]()
        shared.close()
