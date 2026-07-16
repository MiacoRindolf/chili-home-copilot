from __future__ import annotations

from dataclasses import replace
import hashlib
from types import SimpleNamespace
import uuid
from zoneinfo import ZoneInfo

import pytest

from app import models
from app.config import settings
from app.models.trading import BrokerSymbolActionClaim
from app.services.trading.momentum_neural.adaptive_risk_policy import (
    RiskInputEvidence,
    resolve_adaptive_risk,
)
from app.services.trading.momentum_neural.adaptive_risk_runtime_contract import (
    AdaptiveRiskLedgerSnapshot,
    build_adaptive_risk_reservation_claim,
)
from app.services.trading.momentum_neural.adaptive_risk_reservation import (
    AdaptiveRiskReservationRequest,
    ImmutableAccountRiskSnapshot,
    load_adaptive_risk_reservation_request,
)
from app.services.trading.momentum_neural.alpaca_orphan_claims import (
    reserve_alpaca_entry_risk_committed,
)
from app.services.trading.momentum_neural import live_runner
from tests.test_adaptive_risk_policy import _inputs, _policy
from tests.test_alpaca_account_risk_reservations import (
    TEST_ALPACA_ACCOUNT_ID,
    _entry_order_request,
    _session,
    _variant,
)
from tests.test_alpaca_governed_place_bbo import (
    _CertifiedAdapter,
    _alpaca_session,
    _fresh,
    _rail,
    TEST_ALPACA_ACCOUNT_ID as GOVERNED_TEST_ALPACA_ACCOUNT_ID,
)


@pytest.fixture(autouse=True)
def _paper_account(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "chili_alpaca_paper", True, raising=False)
    monkeypatch.setattr(
        settings,
        "chili_alpaca_expected_account_id",
        TEST_ALPACA_ACCOUNT_ID,
        raising=False,
    )


def _packet_and_claim(
    *,
    symbol: str,
    cid: str,
    ledger: AdaptiveRiskLedgerSnapshot,
    pending_risk: float = 0.0,
    pending_gross: float = 0.0,
    pending_bp: float = 0.0,
):
    inputs = _inputs(
        surface="alpaca_paper",
        decision_id=f"decision-{cid}",
        replay_or_paper_run_id=str(uuid.uuid4()),
        symbol=symbol,
        account_identity_sha256=hashlib.sha256(
            TEST_ALPACA_ACCOUNT_ID.encode("utf-8")
        ).hexdigest(),
        correlation_cluster_id=f"equity:{symbol[:1].lower()}",
        pending_reserved_risk_usd=pending_risk,
        pending_portfolio_gross_notional_usd=pending_gross,
        pending_buying_power_impact_usd=pending_bp,
    )
    evidence = dict(inputs.evidence)
    prior = evidence["reservation_ledger"]
    evidence["reservation_ledger"] = RiskInputEvidence(
        source="alpaca_account_advisory_transaction",
        observed_at=prior.observed_at,
        available_at=prior.available_at,
        content_sha256=ledger.content_sha256,
        provider_generation="alpaca-paper-ledger-v1",
    )
    account_evidence = inputs.evidence["account"]
    account = ImmutableAccountRiskSnapshot(
        snapshot_id=f"snapshot-{cid}",
        source=account_evidence.source,
        provider_generation=account_evidence.provider_generation,
        account_scope="alpaca:paper",
        execution_family=inputs.execution_family,
        broker_environment=inputs.broker_environment,
        venue=inputs.venue,
        account_identity_sha256=inputs.account_identity_sha256,
        observed_at=account_evidence.observed_at,
        available_at=account_evidence.available_at,
        equity_usd=inputs.equity_usd,
        buying_power_usd=inputs.buying_power_usd,
        broker_day_change_usd=inputs.broker_day_change_usd,
        local_realized_pnl_usd=inputs.local_realized_pnl_usd,
        pending_policy_buying_power_reflected_usd=0.0,
    )
    exact_account_evidence = RiskInputEvidence(
        source=account.source,
        observed_at=account.observed_at,
        available_at=account.available_at,
        content_sha256=account.snapshot_sha256,
        provider_generation=account.provider_generation,
    )
    evidence["account"] = exact_account_evidence
    evidence["daily_pnl"] = exact_account_evidence
    exact_inputs = replace(inputs, evidence=evidence)
    resolution = resolve_adaptive_risk(_policy(), exact_inputs)
    assert resolution.valid, resolution.rejection_reasons
    packet = resolution.to_decision_packet()
    claim = build_adaptive_risk_reservation_claim(packet, claim_id=cid)
    request = AdaptiveRiskReservationRequest(
        policy=_policy(),
        inputs=exact_inputs,
        account_snapshot=account,
        account_scope="alpaca:paper",
        setup_family="first_dip_reclaim",
        correlation_cluster=inputs.correlation_cluster_id,
        client_order_id=cid,
        entry_limit_price=10.0,
        opportunity_key={
            "account_scope": "alpaca:paper",
            "symbol": inputs.symbol,
            "trading_date": inputs.as_of.astimezone(
                ZoneInfo("America/New_York")
            ).date().isoformat(),
            "setup_family": "first_dip_reclaim",
        },
    )
    return resolution, packet, claim.to_payload(), request.to_payload()


def _run_governed_breaker_case(
    monkeypatch: pytest.MonkeyPatch,
    *,
    breaker_outcomes: list[tuple[bool, str | None]],
    role: str = "primary",
    held_position: bool = False,
    coordinated_release: bool = True,
    transport_started: bool = True,
) -> dict:
    """Drive the real universal Alpaca wrapper with a valid adaptive triplet."""

    ledger = AdaptiveRiskLedgerSnapshot.from_dimensions(
        open_structural_risk_usd=0.0,
        pending_reserved_risk_usd=0.0,
        existing_same_symbol_structural_risk_usd=0.0,
        pending_same_symbol_structural_risk_usd=0.0,
        current_cluster_structural_risk_usd=0.0,
        pending_correlation_cluster_risk_usd=0.0,
        portfolio_gross_notional_usd=0.0,
        pending_portfolio_gross_notional_usd=0.0,
        open_buying_power_impact_usd=0.0,
        pending_buying_power_impact_usd=0.0,
    )
    cid = f"breaker-{role}-{uuid.uuid4().hex[:8]}"
    resolution, packet, claim_payload, request_payload = _packet_and_claim(
        symbol="ACTU",
        cid=cid,
        ledger=ledger,
    )
    loaded_request = load_adaptive_risk_reservation_request(request_payload)
    live_execution = {
        "side_long": True,
        "effective_max_hold_seconds": 3_600,
    }
    if held_position:
        live_execution["position"] = {
            "quantity": 10,
            "avg_entry_price": 9.75,
            "stop_price": 9.50,
        }
    session = _alpaca_session(live_execution=live_execution)
    session.risk_snapshot_json["alpaca_account_id"] = TEST_ALPACA_ACCOUNT_ID
    session.risk_snapshot_json["confirmed_arm_generation"][
        "alpaca_account_id"
    ] = TEST_ALPACA_ACCOUNT_ID
    monkeypatch.setattr(
        settings,
        "chili_alpaca_expected_account_id",
        TEST_ALPACA_ACCOUNT_ID,
        raising=False,
    )
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.market_profile.market_session_now",
        lambda _symbol, now=None: "regular",
    )

    class _AdaptiveAdapter(_CertifiedAdapter):
        def get_account_snapshot(self):
            return {
                "ok": True,
                "paper": True,
                "account_id": TEST_ALPACA_ACCOUNT_ID,
                "equity": 100_000.0,
                "last_equity": 100_000.0,
                "buying_power": 400_000.0,
                "status": "ACTIVE",
            }

    phases: list[str] = []

    def _breaker(_sess, *, phase):
        index = len(phases)
        phases.append(phase)
        allowed, reason = breaker_outcomes[min(index, len(breaker_outcomes) - 1)]
        return allowed, {
            "schema_version": "focused-test-breaker-v1",
            "phase": phase,
            "allowed": allowed,
            "breaker": None if allowed else reason,
            "reason": reason,
            "checks": [],
        }

    monkeypatch.setattr(
        live_runner,
        "_final_alpaca_financial_breaker_admission",
        _breaker,
    )
    monkeypatch.setattr(
        live_runner,
        "_strict_alpaca_owned_entry_posture",
        lambda *_args, **_kwargs: (
            True,
            {"reason": "all_exposure_exactly_chili_owned"},
        ),
    )
    monkeypatch.setattr(
        live_runner,
        "_final_first_dip_adaptive_confirmation",
        lambda *_args, **_kwargs: (
            True,
            {"typed_capture_attestation": "focused-test-only"},
        ),
    )

    reserve_calls: list[dict] = []

    def _reserve_claim(**kwargs):
        reserve_calls.append(dict(kwargs))
        return {
            "ok": True,
            "created": True,
            "claim": {
                "symbol": "ACTU",
                "claim_token": "claim-governed-place-test",
                "account_scope": "alpaca:paper",
                "phase": "claimed",
                "client_order_id": kwargs["client_order_id"],
                "metadata": {
                    "entry_post_bind_token": kwargs["post_bind_token"],
                    "order_request": dict(kwargs["order_request"]),
                },
            },
        }

    reservation_id = uuid.uuid4()
    decision = SimpleNamespace(
        reservation_id=reservation_id,
        quantity_shares=resolution.quantity_shares,
        decision_packet_sha256=resolution.decision_packet_sha256,
    )
    monkeypatch.setattr(
        live_runner,
        "reserve_alpaca_entry_risk_committed",
        _reserve_claim,
    )
    monkeypatch.setattr(
        live_runner,
        "_ensure_adaptive_alpaca_reservation",
        lambda *_args, **_kwargs: {
            "ok": True,
            "decision": decision,
            "request": loaded_request,
            "binding": {"connection_generation": "focused-breaker-generation"},
        },
    )
    monkeypatch.setattr(
        live_runner,
        "update_action_claim_phase_committed",
        lambda **_kwargs: True,
    )

    coordinated_release_calls: list[dict] = []
    refreshed_states: list[object] = []

    release_state = SimpleNamespace(
        reservation_id=reservation_id,
        decision_packet_sha256=resolution.decision_packet_sha256,
        state="released",
        planned_quantity_shares=resolution.quantity_shares,
        cumulative_filled_quantity_shares=0,
        open_quantity_shares=0,
        broker_order_id=None,
        opportunity_status="available",
    )
    monkeypatch.setattr(
        live_runner,
        "release_entry_and_adaptive_reservation_pre_post_committed",
        lambda **kwargs: coordinated_release_calls.append(dict(kwargs))
        or {
            "ok": coordinated_release,
            "confirmed": coordinated_release,
            "adaptive_released": coordinated_release,
            "legacy_released": coordinated_release,
            "reason": kwargs["reason"],
            "reservation_id": kwargs["reservation_id"],
            "reservation_state": (
                "released" if coordinated_release else None
            ),
            "opportunity_status": (
                "available" if coordinated_release else None
            ),
            "release_blocker": (
                None if coordinated_release else "action_claim_transport_state_indeterminate"
            ),
            **({"state": release_state} if coordinated_release else {}),
        },
    )
    monkeypatch.setattr(
        live_runner,
        "_adaptive_alpaca_refresh_binding",
        lambda *_args, state, **_kwargs: refreshed_states.append(state),
    )
    transport_calls: list[dict] = []
    monkeypatch.setattr(
        live_runner,
        "mark_entry_transport_started_committed",
        lambda **kwargs: transport_calls.append(dict(kwargs))
        or transport_started,
    )
    post_calls: list[dict] = []

    result = live_runner._governed_place(
        _AdaptiveAdapter(),
        lambda **kwargs: post_calls.append(dict(kwargs))
        or {
            "ok": True,
            "order_id": "unexpected-post",
            "client_order_id": cid,
            "status": "open",
        },
        sess=session,
        rail_reservation=_rail(),
        execution_bbo_freshness=_fresh(),
        execution_bbo_max_age_seconds=2.0,
        alpaca_order_role=role,
        alpaca_risk_stop_price=9.50,
        alpaca_role_metadata={
            "adaptive_risk_decision_packet": packet,
            "adaptive_risk_reservation_claim": claim_payload,
            live_runner.KEY_ADAPTIVE_RISK_RESERVATION_REQUEST: request_payload,
        },
        product_id="ACTU",
        side="buy",
        position_intent="buy_to_open",
        base_size="5",
        limit_price="10.00",
        client_order_id=cid,
        extended_hours=False,
        time_in_force="day",
    )
    return {
        "result": result,
        "phases": phases,
        "reserve_calls": reserve_calls,
        "coordinated_release_calls": coordinated_release_calls,
        "refreshed_states": refreshed_states,
        "transport_calls": transport_calls,
        "post_calls": post_calls,
        "session": session,
    }


def _reserve(
    *,
    symbol: str,
    owner_id: int,
    cid: str,
    packet: dict,
    claim: dict,
    request: dict,
):
    qty = int(claim["quantity_shares"])
    return reserve_alpaca_entry_risk_committed(
        symbol=symbol,
        claim_token=f"token-{cid}",
        owner_session_id=owner_id,
        client_order_id=cid,
        post_bind_token=f"binder-{cid}",
        order_request=_entry_order_request(symbol, cid, qty=str(qty)),
        order_role="primary",
        reserved_risk_usd=max(0.01, qty * 0.50),
        account_equity_usd=100_000.0,
        account_scope="alpaca:paper",
        # Deliberately hostile legacy values.  A strict adaptive claim must not
        # be truncated by either activation-only dollar ceiling.
        budget_fraction=0.000001,
        per_symbol_cap_usd=1.0,
        role_metadata={
            "alpaca_account_id": TEST_ALPACA_ACCOUNT_ID,
            "adaptive_risk_decision_packet": packet,
            "adaptive_risk_reservation_claim": claim,
            "adaptive_risk_reservation_request": request,
        },
    )


def test_adaptive_claim_bypasses_legacy_dollar_and_serial_caps_atomically(db) -> None:
    first_user = models.User(name=f"adaptive-first-{uuid.uuid4().hex[:8]}")
    second_user = models.User(name=f"adaptive-second-{uuid.uuid4().hex[:8]}")
    db.add_all([first_user, second_user])
    db.flush()
    variant = _variant(db, f"adaptive_reservation_{uuid.uuid4().hex[:8]}")
    first_owner = _session(
        db,
        user_id=first_user.id,
        variant_id=variant.id,
        symbol="ABCD",
        family="alpaca_spot",
        state="live_pending_entry",
        live_execution={"side_long": True},
    )
    second_owner = _session(
        db,
        user_id=second_user.id,
        variant_id=variant.id,
        symbol="WXYZ",
        family="alpaca_spot",
        state="live_pending_entry",
        live_execution={"side_long": True},
    )
    first_owner_id = int(first_owner.id)
    second_owner_id = int(second_owner.id)
    db.commit()

    empty = AdaptiveRiskLedgerSnapshot.from_dimensions(
        open_structural_risk_usd=0.0,
        pending_reserved_risk_usd=0.0,
        existing_same_symbol_structural_risk_usd=0.0,
        pending_same_symbol_structural_risk_usd=0.0,
        current_cluster_structural_risk_usd=0.0,
        pending_correlation_cluster_risk_usd=0.0,
        portfolio_gross_notional_usd=0.0,
        pending_portfolio_gross_notional_usd=0.0,
        open_buying_power_impact_usd=0.0,
        pending_buying_power_impact_usd=0.0,
    )
    first_cid = f"adaptive-first-{uuid.uuid4().hex[:8]}"
    first_resolution, first_packet, first_claim, first_request = _packet_and_claim(
        symbol="ABCD",
        cid=first_cid,
        ledger=empty,
    )
    first = _reserve(
        symbol="ABCD",
        owner_id=first_owner_id,
        cid=first_cid,
        packet=first_packet,
        claim=first_claim,
        request=first_request,
    )
    assert first["ok"] is True, first
    assert first["reserved_risk_usd"] == pytest.approx(
        first_resolution.planned_structural_risk_usd
    )
    assert first["reserved_risk_usd"] > 50.0
    first_retry = _reserve(
        symbol="ABCD",
        owner_id=first_owner_id,
        cid=first_cid,
        packet=first_packet,
        claim=first_claim,
        request=first_request,
    )
    assert first_retry["ok"] is True, first_retry
    assert first_retry["claim"]["client_order_id"] == first_cid

    pending = AdaptiveRiskLedgerSnapshot.from_dimensions(
        open_structural_risk_usd=0.0,
        pending_reserved_risk_usd=first_claim["structural_risk_usd"],
        existing_same_symbol_structural_risk_usd=0.0,
        pending_same_symbol_structural_risk_usd=0.0,
        current_cluster_structural_risk_usd=0.0,
        pending_correlation_cluster_risk_usd=0.0,
        portfolio_gross_notional_usd=0.0,
        pending_portfolio_gross_notional_usd=first_claim["gross_notional_usd"],
        open_buying_power_impact_usd=0.0,
        pending_buying_power_impact_usd=first_claim["buying_power_impact_usd"],
    )
    second_cid = f"adaptive-second-{uuid.uuid4().hex[:8]}"
    second_resolution, second_packet, second_claim, second_request = _packet_and_claim(
        symbol="WXYZ",
        cid=second_cid,
        ledger=pending,
        pending_risk=first_claim["structural_risk_usd"],
        pending_gross=first_claim["gross_notional_usd"],
        pending_bp=first_claim["buying_power_impact_usd"],
    )
    second = _reserve(
        symbol="WXYZ",
        owner_id=second_owner_id,
        cid=second_cid,
        packet=second_packet,
        claim=second_claim,
        request=second_request,
    )
    assert second["ok"] is True, second
    assert second["reserved_risk_usd"] == pytest.approx(
        second_resolution.planned_structural_risk_usd
    )

    rows = (
        db.query(BrokerSymbolActionClaim)
        .filter(BrokerSymbolActionClaim.account_scope == "alpaca:paper")
        .all()
    )
    assert {row.symbol for row in rows} >= {"ABCD", "WXYZ"}
    for row in rows:
        if row.symbol not in {"ABCD", "WXYZ"}:
            continue
        metadata = dict(row.metadata_json or {})
        assert metadata["reserved_gross_notional_usd"] > 0.0
        assert metadata["reserved_buying_power_impact_usd"] > 0.0
        assert metadata["adaptive_risk_reservation_claim"]["claim_sha256"]
        assert metadata["adaptive_risk_reservation_request"]["request_sha256"]


def test_valid_but_unrelated_reservation_request_cannot_complete_triplet(db) -> None:
    ledger = AdaptiveRiskLedgerSnapshot.from_dimensions(
        open_structural_risk_usd=0.0,
        pending_reserved_risk_usd=0.0,
        existing_same_symbol_structural_risk_usd=0.0,
        pending_same_symbol_structural_risk_usd=0.0,
        current_cluster_structural_risk_usd=0.0,
        pending_correlation_cluster_risk_usd=0.0,
        portfolio_gross_notional_usd=0.0,
        pending_portfolio_gross_notional_usd=0.0,
        open_buying_power_impact_usd=0.0,
        pending_buying_power_impact_usd=0.0,
    )
    cid = f"adaptive-mismatched-request-{uuid.uuid4().hex[:8]}"
    _resolution, packet, claim, _request = _packet_and_claim(
        symbol="ACTU",
        cid=cid,
        ledger=ledger,
    )
    changed_ledger = AdaptiveRiskLedgerSnapshot.from_dimensions(
        open_structural_risk_usd=0.0,
        pending_reserved_risk_usd=100.0,
        existing_same_symbol_structural_risk_usd=0.0,
        pending_same_symbol_structural_risk_usd=0.0,
        current_cluster_structural_risk_usd=0.0,
        pending_correlation_cluster_risk_usd=0.0,
        portfolio_gross_notional_usd=0.0,
        pending_portfolio_gross_notional_usd=1_000.0,
        open_buying_power_impact_usd=0.0,
        pending_buying_power_impact_usd=1_000.0,
    )
    _other_resolution, _other_packet, _other_claim, unrelated_request = (
        _packet_and_claim(
            symbol="ACTU",
            cid=cid,
            ledger=changed_ledger,
            pending_risk=100.0,
            pending_gross=1_000.0,
            pending_bp=1_000.0,
        )
    )
    rejected = _reserve(
        symbol="ACTU",
        owner_id=1,
        cid=cid,
        packet=packet,
        claim=claim,
        request=unrelated_request,
    )

    assert rejected["ok"] is False
    assert rejected["reason"] == "adaptive_risk_order_request_mismatch"


@pytest.mark.parametrize(
    "creator_generation_key",
    ("created", "pre_transport_generation_rebound"),
)
def test_alpaca_last_risk_boundary_replaces_legacy_quantity_before_freeze(
    monkeypatch: pytest.MonkeyPatch,
    creator_generation_key: str,
) -> None:
    ledger = AdaptiveRiskLedgerSnapshot.from_dimensions(
        open_structural_risk_usd=0.0,
        pending_reserved_risk_usd=0.0,
        existing_same_symbol_structural_risk_usd=0.0,
        pending_same_symbol_structural_risk_usd=0.0,
        current_cluster_structural_risk_usd=0.0,
        pending_correlation_cluster_risk_usd=0.0,
        portfolio_gross_notional_usd=0.0,
        pending_portfolio_gross_notional_usd=0.0,
        open_buying_power_impact_usd=0.0,
        pending_buying_power_impact_usd=0.0,
    )
    cid = f"adaptive-boundary-{uuid.uuid4().hex[:8]}"
    resolution, packet, claim_payload, request_payload = _packet_and_claim(
        symbol="ACTU",
        cid=cid,
        ledger=ledger,
    )
    assert resolution.quantity_shares != 5
    captured: dict = {}

    def _reserve(**kwargs):
        captured.update(kwargs)
        return {
            "ok": True,
            creator_generation_key: True,
            "claim": {
                "symbol": "ACTU",
                "claim_token": "claim-governed-place-test",
                "account_scope": "alpaca:paper",
                "phase": "claimed",
                "client_order_id": kwargs["client_order_id"],
                "metadata": {
                    "entry_post_bind_token": kwargs["post_bind_token"],
                    "order_request": dict(kwargs["order_request"]),
                },
            },
        }

    monkeypatch.setattr(live_runner, "reserve_alpaca_entry_risk_committed", _reserve)
    loaded_request = load_adaptive_risk_reservation_request(request_payload)
    reservation_id = uuid.uuid4()

    class _Decision:
        quantity_shares = resolution.quantity_shares
        decision_packet_sha256 = resolution.decision_packet_sha256

        def __init__(self) -> None:
            self.reservation_id = reservation_id

    monkeypatch.setattr(
        live_runner,
        "_ensure_adaptive_alpaca_reservation",
        lambda *_args, **_kwargs: {
            "ok": True,
            "decision": _Decision(),
            "request": loaded_request,
            "binding": {"connection_generation": "focused-boundary-generation"},
        },
    )
    monkeypatch.setattr(
        live_runner,
        "update_action_claim_phase_committed",
        lambda **_kwargs: True,
    )
    kwargs = {
        "product_id": "ACTU",
        "side": "buy",
        "position_intent": "buy_to_open",
        # This deliberately represents the obsolete fixed-dollar sizing path.
        "base_size": "5",
        "limit_price": "10.00",
        "client_order_id": cid,
        "extended_hours": False,
        "time_in_force": "day",
    }
    prepared_claim, returned_cid, early = live_runner._prepare_alpaca_place_claim(
        _CertifiedAdapter(),
        _alpaca_session(),
        kwargs,
        order_role="primary",
        role_metadata={
            "adaptive_risk_decision_packet": packet,
            "adaptive_risk_reservation_claim": claim_payload,
            live_runner.KEY_ADAPTIVE_RISK_RESERVATION_REQUEST: request_payload,
        },
        risk_stop_price=9.50,
        account_equity_usd=100_000.0,
    )

    expected_qty = str(resolution.quantity_shares)
    assert early is None
    assert returned_cid == cid
    assert kwargs["base_size"] == expected_qty
    assert captured["order_request"]["base_size"] == expected_qty
    assert captured["role_metadata"]["adaptive_risk_decision_packet"] == packet
    assert captured["role_metadata"]["adaptive_risk_reservation_claim"] == claim_payload
    assert prepared_claim is not None
    assert prepared_claim["_no_transport_proven"] is True
    assert prepared_claim["_post_bind_token"] == captured["post_bind_token"]


def test_adaptive_economics_cross_literal_alpaca_post_without_legacy_re_veto(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the real governed-place seams with hostile legacy dollar settings.

    Durable ledger/retry behavior is covered by the DB tests above.  This focused
    transport test keeps those already-tested commits as deterministic fakes so it
    can prove the remaining call path: canonical packet/claim/request economics are
    reloaded, the obsolete quantity is replaced, the equity-fraction daily budget is
    evaluated, the exact frozen request crosses the literal adapter call unchanged,
    and no $50/$250 activation setting gets a second veto.
    """

    empty = AdaptiveRiskLedgerSnapshot.from_dimensions(
        open_structural_risk_usd=0.0,
        pending_reserved_risk_usd=0.0,
        existing_same_symbol_structural_risk_usd=0.0,
        pending_same_symbol_structural_risk_usd=0.0,
        current_cluster_structural_risk_usd=0.0,
        pending_correlation_cluster_risk_usd=0.0,
        portfolio_gross_notional_usd=0.0,
        pending_portfolio_gross_notional_usd=0.0,
        open_buying_power_impact_usd=0.0,
        pending_buying_power_impact_usd=0.0,
    )
    cid = f"adaptive-post-{uuid.uuid4().hex[:8]}"
    resolution, packet, claim_payload, request_payload = _packet_and_claim(
        symbol="ACTU",
        cid=cid,
        ledger=empty,
    )
    assert resolution.planned_structural_risk_usd > 50.0
    assert resolution.remaining_daily_risk_after_candidate_usd > 250.0

    # These are deliberately hostile obsolete settings.  The content-addressed
    # request policy and its account-wide ledger, not these literals, own the
    # adaptive paper admission below.
    monkeypatch.setattr(
        settings,
        "chili_momentum_risk_max_loss_per_trade_usd",
        50.0,
        raising=False,
    )
    monkeypatch.setattr(
        settings,
        "chili_momentum_risk_max_daily_loss_usd",
        250.0,
        raising=False,
    )
    monkeypatch.setattr(
        settings,
        "chili_global_max_daily_loss_usd",
        250.0,
        raising=False,
    )
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.market_profile.market_session_now",
        lambda _symbol, now=None: "regular",
    )

    session = _alpaca_session()
    session.risk_snapshot_json["alpaca_account_id"] = TEST_ALPACA_ACCOUNT_ID
    session.risk_snapshot_json["confirmed_arm_generation"][
        "alpaca_account_id"
    ] = TEST_ALPACA_ACCOUNT_ID

    class _AdaptivePaperAdapter(_CertifiedAdapter):
        def get_account_snapshot(self):
            return {
                "ok": True,
                "paper": True,
                "account_id": TEST_ALPACA_ACCOUNT_ID,
                "equity": 100_000.0,
                "last_equity": 100_000.0,
                "buying_power": 400_000.0,
                "status": "ACTIVE",
            }

    adapter = _AdaptivePaperAdapter()
    frozen: dict = {}

    def _reserve(**kwargs):
        frozen.update(kwargs)
        return {
            "ok": True,
            "created": True,
            "claim": {
                "symbol": "ACTU",
                "claim_token": "claim-governed-place-test",
                "account_scope": "alpaca:paper",
                "phase": "claimed",
                "client_order_id": kwargs["client_order_id"],
                "metadata": {
                    "entry_post_bind_token": kwargs["post_bind_token"],
                    "order_request": dict(kwargs["order_request"]),
                },
            },
        }

    loaded_request = load_adaptive_risk_reservation_request(request_payload)
    reservation_id = uuid.uuid4()
    decision = SimpleNamespace(
        reservation_id=reservation_id,
        quantity_shares=resolution.quantity_shares,
        decision_packet_sha256=resolution.decision_packet_sha256,
    )
    monkeypatch.setattr(live_runner, "reserve_alpaca_entry_risk_committed", _reserve)
    monkeypatch.setattr(
        live_runner,
        "_ensure_adaptive_alpaca_reservation",
        lambda *_args, **_kwargs: {
            "ok": True,
            "decision": decision,
            "request": loaded_request,
            "binding": {"connection_generation": "focused-post-generation"},
        },
    )
    monkeypatch.setattr(
        live_runner,
        "_strict_alpaca_owned_entry_posture",
        lambda *_args, **_kwargs: (
            True,
            {"reason": "all_exposure_exactly_chili_owned"},
        ),
    )
    # Production remains fail closed until the private two-stage capture API is
    # frozen.  This test-only receipt isolates the already-implemented adaptive
    # financial/order boundary; it is not a caller-trusted production bypass.
    monkeypatch.setattr(
        live_runner,
        "_final_first_dip_adaptive_confirmation",
        lambda *_args, **_kwargs: (
            True,
            {"typed_capture_attestation": "focused-test-only"},
        ),
    )
    breaker_phases: list[str] = []

    def _clean_final_breakers(_sess, *, phase):
        breaker_phases.append(phase)
        return True, {
            "schema_version": "focused-test-breaker-v1",
            "phase": phase,
            "allowed": True,
            "breaker": None,
            "reason": None,
            "checks": [],
        }

    monkeypatch.setattr(
        live_runner,
        "_final_alpaca_financial_breaker_admission",
        _clean_final_breakers,
    )
    monkeypatch.setattr(
        live_runner,
        "update_action_claim_phase_committed",
        lambda **_kwargs: True,
    )
    monkeypatch.setattr(
        live_runner,
        "mark_entry_transport_started_committed",
        lambda **_kwargs: True,
    )
    monkeypatch.setattr(
        live_runner,
        "_sync_adaptive_alpaca_order_lifecycle",
        lambda *_args, **_kwargs: {"ok": True},
    )

    expected_qty = str(resolution.quantity_shares)
    broker_order = SimpleNamespace(
        order_id="oid-adaptive-post",
        client_order_id=cid,
        product_id="ACTU",
        side="buy",
        status="open",
        order_type="limit",
        filled_size=0.0,
        average_filled_price=None,
        raw={
            "qty": expected_qty,
            "limit_price": "10.00",
            "time_in_force": "day",
            "extended_hours": False,
            "position_intent": "buy_to_open",
        },
    )
    monkeypatch.setattr(
        live_runner,
        "_strict_broker_order_id_truth",
        lambda *_args, **_kwargs: ("found", broker_order),
    )

    post_calls: list[dict] = []

    def _post(**kwargs):
        post_calls.append(dict(kwargs))
        return {
            "ok": True,
            "order_id": "oid-adaptive-post",
            "client_order_id": cid,
            "status": "open",
        }

    freshness = _fresh()
    result = live_runner._governed_place(
        adapter,
        _post,
        sess=session,
        rail_reservation=_rail(),
        execution_bbo_freshness=freshness,
        execution_bbo_max_age_seconds=2.0,
        alpaca_order_role="primary",
        alpaca_risk_stop_price=9.50,
        alpaca_role_metadata={
            "adaptive_risk_decision_packet": packet,
            "adaptive_risk_reservation_claim": claim_payload,
            live_runner.KEY_ADAPTIVE_RISK_RESERVATION_REQUEST: request_payload,
        },
        product_id="ACTU",
        side="buy",
        position_intent="buy_to_open",
        base_size="5",
        limit_price="10.00",
        client_order_id=cid,
        extended_hours=False,
        time_in_force="day",
    )

    assert result["ok"] is True
    assert len(post_calls) == 1
    assert post_calls[0]["base_size"] == expected_qty
    assert post_calls[0]["limit_price"] == "10.00"
    assert post_calls[0]["client_order_id"] == cid
    assert frozen["order_request"]["base_size"] == expected_qty
    assert frozen["order_request"] == {
        "account_scope": "alpaca:paper",
        "alpaca_account_id": TEST_ALPACA_ACCOUNT_ID,
        "product_id": "ACTU",
        "side": "buy",
        "base_size": expected_qty,
        "limit_price": "10.00",
        "client_order_id": cid,
        "position_intent": "buy_to_open",
        "order_type": "limit",
        "time_in_force": "day",
        "extended_hours": False,
    }
    daily = frozen["role_metadata"]["broker_daily_loss_admission"]
    assert daily["daily_risk_budget_usd"] > 250.0
    assert daily["captured_economics_match"] is True
    assert frozen["role_metadata"]["adaptive_risk_decision_packet"] == packet
    assert frozen["role_metadata"]["adaptive_risk_reservation_claim"] == claim_payload
    assert breaker_phases == ["pre_reservation", "pre_post"]


def test_final_breaker_flip_before_reservation_never_reserves_or_posts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _run_governed_breaker_case(
        monkeypatch,
        breaker_outcomes=[(False, "profit_giveback")],
    )

    result = case["result"]
    assert result["ok"] is False
    assert result["error"] == "profit_giveback"
    assert result["reservation_created"] is False
    assert case["phases"] == ["pre_reservation"]
    assert case["reserve_calls"] == []
    assert case["coordinated_release_calls"] == []
    assert case["transport_calls"] == []
    assert case["post_calls"] == []


@pytest.mark.parametrize(
    ("coordinated_release", "expected_confirmed"),
    ((True, True), (False, False)),
)
def test_final_breaker_flip_after_reservation_releases_without_transport(
    monkeypatch: pytest.MonkeyPatch,
    coordinated_release: bool,
    expected_confirmed: bool,
) -> None:
    case = _run_governed_breaker_case(
        monkeypatch,
        breaker_outcomes=[(True, None), (False, "green_to_red")],
        coordinated_release=coordinated_release,
    )

    result = case["result"]
    assert result["ok"] is False
    assert result["error"] == "green_to_red"
    assert case["phases"] == ["pre_reservation", "pre_post"]
    assert len(case["reserve_calls"]) == 1
    assert len(case["coordinated_release_calls"]) == 1
    assert result["entry_claim_pre_post_released"] is expected_confirmed
    release = result["entry_claim_pre_post_release"]
    assert release["confirmed"] is expected_confirmed
    assert release["adaptive_released"] is expected_confirmed
    assert release["legacy_released"] is expected_confirmed
    assert release["reason"] == "alpaca_final_breaker_changed_pre_post"
    assert release["reservation_id"]
    assert release.get("snapshot_refreshed") is (
        True if expected_confirmed else None
    )
    assert result["transport_started"] is False
    assert case["transport_calls"] == []
    assert case["post_calls"] == []


@pytest.mark.parametrize("coordinated_release", (True, False))
def test_transport_fence_failure_resolves_or_retains_both_ledgers_without_http(
    monkeypatch: pytest.MonkeyPatch,
    coordinated_release: bool,
) -> None:
    case = _run_governed_breaker_case(
        monkeypatch,
        breaker_outcomes=[(True, None), (True, None)],
        coordinated_release=coordinated_release,
        transport_started=False,
    )

    result = case["result"]
    assert result["ok"] is False
    assert result["error"] == "alpaca_entry_transport_start_fence_failed"
    assert result["pre_place_blocked"] is True
    assert result["transport_started"] is False
    assert result["entry_claim_pre_post_released"] is coordinated_release
    assert result["adaptive_risk_reconciliation_required"] is (
        not coordinated_release
    )
    release = result["entry_claim_pre_post_release"]
    assert release["confirmed"] is coordinated_release
    assert release["adaptive_released"] is coordinated_release
    assert release["legacy_released"] is coordinated_release
    assert release["reason"] == (
        "alpaca_entry_transport_start_fence_not_committed"
    )
    assert len(case["transport_calls"]) == 1
    assert len(case["coordinated_release_calls"]) == 1
    assert case["post_calls"] == []


def test_held_pyramid_with_valid_triplet_cannot_add_after_breaker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _run_governed_breaker_case(
        monkeypatch,
        breaker_outcomes=[(False, "portfolio_dd_breaker")],
        role="pyramid",
        held_position=True,
    )

    result = case["result"]
    assert result["ok"] is False
    assert result["error"] == "portfolio_dd_breaker"
    assert result["reservation_created"] is False
    assert case["reserve_calls"] == []
    assert case["transport_calls"] == []
    assert case["post_calls"] == []


@pytest.mark.parametrize(
    "role",
    ("primary", "repeg", "anticipation", "pyramid", "micro", "pullback", "flag"),
)
def test_governed_place_rejects_every_alpaca_increase_without_triplet(
    role: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Keep config, adapter, and the arm-frozen generation exact so the test
    # reaches the universal exposure-increase choke point without bypassing the
    # production account-identity fence.
    monkeypatch.setattr(
        settings,
        "chili_alpaca_expected_account_id",
        GOVERNED_TEST_ALPACA_ACCOUNT_ID,
        raising=False,
    )
    result = live_runner._governed_place(
        _CertifiedAdapter(),
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("broker transport must remain unreachable")
        ),
        sess=_alpaca_session(),
        alpaca_order_role=role,
        alpaca_risk_stop_price=9.50,
        alpaca_role_metadata={"legacy_add": True},
        product_id="ACTU",
        side="buy",
        position_intent="buy_to_open",
        base_size="1",
        limit_price="10.00",
        client_order_id=f"legacy-add-{uuid.uuid4().hex[:8]}",
        extended_hours=False,
        time_in_force="day",
    )

    assert result["ok"] is False
    assert result["pre_place_blocked"] is True
    assert result["error"] == "builder_missing_capture_binding"
