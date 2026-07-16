from __future__ import annotations

from contextlib import nullcontext
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import uuid
from zoneinfo import ZoneInfo

import pytest

from app.services.trading.momentum_neural import paper_runner
from app.services.trading.momentum_neural.adaptive_risk_policy import (
    RiskInputEvidence,
)
from app.services.trading.momentum_neural.adaptive_risk_request_builder import (
    DbPaperFinalAdmissionMaterial,
    db_paper_admission_component_sha256,
    db_paper_bbo_evidence_payload,
    db_paper_eligibility_evidence_payload,
    db_paper_entry_gate_evidence_payload,
    db_paper_execution_terms_payload,
    db_paper_final_admission_provider,
)
from app.services.trading.momentum_neural.adaptive_risk_reservation import (
    LockedAdaptiveRiskAdmissionSnapshot,
    RESERVATION_LEDGER_GENERATION,
)
from tests.test_adaptive_risk_request_builder import _source


UTC = timezone.utc
ET = ZoneInfo("America/New_York")
ACCOUNT_SCOPE = "db-paper:final-bundle-fixture"
ACCOUNT_IDENTITY_SHA256 = (
    "a843cb478043d11c0d68103ff1726376a045ecbfd6f7dfefb1753ca06191d393"
)
_LOCKED_AGGREGATE_FIELDS = (
    "open_structural_risk_usd",
    "pending_reserved_risk_usd",
    "existing_same_symbol_structural_risk_usd",
    "pending_same_symbol_structural_risk_usd",
    "current_cluster_structural_risk_usd",
    "pending_correlation_cluster_risk_usd",
    "portfolio_gross_notional_usd",
    "pending_portfolio_gross_notional_usd",
    "open_buying_power_impact_usd",
    "pending_buying_power_impact_usd",
)


class _LockedQuery:
    def __init__(self, row):
        self.row = row

    def filter(self, *_args, **_kwargs):
        return self

    def populate_existing(self):
        return self

    def with_for_update(self):
        return self

    def one_or_none(self):
        return self.row


class _LockedDb:
    def __init__(self, row):
        self.row = row

    def query(self, *_args, **_kwargs):
        return _LockedQuery(self.row)

    def get_bind(self):
        return "fixture-bind"

    def in_transaction(self):
        return True

    def connection(self):
        return self

    def begin_nested(self):
        return nullcontext()


def _locked_snapshot(
    *, account_scope, symbol, correlation_cluster, account_snapshot
) -> LockedAdaptiveRiskAdmissionSnapshot:
    return LockedAdaptiveRiskAdmissionSnapshot.create(
        account_scope=account_scope,
        symbol=symbol,
        correlation_cluster=correlation_cluster,
        account_snapshot_sha256=account_snapshot.snapshot_sha256,
        transaction_id="1",
        backend_pid=1,
        lock_receipt_id="00000000-0000-0000-0000-000000000001",
        observed_at=datetime.now(UTC) + timedelta(milliseconds=50),
        aggregates={name: 0.0 for name in _LOCKED_AGGREGATE_FIELDS},
        ledger_payload={
            "schema_version": RESERVATION_LEDGER_GENERATION,
            "account_scope": account_scope,
            "aggregates": {
                name: 0.0 for name in _LOCKED_AGGREGATE_FIELDS
            },
            "symbol": symbol,
            "correlation_cluster": correlation_cluster,
            "active_reservations": [],
            "pending_settlements": [],
            "quarantined_exposures": [],
            "paper_position_bindings": [],
        },
        policy_buying_power_capacity_usd=account_snapshot.buying_power_usd,
    )


def _install_snapshot_store(monkeypatch, captured: dict | None = None) -> None:
    captured = captured if captured is not None else {}

    class _SnapshotStore:
        def __init__(self, bind):
            captured["bind"] = bind

        def lock_admission_snapshot(
            self,
            *,
            account_scope,
            symbol,
            correlation_cluster,
            account_snapshot,
            session,
        ):
            captured["lock"] = {
                "account_scope": account_scope,
                "symbol": symbol,
                "correlation_cluster": correlation_cluster,
                "account_snapshot": account_snapshot,
                "session": session,
            }
            return _locked_snapshot(
                account_scope=account_scope,
                symbol=symbol,
                correlation_cluster=correlation_cluster,
                account_snapshot=account_snapshot,
            )

    monkeypatch.setattr(
        paper_runner, "AdaptiveRiskReservationStore", _SnapshotStore
    )


def _runtime_material(
    boundary: dict,
    *,
    bid: float = 10.18,
    ask: float = 10.20,
    gate_allowed: bool = True,
    gate_reason: str = "all_gates_pass",
    tape_confirmed: bool = True,
) -> DbPaperFinalAdmissionMaterial:
    setup_family = str(
        boundary.get("setup_family") or "momentum_pullback"
    ).strip().lower()
    source = _source(setup_family=setup_family)
    account = replace(
        source.account_snapshot,
        account_scope=ACCOUNT_SCOPE,
        account_identity_sha256=ACCOUNT_IDENTITY_SHA256,
    )
    account_evidence = RiskInputEvidence(
        source=account.source,
        observed_at=account.observed_at,
        available_at=account.available_at,
        content_sha256=account.snapshot_sha256,
        provider_generation=account.provider_generation,
    )
    initial_evidence = dict(source.inputs.evidence)
    initial_evidence["account"] = account_evidence
    initial_evidence["daily_pnl"] = account_evidence
    source = replace(
        source,
        account_snapshot=account,
        account_scope=account.account_scope,
        inputs=replace(
            source.inputs,
            account_identity_sha256=ACCOUNT_IDENTITY_SHA256,
            evidence=initial_evidence,
        ),
    )
    decision_at = boundary["eligibility_available_at"] + timedelta(
        milliseconds=10
    )
    opportunity = {
        "account_scope": source.account_scope,
        "symbol": source.inputs.symbol,
        "trading_date": decision_at.astimezone(ET).date().isoformat(),
        "setup_family": source.setup_family,
    }
    gate_debug = {
        "pullback_low": 9.5,
        "pullback_high": 10.4,
    }
    if setup_family == "first_dip_reclaim":
        gate_debug.update(
            {
                "front_side_via": "first_dip_day_leg",
                "first_dip_tape_confirmed": tape_confirmed,
                "opportunity_key": {
                    key: value
                    for key, value in opportunity.items()
                    if key != "account_scope"
                },
            }
        )
    bbo_observed = decision_at - timedelta(milliseconds=6)
    bbo_available = decision_at - timedelta(milliseconds=5)
    gate_observed = decision_at - timedelta(milliseconds=4)
    gate_available = decision_at - timedelta(milliseconds=3)
    bbo_source = "paper-final-exact-nbbo"
    bbo_generation = "paper-final-bbo-generation-1"
    eligibility_source = "postgresql:locked-viability-read"
    eligibility_generation = "db-paper-session-visibility-1"
    gate_source = "paper-final-entry-gate"
    gate_generation = "paper-final-entry-gate-generation-1"
    # With this fixture's 5% final volatility and structural low, the shared
    # final stop chain recomputes exactly to the structural low.
    structural_stop = 9.5
    bbo_payload = db_paper_bbo_evidence_payload(
        symbol=boundary["symbol"],
        bid=bid,
        ask=ask,
        quote_source=bbo_source,
        observed_at=bbo_observed,
        available_at=bbo_available,
        provider_generation=bbo_generation,
    )
    eligibility_payload = db_paper_eligibility_evidence_payload(
        symbol=boundary["symbol"],
        viability_id=boundary["viability_id"],
        variant_id=boundary["variant_id"],
        viability_score=boundary["viability_score"],
        paper_eligible=boundary["paper_eligible"],
        observed_at=boundary["eligibility_observed_at"],
        available_at=boundary["eligibility_available_at"],
        row_updated_at=boundary["eligibility_row_updated_at"],
        execution_readiness=boundary["execution_readiness"],
        source=eligibility_source,
        provider_generation=eligibility_generation,
    )
    gate_payload = db_paper_entry_gate_evidence_payload(
        symbol=boundary["symbol"],
        allowed=gate_allowed,
        reason=gate_reason,
        debug=gate_debug,
        structural_stop=structural_stop,
        setup_family=source.setup_family,
        opportunity_key=opportunity,
        observed_at=gate_observed,
        available_at=gate_available,
        source=gate_source,
        provider_generation=gate_generation,
    )
    evidence = dict(source.inputs.evidence)
    evidence["bbo"] = RiskInputEvidence(
        source=bbo_source,
        observed_at=bbo_observed,
        available_at=bbo_available,
        content_sha256=db_paper_admission_component_sha256(bbo_payload),
        provider_generation=bbo_generation,
    )
    evidence["paper_eligibility"] = RiskInputEvidence(
        source=eligibility_source,
        observed_at=boundary["eligibility_observed_at"],
        available_at=boundary["eligibility_available_at"],
        content_sha256=db_paper_admission_component_sha256(
            eligibility_payload
        ),
        provider_generation=eligibility_generation,
    )
    evidence["paper_entry_gate"] = RiskInputEvidence(
        source=gate_source,
        observed_at=gate_observed,
        available_at=gate_available,
        content_sha256=db_paper_admission_component_sha256(gate_payload),
        provider_generation=gate_generation,
    )
    slip = float(boundary["entry_slippage_bps"])
    inputs = replace(
        source.inputs,
        as_of=decision_at,
        bid=bid,
        ask=ask,
        structural_stop=structural_stop,
        entry_slippage_bps=slip,
        exit_slippage_bps=float(boundary["exit_slippage_bps"]),
        evidence=evidence,
    )
    source = replace(source, inputs=inputs)
    execution_terms = db_paper_execution_terms_payload(
        effective_config_sha256=source.inputs.effective_config_sha256,
        stop_atr_mult=boundary["stop_atr_mult"],
        target_atr_mult=boundary["target_atr_mult"],
        vol_floor_mult=boundary["vol_floor_mult"],
        reward_risk=boundary["reward_risk"],
        entry_slippage_bps=boundary["entry_slippage_bps"],
        exit_slippage_bps=boundary["exit_slippage_bps"],
        fee_to_target_ratio=boundary["fee_to_target_ratio"],
    )
    return DbPaperFinalAdmissionMaterial.create(
        source,
        quote_source=bbo_source,
        gate_allowed=gate_allowed,
        gate_reason=gate_reason,
        gate_debug=gate_debug,
        opportunity_key=opportunity,
        eligibility=eligibility_payload,
        execution_terms=execution_terms,
    )


def _session_and_viability(
    *,
    setup_family: str = "momentum_pullback",
):
    now = datetime.now(UTC)
    sess = SimpleNamespace(
        id=91,
        symbol="VEEE",
        variant_id=17,
        execution_family="alpaca_spot",
        venue="alpaca",
        risk_snapshot_json={
            "db_paper_account_binding": {
                "account_scope": ACCOUNT_SCOPE,
                "account_identity_sha256": ACCOUNT_IDENTITY_SHA256,
            }
        },
    )
    via = SimpleNamespace(
        id=81,
        symbol="VEEE",
        variant_id=17,
        viability_score=0.91,
        paper_eligible=True,
        freshness_ts=now - timedelta(milliseconds=20),
        updated_at=now - timedelta(milliseconds=10),
        execution_readiness_json={
            "spread_bps": 10.0,
            "slippage_estimate_bps": 10.0,
            "fee_to_target_ratio": 0.08,
        },
    )
    variant = SimpleNamespace(family=setup_family)
    return sess, via, variant


def _pe(*, setup_family: str = "momentum_pullback") -> dict:
    debug = {"pullback_low": 9.5}
    if setup_family == "first_dip_reclaim":
        debug.update(
            {
                "front_side_via": "first_dip_day_leg",
                "first_dip_tape_confirmed": True,
                "opportunity_key": {
                    "symbol": "VEEE",
                    "trading_date": datetime.now(UTC)
                    .astimezone(ET)
                    .date()
                    .isoformat(),
                    "setup_family": "first_dip_reclaim",
                },
            }
        )
    return {"entry_trigger_debug": debug}


def _final_call(db, sess, via, variant, pe):
    return paper_runner._final_revalidate_adaptive_db_paper_entry(
        db,
        sess,
        pe,
        via=via,
        variant=variant,
        stop_atr_mult=0.6,
        target_atr_mult=0.9,
        vol_floor_mult=0.5,
    )


def _reserve_call(db, sess, pe, final, *, final_bundle=None):
    return paper_runner._reserve_adaptive_db_paper_entry(
        db,
        sess,
        pe,
        bid=final["bid"],
        ask=final["ask"],
        entry_price=final["entry_price"],
        structural_stop=final["stop_price"],
        setup_family=final["setup_family"],
        builder_source=final["source"],
        final_observation=final["observation"],
        final_bundle=final_bundle or final["bundle"],
        locked_snapshot=final["locked_snapshot"],
        reference_price=final["mid"],
        target_price=final["target_price"],
        effective_atr=final["effective_atr"],
        fee_ratio=final["fee_ratio"],
    )


def test_final_bundle_replaces_old_tick_quote_and_persists_source(
    monkeypatch,
) -> None:
    sess, via, variant = _session_and_viability()
    pe = _pe()
    captured = {}
    _install_snapshot_store(monkeypatch, captured)
    with db_paper_final_admission_provider(
        lambda **boundary: _runtime_material(
            boundary, bid=10.18, ask=10.20
        )
    ):
        result = _final_call(_LockedDb(via), sess, via, variant, pe)

    assert result["ok"] is True
    assert result["bid"] == 10.18
    assert result["ask"] == 10.20
    assert result["entry_price"] == 10.20 * 1.001
    assert result["entry_price"] != 10.0 * 1.001
    assert result["source"].inputs.as_of == result["decision_at"]
    assert captured["lock"]["account_scope"] == ACCOUNT_SCOPE
    assert captured["lock"]["session"].row is via
    assert pe["adaptive_risk_builder_source"]["source_sha256"] == (
        result["source"].source_sha256
    )
    assert pe["adaptive_risk_final_admission_bundle"]["content_sha256"] == (
        result["bundle"].content_sha256
    )
    assert pe["adaptive_risk_final_observation"]["content_sha256"] == (
        result["observation"].content_sha256
    )


def test_fresh_final_bbo_drives_reservation_request_not_old_tick(
    monkeypatch,
) -> None:
    sess, via, variant = _session_and_viability()
    pe = _pe()
    _install_snapshot_store(monkeypatch)
    with db_paper_final_admission_provider(
        lambda **boundary: _runtime_material(
            boundary, bid=10.18, ask=10.20
        )
    ):
        final = _final_call(_LockedDb(via), sess, via, variant, pe)
    assert final["ok"] is True

    captured = {}
    reservation_id = uuid.UUID("00000000-0000-0000-0000-000000000091")

    class _ReserveStore:
        def __init__(self, bind):
            captured["bind"] = bind

        def reserve(
            self,
            request,
            *,
            session,
            locked_snapshot,
            prepared_resolution,
            prepared_decision_packet,
        ):
            captured["request"] = request
            captured["session"] = session
            captured["locked_snapshot"] = locked_snapshot
            captured["prepared_decision_packet"] = prepared_decision_packet
            return SimpleNamespace(
                admission_accepted=True,
                reservation_id=reservation_id,
                quantity_shares=prepared_resolution.quantity_shares,
                structural_risk_usd=(
                    prepared_resolution.planned_structural_risk_usd
                ),
                gross_notional_usd=prepared_resolution.planned_notional_usd,
                buying_power_impact_usd=(
                    prepared_resolution.planned_buying_power_impact_usd
                ),
                decision_packet_sha256=(
                    prepared_resolution.decision_packet_sha256
                ),
                rejection_reasons=(),
            )

    monkeypatch.setattr(
        paper_runner, "AdaptiveRiskReservationStore", _ReserveStore
    )
    admission = _reserve_call(_LockedDb(via), sess, pe, final)

    assert admission["ok"] is True
    request = captured["request"]
    assert request.inputs.bid == 10.18
    assert request.inputs.ask == 10.20
    assert request.entry_limit_price == 10.20 * 1.001
    assert request.entry_limit_price != 10.0 * 1.001
    assert captured["locked_snapshot"] is final["locked_snapshot"]


def test_final_tape_veto_never_reaches_reservation(monkeypatch) -> None:
    sess, via, variant = _session_and_viability(
        setup_family="first_dip_reclaim"
    )
    calls = {"store": 0}

    class _StoreMustNotRun:
        def __init__(self, *_args, **_kwargs):
            calls["store"] += 1
            raise AssertionError(
                "reservation must not run after a final tape veto"
            )

    monkeypatch.setattr(
        paper_runner, "AdaptiveRiskReservationStore", _StoreMustNotRun
    )
    with db_paper_final_admission_provider(
        lambda **boundary: _runtime_material(
            boundary,
            gate_allowed=False,
            gate_reason="flush_dip_first_dip_tape_not_confirmed",
            tape_confirmed=False,
        )
    ):
        result = _final_call(
            _LockedDb(via),
            sess,
            via,
            variant,
            _pe(setup_family="first_dip_reclaim"),
        )

    assert result["reason"] == "db_paper_final_entry_gate_veto"
    assert calls["store"] == 0


def test_first_dip_db_paper_waits_for_request_bound_second_checkpoint(
    monkeypatch,
) -> None:
    """The old pre-request synthetic envelope must never make this green."""

    sess, via, variant = _session_and_viability(
        setup_family="first_dip_reclaim"
    )
    captured: dict = {}
    # Reading/locking the account snapshot is not a reservation.  The actual
    # reservation boundary is never called after the observation rejects.
    _install_snapshot_store(monkeypatch, captured)
    with db_paper_final_admission_provider(
        lambda **boundary: _runtime_material(boundary)
    ):
        result = _final_call(
            _LockedDb(via),
            sess,
            via,
            variant,
            _pe(setup_family="first_dip_reclaim"),
        )

    assert result["ok"] is False
    assert result["reason"] == "db_paper_final_entry_gate_veto"
    assert result["detail"] == (
        "first_dip_final_admission_active_context_missing"
    )
    assert captured["lock"]["account_scope"] == ACCOUNT_SCOPE


def test_locked_eligibility_refresh_sees_post_candidate_veto() -> None:
    sess, stale_via, variant = _session_and_viability()
    refreshed = SimpleNamespace(**stale_via.__dict__)
    refreshed.paper_eligible = False
    provider_calls = 0

    def _provider(**_kwargs):
        nonlocal provider_calls
        provider_calls += 1
        raise AssertionError(
            "ineligible locked row must veto before capture provider"
        )

    with db_paper_final_admission_provider(_provider):
        result = _final_call(
            _LockedDb(refreshed), sess, stale_via, variant, _pe()
        )

    assert result["reason"] == "db_paper_final_eligibility_veto"
    assert provider_calls == 0


def test_missing_final_bundle_provider_is_explicit_fail_closed() -> None:
    sess, via, variant = _session_and_viability()
    with db_paper_final_admission_provider(None):
        result = _final_call(_LockedDb(via), sess, via, variant, _pe())
    assert result["reason"] == "builder_missing_final_admission_provider"


@pytest.mark.parametrize(
    ("mutation", "expected_detail"),
    (
        ("missing_key", "first_dip_opportunity_key_missing"),
        ("relabelled_key", "first_dip_marker_opportunity_mismatch"),
    ),
)
def test_classified_first_dip_cannot_downgrade_before_final_provider(
    mutation, expected_detail
) -> None:
    sess, via, variant = _session_and_viability(
        setup_family="first_dip_reclaim"
    )
    # The production overlay is not a dedicated first-dip variant.  The
    # structural marker must therefore own the boundary without help from the
    # variant-family fallback.
    variant.family = "momentum_pullback"
    pe = _pe(setup_family="first_dip_reclaim")
    debug = pe["entry_trigger_debug"]
    if mutation == "missing_key":
        debug.pop("opportunity_key")
    else:
        debug["opportunity_key"]["setup_family"] = "momentum_pullback"
    provider_calls = 0

    def _provider(**_kwargs):
        nonlocal provider_calls
        provider_calls += 1
        raise AssertionError("downgraded first dip must not reach capture provider")

    with db_paper_final_admission_provider(_provider):
        result = _final_call(_LockedDb(via), sess, via, variant, pe)

    assert result["ok"] is False
    assert result["reason"] == "adaptive_risk_builder_boundary_mismatch"
    assert result["detail"] == expected_detail
    assert provider_calls == 0


@pytest.mark.parametrize(
    ("binding_field", "wrong_value"),
    (
        ("account_scope", "db-paper:wrong-account"),
        ("account_identity_sha256", "f" * 64),
    ),
)
def test_wrong_final_account_binding_fails_closed_before_risk_lock(
    monkeypatch, binding_field, wrong_value
) -> None:
    sess, via, variant = _session_and_viability()
    sess.risk_snapshot_json["db_paper_account_binding"][
        binding_field
    ] = wrong_value
    calls = {"store": 0}

    class _StoreMustNotRun:
        def __init__(self, *_args, **_kwargs):
            calls["store"] += 1
            raise AssertionError("mismatched account must fail before risk lock")

    monkeypatch.setattr(
        paper_runner, "AdaptiveRiskReservationStore", _StoreMustNotRun
    )
    with db_paper_final_admission_provider(
        lambda **boundary: _runtime_material(boundary)
    ):
        result = _final_call(_LockedDb(via), sess, via, variant, _pe())

    assert result["ok"] is False
    assert result["reason"] == "adaptive_risk_builder_boundary_mismatch"
    assert result["detail"] == binding_field
    assert calls["store"] == 0


def test_mutated_finalized_bundle_fails_rehash_before_reservation(
    monkeypatch,
) -> None:
    sess, via, variant = _session_and_viability()
    pe = _pe()
    _install_snapshot_store(monkeypatch)
    with db_paper_final_admission_provider(
        lambda **boundary: _runtime_material(boundary)
    ):
        final = _final_call(_LockedDb(via), sess, via, variant, pe)
    assert final["ok"] is True

    tampered = replace(
        final["bundle"], gate_reason="mutated_after_finalization"
    )
    calls = {"store": 0}

    class _StoreMustNotRun:
        def __init__(self, *_args, **_kwargs):
            calls["store"] += 1
            raise AssertionError("tampered bundle must fail before reservation")

    monkeypatch.setattr(
        paper_runner, "AdaptiveRiskReservationStore", _StoreMustNotRun
    )
    admission = _reserve_call(
        _LockedDb(via), sess, pe, final, final_bundle=tampered
    )

    assert admission == {
        "ok": False,
        "reason": "db_paper_final_bundle_hash_mismatch",
    }
    assert calls["store"] == 0
