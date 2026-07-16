"""Marketable-LIMIT entry (Ross-style, sweep-protected) — the momentum live runner
places the entry as a marketable limit capped at the guarded ask, NOT a market order
that can sweep a thin low-float book. Root cause: 0 clean equity fills ever; the live
gate correctly refused market-order entries into 4.6%-avg spreads (project memory
project_momentum_zero_fills_root_cause)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import uuid

import pytest
from sqlalchemy.orm import Session

from app.config import settings
from app.services.trading import governance as gov
from app.services.trading.momentum_neural import automation_query as aq
from app.services.trading.momentum_neural.live_fsm import (
    STATE_LIVE_CANCELLED,
    STATE_LIVE_ERROR,
    STATE_LIVE_PENDING_ENTRY,
    STATE_WATCHING_LIVE,
)
from app.services.trading.momentum_neural.live_runner import (
    _decline_terminal,
    _entry_spread_risk_decision,
    _final_entry_bbo,
    _fmt_limit_price_buy,
    _governed_place,
    _is_confirmed_pre_http_alpaca_arm_claim,
    _recover_owner_alpaca_entry_claim,
    _safe_transition,
    tick_live_session,
)
from app.services.trading.momentum_neural.alpaca_orphan_claims import (
    acquire_action_claim,
    read_action_claim,
    update_action_claim_phase,
)
from app.services.trading.momentum_neural.persistence import create_trading_automation_session
from app.services.trading.momentum_neural.paper_fsm import STATE_LIVE_ARM_PENDING
from app.services.trading.momentum_neural.rail_governor import AcquireResult
from app.services.trading.momentum_neural.risk_policy import RISK_SNAPSHOT_KEY
from app.services.trading.venue.coinbase_spot import reset_duplicate_client_order_guard_for_tests
from app.services.trading.venue.alpaca_spot import (
    quantize_alpaca_equity_limit_price,
)
from app.services.trading.venue.protocol import (
    FreshnessMeta,
    NormalizedOrder,
    NormalizedProduct,
    NormalizedTicker,
)

from tests.test_momentum_live_runner import _fresh, _mk_adapter, _uid
from tests.test_momentum_paper_runner import _seed_live_eligible_row


TEST_ALPACA_ACCOUNT_ID = "00000000-0000-0000-0000-00000000a111"


@pytest.fixture(autouse=True)
def _certified_test_boundaries(
    monkeypatch,
    stable_non_alpaca_account_identity,
):
    monkeypatch.setattr(
        settings,
        "chili_alpaca_expected_account_id",
        TEST_ALPACA_ACCOUNT_ID,
        raising=False,
    )
    monkeypatch.setattr(settings, "chili_alpaca_paper", True, raising=False)
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.market_profile.market_session_now",
        lambda _symbol: "regular",
    )

    def _healthy_daily_loss(_db, family, *, user_id=None, force_refresh=False):
        assert family == "alpaca_spot"
        assert force_refresh is True
        return False, {
            "family": family,
            "realized": 0.0,
            "cap": 250.0,
            "transient": False,
            "data_source": "alpaca_account_equity_delta",
            "broker_snapshot_cache_bypassed": True,
        }

    monkeypatch.setattr(gov, "broker_daily_loss_breached", _healthy_daily_loss)


def _certify_alpaca_adapter(ad):
    ad.bind_account_id.side_effect = (
        lambda expected: str(expected or "").strip() == TEST_ALPACA_ACCOUNT_ID
    )
    ad.get_account_snapshot.return_value = {
        "ok": True,
        "paper": True,
        "account_id": TEST_ALPACA_ACCOUNT_ID,
    }

    def _clock():
        now = datetime.now(timezone.utc)
        return {
            "ok": True,
            "paper": True,
            "is_open": True,
            "timestamp": now.isoformat(),
            "next_close": (now + timedelta(hours=8)).isoformat(),
        }

    ad.get_market_clock_snapshot.side_effect = _clock

    def _equity_product(product_id):
        symbol = str(product_id).strip().upper()
        return (
            NormalizedProduct(
                product_id=symbol,
                base_currency=symbol,
                quote_currency="USD",
                status="online",
                trading_disabled=False,
                cancel_only=False,
                limit_only=False,
                post_only=False,
                auction_mode=False,
                base_increment=1.0,
                base_min_size=1.0,
            ),
            _fresh(),
        )

    ad.get_product.side_effect = _equity_product
    return ad


# ── pure: the buy-limit price formatter ──────────────────────────────────────
def test_fmt_limit_price_buy_equity_rounds_up_to_penny() -> None:
    # >= $1: penny tick, rounded UP so a marketable buy stays marketable.
    assert _fmt_limit_price_buy(2.2155) == "2.22"
    assert _fmt_limit_price_buy(15.031) == "15.04"
    assert _fmt_limit_price_buy(5.0) == "5.00"       # already on the tick
    assert _fmt_limit_price_buy(1.0) == "1.00"


def test_fmt_limit_price_buy_subdollar_keeps_precision() -> None:
    # < $1 (crypto / penny names): finer precision for the venue to quantize.
    assert _fmt_limit_price_buy(0.12345678) == "0.12345678"
    assert _fmt_limit_price_buy(0.5) == "0.5"


def test_alpaca_buy_limit_canonicalizes_exact_subdollar_zeroes() -> None:
    assert quantize_alpaca_equity_limit_price(0.5, "buy") == "0.5000"
    assert quantize_alpaca_equity_limit_price("0.12371", "buy") == "0.1238"


def test_alpaca_governed_entry_rejects_raw_subpenny_before_reservation_or_post(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.live_runner.reserve_alpaca_entry_risk_committed",
        lambda **_k: (_ for _ in ()).throw(
            AssertionError("noncanonical price reached reservation")
        ),
    )
    place = MagicMock(return_value={"ok": True, "order_id": "must-not-exist"})
    out = _governed_place(
        object(),
        place,
        sess=SimpleNamespace(execution_family="alpaca_spot"),
        product_id="ACTU",
        side="buy",
        position_intent="buy_to_open",
        base_size="100000",
        limit_price="0.12371",
        client_order_id="cid-raw-subpenny",
        extended_hours=False,
        time_in_force="day",
    )

    assert out["error"] == "alpaca_entry_limit_not_canonical"
    assert out["canonical_limit_price"] == "0.1238"
    assert out["pre_place_blocked"] is True
    place.assert_not_called()


def test_fmt_limit_price_buy_invalid_is_zero() -> None:
    assert _fmt_limit_price_buy(0.0) == "0"
    assert _fmt_limit_price_buy(-3.0) == "0"
    assert _fmt_limit_price_buy(float("nan")) == "0"
    assert _fmt_limit_price_buy(float("inf")) == "0"


def test_fmt_limit_price_buy_never_below_input_for_buy() -> None:
    # Rounding UP must never make the marketable buy LESS marketable.
    for px in (1.001, 2.349, 9.999, 14.872):
        assert float(_fmt_limit_price_buy(px)) >= px


def test_final_entry_bbo_requires_explicit_fresh_source() -> None:
    now = datetime.now(timezone.utc)

    class _Adapter:
        def get_execution_bbo(self, product_id, *, max_age_seconds):
            meta = FreshnessMeta(
                retrieved_at_utc=now,
                provider_time_utc=now - timedelta(milliseconds=250),
                max_age_seconds=max_age_seconds,
            )
            return (
                NormalizedTicker(
                    product_id=product_id,
                    bid=1.47,
                    ask=1.48,
                    mid=1.475,
                    freshness=meta,
                    raw={"feed": "iqfeed_l1", "tape_row_id": 42},
                ),
                meta,
            )

    tick, snap = _final_entry_bbo(_Adapter(), "ACTU", max_age_seconds=2.0)
    assert tick is not None
    assert snap["ok"] is True
    assert snap["source"] == "iqfeed_l1"
    assert snap["tape_row_id"] == 42
    assert snap["age_seconds"] < 2.0


def test_final_entry_bbo_rejects_stale_quote() -> None:
    now = datetime.now(timezone.utc)

    class _Adapter:
        def get_execution_bbo(self, product_id, *, max_age_seconds):
            meta = FreshnessMeta(
                retrieved_at_utc=now,
                provider_time_utc=now - timedelta(seconds=120),
                max_age_seconds=max_age_seconds,
            )
            return (
                NormalizedTicker(
                    product_id=product_id,
                    bid=7.25,
                    ask=7.37,
                    mid=7.31,
                    freshness=meta,
                    raw={"feed": "iqfeed_l1"},
                ),
                meta,
            )

    tick, snap = _final_entry_bbo(_Adapter(), "BRAI", max_age_seconds=2.0)
    assert tick is None
    assert snap["reason"] == "execution_bbo_stale"
    assert snap["age_seconds"] >= 120.0


def test_final_entry_bbo_rejects_mismatched_tick_freshness() -> None:
    """The broker seam re-checks the ticker's metadata, so a fresh tuple-level
    wrapper must never mask a stale ticker clock."""
    now = datetime.now(timezone.utc)
    tuple_meta = FreshnessMeta(retrieved_at_utc=now, max_age_seconds=2.0)
    stale_tick_meta = FreshnessMeta(
        retrieved_at_utc=now - timedelta(seconds=30),
        max_age_seconds=2.0,
    )

    class _Adapter:
        def get_execution_bbo(self, product_id, *, max_age_seconds):
            return (
                NormalizedTicker(
                    product_id=product_id,
                    bid=1.47,
                    ask=1.48,
                    mid=1.475,
                    freshness=stale_tick_meta,
                    raw={"feed": "iqfeed_l1"},
                ),
                tuple_meta,
            )

    tick, snap = _final_entry_bbo(_Adapter(), "ACTU", max_age_seconds=2.0)

    assert tick is None
    assert snap["reason"] == "execution_bbo_stale"
    assert snap["age_seconds"] >= 30.0


def test_governed_place_blocks_quote_that_expires_during_rail_wait(
    monkeypatch,
) -> None:
    """The governor wait is deterministic here: it advances the quote clock past
    the freshness budget before returning a token.  The final seam must veto the
    instruction without invoking the broker adapter."""
    clock = {"age_seconds": 1.25}

    class _AdvancingFreshness:
        def age_seconds(self) -> float:
            return float(clock["age_seconds"])

    def _wait_then_acquire(_settings, *, lane_key):
        assert lane_key
        clock["age_seconds"] += 1.0
        return AcquireResult(acquired=True, waited_s=1.0, refill_rps=2.0)

    monkeypatch.setattr(
        "app.services.trading.momentum_neural.rail_governor.acquire_rail",
        _wait_then_acquire,
    )
    place = MagicMock(return_value={"ok": True, "order_id": "must-not-exist"})

    out = _governed_place(
        None,
        place,
        execution_bbo_freshness=_AdvancingFreshness(),
        execution_bbo_max_age_seconds=2.0,
        product_id="ACTU",
        side="buy",
        base_size="100",
        limit_price="1.48",
        client_order_id="cid-final-bbo",
    )

    assert out["error"] == "execution_bbo_stale_at_place"
    assert out["execution_bbo_age_seconds"] == 2.25
    assert out["pre_place_blocked"] is True
    place.assert_not_called()


def test_spread_risk_gate_matches_incident_liquidity_quality() -> None:
    # VEEE/SOBR: spread cost was a small slice of structural risk; permit.
    veee_ok, veee = _entry_spread_risk_decision(
        bid=10.15,
        ask=10.22,
        quantity=1519,
        stop_distance=0.76699929,
        max_fraction=0.25,
    )
    sobr_ok, sobr = _entry_spread_risk_decision(
        bid=1.45,
        ask=1.46,
        quantity=11325,
        stop_distance=0.09465238,
        max_fraction=0.25,
    )
    # BRAI/ACTU/TRNR: crossing the book consumed too much of one trade's
    # structural risk before the thesis had any chance to work; veto.
    brai_ok, _ = _entry_spread_risk_decision(
        bid=7.25,
        ask=7.37,
        quantity=5933,
        stop_distance=0.12,
        max_fraction=0.25,
    )
    actu_ok, _ = _entry_spread_risk_decision(
        bid=1.47,
        ask=1.50,
        quantity=17991,
        stop_distance=0.049,
        max_fraction=0.25,
    )
    trnr_ok, _ = _entry_spread_risk_decision(
        bid=2.95,
        ask=2.99,
        quantity=4578,
        stop_distance=0.07384024,
        max_fraction=0.25,
    )

    assert veee_ok is True and veee["spread_fraction_of_risk"] < 0.25
    assert sobr_ok is True and sobr["spread_fraction_of_risk"] < 0.25
    assert brai_ok is False
    assert actu_ok is False
    assert trnr_ok is False


# ── integration: the entry places a marketable LIMIT at the guarded ask ───────
def _mk_pending_entry_session(
    db: Session, symbol: str, *, execution_family: str = "coinbase_spot"
):
    vid, _ = _seed_live_eligible_row(db, symbol=symbol)
    db.commit()
    uid = _uid(db, "limit_entry")
    live_execution = {"entry_submitted": False}
    if execution_family == "alpaca_spot":
        # The order-boundary reservation must receive a positive, already
        # approved stop so it can calculate exact candidate dollars at risk.
        live_execution["entry_reservation_stop_price"] = 1.40
        live_execution["side_long"] = True
        live_execution["effective_max_hold_seconds"] = 3_600
    risk_snapshot = {
        RISK_SNAPSHOT_KEY: {"allowed": True},
        "momentum_risk_policy_summary": {"disable_live_if_governance_inhibit": True},
        "momentum_live_execution": live_execution,
    }
    if execution_family == "alpaca_spot":
        # Alpaca recovery is authorized only inside the exact frozen paper
        # account scope.  These tests exercise submit/reconcile behavior, not
        # the separate missing-scope quarantine contract.
        risk_snapshot["alpaca_account_scope"] = "alpaca:paper"
        risk_snapshot["alpaca_account_id"] = TEST_ALPACA_ACCOUNT_ID
    sess = create_trading_automation_session(
        db,
        user_id=uid,
        venue=("alpaca" if execution_family == "alpaca_spot" else "coinbase"),
        execution_family=execution_family,
        symbol=symbol,
        variant_id=vid,
        mode="live",
        state=STATE_LIVE_PENDING_ENTRY,
        risk_snapshot_json=risk_snapshot,
    )
    db.commit()
    db.refresh(sess)
    if execution_family == "alpaca_spot":
        confirmed_at = datetime.now(timezone.utc).isoformat()
        expires_at = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        arm_token = f"limit-entry-arm-{sess.id}-{uuid.uuid4().hex}"
        claim_token = f"arm-{arm_token}"
        snapshot = dict(sess.risk_snapshot_json or {})
        snapshot.update(
            {
                "arm_token": arm_token,
                "expires_at_utc": expires_at,
                "arm_confirmed_at_utc": confirmed_at,
                "alpaca_account_scope": "alpaca:paper",
                "alpaca_account_id": TEST_ALPACA_ACCOUNT_ID,
                "alpaca_symbol_claim_token": claim_token,
                "confirmed_arm_generation": {
                    "version": 1,
                    "session_id": int(sess.id),
                    "arm_token": arm_token,
                    "expires_at_utc": expires_at,
                    "alpaca_symbol_claim_token": claim_token,
                    "alpaca_account_scope": "alpaca:paper",
                    "alpaca_account_id": TEST_ALPACA_ACCOUNT_ID,
                    "confirmed_at_utc": confirmed_at,
                },
            }
        )
        sess.risk_snapshot_json = snapshot
        db.add(sess)
        claim = acquire_action_claim(
            db,
            symbol=sess.symbol,
            action="entry",
            claim_token=claim_token,
            owner_session_id=int(sess.id),
            metadata={
                "stage": "live_arm_reserved",
                "variant_id": int(sess.variant_id),
                "alpaca_account_id": TEST_ALPACA_ACCOUNT_ID,
            },
            account_scope="alpaca:paper",
        )
        assert claim.get("ok") is True, claim
        db.commit()
        db.refresh(sess)
    return sess


def _prepare_alpaca_submit_recovery_case(
    monkeypatch,
    db: Session,
    *,
    symbol: str,
):
    reset_duplicate_client_order_guard_for_tests()
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(settings, "brain_enable_decision_ledger", False)
    monkeypatch.setattr(settings, "brain_decision_packet_required_for_runners", False)
    monkeypatch.setattr(settings, "chili_momentum_decouple_watching_enabled", False)
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.live_runner._venue_broker_connected",
        lambda _ef: True,
    )
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.live_runner.runner_boundary_risk_ok",
        lambda *_args, **_kwargs: (True, {"allowed": True}),
    )
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.risk_policy._account_equity_usd",
        lambda *_args, **_kwargs: 10_000.0,
    )
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.live_runner.is_kill_switch_active",
        lambda: False,
    )
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.market_profile.schedule_window_now",
        lambda: "hot",
    )
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.rail_governor.acquire_rail",
        lambda _settings, *, lane_key: AcquireResult(
            acquired=True, waited_s=0.0, refill_rps=2.0
        ),
    )
    sess = _mk_pending_entry_session(
        db,
        symbol,
        execution_family="alpaca_spot",
    )
    ad = _certify_alpaca_adapter(_mk_adapter())
    ad.list_positions.side_effect = lambda: ([], _fresh())
    ad.list_open_orders.side_effect = lambda **_kwargs: ([], _fresh())
    ad.get_position_quantity.side_effect = lambda *_args, **_kwargs: float(
        ad.place_limit_order_gtc.call_args.kwargs["base_size"]
    )

    def _setup_bbo(*_args, **_kwargs):
        meta = _fresh()
        return (
            NormalizedTicker(
                product_id=symbol,
                bid=1.479,
                ask=1.48,
                mid=1.4795,
                freshness=meta,
            ),
            meta,
        )

    ad.get_best_bid_ask.side_effect = _setup_bbo

    def _execution_bbo(*_args, **_kwargs):
        meta = _fresh()
        return (
            NormalizedTicker(
                product_id=symbol,
                bid=1.479,
                ask=1.48,
                mid=1.4795,
                freshness=meta,
                raw={"feed": "iqfeed_l1", "tape_row_id": 2},
            ),
            meta,
        )

    ad.get_execution_bbo.side_effect = _execution_bbo
    ad.place_deadman_stop.return_value = {"ok": False, "error": "test_disabled"}
    return sess, ad


def _mk_unconfirmed_alpaca_arm_session(
    db: Session,
    symbol: str,
    *,
    promoted: bool = False,
):
    vid, _ = _seed_live_eligible_row(db, symbol=symbol)
    db.commit()
    uid = _uid(db, "unconfirmed_alpaca_arm")
    arm_token = f"unconfirmed-arm-{symbol}-{uuid.uuid4().hex}"
    claim_token = f"arm-{arm_token}"
    paper = None
    if promoted:
        paper = create_trading_automation_session(
            db,
            user_id=uid,
            venue="alpaca",
            execution_family="alpaca_spot",
            symbol=symbol,
            variant_id=vid,
            mode="paper",
            state="finished",
            risk_snapshot_json={},
        )
        db.flush()
    snapshot = {
        "arm_token": arm_token,
        "expires_at_utc": (
            datetime.now(timezone.utc) + timedelta(hours=2)
        ).isoformat(),
        "alpaca_symbol_claim_token": claim_token,
        "alpaca_account_scope": "alpaca:paper",
        "alpaca_account_id": TEST_ALPACA_ACCOUNT_ID,
        "momentum_live_execution": {},
    }
    if paper is not None:
        snapshot["promoted_from_paper_session_id"] = int(paper.id)
    sess = create_trading_automation_session(
        db,
        user_id=uid,
        venue="alpaca",
        execution_family="alpaca_spot",
        symbol=symbol,
        variant_id=vid,
        mode="live",
        state=STATE_LIVE_ARM_PENDING,
        risk_snapshot_json=snapshot,
        source_paper_session_id=(int(paper.id) if paper is not None else None),
    )
    claim_metadata = {
        "stage": (
            "promoted_live_arm_reserved" if promoted else "live_arm_reserved"
        ),
        "alpaca_account_id": TEST_ALPACA_ACCOUNT_ID,
    }
    if promoted:
        claim_metadata["paper_session_id"] = int(paper.id)
    else:
        claim_metadata["variant_id"] = int(vid)
    claim = acquire_action_claim(
        db,
        symbol=symbol,
        action="entry",
        claim_token=claim_token,
        owner_session_id=int(sess.id),
        metadata=claim_metadata,
        account_scope="alpaca:paper",
    )
    assert claim.get("ok") is True, claim
    db.commit()
    db.refresh(sess)
    return sess


def test_confirmed_pre_http_alpaca_arm_claim_reaches_cid_binding(
    monkeypatch,
    db: Session,
) -> None:
    """A real confirmed arm reservation must reach the one broker POST seam."""
    monkeypatch.setattr(settings, "chili_momentum_anticipation_starter_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_anticipation_probe_fraction", 0.25)
    sess, ad = _prepare_alpaca_submit_recovery_case(monkeypatch, db, symbol="ACTU")

    result = tick_live_session(db, sess.id, adapter_factory=lambda: ad)
    db.commit()
    db.refresh(sess)

    assert ad.place_limit_order_gtc.call_count == 1, result
    submitted_cid = str(ad.place_limit_order_gtc.call_args.kwargs["client_order_id"])
    readable, claim = read_action_claim(
        db,
        symbol=sess.symbol,
        account_scope="alpaca:paper",
    )
    assert readable is True and claim is not None
    assert claim["owner_session_id"] == int(sess.id)
    assert claim["claim_token"] == (sess.risk_snapshot_json or {})[
        "alpaca_symbol_claim_token"
    ]
    assert claim["client_order_id"] == submitted_cid
    assert (claim.get("metadata") or {}).get("stage") == "pre_broker_place"
    le = (sess.risk_snapshot_json or {})["momentum_live_execution"]
    assert le.get("anticipation_armed") is not True
    assert "anticipation_remainder_qty" not in le


def test_unexpected_cidless_claim_metadata_stays_fail_closed(db: Session) -> None:
    sess = _mk_pending_entry_session(db, "BRAI", execution_family="alpaca_spot")
    snapshot = dict(sess.risk_snapshot_json or {})
    token = str(snapshot["alpaca_symbol_claim_token"])
    changed = acquire_action_claim(
        db,
        symbol=sess.symbol,
        action="entry",
        claim_token=token,
        owner_session_id=int(sess.id),
        metadata={"unexpected_transport_marker": True},
        account_scope="alpaca:paper",
    )
    assert changed.get("ok") is True, changed
    db.commit()
    db.refresh(sess)
    ad = _certify_alpaca_adapter(_mk_adapter())
    le = dict((sess.risk_snapshot_json or {})["momentum_live_execution"])

    recovered = _recover_owner_alpaca_entry_claim(
        db,
        sess,
        ad,
        le=le,
        product_id=sess.symbol,
        operator_paused=False,
    )

    assert recovered["block_new_entries"] is True
    assert recovered["reason"] == "claim_without_cid"
    ad.place_limit_order_gtc.assert_not_called()


@pytest.mark.parametrize(
    ("field", "malformed"),
    [
        ("entry_submitted", 1),
        ("entry_submitted", "true"),
        ("entry_order_id", 0),
        ("entry_client_order_id", []),
        ("entry_reconcile_pending_client_order_id", {}),
        ("entry_order_ids_all", ["historical-entry-oid"]),
        ("entry_order_ids_all", {}),
        ("entry_orders_resolved", {"historical-entry-oid": "void"}),
        ("entry_orders_resolved", []),
        ("position", []),
    ],
)
def test_confirmed_pre_http_claim_rejects_malformed_local_transport(
    db: Session,
    field: str,
    malformed,
) -> None:
    sess = _mk_pending_entry_session(db, "ACTU", execution_family="alpaca_spot")
    readable, claim = read_action_claim(
        db,
        symbol=sess.symbol,
        account_scope="alpaca:paper",
    )
    assert readable is True and claim is not None
    le = dict((sess.risk_snapshot_json or {})["momentum_live_execution"])
    le[field] = malformed

    assert _is_confirmed_pre_http_alpaca_arm_claim(sess, claim, le=le) is False


def test_promoted_pre_http_claim_requires_exact_paper_source(db: Session) -> None:
    sess = _mk_pending_entry_session(db, "VEEE", execution_family="alpaca_spot")
    sess.source_paper_session_id = 4242
    snapshot = dict(sess.risk_snapshot_json or {})
    token = str(snapshot["alpaca_symbol_claim_token"])
    promoted_claim = {
        "account_scope": "alpaca:paper",
        "symbol": sess.symbol,
        "claim_token": token,
        "action": "entry",
        "phase": "claimed",
        "owner_session_id": int(sess.id),
        "client_order_id": None,
        "broker_order_id": None,
        "metadata": {
            "stage": "promoted_live_arm_reserved",
            "paper_session_id": 4242,
            "alpaca_account_id": TEST_ALPACA_ACCOUNT_ID,
        },
        "lease_expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
        "resolved_at": None,
    }
    le = dict(snapshot["momentum_live_execution"])

    assert _is_confirmed_pre_http_alpaca_arm_claim(
        sess,
        promoted_claim,
        le=le,
    ) is True
    promoted_claim["metadata"] = {
        **promoted_claim["metadata"],
        "paper_session_id": 4243,
    }
    assert _is_confirmed_pre_http_alpaca_arm_claim(
        sess,
        promoted_claim,
        le=le,
    ) is False


def test_terminal_transition_retires_exact_pre_http_claim_and_allows_rearm(
    db: Session,
) -> None:
    sess = _mk_pending_entry_session(db, "SOBR", execution_family="alpaca_spot")
    old_token = str((sess.risk_snapshot_json or {})["alpaca_symbol_claim_token"])

    _safe_transition(db, sess, STATE_LIVE_ERROR)
    db.commit()
    db.refresh(sess)

    assert sess.state == STATE_LIVE_ERROR
    readable, retired = read_action_claim(
        db,
        symbol=sess.symbol,
        account_scope="alpaca:paper",
    )
    assert readable is True and retired is not None
    assert retired["claim_token"] == old_token
    assert retired["phase"] == "resolved"
    assert retired["client_order_id"] is None
    assert retired["broker_order_id"] is None
    assert (retired.get("metadata") or {}).get("proven_no_transport") is True

    rearmed = acquire_action_claim(
        db,
        symbol=sess.symbol,
        action="entry",
        claim_token=f"rearm-{uuid.uuid4().hex}",
        owner_session_id=int(sess.id),
        metadata={"test": "subsequent_generation"},
        account_scope="alpaca:paper",
    )
    assert rearmed.get("ok") is True, rearmed
    assert rearmed.get("replaced") is True


def test_terminal_transition_keeps_ambiguous_pre_http_claim_and_owner_serviceable(
    db: Session,
) -> None:
    sess = _mk_pending_entry_session(db, "TRNR", execution_family="alpaca_spot")
    snapshot = dict(sess.risk_snapshot_json or {})
    token = str(snapshot["alpaca_symbol_claim_token"])
    changed = acquire_action_claim(
        db,
        symbol=sess.symbol,
        action="entry",
        claim_token=token,
        owner_session_id=int(sess.id),
        metadata={"unexpected_transport_marker": True},
        account_scope="alpaca:paper",
    )
    assert changed.get("ok") is True, changed

    with pytest.raises(
        RuntimeError,
        match="alpaca_pre_http_claim_terminalization_ambiguous",
    ):
        _safe_transition(db, sess, STATE_LIVE_ERROR)

    assert sess.state == STATE_LIVE_PENDING_ENTRY
    readable, retained = read_action_claim(
        db,
        symbol=sess.symbol,
        account_scope="alpaca:paper",
    )
    assert readable is True and retained is not None
    assert retained["phase"] == "claimed"
    assert retained["claim_token"] == token


def test_terminal_transition_never_releases_cid_bound_claim(db: Session) -> None:
    sess = _mk_pending_entry_session(db, "MIRA", execution_family="alpaca_spot")
    token = str((sess.risk_snapshot_json or {})["alpaca_symbol_claim_token"])
    assert update_action_claim_phase(
        db,
        symbol=sess.symbol,
        claim_token=token,
        phase="claimed",
        client_order_id="chili-bound-entry-cid",
        account_scope="alpaca:paper",
    ) is True
    db.commit()

    with pytest.raises(
        RuntimeError,
        match="alpaca_pre_http_claim_terminalization_ambiguous",
    ):
        _safe_transition(db, sess, STATE_LIVE_ERROR)

    assert sess.state == STATE_LIVE_PENDING_ENTRY
    readable, retained = read_action_claim(
        db,
        symbol=sess.symbol,
        account_scope="alpaca:paper",
    )
    assert readable is True and retained is not None
    assert retained["phase"] == "claimed"
    assert retained["client_order_id"] == "chili-bound-entry-cid"


def test_terminal_transition_fails_closed_when_claim_read_is_unreadable(
    monkeypatch,
    db: Session,
) -> None:
    sess = _mk_pending_entry_session(db, "CERO", execution_family="alpaca_spot")
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.live_runner.read_action_claim",
        lambda *_a, **_k: (False, None),
    )

    with pytest.raises(
        RuntimeError,
        match="alpaca_pre_http_claim_terminalization_unreadable",
    ):
        _safe_transition(db, sess, STATE_LIVE_ERROR)

    assert sess.state == STATE_LIVE_PENDING_ENTRY


def test_terminal_transition_fails_closed_when_claim_resolution_cas_loses(
    monkeypatch,
    db: Session,
) -> None:
    sess = _mk_pending_entry_session(db, "NXL", execution_family="alpaca_spot")
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.live_runner.resolve_action_claim",
        lambda *_a, **_k: False,
    )

    with pytest.raises(
        RuntimeError,
        match="alpaca_pre_http_claim_terminalization_cas_failed",
    ):
        _safe_transition(db, sess, STATE_LIVE_ERROR)

    assert sess.state == STATE_LIVE_PENDING_ENTRY
    readable, retained = read_action_claim(
        db,
        symbol=sess.symbol,
        account_scope="alpaca:paper",
    )
    assert readable is True and retained is not None
    assert retained["phase"] == "claimed"


def test_terminal_transition_rolls_back_unverified_claim_resolution(
    monkeypatch,
    db: Session,
) -> None:
    sess = _mk_pending_entry_session(db, "MLGO", execution_family="alpaca_spot")
    calls = 0

    def _tamper_second_read(*args, **kwargs):
        nonlocal calls
        calls += 1
        readable, claim = read_action_claim(*args, **kwargs)
        if calls == 1:
            return readable, claim
        assert readable is True and claim is not None
        return True, {**claim, "owner_session_id": int(sess.id) + 1}

    monkeypatch.setattr(
        "app.services.trading.momentum_neural.live_runner.read_action_claim",
        _tamper_second_read,
    )

    with pytest.raises(
        RuntimeError,
        match="alpaca_pre_http_claim_terminalization_unverified",
    ):
        _safe_transition(db, sess, STATE_LIVE_ERROR)
    # A monitor caller may catch this error and commit unrelated work. The claim
    # resolution itself must already have rolled back to its savepoint.
    db.commit()
    db.refresh(sess)

    assert sess.state == STATE_LIVE_PENDING_ENTRY
    readable, retained = read_action_claim(
        db,
        symbol=sess.symbol,
        account_scope="alpaca:paper",
    )
    assert readable is True and retained is not None
    assert retained["phase"] == "claimed"


@pytest.mark.parametrize(
    "operator_action",
    [aq.stop_automation_session, aq.cancel_automation_session],
    ids=["stop", "cancel"],
)
def test_operator_terminal_action_retires_exact_pre_http_claim_atomically(
    monkeypatch,
    db: Session,
    operator_action,
) -> None:
    sess = _mk_pending_entry_session(db, "SNTG", execution_family="alpaca_spot")
    old_token = str((sess.risk_snapshot_json or {})["alpaca_symbol_claim_token"])
    monkeypatch.setattr(aq, "_tables_present", lambda _db: True)
    monkeypatch.setattr(
        aq,
        "_reaper_broker_position_truth",
        lambda *_a, **_k: pytest.fail(
            "exact pre-HTTP no-transport proof must not need broker position I/O"
        ),
    )
    monkeypatch.setattr(
        aq,
        "tick_live_session",
        lambda *_a, **_k: pytest.fail(
            "exact pre-HTTP no-transport proof must not service the broker"
        ),
    )

    result = operator_action(db, user_id=sess.user_id, session_id=sess.id)
    db.commit()
    db.refresh(sess)

    assert result["ok"] is True
    assert result["state"] == STATE_LIVE_CANCELLED
    assert sess.state == STATE_LIVE_CANCELLED
    assert sess.ended_at is not None
    readable, retired = read_action_claim(
        db,
        symbol=sess.symbol,
        account_scope="alpaca:paper",
    )
    assert readable is True and retired is not None
    assert retired["claim_token"] == old_token
    assert retired["phase"] == "resolved"
    assert retired["client_order_id"] is None
    assert retired["broker_order_id"] is None


@pytest.mark.parametrize(
    "operator_action",
    [aq.stop_automation_session, aq.cancel_automation_session],
    ids=["stop", "cancel"],
)
def test_operator_terminal_action_pauses_ambiguous_cidless_claim_without_release(
    monkeypatch,
    db: Session,
    operator_action,
) -> None:
    sess = _mk_pending_entry_session(db, "LGMK", execution_family="alpaca_spot")
    snapshot = dict(sess.risk_snapshot_json or {})
    token = str(snapshot["alpaca_symbol_claim_token"])
    changed = acquire_action_claim(
        db,
        symbol=sess.symbol,
        action="entry",
        claim_token=token,
        owner_session_id=int(sess.id),
        metadata={"unexpected_transport_marker": True},
        account_scope="alpaca:paper",
    )
    assert changed.get("ok") is True, changed
    db.commit()
    db.refresh(sess)
    monkeypatch.setattr(aq, "_tables_present", lambda _db: True)
    monkeypatch.setattr(
        aq,
        "_reaper_broker_position_truth",
        lambda *_a, **_k: pytest.fail(
            "ambiguous CID-less ownership must pause before broker service"
        ),
    )
    monkeypatch.setattr(
        aq,
        "tick_live_session",
        lambda *_a, **_k: pytest.fail(
            "ambiguous CID-less ownership must pause before broker service"
        ),
    )

    result = operator_action(db, user_id=sess.user_id, session_id=sess.id)
    db.commit()
    db.refresh(sess)

    assert result["ok"] is True
    assert result["terminalization_deferred"] is True
    assert result["pending"] == "durable_alpaca_entry_claim_reconcile"
    assert sess.state == STATE_LIVE_PENDING_ENTRY
    assert sess.ended_at is None
    assert (sess.risk_snapshot_json or {}).get("operator_pause")
    readable, retained = read_action_claim(
        db,
        symbol=sess.symbol,
        account_scope="alpaca:paper",
    )
    assert readable is True and retained is not None
    assert retained["claim_token"] == token
    assert retained["phase"] == "claimed"
    assert retained["client_order_id"] is None
    assert retained["broker_order_id"] is None


@pytest.mark.parametrize(
    "operator_action",
    [aq.stop_automation_session, aq.cancel_automation_session],
    ids=["stop", "cancel"],
)
@pytest.mark.parametrize("promoted", [False, True], ids=["direct", "promoted"])
def test_operator_terminal_action_releases_exact_unconfirmed_arm_without_broker_io(
    monkeypatch,
    db: Session,
    operator_action,
    promoted: bool,
) -> None:
    sess = _mk_unconfirmed_alpaca_arm_session(
        db,
        "APVO",
        promoted=promoted,
    )
    token = str((sess.risk_snapshot_json or {})["alpaca_symbol_claim_token"])
    monkeypatch.setattr(aq, "_tables_present", lambda _db: True)
    monkeypatch.setattr(
        aq,
        "_reaper_broker_position_truth",
        lambda *_a, **_k: pytest.fail(
            "unconfirmed exact arm cancellation must not read broker position"
        ),
    )
    monkeypatch.setattr(
        aq,
        "tick_live_session",
        lambda *_a, **_k: pytest.fail(
            "unconfirmed exact arm cancellation must not service broker orders"
        ),
    )

    result = operator_action(db, user_id=sess.user_id, session_id=sess.id)
    db.commit()
    db.refresh(sess)

    assert result["ok"] is True
    assert result["state"] == STATE_LIVE_CANCELLED
    assert sess.state == STATE_LIVE_CANCELLED
    assert sess.ended_at is not None
    readable, retired = read_action_claim(
        db,
        symbol=sess.symbol,
        account_scope="alpaca:paper",
    )
    assert readable is True and retired is not None
    assert retired["claim_token"] == token
    assert retired["phase"] == "resolved"
    assert retired["client_order_id"] is None
    assert retired["broker_order_id"] is None


def test_pre_adapter_kill_retires_pre_http_claim_without_broker_access(
    monkeypatch,
    db: Session,
) -> None:
    sess = _mk_pending_entry_session(db, "VEEE", execution_family="alpaca_spot")
    sess.state = STATE_WATCHING_LIVE
    db.commit()
    db.refresh(sess)
    old_token = str((sess.risk_snapshot_json or {})["alpaca_symbol_claim_token"])
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.live_runner.is_kill_switch_active",
        lambda: True,
    )
    adapter_factory = MagicMock(side_effect=AssertionError("broker access forbidden"))

    result = tick_live_session(db, sess.id, adapter_factory=adapter_factory)
    db.commit()
    db.refresh(sess)

    assert result["blocked"] is True
    assert result["broker_calls"] == 0
    assert sess.state == STATE_LIVE_ERROR
    adapter_factory.assert_not_called()
    readable, retired = read_action_claim(
        db,
        symbol=sess.symbol,
        account_scope="alpaca:paper",
    )
    assert readable is True and retired is not None
    assert retired["phase"] == "resolved"
    assert retired["claim_token"] == old_token
    rearmed = acquire_action_claim(
        db,
        symbol=sess.symbol,
        action="entry",
        claim_token=f"kill-rearm-{uuid.uuid4().hex}",
        owner_session_id=int(sess.id),
        metadata={"test": "post_kill_generation"},
        account_scope="alpaca:paper",
    )
    assert rearmed.get("ok") is True and rearmed.get("replaced") is True


def test_clean_decline_retires_pre_http_claim_before_cancelling(
    monkeypatch,
    db: Session,
) -> None:
    sess = _mk_pending_entry_session(db, "BRAI", execution_family="alpaca_spot")
    sess.state = STATE_WATCHING_LIVE
    db.commit()
    db.refresh(sess)
    old_token = str((sess.risk_snapshot_json or {})["alpaca_symbol_claim_token"])
    monkeypatch.setattr(
        settings,
        "chili_momentum_clean_decline_terminal_enabled",
        True,
    )

    _decline_terminal(db, sess, reason="certified_policy_decline")
    db.commit()
    db.refresh(sess)

    assert sess.state == STATE_LIVE_CANCELLED
    readable, retired = read_action_claim(
        db,
        symbol=sess.symbol,
        account_scope="alpaca:paper",
    )
    assert readable is True and retired is not None
    assert retired["phase"] == "resolved"
    assert retired["claim_token"] == old_token
    rearmed = acquire_action_claim(
        db,
        symbol=sess.symbol,
        action="entry",
        claim_token=f"decline-rearm-{uuid.uuid4().hex}",
        owner_session_id=int(sess.id),
        metadata={"test": "post_decline_generation"},
        account_scope="alpaca:paper",
    )
    assert rearmed.get("ok") is True and rearmed.get("replaced") is True


def test_entry_places_marketable_limit_not_market(monkeypatch, db: Session) -> None:
    reset_duplicate_client_order_guard_for_tests()
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.live_runner._venue_broker_connected",
        lambda _ef: True,
    )
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.risk_policy._account_equity_usd",
        lambda *_args, **_kwargs: 10_000.0,
    )
    monkeypatch.setattr(settings, "brain_enable_decision_ledger", False)
    # This case isolates the non-maker marketable-limit path.  The operator's
    # local environment may enable Coinbase maker-only posting globally.
    monkeypatch.setattr(settings, "chili_coinbase_maker_only_enabled", False)
    # Don't require a brain decision packet for this unit (it's exercised elsewhere).
    monkeypatch.setattr(settings, "brain_decision_packet_required_for_runners", False)
    sess = _mk_pending_entry_session(db, "SOL-USD")
    ad = _mk_adapter()  # ask=100.05 -> guarded_ask 100.05*1.0025=100.30 (penny ceil)

    with patch(
        "app.services.trading.momentum_neural.live_runner.is_kill_switch_active",
        return_value=False,
    ):
        out = tick_live_session(db, sess.id, adapter_factory=lambda: ad)
    db.commit(); db.refresh(sess)

    # Entry must be a marketable LIMIT, never a market order (no thin-book sweep).
    ad.place_market_order.assert_not_called()
    assert ad.place_limit_order_gtc.call_count == 1, out
    kw = ad.place_limit_order_gtc.call_args.kwargs
    assert kw["side"] == "buy"
    # capped at the guarded ask (ask + the notional-guard buffer), penny-ceil'd:
    # 100.05 * 1.0025 = 100.300125 -> ceil-penny -> 100.31
    assert kw["limit_price"] == "100.31", (out, kw)
    le = (sess.risk_snapshot_json or {}).get("momentum_live_execution", {})
    assert le.get("entry_order_type") == "limit"
    assert le.get("entry_limit_price") == "100.31"


def test_duplicate_client_id_retries_lookup_without_resubmit(monkeypatch, db: Session) -> None:
    """ACTU regression: a lost first ack followed by Alpaca 40010001 must never
    return to WATCHING or place a third order. Broker truth is recovered by the
    deterministic client id on the next pending-entry tick."""
    reset_duplicate_client_order_guard_for_tests()
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(settings, "brain_decision_packet_required_for_runners", False)
    monkeypatch.setattr(settings, "chili_momentum_decouple_watching_enabled", False)
    sess = _mk_pending_entry_session(db, "ACTU", execution_family="alpaca_spot")
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.live_runner._venue_broker_connected",
        lambda _ef: True,
    )
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.live_runner.runner_boundary_risk_ok",
        lambda *_args, **_kwargs: (True, {"allowed": True}),
    )
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.risk_policy._account_equity_usd",
        lambda *_args, **_kwargs: 10_000.0,
    )
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.market_profile.schedule_window_now",
        lambda: "hot",
    )
    ad = _certify_alpaca_adapter(_mk_adapter())
    ad.list_positions.side_effect = lambda: ([], _fresh())
    ad.list_open_orders.side_effect = lambda **_kwargs: ([], _fresh())
    ad.get_position_quantity.side_effect = lambda *_args, **_kwargs: float(
        ad.place_limit_order_gtc.call_args.kwargs["base_size"]
    )

    def _setup_bbo(*_args, **_kwargs):
        meta = _fresh()
        return (
            NormalizedTicker(
                product_id="ACTU",
                bid=1.479,
                ask=1.48,
                mid=1.4795,
                freshness=meta,
            ),
            meta,
        )

    ad.get_best_bid_ask.side_effect = _setup_bbo
    boundary_order: list[str] = []

    def _reserve_before_bbo(_settings, *, lane_key):
        assert lane_key
        boundary_order.append("reserve")
        # A real bucket is allowed to wait here; the execution BBO must be fetched
        # only after this reservation returns.
        return AcquireResult(acquired=True, waited_s=1.25, refill_rps=2.0)

    monkeypatch.setattr(
        "app.services.trading.momentum_neural.rail_governor.acquire_rail",
        _reserve_before_bbo,
    )
    def _execution_bbo(*_args, **_kwargs):
        boundary_order.append("bbo")
        _actu_meta = _fresh()
        return (
            NormalizedTicker(
                product_id="ACTU",
                bid=1.479,
                ask=1.48,
                mid=1.4795,
                freshness=_actu_meta,
                raw={"feed": "iqfeed_l1", "tape_row_id": 1},
            ),
            _actu_meta,
        )

    ad.get_execution_bbo.side_effect = _execution_bbo
    ad.place_deadman_stop.return_value = {"ok": False, "error": "test_disabled"}

    def _duplicate_place(**_kwargs):
        boundary_order.append("place")
        return {
            "ok": False,
            "error": '{"code":40010001,"message":"client_order_id must be unique"}',
        }

    ad.place_limit_order_gtc.side_effect = _duplicate_place

    lookups = {"count": 0, "cid": None}

    def _submitted_raw() -> dict:
        request = ad.place_limit_order_gtc.call_args.kwargs
        return {
            "qty": str(request["base_size"]),
            "time_in_force": str(request["time_in_force"]),
            "extended_hours": bool(request["extended_hours"]),
            "position_intent": str(request["position_intent"]),
            "limit_price": str(request["limit_price"]),
        }

    def _lookup(cid):
        lookups["count"] += 1
        lookups["cid"] = cid
        if lookups["count"] == 1:
            return None, _fresh()
        submitted_qty = float(ad.place_limit_order_gtc.call_args.kwargs["base_size"])
        return (
            NormalizedOrder(
                order_id="alpaca-actu-entry",
                client_order_id=cid,
                product_id="ACTU",
                side="buy",
                status="filled",
                order_type="limit",
                filled_size=submitted_qty,
                average_filled_price=1.48,
                raw=_submitted_raw(),
            ),
            _fresh(),
        )

    ad.get_order_by_client_order_id.side_effect = _lookup
    def _get_order(_order_id):
        submitted_qty = float(ad.place_limit_order_gtc.call_args.kwargs["base_size"])
        return (
            NormalizedOrder(
                order_id="alpaca-actu-entry",
                client_order_id=str(lookups["cid"]),
                product_id="ACTU",
                side="buy",
                status="filled",
                order_type="limit",
                filled_size=submitted_qty,
                average_filled_price=1.48,
                raw=_submitted_raw(),
            ),
            _fresh(),
        )

    ad.get_order.side_effect = _get_order

    with patch(
        "app.services.trading.momentum_neural.live_runner.is_kill_switch_active",
        return_value=False,
    ):
        first = tick_live_session(db, sess.id, adapter_factory=lambda: ad)
    db.commit(); db.refresh(sess)

    first_le = (sess.risk_snapshot_json or {})["momentum_live_execution"]
    assert first.get("reconcile") == "dup_reference_client_id_pending", (
        first,
        {
            key: first_le.get(key)
            for key in (
                "entry_reservation_stop_price",
                "entry_notional_guard",
                "entry_limit_price",
                "entry_want_qty",
            )
        },
    )
    assert sess.state == STATE_LIVE_PENDING_ENTRY
    assert first_le["entry_submitted"] is True
    assert first_le["entry_order_id"] is None
    assert first_le["entry_reconcile_pending_client_order_id"] == lookups["cid"]
    assert ad.place_limit_order_gtc.call_count == 1
    first_buy = ad.place_limit_order_gtc.call_args.kwargs
    assert first_buy["limit_price"] == "1.48"
    assert first_le["entry_limit_price"] == "1.48"
    assert first_le["entry_notional_guard"]["planned_limit_price"] == "1.49"
    assert first_le["entry_notional_guard"]["actual_submitted_limit_price"] == "1.48"
    assert first_le["entry_final_bbo"]["broker_limit_price"] == "1.48"
    assert boundary_order.index("reserve") < boundary_order.index("bbo")
    assert boundary_order.index("bbo") < boundary_order.index("place")

    with patch(
        "app.services.trading.momentum_neural.live_runner.is_kill_switch_active",
        return_value=False,
    ):
        second = tick_live_session(db, sess.id, adapter_factory=lambda: ad)
    db.commit(); db.refresh(sess)

    second_le = (sess.risk_snapshot_json or {})["momentum_live_execution"]
    assert second.get("ok") is True
    # Exact owner-claim recovery may independently re-read the same CID; the
    # invariant is idempotent identity and one broker submit, not lookup count.
    assert lookups["count"] >= 2
    assert second_le["entry_order_id"] == "alpaca-actu-entry"
    assert "entry_reconcile_pending_client_order_id" not in second_le
    buy_calls = [
        call for call in ad.place_limit_order_gtc.call_args_list
        if call.kwargs.get("side") == "buy"
    ]
    assert len(buy_calls) == 1  # later sell-to-strength order is not a replacement entry


def test_indeterminate_first_submit_recovers_accepted_fill_without_resubmit(
    monkeypatch,
    db: Session,
) -> None:
    """Alpaca accepted the deterministic id, but the response timed out. The
    runner must recover that exact order and adopt its fill, never terminalize or
    send a replacement entry."""
    sess, ad = _prepare_alpaca_submit_recovery_case(
        monkeypatch,
        db,
        symbol="ACTU",
    )
    captured: dict = {}

    def _accepted_then_timeout(**kwargs):
        captured["cid"] = str(kwargs["client_order_id"])
        captured["quantity"] = str(kwargs["base_size"])
        captured["request"] = dict(kwargs)
        return {
            "ok": False,
            "error": "ReadTimeout: response timed out after submit",
            "client_order_id": captured["cid"],
            "submit_outcome": "indeterminate",
            "error_type": "ReadTimeout",
            "http_status": None,
        }

    ad.place_limit_order_gtc.side_effect = _accepted_then_timeout

    def _filled_order(cid):
        return (
            NormalizedOrder(
                order_id="alpaca-timeout-entry",
                client_order_id=cid,
                product_id="ACTU",
                side="buy",
                status="filled",
                order_type="limit",
                filled_size=float(captured["quantity"]),
                average_filled_price=1.48,
                raw={
                    "qty": captured["quantity"],
                    "time_in_force": str(captured["request"]["time_in_force"]),
                    "extended_hours": bool(captured["request"]["extended_hours"]),
                    "position_intent": str(captured["request"]["position_intent"]),
                    "limit_price": str(captured["request"]["limit_price"]),
                },
            ),
            _fresh(),
        )

    ad.get_order_by_client_order_id.side_effect = _filled_order
    ad.get_order.side_effect = lambda _oid: _filled_order(captured["cid"])

    first = tick_live_session(db, sess.id, adapter_factory=lambda: ad)
    db.commit()
    db.refresh(sess)

    first_le = (sess.risk_snapshot_json or {})["momentum_live_execution"]
    assert first["reconcile"] == "indeterminate_submit_order_recovered"
    assert sess.state == STATE_LIVE_PENDING_ENTRY
    assert first_le["entry_order_id"] == "alpaca-timeout-entry"
    assert first_le["entry_client_order_id"] == captured["cid"]
    assert ad.place_limit_order_gtc.call_count == 1

    second = tick_live_session(db, sess.id, adapter_factory=lambda: ad)
    db.commit()
    db.refresh(sess)

    second_le = (sess.risk_snapshot_json or {})["momentum_live_execution"]
    assert second.get("ok") is True
    assert isinstance(second_le.get("position"), dict)
    assert second_le["position"]["quantity"] == float(captured["quantity"])
    buy_calls = [
        call
        for call in ad.place_limit_order_gtc.call_args_list
        if call.kwargs.get("side") == "buy"
    ]
    assert len(buy_calls) == 1


def test_indeterminate_first_submit_stays_pending_until_client_id_is_visible(
    monkeypatch,
    db: Session,
) -> None:
    """A response-less first submit with no visible broker order remains
    indeterminate. Repeated ticks may only re-read the exact client id; they may
    not return to WATCHING, terminalize, or place a second clip."""
    sess, ad = _prepare_alpaca_submit_recovery_case(
        monkeypatch,
        db,
        symbol="BRAI",
    )
    captured: dict[str, str] = {}

    def _connection_reset(**kwargs):
        captured["cid"] = str(kwargs["client_order_id"])
        return {
            "ok": False,
            "error": "ConnectionResetError while awaiting submit response",
            "client_order_id": captured["cid"],
            "submit_outcome": "indeterminate",
            "error_type": "ConnectionResetError",
            "http_status": None,
        }

    ad.place_limit_order_gtc.side_effect = _connection_reset
    ad.get_order_by_client_order_id.return_value = (None, _fresh())

    first = tick_live_session(db, sess.id, adapter_factory=lambda: ad)
    db.commit()
    db.refresh(sess)
    first_le = (sess.risk_snapshot_json or {})["momentum_live_execution"]

    assert first["reconcile"] == "indeterminate_submit_client_id_pending"
    assert sess.state == STATE_LIVE_PENDING_ENTRY
    assert first_le["entry_reconcile_pending_client_order_id"] == captured["cid"]
    assert ad.place_limit_order_gtc.call_count == 1

    second = tick_live_session(db, sess.id, adapter_factory=lambda: ad)
    db.commit()
    db.refresh(sess)
    second_le = (sess.risk_snapshot_json or {})["momentum_live_execution"]

    assert second["pending"] == "entry_client_id_reconcile"
    assert sess.state == STATE_LIVE_PENDING_ENTRY
    assert second_le["entry_reconcile_pending_client_order_id"] == captured["cid"]
    assert ad.get_order_by_client_order_id.call_count == 2
    assert ad.place_limit_order_gtc.call_count == 1
