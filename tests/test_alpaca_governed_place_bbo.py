from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.config import settings
from app.services.trading import governance as gov
from app.services.trading.momentum_neural import live_runner as lr
from app.services.trading.venue.protocol import (
    FreshnessMeta,
    NormalizedOrder,
    NormalizedTicker,
)


TEST_ALPACA_ACCOUNT_ID = "acct-governed-place-test"


def _fresh() -> FreshnessMeta:
    now = datetime.now(timezone.utc)
    return FreshnessMeta(
        retrieved_at_utc=now,
        provider_time_utc=now,
        max_age_seconds=2.0,
    )


def _tick(symbol: str) -> NormalizedTicker:
    meta = _fresh()
    return NormalizedTicker(
        product_id=symbol,
        bid=9.99,
        ask=10.01,
        mid=10.0,
        freshness=meta,
        raw={
            "feed": "iqfeed_l1",
            "provider_event_at_utc": datetime.now(timezone.utc).isoformat(),
            "received_at_utc": datetime.now(timezone.utc).isoformat(),
            "timestamp_basis": "provider_quote_event_at",
        },
    )


def _alpaca_session(*, live_execution: dict | None = None):
    live = dict(
        live_execution
        or {
            "side_long": True,
            "effective_max_hold_seconds": 3_600,
        }
    )
    live.setdefault("effective_max_hold_seconds", 3_600)
    confirmed_at = datetime.now(timezone.utc).isoformat()
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    arm_token = "arm-governed-place-test"
    claim_token = "claim-governed-place-test"
    return SimpleNamespace(
        id=101,
        user_id=42,
        symbol="ACTU",
        execution_family="alpaca_spot",
        risk_snapshot_json={
            "arm_token": arm_token,
            "expires_at_utc": expires_at,
            "arm_confirmed_at_utc": confirmed_at,
            "alpaca_account_scope": "alpaca:paper",
            "alpaca_account_id": TEST_ALPACA_ACCOUNT_ID,
            "alpaca_symbol_claim_token": claim_token,
            "confirmed_arm_generation": {
                "version": 1,
                "session_id": 101,
                "arm_token": arm_token,
                "expires_at_utc": expires_at,
                "alpaca_symbol_claim_token": claim_token,
                "alpaca_account_scope": "alpaca:paper",
                "alpaca_account_id": TEST_ALPACA_ACCOUNT_ID,
                "confirmed_at_utc": confirmed_at,
            },
            "momentum_live_execution": live,
        },
    )


class _CertifiedAdapter:
    def get_account_snapshot(self):
        return {
            "ok": True,
            "paper": True,
            "account_id": TEST_ALPACA_ACCOUNT_ID,
        }

    def get_market_clock_snapshot(self):
        now = datetime.now(timezone.utc)
        return {
            "ok": True,
            "paper": True,
            "is_open": True,
            "timestamp": now.isoformat(),
            "next_close": (now + timedelta(hours=4)).isoformat(),
        }


@pytest.fixture(autouse=True)
def _certified_alpaca_boundary(monkeypatch):
    monkeypatch.setattr(
        settings,
        "chili_alpaca_expected_account_id",
        TEST_ALPACA_ACCOUNT_ID,
        raising=False,
    )
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.market_profile.market_session_now",
        lambda _symbol: "regular",
    )


def _rail():
    return SimpleNamespace(acquired=True, waited_s=0.0, refill_rps=1.0)


def test_confirmed_arm_generation_is_exact_for_entries_but_not_required_for_close():
    session = _alpaca_session()
    assert lr._confirmed_alpaca_arm_generation_reason(session) is None

    session.risk_snapshot_json["confirmed_arm_generation"]["arm_token"] = "stale"
    assert (
        lr._confirmed_alpaca_arm_generation_reason(session)
        == "alpaca_confirmed_arm_generation_mismatch"
    )
    entry = {
        "product_id": "ACTU",
        "side": "buy",
        "position_intent": "buy_to_open",
        "base_size": "5",
        "limit_price": "10.00",
        "client_order_id": "stale-generation-entry",
        "extended_hours": False,
        "time_in_force": "day",
    }
    _claim, _cid, early = lr._prepare_alpaca_place_claim(
        _CertifiedAdapter(),
        session,
        entry,
        risk_stop_price=9.50,
        account_equity_usd=10_000.0,
    )
    assert early["error"] == "alpaca_confirmed_arm_generation_mismatch"
    assert early["pre_place_blocked"] is True

    close = {
        "product_id": "ACTU",
        "side": "sell",
        "position_intent": "sell_to_close",
        "base_size": "5",
        "limit_price": "9.50",
        "client_order_id": "legacy-held-close",
        "time_in_force": "day",
    }
    assert lr._prepare_alpaca_place_claim(
        _CertifiedAdapter(), session, close
    ) == (None, "", None)


def _healthy_forced_daily_loss(_db, family, *, user_id=None, force_refresh=False):
    assert family == "alpaca_spot"
    assert user_id == 42
    assert force_refresh is True
    return False, {
        "family": family,
        "realized": -10.0,
        "cap": 250.0,
        "transient": False,
        "data_source": "alpaca_account_equity_delta",
        "broker_snapshot_cache_bypassed": True,
    }


def _creator_reservation(captured: dict):
    def _reserve(**kwargs):
        captured.update(kwargs)
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

    return _reserve


def test_direction_helper_rejects_nested_short_or_contradictory_intent():
    assert lr._le_side_long({"side_long": True, "position": {"side_long": False}}) is False
    assert lr._le_side_long({"side_long": True, "position": {"side": "short"}}) is False
    assert lr._le_side_long({"side_long": True, "position": {"intent": "sell_to_open"}}) is False
    assert lr._le_side_long({"side_long": True, "position": {"side": "long"}}) is True


def test_fresh_metadata_and_generic_bbo_cannot_authorize_alpaca_post():
    class GenericOnly:
        def get_best_bid_ask(self, _symbol):
            tick = _tick("ACTU")
            return tick, tick.freshness

    kwargs = {
        "product_id": "ACTU",
        "side": "buy",
        "limit_price": "10.50",
    }
    ok, evidence = lr._final_alpaca_execution_bbo_check(
        GenericOnly(),
        kwargs,
        freshness=_fresh(),
        configured_max_age=2.0,
        risk_stop_price=9.50,
    )
    assert ok is False
    assert evidence["reason"] == "execution_bbo_capability_missing"
    assert kwargs["limit_price"] == "10.50"


def test_strict_execution_bbo_clamps_buy_limit_and_rejects_symbol_mismatch():
    class Strict:
        symbol = "ACTU"

        def get_execution_bbo(self, _symbol, *, max_age_seconds):
            assert max_age_seconds == 2.0
            tick = _tick(self.symbol)
            return tick, tick.freshness

    adapter = Strict()
    kwargs = {
        "product_id": "ACTU",
        "side": "buy",
        "limit_price": "10.50",
    }
    ok, evidence = lr._final_alpaca_execution_bbo_check(
        adapter,
        kwargs,
        freshness=None,
        configured_max_age=2.0,
        risk_stop_price=9.50,
    )
    assert ok is True
    assert float(kwargs["limit_price"]) == 10.01
    assert float(kwargs["limit_price"]) <= 10.50
    assert evidence["spread_risk_gate"]["reason"] == "within_budget"
    assert evidence["source"] == "iqfeed_l1"

    adapter.symbol = "WRONG"
    kwargs["limit_price"] = "10.50"
    ok, evidence = lr._final_alpaca_execution_bbo_check(
        adapter,
        kwargs,
        freshness=None,
        configured_max_age=2.0,
        risk_stop_price=9.50,
    )
    assert ok is False
    assert evidence["reason"] == "execution_bbo_symbol_mismatch"


def test_wide_final_add_bbo_blocks_before_reservation_or_post(monkeypatch):
    class StrictWide(_CertifiedAdapter):
        def get_execution_bbo(self, _symbol, *, max_age_seconds):
            meta = _fresh()
            tick = NormalizedTicker(
                product_id="ACTU",
                bid=9.70,
                ask=10.10,
                mid=9.90,
                freshness=meta,
                raw={
                    "feed": "iqfeed_l1",
                    "provider_event_at_utc": datetime.now(timezone.utc).isoformat(),
                    "received_at_utc": datetime.now(timezone.utc).isoformat(),
                    "timestamp_basis": "provider_quote_event_at",
                },
            )
            return tick, meta

    kwargs = {
        "product_id": "ACTU",
        "side": "buy",
        "limit_price": "10.50",
    }
    ok, evidence = lr._final_alpaca_execution_bbo_check(
        StrictWide(),
        kwargs,
        freshness=None,
        configured_max_age=2.0,
        risk_stop_price=9.50,
    )

    assert ok is False
    assert evidence["reason"] == "alpaca_final_spread_risk_blocked"
    assert evidence["spread_risk_gate"]["spread_fraction_of_risk"] > 0.25

    monkeypatch.setattr(
        "app.services.trading.momentum_neural.risk_policy._account_equity_usd",
        lambda *_a, **_k: 10_000.0,
    )
    monkeypatch.setattr(
        lr,
        "reserve_alpaca_entry_risk_committed",
        lambda **_k: (_ for _ in ()).throw(AssertionError("reservation must not run")),
    )
    place_calls: list[dict] = []
    result = lr._governed_place(
        StrictWide(),
        lambda **place_kwargs: place_calls.append(place_kwargs),
        sess=_alpaca_session(),
        rail_reservation=_rail(),
        alpaca_order_role="pyramid",
        alpaca_risk_stop_price=9.50,
        product_id="ACTU",
        side="buy",
        position_intent="buy_to_open",
        base_size="5",
        limit_price="10.50",
        client_order_id="cid-wide",
        extended_hours=False,
        time_in_force="day",
    )
    assert result["error"] == "alpaca_final_spread_risk_blocked"
    assert place_calls == []


@pytest.mark.parametrize(
    ("side", "intent", "tif", "error"),
    [
        ("sell", None, "day", "alpaca_instruction_side_intent_not_certified"),
        ("sell", "garbage", "day", "alpaca_instruction_side_intent_not_certified"),
        ("sell", "sell_to_open", "day", "alpaca_instruction_side_intent_not_certified"),
        ("buy", None, "day", "alpaca_instruction_side_intent_not_certified"),
        ("buy", "sell_to_close", "day", "alpaca_instruction_side_intent_not_certified"),
        ("buy", "buy_to_open", "gtc", "alpaca_entry_tif_not_day"),
    ],
)
def test_alpaca_boundary_rejects_ambiguous_or_risk_increasing_instruction_without_calls(
    side,
    intent,
    tif,
    error,
):
    class ForbiddenAdapter:
        calls = 0

        def __getattr__(self, _name):
            self.calls += 1
            raise AssertionError("invalid instruction touched the adapter")

    adapter = ForbiddenAdapter()
    place_calls: list[dict] = []
    result = lr._governed_place(
        adapter,
        lambda **kwargs: place_calls.append(kwargs),
        sess=_alpaca_session(),
        product_id="ACTU",
        side=side,
        position_intent=intent,
        base_size="1",
        limit_price="10.00",
        client_order_id="cid-invalid",
        extended_hours=False,
        time_in_force=tif,
    )
    assert result["error"] == error
    assert result["pre_place_blocked"] is True
    assert adapter.calls == 0
    assert place_calls == []


def test_tight_final_bbo_freezes_same_day_request_used_at_post(monkeypatch):
    class StrictFlat(_CertifiedAdapter):
        def get_execution_bbo(self, _symbol, *, max_age_seconds):
            tick = _tick("ACTU")
            return tick, tick.freshness

        def list_positions(self):
            return [], _fresh()

        def list_open_orders(self, *, strict=False):
            assert strict is True
            return [], _fresh()

    monkeypatch.setattr(
        "app.services.trading.momentum_neural.risk_policy._account_equity_usd",
        lambda *_a, **_k: 10_000.0,
    )
    monkeypatch.setattr(
        gov,
        "broker_daily_loss_breached",
        _healthy_forced_daily_loss,
    )
    frozen: dict = {}

    def _reserve(**kwargs):
        frozen.update(kwargs)
        return {
            "ok": True,
            "created": True,
            "claim": {
                "symbol": "ACTU",
                "claim_token": "entry-101",
                "account_scope": "alpaca:paper",
                "phase": "claimed",
                "client_order_id": "cid-tight",
                "metadata": {
                    "entry_post_bind_token": kwargs["post_bind_token"],
                },
            },
        }

    monkeypatch.setattr(lr, "reserve_alpaca_entry_risk_committed", _reserve)
    monkeypatch.setattr(
        lr,
        "mark_entry_transport_started_committed",
        lambda **_k: True,
    )
    monkeypatch.setattr(lr, "update_action_claim_phase_committed", lambda **_k: True)
    post_calls: list[dict] = []

    def _post(**kwargs):
        post_calls.append(dict(kwargs))
        return {
            "ok": True,
            "order_id": "oid-tight",
            "client_order_id": kwargs["client_order_id"],
            "status": "open",
        }

    result = lr._governed_place(
        StrictFlat(),
        _post,
        sess=_alpaca_session(),
        rail_reservation=_rail(),
        alpaca_order_role="primary",
        alpaca_risk_stop_price=9.50,
        product_id="ACTU",
        side="buy",
        position_intent="buy_to_open",
        base_size="5",
        limit_price="10.50",
        client_order_id="cid-tight",
        extended_hours=False,
        time_in_force="gfd",
    )

    assert result["ok"] is True
    assert len(post_calls) == 1
    request = frozen["order_request"]
    assert request["time_in_force"] == "day"
    assert request["position_intent"] == "buy_to_open"
    assert request["limit_price"] == post_calls[0]["limit_price"] == "10.01"
    assert post_calls[0]["time_in_force"] == "day"
    assert (
        frozen["role_metadata"]["broker_daily_loss_admission"]
        ["broker_snapshot_cache_bypassed"]
        is True
    )


@pytest.mark.parametrize(
    ("positions", "orders", "expected"),
    [
        ([{"product_id": "MANUAL", "qty": 1}], [], "alpaca_account_position_exposure_present"),
        ([], [object()], "alpaca_account_open_orders_present"),
        (None, [], "alpaca_account_posture_unreadable"),
        ([], None, "alpaca_account_posture_unreadable"),
    ],
)
def test_broker_account_posture_blocks_before_reservation_and_post(
    monkeypatch,
    positions,
    orders,
    expected,
):
    class StrictPosture(_CertifiedAdapter):
        def get_execution_bbo(self, _symbol, *, max_age_seconds):
            tick = _tick("ACTU")
            return tick, tick.freshness

        def list_positions(self):
            return positions, _fresh()

        def list_open_orders(self, *, strict=False):
            assert strict is True
            return orders, _fresh()

    monkeypatch.setattr(
        "app.services.trading.momentum_neural.risk_policy._account_equity_usd",
        lambda *_a, **_k: 10_000.0,
    )
    monkeypatch.setattr(
        lr,
        "reserve_alpaca_entry_risk_committed",
        lambda **_k: (_ for _ in ()).throw(AssertionError("reservation must not run")),
    )
    post_calls: list[dict] = []
    result = lr._governed_place(
        StrictPosture(),
        lambda **kwargs: post_calls.append(kwargs),
        sess=_alpaca_session(),
        rail_reservation=_rail(),
        alpaca_order_role="primary",
        alpaca_risk_stop_price=9.50,
        product_id="ACTU",
        side="buy",
        position_intent="buy_to_open",
        base_size="5",
        limit_price="10.50",
        client_order_id="cid-posture",
        extended_hours=False,
        time_in_force="day",
    )
    assert result["error"] == expected
    assert post_calls == []


@pytest.mark.parametrize(
    ("outcome", "expected_error"),
    [
        (
            (
                True,
                {
                    "family": "alpaca_spot",
                    "realized": -260.0,
                    "cap": 250.0,
                    "transient": False,
                    "broker_snapshot_cache_bypassed": True,
                },
            ),
            "alpaca_broker_daily_loss_limit_breached",
        ),
        (
            (
                True,
                {
                    "family": "alpaca_spot",
                    "realized": None,
                    "cap": 250.0,
                    "transient": True,
                    "reason": "alpaca_account_daily_change_unavailable",
                    "broker_snapshot_cache_bypassed": True,
                },
            ),
            "alpaca_broker_daily_loss_snapshot_unavailable",
        ),
        (RuntimeError("offline"), "alpaca_broker_daily_loss_snapshot_unavailable"),
    ],
)
def test_literal_daily_loss_refresh_blocks_before_reservation_and_post(
    monkeypatch,
    outcome,
    expected_error,
):
    class StrictFlat(_CertifiedAdapter):
        def get_execution_bbo(self, _symbol, *, max_age_seconds):
            tick = _tick("ACTU")
            return tick, tick.freshness

        def list_positions(self):
            return [], _fresh()

        def list_open_orders(self, *, strict=False):
            assert strict is True
            return [], _fresh()

    refresh_calls: list[dict] = []

    def _forced(_db, family, *, user_id=None, force_refresh=False):
        refresh_calls.append(
            {
                "family": family,
                "user_id": user_id,
                "force_refresh": force_refresh,
            }
        )
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(
        "app.services.trading.momentum_neural.risk_policy._account_equity_usd",
        lambda *_a, **_k: 10_000.0,
    )
    monkeypatch.setattr(gov, "broker_daily_loss_breached", _forced)
    monkeypatch.setattr(
        lr,
        "reserve_alpaca_entry_risk_committed",
        lambda **_k: (_ for _ in ()).throw(
            AssertionError("reservation must not run")
        ),
    )
    post_calls: list[dict] = []

    result = lr._governed_place(
        StrictFlat(),
        lambda **kwargs: post_calls.append(kwargs),
        sess=_alpaca_session(),
        rail_reservation=_rail(),
        alpaca_order_role="primary",
        alpaca_risk_stop_price=9.50,
        product_id="ACTU",
        side="buy",
        position_intent="buy_to_open",
        base_size="5",
        limit_price="10.50",
        client_order_id="cid-daily-loss",
        extended_hours=False,
        time_in_force="day",
    )

    assert result["error"] == expected_error
    assert result["pre_place_blocked"] is True
    assert refresh_calls == [
        {"family": "alpaca_spot", "user_id": 42, "force_refresh": True}
    ]
    assert post_calls == []


def test_reused_claim_unknown_or_mismatched_cid_truth_never_retries_post(monkeypatch):
    cid = "cid-reused"
    request = {
        "product_id": "ACTU",
        "side": "buy",
        "base_size": "5",
        "limit_price": "10.00",
        "client_order_id": cid,
        "position_intent": "buy_to_open",
        "order_type": "limit",
        "time_in_force": "day",
        "extended_hours": False,
    }
    claim = {
        "symbol": "ACTU",
        "claim_token": "entry-101",
        "account_scope": "alpaca:paper",
        "phase": "claimed",
        "client_order_id": cid,
        "metadata": {"order_role": "primary", "order_request": dict(request)},
    }
    monkeypatch.setattr(
        lr,
        "reserve_alpaca_entry_risk_committed",
        lambda **_k: {
            "ok": True,
            "reused": True,
            "client_order_id_bound": False,
            "prior_phase": "claimed",
            "prior_lease_expires_at": "2000-01-01T00:00:00+00:00",
            "claim": dict(claim),
        },
    )

    class StrictTruth(_CertifiedAdapter):
        def __init__(self, result):
            self.result = result
            self.calls = 0

        def get_order_by_client_order_id_truth(self, _cid):
            self.calls += 1
            return self.result

    for truth, expected in (
        ({"readable": False, "found": False, "order": None}, "alpaca_claim_reconcile_pending"),
        (
            {
                "readable": True,
                "found": True,
                "order": SimpleNamespace(
                    order_id="oid-wrong",
                    client_order_id=cid,
                    product_id="ACTU",
                    side="sell",
                ),
            },
            "alpaca_claim_identity_mismatch",
        ),
    ):
        # Each truth scenario is an independent recovery attempt.  The governed
        # claim seam deliberately freezes/mutates the session's claim token, so
        # reusing one in-memory session here would manufacture a stale arm
        # generation before the second CID-truth assertion is reached.
        session = _alpaca_session()
        adapter = StrictTruth(truth)
        kwargs = {
            "product_id": "ACTU",
            "side": "buy",
            "position_intent": "buy_to_open",
            "base_size": "5",
            "limit_price": "10.00",
            "client_order_id": cid,
            "extended_hours": False,
            "time_in_force": "day",
        }
        _claim, _cid, early = lr._prepare_alpaca_place_claim(
            adapter,
            session,
            kwargs,
            order_role="primary",
            risk_stop_price=9.50,
            account_equity_usd=10_000.0,
        )
        assert early["error"] == expected
        assert early["pre_place_blocked"] is True
        assert adapter.calls == 1


def test_sub_dollar_canonical_request_reserves_executable_risk_and_recovers_exactly(
    monkeypatch,
):
    frozen: dict = {}
    monkeypatch.setattr(
        lr,
        "reserve_alpaca_entry_risk_committed",
        _creator_reservation(frozen),
    )
    kwargs = {
        "product_id": "ACTU",
        "side": "buy",
        "position_intent": "buy_to_open",
        "base_size": "100000",
        "limit_price": "0.1238",
        "client_order_id": "cid-canonical-penny",
        "extended_hours": False,
        "time_in_force": "day",
    }
    claim, cid, early = lr._prepare_alpaca_place_claim(
        _CertifiedAdapter(),
        _alpaca_session(),
        kwargs,
        risk_stop_price=0.1233,
        account_equity_usd=10_000.0,
    )

    assert early is None
    assert cid == "cid-canonical-penny"
    assert frozen["order_request"]["limit_price"] == "0.1238"
    # The raw pre-tick idea would reserve only $41.  The executable BUY tick
    # reserves the full $50 and therefore cannot exceed the hard cap silently.
    assert (0.12371 - 0.1233) * 100000 == pytest.approx(41.0)
    assert frozen["reserved_risk_usd"] == pytest.approx(50.0)
    assert frozen["reserved_risk_usd"] <= 50.0 + 1e-9

    order = NormalizedOrder(
        order_id="oid-canonical-penny",
        client_order_id=cid,
        product_id="ACTU",
        side="buy",
        status="open",
        order_type="limit",
        filled_size=0.0,
        average_filled_price=None,
        raw={
            "qty": "100000",
            "limit_price": "0.123800",
            "time_in_force": "day",
            "extended_hours": False,
            "position_intent": "buy_to_open",
        },
    )
    assert lr._alpaca_claim_order_matches(
        order,
        claim,
        frozen["order_request"],
    ) is True


def test_noncanonical_zero_padded_entry_is_rejected_before_reservation(monkeypatch):
    monkeypatch.setattr(
        lr,
        "reserve_alpaca_entry_risk_committed",
        lambda **_k: (_ for _ in ()).throw(
            AssertionError("noncanonical limit reached reservation")
        ),
    )
    result = lr._governed_place(
        object(),
        lambda **_k: (_ for _ in ()).throw(
            AssertionError("noncanonical limit reached transport")
        ),
        sess=_alpaca_session(),
        product_id="ACTU",
        side="buy",
        position_intent="buy_to_open",
        base_size="100",
        limit_price="0.5",
        client_order_id="cid-missing-zeroes",
        extended_hours=False,
        time_in_force="day",
    )

    assert result["error"] == "alpaca_entry_limit_not_canonical"
    assert result["canonical_limit_price"] == "0.5000"
    assert result["pre_place_blocked"] is True


@pytest.mark.parametrize("appears", ["position", "order"])
def test_post_reservation_manual_exposure_releases_claim_without_transport(
    monkeypatch,
    appears,
):
    class ChangesAfterReservation(_CertifiedAdapter):
        def __init__(self):
            self.posture_reads = 0

        def get_execution_bbo(self, _symbol, *, max_age_seconds):
            tick = _tick("ACTU")
            return tick, tick.freshness

        def list_positions(self):
            self.posture_reads += 1
            positions = (
                [{"product_id": "MANUAL", "qty": 1}]
                if self.posture_reads == 2 and appears == "position"
                else []
            )
            return positions, _fresh()

        def list_open_orders(self, *, strict=False):
            assert strict is True
            orders = (
                [object()]
                if self.posture_reads == 2 and appears == "order"
                else []
            )
            return orders, _fresh()

        def cancel_order(self, *_a, **_k):
            raise AssertionError("posture veto must not cancel broker orders")

    frozen: dict = {}
    releases: list[dict] = []
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.risk_policy._account_equity_usd",
        lambda *_a, **_k: 10_000.0,
    )
    monkeypatch.setattr(gov, "broker_daily_loss_breached", _healthy_forced_daily_loss)
    monkeypatch.setattr(
        lr,
        "reserve_alpaca_entry_risk_committed",
        _creator_reservation(frozen),
    )
    monkeypatch.setattr(
        lr,
        "release_entry_claim_pre_post_committed",
        lambda **kwargs: releases.append(dict(kwargs)) or True,
    )
    monkeypatch.setattr(
        lr,
        "mark_entry_transport_started_committed",
        lambda **_k: (_ for _ in ()).throw(
            AssertionError("manual exposure reached transport-start")
        ),
    )
    posts: list[dict] = []
    adapter = ChangesAfterReservation()
    result = lr._governed_place(
        adapter,
        lambda **kwargs: posts.append(dict(kwargs)),
        sess=_alpaca_session(),
        rail_reservation=_rail(),
        alpaca_order_role="primary",
        alpaca_risk_stop_price=9.50,
        product_id="ACTU",
        side="buy",
        position_intent="buy_to_open",
        base_size="5",
        limit_price="10.50",
        client_order_id=f"cid-manual-{appears}",
        extended_hours=False,
        time_in_force="day",
    )

    assert result["error"] == (
        "alpaca_account_position_exposure_present"
        if appears == "position"
        else "alpaca_account_open_orders_present"
    )
    assert result["entry_claim_pre_post_released"] is True
    assert adapter.posture_reads == 2
    assert posts == []
    assert len(releases) == 1
    assert releases[0]["post_bind_token"] == frozen["post_bind_token"]
    assert releases[0]["client_order_id"] == f"cid-manual-{appears}"
    assert releases[0]["reason"] == "alpaca_account_posture_changed_pre_post"


def test_post_reservation_daily_loss_crossing_releases_claim_without_transport(
    monkeypatch,
):
    class AlwaysFlat(_CertifiedAdapter):
        def __init__(self):
            self.posture_reads = 0

        def get_execution_bbo(self, _symbol, *, max_age_seconds):
            tick = _tick("ACTU")
            return tick, tick.freshness

        def list_positions(self):
            self.posture_reads += 1
            return [], _fresh()

        def list_open_orders(self, *, strict=False):
            assert strict is True
            return [], _fresh()

        def cancel_order(self, *_a, **_k):
            raise AssertionError("daily-loss veto must not cancel broker orders")

    daily_reads: list[int] = []

    def _crosses_cap(_db, family, *, user_id=None, force_refresh=False):
        assert family == "alpaca_spot"
        assert user_id == 42
        assert force_refresh is True
        daily_reads.append(1)
        if len(daily_reads) == 1:
            return _healthy_forced_daily_loss(
                _db,
                family,
                user_id=user_id,
                force_refresh=force_refresh,
            )
        return True, {
            "family": family,
            "realized": -251.0,
            "cap": 250.0,
            "transient": False,
            "broker_snapshot_cache_bypassed": True,
        }

    frozen: dict = {}
    releases: list[dict] = []
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.risk_policy._account_equity_usd",
        lambda *_a, **_k: 10_000.0,
    )
    monkeypatch.setattr(gov, "broker_daily_loss_breached", _crosses_cap)
    monkeypatch.setattr(
        lr,
        "reserve_alpaca_entry_risk_committed",
        _creator_reservation(frozen),
    )
    monkeypatch.setattr(
        lr,
        "release_entry_claim_pre_post_committed",
        lambda **kwargs: releases.append(dict(kwargs)) or True,
    )
    monkeypatch.setattr(
        lr,
        "mark_entry_transport_started_committed",
        lambda **_k: (_ for _ in ()).throw(
            AssertionError("crossed loss cap reached transport-start")
        ),
    )
    posts: list[dict] = []
    adapter = AlwaysFlat()
    result = lr._governed_place(
        adapter,
        lambda **kwargs: posts.append(dict(kwargs)),
        sess=_alpaca_session(),
        rail_reservation=_rail(),
        alpaca_order_role="primary",
        alpaca_risk_stop_price=9.50,
        product_id="ACTU",
        side="buy",
        position_intent="buy_to_open",
        base_size="5",
        limit_price="10.50",
        client_order_id="cid-loss-crossing",
        extended_hours=False,
        time_in_force="day",
    )

    assert result["error"] == "alpaca_broker_daily_loss_limit_breached"
    assert result["entry_claim_pre_post_released"] is True
    assert len(daily_reads) == 2
    assert adapter.posture_reads == 2
    assert posts == []
    assert len(releases) == 1
    assert releases[0]["post_bind_token"] == frozen["post_bind_token"]
    assert releases[0]["reason"] == "alpaca_daily_loss_changed_pre_post"
