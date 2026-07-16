"""Account-wide risk reservations for durable Alpaca action claims.

The session snapshot is written by the outer live-runner transaction, while an
Alpaca action claim is committed before the broker call.  Therefore the claim,
not a later session-JSON write, is the crash-safe source of truth for in-flight
risk.  These tests model the two windows that previously fabricated a flat
account: broker acceptance before the owner transaction commits, and concurrent
adds on two different symbols in the same Alpaca account.
"""

from __future__ import annotations

import os
import threading
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

from app import models
from app.config import settings
from app.models.trading import (
    BrokerSymbolActionClaim,
    MomentumStrategyVariant,
    TradingAutomationSession,
)
from app.services.trading.momentum_neural.risk_evaluator import (
    aggregate_open_risk_usd,
    sum_inflight_entry_risk_usd,
)
from app.services.trading.momentum_neural.risk_policy import (
    admit_by_aggregate_risk,
)


TEST_ALPACA_ACCOUNT_ID = "ae7b7443-9a5f-4db2-8e58-9b872f5015cf"


@pytest.fixture(autouse=True)
def _paper_account_scope(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "chili_alpaca_paper", True, raising=False)
    monkeypatch.setattr(
        settings,
        "chili_alpaca_expected_account_id",
        TEST_ALPACA_ACCOUNT_ID,
        raising=False,
    )


def _variant(db, key: str) -> MomentumStrategyVariant:
    variant = MomentumStrategyVariant(
        family="alpaca-account-risk-reservation",
        variant_key=key,
        label=key,
        params_json={},
    )
    db.add(variant)
    db.flush()
    return variant


def _session(
    db,
    *,
    user_id: int,
    variant_id: int,
    symbol: str,
    family: str,
    state: str,
    live_execution: dict | None = None,
) -> TradingAutomationSession:
    session = TradingAutomationSession(
        user_id=user_id,
        venue="alpaca",
        execution_family=family,
        mode="live",
        symbol=symbol,
        variant_id=variant_id,
        state=state,
        risk_snapshot_json={
            "alpaca_account_scope": "alpaca:paper",
            "alpaca_account_id": TEST_ALPACA_ACCOUNT_ID,
            "momentum_live_execution": dict(live_execution or {})
        },
    )
    db.add(session)
    db.flush()
    return session


def _claim(
    db,
    *,
    symbol: str,
    owner_session_id: int,
    token: str,
    cid: str,
    phase: str,
    role: str,
    reserved_risk_usd: float,
    account_scope: str = "alpaca:paper",
) -> BrokerSymbolActionClaim:
    claim = BrokerSymbolActionClaim(
        account_scope=account_scope,
        symbol=symbol,
        claim_token=token,
        action="entry",
        phase=phase,
        owner_session_id=owner_session_id,
        client_order_id=cid,
        broker_order_id=(f"oid-{token}" if phase == "submitted" else None),
        metadata_json={
            "order_role": role,
            "reserved_risk_usd": reserved_risk_usd,
            "alpaca_account_id": TEST_ALPACA_ACCOUNT_ID,
            "order_request": {
                "product_id": symbol,
                "alpaca_account_id": TEST_ALPACA_ACCOUNT_ID,
                "side": "sell" if role == "short" else "buy",
                "base_size": "1",
                "limit_price": "10.00",
                "client_order_id": cid,
                "position_intent": (
                    "sell_to_open" if role == "short" else "buy_to_open"
                ),
                "order_type": "limit",
                "time_in_force": "day",
                "extended_hours": False,
            },
        },
    )
    db.add(claim)
    db.flush()
    return claim


def _entry_order_request(
    symbol: str,
    cid: str,
    *,
    qty: str,
    limit: str = "10.00",
) -> dict:
    return {
        "product_id": symbol,
        "alpaca_account_id": TEST_ALPACA_ACCOUNT_ID,
        "side": "buy",
        "base_size": qty,
        "limit_price": limit,
        "client_order_id": cid,
        "position_intent": "buy_to_open",
        "order_type": "limit",
        "time_in_force": "day",
        "extended_hours": False,
    }


def test_submitted_claim_reserves_risk_before_owner_session_commit(db) -> None:
    user = models.User(name="claim-crash-risk")
    db.add(user)
    db.flush()
    variant = _variant(db, "claim_crash_risk_v1")
    owner = _session(
        db,
        user_id=user.id,
        variant_id=variant.id,
        symbol="CRSH",
        family="alpaca_spot",
        state="live_pending_entry",
        # Simulate rollback/crash of the outer runner transaction: no
        # entry_submitted flag and no entry_inflight_risk_usd survived.
        live_execution={},
    )
    _claim(
        db,
        symbol="CRSH",
        owner_session_id=owner.id,
        token="claim-crash",
        cid="chili-test-claim-crash",
        phase="submitted",
        role="primary",
        reserved_risk_usd=75.0,
    )
    db.flush()

    reserved = sum_inflight_entry_risk_usd(
        db,
        user_id=user.id,
        execution_family="alpaca_spot",
        per_trade_fallback_usd=50.0,
    )

    assert reserved == 75.0


def test_session_and_matching_claim_are_not_double_counted(db) -> None:
    user = models.User(name="claim-no-double-count")
    db.add(user)
    db.flush()
    variant = _variant(db, "claim_no_double_count_v1")
    owner = _session(
        db,
        user_id=user.id,
        variant_id=variant.id,
        symbol="ONCE",
        family="alpaca_spot",
        state="live_pending_entry",
        live_execution={
            "entry_submitted": True,
            "entry_client_order_id": "chili-test-once",
            "entry_inflight_risk_usd": 50.0,
        },
    )
    _claim(
        db,
        symbol="ONCE",
        owner_session_id=owner.id,
        token="claim-once",
        cid="chili-test-once",
        phase="submitted",
        role="primary",
        reserved_risk_usd=50.0,
    )
    db.flush()

    reserved = sum_inflight_entry_risk_usd(
        db,
        user_id=user.id,
        execution_family="alpaca_short",
        per_trade_fallback_usd=50.0,
    )

    assert reserved == 50.0


def test_two_cross_symbol_add_claims_share_one_alpaca_account_envelope(db) -> None:
    long_user = models.User(name="claim-cross-symbol-adds-long")
    second_user = models.User(name="claim-cross-symbol-adds-second")
    db.add_all([long_user, second_user])
    db.flush()
    variant = _variant(db, "claim_cross_symbol_adds_v1")
    long_owner = _session(
        db,
        user_id=long_user.id,
        variant_id=variant.id,
        symbol="LONG",
        family="alpaca_spot",
        state="live_trailing",
        live_execution={
            "side_long": True,
            "position": {
                "quantity": 100,
                "avg_entry_price": 10.0,
                "stop_price": 10.0,
            },
        },
    )
    second_add_owner = _session(
        db,
        user_id=second_user.id,
        variant_id=variant.id,
        symbol="ADD2",
        family="alpaca_spot",
        state="live_trailing",
        live_execution={
            "side_long": True,
            "position": {
                "quantity": 100,
                "avg_entry_price": 10.0,
                "stop_price": 10.0,
            },
        },
    )
    _claim(
        db,
        symbol="LONG",
        owner_session_id=long_owner.id,
        token="claim-long-add",
        cid="chili-test-long-add",
        phase="submit_indeterminate",
        role="pyramid",
        reserved_risk_usd=40.0,
    )
    _claim(
        db,
        symbol="ADD2",
        owner_session_id=second_add_owner.id,
        token="claim-second-add",
        cid="chili-test-second-add",
        phase="submitted",
        role="pullback",
        reserved_risk_usd=45.0,
    )
    # A resolved historical claim and the other account posture must not consume
    # this paper account's live envelope.
    _claim(
        db,
        symbol="DONE",
        owner_session_id=long_owner.id,
        token="claim-resolved",
        cid="chili-test-resolved",
        phase="resolved",
        role="flag",
        reserved_risk_usd=900.0,
    )
    _claim(
        db,
        symbol="LIVE",
        owner_session_id=long_owner.id,
        token="claim-other-posture",
        cid="chili-test-other-posture",
        phase="submitted",
        role="micro",
        reserved_risk_usd=800.0,
        account_scope="alpaca:live",
    )
    db.flush()

    long_view = sum_inflight_entry_risk_usd(
        db,
        user_id=long_user.id,
        execution_family="alpaca_spot",
        per_trade_fallback_usd=50.0,
    )
    short_view = sum_inflight_entry_risk_usd(
        db,
        user_id=second_user.id,
        execution_family="alpaca_short",
        per_trade_fallback_usd=50.0,
    )
    admitted, meta = admit_by_aggregate_risk(
        open_risk_usd=long_view,
        candidate_risk_usd=20.0,
        equity_usd=1_000.0,
        budget_fraction=0.10,
    )

    assert long_view == 85.0
    assert short_view == 85.0
    assert admitted is False
    assert meta["projected_usd"] == 105.0


def test_held_stop_risk_and_same_owner_add_claim_both_count(db) -> None:
    """Owner-level dedupe must not erase incremental add exposure.

    A held position and its pending add are two distinct pieces of risk even
    though both belong to the same automation session.  Only a same-CID legacy
    pending-session mirror may be deduplicated against its durable claim.
    """
    user = models.User(name="claim-held-plus-add")
    db.add(user)
    db.flush()
    variant = _variant(db, "claim_held_plus_add_v1")
    owner = _session(
        db,
        user_id=user.id,
        variant_id=variant.id,
        symbol="STACK",
        family="alpaca_spot",
        state="live_trailing",
        live_execution={
            "side_long": True,
            "position": {
                "quantity": 100,
                "avg_entry_price": 10.0,
                "stop_price": 9.70,
            },
        },
    )
    _claim(
        db,
        symbol="STACK",
        owner_session_id=owner.id,
        token="claim-held-plus-add",
        cid="chili-test-held-plus-add",
        phase="submitted",
        role="pyramid",
        reserved_risk_usd=20.0,
    )
    db.flush()

    held_risk, _ = aggregate_open_risk_usd(
        db,
        user_id=user.id,
        execution_family="alpaca_spot",
    )
    add_risk = sum_inflight_entry_risk_usd(
        db,
        user_id=user.id,
        execution_family="alpaca_spot",
        per_trade_fallback_usd=50.0,
    )

    assert held_risk == pytest.approx(30.0)
    assert add_risk == pytest.approx(20.0)
    assert held_risk + add_risk == pytest.approx(50.0)


def test_any_persisted_position_disables_add_reservation(db) -> None:
    from app.services.trading.momentum_neural import alpaca_orphan_claims

    reserve = getattr(
        alpaca_orphan_claims,
        "reserve_alpaca_entry_risk_committed",
        None,
    )
    assert callable(reserve)
    user = models.User(name="claim-symbol-cap")
    db.add(user)
    db.flush()
    variant = _variant(db, "claim_symbol_cap_v1")
    owner = _session(
        db,
        user_id=user.id,
        variant_id=variant.id,
        symbol="LIMIT",
        family="alpaca_spot",
        state="live_trailing",
        live_execution={
            "side_long": True,
            "position": {
                "quantity": 100,
                "avg_entry_price": 10.0,
                "stop_price": 9.70,
            },
        },
    )
    owner_id = int(owner.id)
    db.commit()

    common = {
        "symbol": "LIMIT",
        "claim_token": f"symbol-cap-{uuid.uuid4().hex[:12]}",
        "owner_session_id": owner_id,
        "order_role": "pyramid",
        "account_equity_usd": 10_000.0,
        "account_scope": "alpaca:paper",
        "budget_fraction": 0.50,
        "per_symbol_cap_usd": 50.0,
    }
    over_cid = f"cid-symbol-over-{uuid.uuid4().hex[:10]}"
    too_large = reserve(
        **common,
        client_order_id=over_cid,
        post_bind_token=f"binder-{over_cid}",
        order_request=_entry_order_request("LIMIT", over_cid, qty="5"),
        reserved_risk_usd=25.0,
    )
    assert too_large["ok"] is False
    assert too_large["reason"] == "account_position_exposure_present"
    assert too_large["position_session_id"] == owner_id

    exact_cid = f"cid-symbol-exact-{uuid.uuid4().hex[:10]}"
    exact_cap = reserve(
        **common,
        client_order_id=exact_cid,
        post_bind_token=f"binder-{exact_cid}",
        order_request=_entry_order_request("LIMIT", exact_cid, qty="4"),
        reserved_risk_usd=20.0,
    )
    assert exact_cap["ok"] is False
    assert exact_cap["reason"] == "account_position_exposure_present"


def test_anticipation_remainder_readmits_after_probe_fill(db) -> None:
    """A filled probe is held risk; its remainder gets no stale plan entitlement."""
    from app.services.trading.momentum_neural import alpaca_orphan_claims

    reserve = getattr(
        alpaca_orphan_claims,
        "reserve_alpaca_entry_risk_committed",
        None,
    )
    assert callable(reserve)
    probe_user = models.User(name="claim-anticipation-probe")
    sibling_user = models.User(name="claim-anticipation-sibling")
    db.add_all([probe_user, sibling_user])
    db.flush()
    variant = _variant(db, "claim_anticipation_readmit_v1")
    probe = _session(
        db,
        user_id=probe_user.id,
        variant_id=variant.id,
        symbol="PROBE",
        family="alpaca_spot",
        state="live_trailing",
        live_execution={
            "side_long": True,
            "anticipation_armed": True,
            "anticipation_remainder_qty": 40.0,
            "position": {
                "quantity": 40,
                "avg_entry_price": 10.0,
                "stop_price": 9.50,
            },
        },
    )
    sibling = _session(
        db,
        user_id=sibling_user.id,
        variant_id=variant.id,
        symbol="OTHER",
        family="alpaca_spot",
        state="live_pending_entry",
    )
    _claim(
        db,
        symbol="OTHER",
        owner_session_id=sibling.id,
        token="claim-account-consumer",
        cid="chili-test-account-consumer",
        phase="submitted",
        role="primary",
        reserved_risk_usd=70.0,
    )
    probe_id = int(probe.id)
    db.commit()

    anticipation_cid = f"cid-anticipation-{uuid.uuid4().hex[:12]}"
    remainder = reserve(
        symbol="PROBE",
        claim_token=f"anticipation-{uuid.uuid4().hex[:12]}",
        owner_session_id=probe_id,
        client_order_id=anticipation_cid,
        post_bind_token=f"binder-{anticipation_cid}",
        order_request=_entry_order_request("PROBE", anticipation_cid, qty="40"),
        order_role="anticipation",
        reserved_risk_usd=20.0,
        account_equity_usd=1_000.0,
        account_scope="alpaca:paper",
        budget_fraction=0.10,
        per_symbol_cap_usd=50.0,
        role_metadata={"anticipation_remainder_qty": 40.0},
    )

    assert remainder["ok"] is False
    assert remainder["reason"] == "account_position_exposure_present"
    assert remainder["position_session_id"] == probe_id
    readable, claim = alpaca_orphan_claims.read_action_claim(
        db,
        symbol="PROBE",
        account_scope="alpaca:paper",
    )
    assert readable is True
    assert claim is None


def test_two_users_share_the_same_alpaca_account_reservation_lock(db) -> None:
    """A short account transaction commits risk and releases before broker HTTP."""
    from app.services.trading.momentum_neural import alpaca_orphan_claims

    lock_key_fn = getattr(
        alpaca_orphan_claims,
        "alpaca_account_risk_lock_key",
        None,
    )
    assert callable(lock_key_fn), "Alpaca account risk needs a stable account-scope lock key"
    reserve = getattr(
        alpaca_orphan_claims,
        "reserve_alpaca_entry_risk_committed",
        None,
    )
    assert callable(reserve), "Alpaca entry risk needs a committed reservation helper"

    first_user = models.User(name=f"alpaca-lock-u1-{uuid.uuid4().hex[:10]}")
    second_user = models.User(name=f"alpaca-lock-u2-{uuid.uuid4().hex[:10]}")
    db.add_all([first_user, second_user])
    db.flush()
    variant = _variant(db, f"alpaca_lock_{uuid.uuid4().hex[:10]}")
    owner = _session(
        db,
        user_id=first_user.id,
        variant_id=variant.id,
        symbol="LOCK1",
        family="alpaca_spot",
        state="live_pending_entry",
    )
    second_owner = _session(
        db,
        user_id=second_user.id,
        variant_id=variant.id,
        symbol="LOCK2",
        family="alpaca_spot",
        state="live_pending_entry",
    )
    owner_id = int(owner.id)
    second_owner_id = int(second_owner.id)
    db.commit()

    scope = "alpaca:paper"
    first_key = int(lock_key_fn(scope))
    second_key = int(lock_key_fn(scope))
    assert first_key == second_key

    post_started = threading.Event()
    release_post = threading.Event()
    worker_result: dict[str, object] = {}

    def _reserve_then_block_in_fake_post() -> None:
        try:
            first_cid = f"cid-lock-one-{uuid.uuid4().hex[:12]}"
            worker_result["reservation"] = reserve(
                symbol="LOCK1",
                claim_token=f"lock-one-{uuid.uuid4().hex[:12]}",
                owner_session_id=owner_id,
                client_order_id=first_cid,
                post_bind_token=f"binder-{first_cid}",
                order_request=_entry_order_request("LOCK1", first_cid, qty="4"),
                order_role="primary",
                reserved_risk_usd=40.0,
                account_equity_usd=1_000.0,
                account_scope=scope,
                budget_fraction=0.06,
                per_symbol_cap_usd=50.0,
            )
            if not dict(worker_result["reservation"]).get("ok"):
                return
            # This is the broker-POST seam.  It deliberately blocks after the
            # helper returned; no DB/advisory transaction may remain held here.
            post_started.set()
            release_post.wait(timeout=10.0)
        except BaseException as exc:  # surfaced in the parent test thread
            worker_result["error"] = exc

    worker = threading.Thread(target=_reserve_then_block_in_fake_post, daemon=True)
    worker.start()
    assert post_started.wait(timeout=10.0), worker_result

    url = os.environ.get("TEST_DATABASE_URL")
    assert url
    engine = create_engine(url, poolclass=NullPool)
    try:
        # While the fake broker POST is still blocked, an independent DB
        # transaction can acquire the SAME account lock and see the first
        # reservation.  This fails if the implementation holds the lock over HTTP.
        with engine.begin() as conn:
            acquired_during_post = bool(
                conn.execute(
                    text("SELECT pg_try_advisory_xact_lock(:key)"),
                    {"key": second_key},
                ).scalar()
            )
            observed = float(
                conn.execute(
                    text(
                        "SELECT COALESCE(SUM((metadata_json->>'reserved_risk_usd')::float), 0) "
                        "FROM broker_symbol_action_claims "
                        "WHERE account_scope = :scope AND phase <> 'resolved'"
                    ),
                    {"scope": scope},
                ).scalar()
                or 0.0
            )
        assert acquired_during_post is True
        assert observed == 40.0

        # User two shares the broker account even though CHILI user ids differ.
        # The helper must see user one's committed claim and serialize the account;
        # both workers may have observed a flat broker before the first reservation.
        second_cid = f"cid-lock-two-{uuid.uuid4().hex[:12]}"
        second = reserve(
            symbol="LOCK2",
            claim_token=f"lock-two-{uuid.uuid4().hex[:12]}",
            owner_session_id=second_owner_id,
            client_order_id=second_cid,
            post_bind_token=f"binder-{second_cid}",
            order_request=_entry_order_request("LOCK2", second_cid, qty="3"),
            order_role="primary",
            reserved_risk_usd=30.0,
            account_equity_usd=1_000.0,
            account_scope=scope,
            budget_fraction=0.06,
            per_symbol_cap_usd=50.0,
        )
        assert second["ok"] is False
        assert second["reason"] == "account_entry_claim_present"
    finally:
        release_post.set()
        worker.join(timeout=10.0)
        engine.dispose()
    assert not worker.is_alive()
    assert "error" not in worker_result
    assert dict(worker_result["reservation"])["ok"] is True


def test_direct_cap_override_cannot_raise_hard_fifty_dollar_ceiling(db) -> None:
    from app.services.trading.momentum_neural.alpaca_orphan_claims import (
        reserve_alpaca_entry_risk_committed,
    )

    user = models.User(name=f"hard-cap-{uuid.uuid4().hex[:10]}")
    db.add(user)
    db.flush()
    variant = _variant(db, f"hard_cap_{uuid.uuid4().hex[:10]}")
    owner = _session(
        db,
        user_id=user.id,
        variant_id=variant.id,
        symbol="HARD",
        family="alpaca_spot",
        state="live_pending_entry",
        live_execution={"side_long": True},
    )
    owner_id = int(owner.id)
    db.commit()
    cid = f"cid-hard-{uuid.uuid4().hex[:10]}"
    result = reserve_alpaca_entry_risk_committed(
        symbol="HARD",
        claim_token=f"hard-{uuid.uuid4().hex[:10]}",
        owner_session_id=owner_id,
        client_order_id=cid,
        post_bind_token=f"binder-{cid}",
        order_request=_entry_order_request("HARD", cid, qty="10"),
        order_role="primary",
        reserved_risk_usd=51.0,
        account_equity_usd=1_000_000.0,
        account_scope="alpaca:paper",
        budget_fraction=1.0,
        per_symbol_cap_usd=5_000.0,
    )
    assert result["ok"] is False
    assert result["reason"] == "symbol_risk_cap_exceeded"
    assert result["symbol_cap_usd"] == 50.0
    assert result["projected_symbol_risk_usd"] == 51.0


@pytest.mark.parametrize(
    ("position_state", "entry_submitted"),
    [("live_error", False), ("live_pending_entry", True)],
)
def test_position_evidence_blocks_regardless_of_fsm_state(
    db,
    position_state,
    entry_submitted,
) -> None:
    from app.services.trading.momentum_neural.alpaca_orphan_claims import (
        reserve_alpaca_entry_risk_committed,
    )

    exposure_user = models.User(name=f"state-hole-a-{uuid.uuid4().hex[:8]}")
    candidate_user = models.User(name=f"state-hole-b-{uuid.uuid4().hex[:8]}")
    db.add_all([exposure_user, candidate_user])
    db.flush()
    variant = _variant(db, f"state_hole_{uuid.uuid4().hex[:10]}")
    exposure = _session(
        db,
        user_id=exposure_user.id,
        variant_id=variant.id,
        symbol="EXPO",
        family="alpaca_spot",
        state=position_state,
        live_execution={
            "side_long": True,
            "entry_submitted": entry_submitted,
            "position": {
                "quantity": 7,
                "avg_entry_price": 10.0,
                "stop_price": 9.5,
                "side_long": True,
                "side": "long",
                "position_intent": "buy_to_open",
            },
        },
    )
    candidate = _session(
        db,
        user_id=candidate_user.id,
        variant_id=variant.id,
        symbol="NEWB",
        family="alpaca_spot",
        state="live_pending_entry",
        live_execution={"side_long": True},
    )
    exposure_id = int(exposure.id)
    candidate_id = int(candidate.id)
    db.commit()
    cid = f"cid-state-{uuid.uuid4().hex[:10]}"
    result = reserve_alpaca_entry_risk_committed(
        symbol="NEWB",
        claim_token=f"state-{uuid.uuid4().hex[:10]}",
        owner_session_id=candidate_id,
        client_order_id=cid,
        post_bind_token=f"binder-{cid}",
        order_request=_entry_order_request("NEWB", cid, qty="1"),
        order_role="primary",
        reserved_risk_usd=10.0,
        account_equity_usd=100_000.0,
        account_scope="alpaca:paper",
        budget_fraction=1.0,
        per_symbol_cap_usd=50.0,
    )
    assert result["ok"] is False
    assert result["reason"] == "account_position_exposure_present"
    assert result["position_session_id"] == exposure_id
    assert result["position_state"] == position_state


def test_unresolved_orphan_flatten_claim_blocks_other_symbol_entry(db) -> None:
    from app.services.trading.momentum_neural.alpaca_orphan_claims import (
        reserve_alpaca_entry_risk_committed,
    )

    user = models.User(name=f"orphan-block-{uuid.uuid4().hex[:8]}")
    db.add(user)
    db.flush()
    variant = _variant(db, f"orphan_block_{uuid.uuid4().hex[:10]}")
    orphan_owner = _session(
        db,
        user_id=user.id,
        variant_id=variant.id,
        symbol="ORPH",
        family="alpaca_spot",
        state="live_error",
        live_execution={"side_long": True},
    )
    candidate = _session(
        db,
        user_id=user.id,
        variant_id=variant.id,
        symbol="CAND",
        family="alpaca_spot",
        state="live_pending_entry",
        live_execution={"side_long": True},
    )
    db.add(BrokerSymbolActionClaim(
        account_scope="alpaca:paper",
        symbol="ORPH",
        claim_token=f"orphan-{uuid.uuid4().hex[:10]}",
        action="orphan_flatten",
        phase="claimed",
        owner_session_id=orphan_owner.id,
        client_order_id=f"cid-orphan-{uuid.uuid4().hex[:10]}",
            metadata_json={
                "stage": "runner_emergency_close_only",
                "alpaca_account_id": TEST_ALPACA_ACCOUNT_ID,
            },
    ))
    candidate_id = int(candidate.id)
    db.commit()
    cid = f"cid-candidate-{uuid.uuid4().hex[:10]}"
    result = reserve_alpaca_entry_risk_committed(
        symbol="CAND",
        claim_token=f"candidate-{uuid.uuid4().hex[:10]}",
        owner_session_id=candidate_id,
        client_order_id=cid,
        post_bind_token=f"binder-{cid}",
        order_request=_entry_order_request("CAND", cid, qty="1"),
        order_role="primary",
        reserved_risk_usd=10.0,
        account_equity_usd=100_000.0,
        account_scope="alpaca:paper",
        budget_fraction=1.0,
        per_symbol_cap_usd=50.0,
    )
    assert result["ok"] is False
    assert result["reason"] == "account_unresolved_non_entry_claim"
    assert result["blocking_claim_action"] == "orphan_flatten"
    assert result["blocking_claim_symbol"] == "ORPH"


def test_nested_short_direction_marker_rejects_pending_entry_owner(db) -> None:
    from app.services.trading.momentum_neural.alpaca_orphan_claims import (
        reserve_alpaca_entry_risk_committed,
    )

    user = models.User(name=f"nested-short-{uuid.uuid4().hex[:8]}")
    db.add(user)
    db.flush()
    variant = _variant(db, f"nested_short_{uuid.uuid4().hex[:10]}")
    owner = _session(
        db,
        user_id=user.id,
        variant_id=variant.id,
        symbol="NEST",
        family="alpaca_spot",
        state="live_pending_entry",
        live_execution={
            "side_long": True,
            "position": {"side_long": False},
        },
    )
    owner_id = int(owner.id)
    db.commit()
    cid = f"cid-nested-{uuid.uuid4().hex[:10]}"
    result = reserve_alpaca_entry_risk_committed(
        symbol="NEST",
        claim_token=f"nested-{uuid.uuid4().hex[:10]}",
        owner_session_id=owner_id,
        client_order_id=cid,
        post_bind_token=f"binder-{cid}",
        order_request=_entry_order_request("NEST", cid, qty="1"),
        order_role="primary",
        reserved_risk_usd=10.0,
        account_equity_usd=100_000.0,
        account_scope="alpaca:paper",
        budget_fraction=1.0,
        per_symbol_cap_usd=50.0,
    )
    assert result["ok"] is False
    assert result["reason"] == "owner_session_not_certified"
