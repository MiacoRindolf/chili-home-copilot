"""Safety invariants for account-scoped momentum risk admission.

These tests intentionally keep broker and database I/O out of process.  The
query doubles model a ledger read that fails after the query has been built,
which must never be interpreted as a flat account or zero in-flight risk.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.services.trading.momentum_neural import live_runner
from app.services.trading.momentum_neural import risk_evaluator as evaluator
from app.services.trading.momentum_neural import risk_policy
from app.services.trading.venue.protocol import FreshnessMeta


_ALPACA_ACCOUNT_ID = "00000000-0000-0000-0000-000000000001"


class _FailingQuery:
    def filter(self, *_args, **_kwargs):
        return self

    def all(self):
        raise RuntimeError("risk ledger unavailable")


class _FailingLedger:
    def query(self, *_args, **_kwargs):
        return _FailingQuery()


@pytest.mark.parametrize(
    ("reader", "kwargs"),
    [
        (
            evaluator.aggregate_open_risk_usd,
            {"user_id": 7, "execution_family": "alpaca_spot"},
        ),
        (
            evaluator.count_inflight_entry_orders,
            {"user_id": 7, "execution_family": "alpaca_spot"},
        ),
        (
            evaluator.sum_inflight_entry_risk_usd,
            {
                "user_id": 7,
                "execution_family": "alpaca_spot",
                "per_trade_fallback_usd": 50.0,
            },
        ),
    ],
)
def test_risk_ledger_failure_never_fabricates_zero_exposure(reader, kwargs):
    with pytest.raises(RuntimeError, match="risk ledger unavailable"):
        reader(_FailingLedger(), **kwargs)


def test_aggregate_admission_rejects_unknown_open_risk():
    admitted, meta = risk_policy.admit_by_aggregate_risk(
        open_risk_usd=float("nan"),
        candidate_risk_usd=50.0,
        equity_usd=10_000.0,
        budget_fraction=0.03,
    )

    assert admitted is False
    assert meta["reason"] == "open_risk_invalid"


@pytest.mark.parametrize("candidate", [0.0, -1.0, float("nan"), float("inf")])
def test_aggregate_admission_rejects_nonpositive_or_nonfinite_candidate(candidate):
    admitted, meta = risk_policy.admit_by_aggregate_risk(
        open_risk_usd=0.0,
        candidate_risk_usd=candidate,
        equity_usd=10_000.0,
        budget_fraction=0.03,
    )

    assert admitted is False
    assert meta["reason"] == "candidate_risk_invalid"


@pytest.mark.parametrize("family", ["alpaca_spot", "alpaca_short"])
def test_alpaca_per_trade_policy_cap_scales_from_equity(
    monkeypatch: pytest.MonkeyPatch,
    family: str,
):
    monkeypatch.setattr(
        risk_policy,
        "_account_equity_usd",
        lambda *_args, **_kwargs: 75_000.0,
    )
    monkeypatch.setattr(
        risk_policy.settings,
        "chili_momentum_risk_loss_fraction_of_equity",
        0.01,
        raising=False,
    )

    assert risk_policy.equity_relative_loss_cap(50.0, family) == 750.0


@pytest.mark.parametrize(
    "bad_or_oversized_cap",
    [0.0, -1.0, float("nan"), "not-a-number", 50.01, 500.0],
)
@pytest.mark.parametrize("family", ["alpaca_spot", "alpaca_short"])
def test_alpaca_activation_only_hard_cap_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
    bad_or_oversized_cap,
    family: str,
):
    monkeypatch.setattr(
        risk_policy.settings,
        "chili_momentum_risk_max_loss_per_trade_usd",
        bad_or_oversized_cap,
        raising=False,
    )

    assert risk_policy.alpaca_paper_hard_loss_cap_usd(family) is None


def _entry_claim(*, phase: str = "resolved") -> dict:
    request = {
        "product_id": "ACTU",
        "side": "buy",
        "base_size": "10",
        "limit_price": "2.50",
        "client_order_id": "entry-cid",
        "position_intent": "buy_to_open",
        "order_type": "limit",
        "time_in_force": "day",
        "extended_hours": False,
    }
    return {
        "account_scope": "alpaca:paper",
        "symbol": "ACTU",
        "claim_token": "entry-token",
        "action": "entry",
        "phase": phase,
        "owner_session_id": 71,
        "client_order_id": "entry-cid",
        "broker_order_id": "entry-oid",
        "metadata": {"order_role": "primary", "order_request": request},
    }


def _entry_order() -> SimpleNamespace:
    return SimpleNamespace(
        order_id="entry-oid",
        client_order_id="entry-cid",
        product_id="ACTU",
        side="buy",
        status="filled",
        order_type="limit",
        filled_size=10.0,
        raw={
            "qty": "10",
            "time_in_force": "day",
            "extended_hours": False,
            "position_intent": "buy_to_open",
            "limit_price": "2.50",
        },
    )


def _retry_cap_session(**overrides) -> tuple[SimpleNamespace, dict]:
    intents = [
        {
            "product_id": "ACTU",
            "side": "sell",
            "client_order_id": f"old-exit-{idx}",
        }
        for idx in range(8)
    ]
    le = {
        "position": {
            "product_id": "ACTU",
            "side": "long",
            "quantity": 10.0,
            "avg_entry_price": 2.50,
        },
        "exit_submit_attempts": 8,
        "exit_execution_intents": intents,
        "exit_submit_transport_identities": [
            {
                "attempt_no": idx + 1,
                "alpaca_account_id": _ALPACA_ACCOUNT_ID,
                "product_id": "ACTU",
                "side": "sell",
                "client_order_id": f"old-exit-{idx}",
            }
            for idx in range(8)
        ],
    }
    snapshot = {
        "alpaca_account_scope": "alpaca:paper",
        "alpaca_account_id": _ALPACA_ACCOUNT_ID,
        "alpaca_symbol_claim_token": "entry-token",
        live_runner.KEY_LIVE_EXEC: le,
    }
    values = {
        "id": 71,
        "symbol": "ACTU",
        "execution_family": "alpaca_spot",
        "state": live_runner.STATE_LIVE_SCALING_OUT,
        "risk_snapshot_json": snapshot,
        "correlation_id": "corr-71",
    }
    values.update(overrides)
    return SimpleNamespace(**values), le


class _ExactRetryCapAdapter:
    def __init__(self, *, quantity=10.0, open_orders=()):
        self.quantity = quantity
        self.open_orders = open_orders
        self.place_calls = 0

    def get_position_quantity(self, product_id):
        assert product_id == "ACTU"
        return self.quantity

    def get_account_snapshot(self):
        return {
            "ok": True,
            "paper": True,
            "account_id": _ALPACA_ACCOUNT_ID,
        }

    def list_open_orders(self, *, product_id, limit, strict):
        assert (product_id, limit, strict) == ("ACTU", 100, True)
        return self.open_orders, FreshnessMeta(
            retrieved_at_utc=datetime.now(timezone.utc),
            max_age_seconds=5.0,
        )

    def get_order(self, order_id):
        assert order_id == "entry-oid"
        return _entry_order(), None

    def place_limit_order_gtc(self, **_kwargs):
        self.place_calls += 1
        pytest.fail("retry-cap proof attempted a duplicate limit POST")

    def place_market_order(self, **_kwargs):
        self.place_calls += 1
        pytest.fail("retry-cap proof attempted a duplicate market POST")


@pytest.mark.parametrize(
    ("orders", "expected_ok", "expected_reason"),
    [
        ((), True, "ok"),
        (None, False, "open_order_truth_unreadable"),
    ],
)
def test_retry_cap_open_order_truth_parses_real_alpaca_tuple_contract(
    orders,
    expected_ok,
    expected_reason,
):
    ok, reason, normalized = live_runner._retry_cap_exact_open_orders_truth(
        _ExactRetryCapAdapter(open_orders=orders),
        symbol="ACTU",
    )

    assert ok is expected_ok
    assert reason == expected_reason
    assert normalized == []


def test_retry_cap_promotes_exact_alpaca_paper_long_to_durable_emergency_authority(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        live_runner.settings,
        "chili_alpaca_paper",
        True,
        raising=False,
    )
    sess, le = _retry_cap_session()
    transitions: list[str] = []
    events: list[str] = []
    acquired: list[dict] = []
    monkeypatch.setattr(live_runner, "_broker_position_confirms_zero", lambda _sess: False)
    monkeypatch.setattr(
        live_runner,
        "_strict_client_order_id_truth",
        lambda _adapter, cid: (
            ("found", _entry_order()) if cid == "entry-cid" else ("absent", None)
        ),
    )
    monkeypatch.setattr(
        live_runner,
        "read_action_claim",
        lambda *_args, **_kwargs: (True, _entry_claim()),
    )

    def _acquire(_db, **kwargs):
        acquired.append(kwargs)
        return {
            "ok": True,
            "claim": {
                **_entry_claim(phase="claimed"),
                "metadata": dict(kwargs["metadata"]),
            },
            "replaced": True,
        }

    monkeypatch.setattr(live_runner, "acquire_action_claim", _acquire)
    monkeypatch.setattr(
        live_runner,
        "_safe_transition",
        lambda _db, _sess, state: transitions.append(state),
    )
    monkeypatch.setattr(
        live_runner,
        "_emit",
        lambda _db, _sess, event, _payload: events.append(event),
    )

    result = live_runner._live_exit_submit_succeeded(
        object(),
        sess,
        adapter=_ExactRetryCapAdapter(),
        le=le,
        result={"ok": False, "cap_exceeded": True, "attempts": 8},
        reason="trail_stop",
    )

    assert result is False
    assert transitions == []
    assert sess.state == live_runner.STATE_LIVE_SCALING_OUT
    assert acquired and acquired[0]["action"] == "entry"
    assert acquired[0]["claim_token"] == "entry-token"
    assert acquired[0]["client_order_id"] == "entry-cid"
    assert acquired[0]["metadata"]["runner_exit_guard"] is True
    authority = le["emergency_exit_authority"]
    assert authority["phase"] == "prepared"
    assert authority["identity_contract"] == "alpaca_close_v1"
    assert authority["source_exit_reason"] == "trail_stop"
    assert authority["entry_exposure_claim_token"] == "entry-token"
    assert le["alpaca_entry_claim_resolution_pending"]["retain_until_broker_flat"] is True
    assert le["alpaca_entries_quarantined"] is True
    assert le["exit_submit_attempts"] == 0
    assert "exit_retry_cap_emergency_block" not in le
    assert "alpaca_close_only_recertification" not in sess.risk_snapshot_json
    assert "live_exit_retry_cap_emergency_promoted" in events


def test_retry_cap_adopts_one_exact_late_visible_close_without_duplicate_post(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        live_runner.settings,
        "chili_alpaca_paper",
        True,
        raising=False,
    )
    sess, le = _retry_cap_session()
    transport_row = le["exit_submit_transport_identities"][-1]
    transport_row.update({
        "base_size": "10",
        "position_intent": "sell_to_close",
        "order_type": "limit",
        "time_in_force": "gtc",
        "extended_hours": False,
        "limit_price": "2.45",
        "recorded_at_utc": "2026-07-13T17:01:02",
    })
    late_order = SimpleNamespace(
        order_id="late-visible-close-oid",
        client_order_id="old-exit-7",
        product_id="ACTU",
        side="sell",
        status="new",
        order_type="limit",
        filled_size=0.0,
        raw={
            "qty": "10",
            "limit_price": "2.45",
            "time_in_force": "gtc",
            "extended_hours": False,
            "position_intent": "sell_to_close",
        },
    )
    adapter = _ExactRetryCapAdapter(open_orders=(late_order,))
    acquired: list[dict] = []

    def _strict_truth(_adapter, cid):
        if cid == "old-exit-7":
            return "found", late_order
        if cid == "entry-cid":
            return "found", _entry_order()
        return "absent", None

    def _acquire(_db, **kwargs):
        acquired.append(kwargs)
        return {
            "ok": True,
            "claim": {
                **_entry_claim(phase="claimed"),
                "metadata": dict(kwargs["metadata"]),
            },
        }

    monkeypatch.setattr(live_runner, "_strict_client_order_id_truth", _strict_truth)
    monkeypatch.setattr(
        live_runner,
        "read_action_claim",
        lambda *_args, **_kwargs: (True, _entry_claim()),
    )
    monkeypatch.setattr(live_runner, "acquire_action_claim", _acquire)
    monkeypatch.setattr(live_runner, "_emit", lambda *_args, **_kwargs: None)

    promoted = live_runner._promote_alpaca_retry_cap_to_emergency(
        object(),
        sess,
        adapter,
        le=le,
        reason="trail_stop",
        attempts=8,
    )

    assert promoted == "promoted"
    assert sess.state == live_runner.STATE_LIVE_SCALING_OUT
    authority = le["emergency_exit_authority"]
    assert authority["phase"] == "submitted"
    assert authority["client_order_id"] == "old-exit-7"
    assert authority["order_id"] == "late-visible-close-oid"
    assert authority["submitted_quantity"] == 10.0
    assert authority["recovered_from_retry_cap_transport_chain"] is True
    assert authority["order_request"] == {
        "account_scope": "alpaca:paper",
        "alpaca_account_id": _ALPACA_ACCOUNT_ID,
        "product_id": "ACTU",
        "side": "sell",
        "base_size": "10",
        "client_order_id": "old-exit-7",
        "position_intent": "sell_to_close",
        "order_type": "limit",
        "time_in_force": "gtc",
        "extended_hours": False,
        "limit_price": "2.45",
    }
    assert le["exit_order_id"] == "late-visible-close-oid"
    assert le["exit_client_order_id"] == "old-exit-7"
    assert le["pending_exit_quantity"] == 10.0
    assert acquired and acquired[0]["claim_token"] == "entry-token"


@pytest.mark.parametrize(
    ("status", "open_at_snapshot"),
    [
        ("partially_filled", True),
        ("cancelled", False),
    ],
)
def test_retry_cap_adopts_and_accounts_exact_partial_close_remainder_once(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    open_at_snapshot: bool,
):
    monkeypatch.setattr(
        live_runner.settings,
        "chili_alpaca_paper",
        True,
        raising=False,
    )
    sess, le = _retry_cap_session()
    transport_row = le["exit_submit_transport_identities"][-1]
    transport_row.update({
        "base_size": "10",
        "position_intent": "sell_to_close",
        "order_type": "limit",
        "time_in_force": "gtc",
        "extended_hours": False,
        "limit_price": "2.45",
        "recorded_at_utc": "2026-07-13T17:01:02",
    })
    partial_order = SimpleNamespace(
        order_id="partial-close-oid",
        client_order_id="old-exit-7",
        product_id="ACTU",
        side="sell",
        status=status,
        order_type="limit",
        filled_size=4.0,
        average_filled_price=2.40,
        raw={
            "qty": "10",
            "limit_price": "2.45",
            "time_in_force": "gtc",
            "extended_hours": False,
            "position_intent": "sell_to_close",
            "total_fees": "0.05",
        },
    )
    adapter = _ExactRetryCapAdapter(
        quantity=6.0,
        open_orders=((partial_order,) if open_at_snapshot else ()),
    )

    def _strict_truth(_adapter, cid):
        if cid == "old-exit-7":
            return "found", partial_order
        if cid == "entry-cid":
            return "found", _entry_order()
        return "absent", None

    monkeypatch.setattr(live_runner, "_strict_client_order_id_truth", _strict_truth)
    monkeypatch.setattr(
        live_runner,
        "read_action_claim",
        lambda *_args, **_kwargs: (True, _entry_claim()),
    )
    monkeypatch.setattr(
        live_runner,
        "acquire_action_claim",
        lambda _db, **kwargs: {
            "ok": True,
            "claim": {
                **_entry_claim(phase="claimed"),
                "metadata": dict(kwargs["metadata"]),
            },
        },
    )
    monkeypatch.setattr(live_runner, "_emit", lambda *_args, **_kwargs: None)

    promoted = live_runner._promote_alpaca_retry_cap_to_emergency(
        object(),
        sess,
        adapter,
        le=le,
        reason="trail_stop",
        attempts=8,
    )

    assert promoted == "promoted"
    assert adapter.place_calls == 0
    assert le["position"]["quantity"] == pytest.approx(6.0)
    assert le["realized_pnl_usd"] == pytest.approx(-0.45)
    assert le["fees_usd_total"] == pytest.approx(0.05)
    authority = le["emergency_exit_authority"]
    assert authority["phase"] == "submitted"
    assert authority["applied_filled_size"] == pytest.approx(4.0)
    assert authority["retry_cap_broker_remaining_quantity"] == pytest.approx(6.0)
    assert le["pending_exit_quantity"] == pytest.approx(10.0)


def test_retry_cap_partial_without_fill_price_quarantines_accounting_not_pnl(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        live_runner.settings,
        "chili_alpaca_paper",
        True,
        raising=False,
    )
    sess, le = _retry_cap_session()
    le["exit_submit_transport_identities"][-1].update({
        "base_size": "10",
        "position_intent": "sell_to_close",
        "order_type": "limit",
        "time_in_force": "gtc",
        "extended_hours": False,
        "limit_price": "2.45",
        "recorded_at_utc": "2026-07-13T17:01:02",
    })
    terminal_partial = SimpleNamespace(
        order_id="partial-no-price-oid",
        client_order_id="old-exit-7",
        product_id="ACTU",
        side="sell",
        status="cancelled",
        order_type="limit",
        filled_size=4.0,
        average_filled_price=None,
        raw={
            "qty": "10",
            "limit_price": "2.45",
            "time_in_force": "gtc",
            "extended_hours": False,
            "position_intent": "sell_to_close",
        },
    )
    adapter = _ExactRetryCapAdapter(quantity=6.0, open_orders=())

    def _strict_truth(_adapter, cid):
        if cid == "old-exit-7":
            return "found", terminal_partial
        if cid == "entry-cid":
            return "found", _entry_order()
        return "absent", None

    monkeypatch.setattr(live_runner, "_strict_client_order_id_truth", _strict_truth)
    monkeypatch.setattr(
        live_runner,
        "read_action_claim",
        lambda *_args, **_kwargs: (True, _entry_claim()),
    )
    monkeypatch.setattr(
        live_runner,
        "acquire_action_claim",
        lambda _db, **kwargs: {
            "ok": True,
            "claim": {
                **_entry_claim(phase="claimed"),
                "metadata": dict(kwargs["metadata"]),
            },
        },
    )
    monkeypatch.setattr(live_runner, "_emit", lambda *_args, **_kwargs: None)

    assert live_runner._promote_alpaca_retry_cap_to_emergency(
        object(),
        sess,
        adapter,
        le=le,
        reason="trail_stop",
        attempts=8,
    ) == "promoted"

    assert adapter.place_calls == 0
    assert "realized_pnl_usd" not in le
    assert le["position"]["quantity"] == pytest.approx(6.0)
    pending = le["emergency_exit_accounting_pending"]
    assert pending["unpriced_quantity"] == pytest.approx(4.0)
    assert pending["remaining_quantity"] == pytest.approx(6.0)
    assert le["emergency_exit_authority"]["applied_filled_size"] == pytest.approx(4.0)


@pytest.mark.parametrize(
    ("adapter", "cid_truth", "expected_block"),
    [
        (
            _ExactRetryCapAdapter(),
            "unknown",
            "prior_exit_cid_truth_unknown",
        ),
        (
            _ExactRetryCapAdapter(open_orders=None),
            "absent",
            "open_order_truth_unreadable",
        ),
        (
            _ExactRetryCapAdapter(open_orders=(object(),)),
            "absent",
            "competing_open_order_present",
        ),
        (
            _ExactRetryCapAdapter(quantity=9.0),
            "absent",
            "broker_local_quantity_mismatch",
        ),
    ],
)
def test_retry_cap_uncertain_exact_proof_stays_runnable_without_new_claim(
    monkeypatch: pytest.MonkeyPatch,
    adapter,
    cid_truth,
    expected_block,
):
    monkeypatch.setattr(
        live_runner.settings,
        "chili_alpaca_paper",
        True,
        raising=False,
    )
    sess, le = _retry_cap_session()
    claims: list[dict] = []
    transitions: list[str] = []
    monkeypatch.setattr(live_runner, "_broker_position_confirms_zero", lambda _sess: False)
    monkeypatch.setattr(
        live_runner,
        "_strict_client_order_id_truth",
        lambda _adapter, _cid: (cid_truth, None),
    )
    monkeypatch.setattr(
        live_runner,
        "acquire_action_claim",
        lambda *_args, **kwargs: claims.append(kwargs),
    )
    monkeypatch.setattr(
        live_runner,
        "read_action_claim",
        lambda *_args, **_kwargs: pytest.fail("claim read crossed an unproven close boundary"),
    )
    monkeypatch.setattr(live_runner, "_emit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        live_runner,
        "_safe_transition",
        lambda _db, _sess, state: transitions.append(state),
    )

    result = live_runner._live_exit_submit_succeeded(
        object(),
        sess,
        adapter=adapter,
        le=le,
        result={"ok": False, "cap_exceeded": True, "attempts": 8},
        reason="stop",
    )

    assert result is False
    assert transitions == []
    assert sess.state == live_runner.STATE_LIVE_SCALING_OUT
    assert claims == []
    assert "emergency_exit_authority" not in le
    assert le["exit_retry_cap_emergency_block"]["block_reason"] == expected_block
    assert live_runner._paused_session_has_exit_authority(sess) is True


@pytest.mark.parametrize(
    "case",
    [
        "short_family",
        "crypto_symbol",
        "live_posture",
        "missing_scope",
        "explicit_short",
        "missing_direction",
        "local_symbol_mismatch",
    ],
)
def test_retry_cap_promotion_rejects_uncertified_postures_without_broker_reads(
    monkeypatch: pytest.MonkeyPatch,
    case: str,
):
    monkeypatch.setattr(
        live_runner.settings,
        "chili_alpaca_paper",
        case != "live_posture",
        raising=False,
    )
    sess, le = _retry_cap_session()
    if case == "short_family":
        sess.execution_family = "alpaca_short"
    elif case == "crypto_symbol":
        sess.symbol = "ACTU-USD"
        le["position"]["product_id"] = "ACTU-USD"
    elif case == "missing_scope":
        sess.risk_snapshot_json.pop("alpaca_account_scope")
    elif case == "explicit_short":
        le["position"]["side"] = "short"
    elif case == "missing_direction":
        le["position"].pop("side")
    elif case == "local_symbol_mismatch":
        le["position"]["product_id"] = "OTHER"

    class _NoReads:
        def get_position_quantity(self, _symbol):
            pytest.fail("uncertified posture reached broker position read")

        def list_open_orders(self, **_kwargs):
            pytest.fail("uncertified posture reached broker order read")

    monkeypatch.setattr(
        live_runner,
        "read_action_claim",
        lambda *_args, **_kwargs: pytest.fail("uncertified posture reached claim read"),
    )
    monkeypatch.setattr(
        live_runner,
        "acquire_action_claim",
        lambda *_args, **_kwargs: pytest.fail("uncertified posture reached claim mutation"),
    )

    promoted = live_runner._promote_alpaca_retry_cap_to_emergency(
        object(),
        sess,
        _NoReads(),
        le=le,
        reason="stop",
        attempts=8,
    )

    assert promoted == "not_applicable"
    assert "emergency_exit_authority" not in le
    assert "exit_retry_cap_emergency_block" not in le
    assert "alpaca_entries_quarantined" not in le


def test_retry_cap_broker_flat_resolution_clears_entry_quarantine_before_recycle(
    monkeypatch: pytest.MonkeyPatch,
):
    sess, le = _retry_cap_session()
    le["alpaca_entry_claim_resolution_pending"] = {
        "claim_token": "entry-token",
        "client_order_id": "entry-cid",
        "broker_order_id": "entry-oid",
        "broker_order_status": "filled",
        "filled_size": 10.0,
        "retain_until_broker_flat": True,
        "runner_exit_guard": True,
    }
    le["alpaca_entries_quarantined"] = True
    calls: list[dict] = []

    def _resolve(_db, **kwargs):
        calls.append(kwargs)
        return True

    monkeypatch.setattr(live_runner, "resolve_action_claim", _resolve)

    assert live_runner._resolve_retained_alpaca_entry_claim_after_broker_flat(
        object(),
        sess,
        le=le,
    ) is True
    assert calls and calls[0]["terminal_owner_broker_flat"] is True
    assert calls[0]["broker_position_zero"] is True
    assert "alpaca_entry_claim_resolution_pending" not in le
    assert "alpaca_entries_quarantined" not in le


def test_retry_cap_entry_claim_is_not_released_while_position_is_still_held(
    monkeypatch: pytest.MonkeyPatch,
):
    sess, le = _retry_cap_session()
    le["alpaca_entry_claim_resolution_pending"] = {
        "claim_token": "entry-token",
        "client_order_id": "entry-cid",
        "broker_order_id": "entry-oid",
        "broker_order_status": "filled",
        "filled_size": 10.0,
        "retain_until_broker_flat": True,
        "runner_exit_guard": True,
    }
    monkeypatch.setattr(
        live_runner,
        "resolve_action_claim_committed",
        lambda **_kwargs: pytest.fail("held exposure released its durable entry claim"),
    )

    assert live_runner._resolve_committed_alpaca_entry_claim_pending(sess, le) is True
    assert le["alpaca_entry_claim_resolution_pending"]["retain_until_broker_flat"] is True


class _DurableOwnerOutbox:
    """In-memory model of the independently committed retained-owner row."""

    def __init__(self, events: list[str]):
        self.events = events
        self.current: dict | None = None
        self.history: list[dict] = []

    def read_claim(self, **_kwargs):
        claim = _entry_claim(phase="claimed")
        metadata = dict(claim["metadata"])
        entry_request = dict(metadata["order_request"])
        entry_request["alpaca_account_id"] = _ALPACA_ACCOUNT_ID
        metadata.update({
            "alpaca_account_id": _ALPACA_ACCOUNT_ID,
            "order_request": entry_request,
            "owner_transport_history": deepcopy(self.history),
        })
        if self.current is not None:
            metadata["owner_transport"] = deepcopy(self.current)
        claim["metadata"] = metadata
        return True, claim

    def lease(self, **kwargs):
        request = deepcopy(kwargs["order_request"])
        cid = str(kwargs["client_order_id"])
        lease_token = str(kwargs["lease_token"])
        current = self.current
        if current is not None and current.get("phase") != "resolved":
            same = bool(
                current.get("transport_kind") == kwargs["transport_kind"]
                and current.get("client_order_id") == cid
                and current.get("order_request") == request
            )
            if same and kwargs.get("strict_cid_absent_after_expiry") is True:
                current.update({
                    "phase": "submitting",
                    "lease_token": lease_token,
                    "lease_expires_at_utc": (
                        datetime.now(timezone.utc) + timedelta(minutes=5)
                    ).isoformat(),
                    "same_cid_replay_count": int(
                        current.get("same_cid_replay_count") or 0
                    ) + 1,
                })
                self.events.append(f"outbox_commit:{cid}")
                return {
                    "ok": True,
                    "transport": deepcopy(current),
                    "same_cid_replay": True,
                }
            return {
                "ok": False,
                "reason": "owner_transport_leased",
                "transport": deepcopy(current),
            }
        self.current = {
            "identity_contract": "alpaca_owner_transport_v1",
            "transport_kind": str(kwargs["transport_kind"]),
            "client_order_id": cid,
            "order_request": request,
            "phase": "submitting",
            "lease_token": lease_token,
            "lease_expires_at_utc": (
                datetime.now(timezone.utc) + timedelta(minutes=5)
            ).isoformat(),
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        self.events.append(f"outbox_commit:{cid}")
        return {"ok": True, "transport": deepcopy(self.current)}

    def advance(self, **kwargs):
        if not (
            self.current is not None
            and self.current.get("client_order_id") == kwargs["client_order_id"]
            and self.current.get("lease_token") == kwargs["lease_token"]
        ):
            return False
        self.current.update({
            "phase": kwargs["phase"],
            "broker_order_id": kwargs.get("broker_order_id"),
        })
        self.events.append(f"outbox_advance:{kwargs['client_order_id']}")
        return True

    def resolve(self, **kwargs):
        if not (
            self.current is not None
            and self.current.get("client_order_id") == kwargs["client_order_id"]
            and self.current.get("lease_token") == kwargs["lease_token"]
        ):
            return False
        self.current.update({
            "phase": "resolved",
            "broker_order_status": kwargs["broker_order_status"],
            "filled_size": float(kwargs["filled_size"]),
        })
        self.history.append(deepcopy(self.current))
        self.events.append(f"outbox_resolve:{kwargs['client_order_id']}")
        return True


class _DurableExitAdapter:
    def __init__(self, events: list[str], outcomes: list[object]):
        self.events = events
        self.outcomes = list(outcomes)
        self.orders: dict[str, SimpleNamespace] = {}
        self.place_requests: list[dict] = []
        self.place_methods: list[str] = []

    def get_account_snapshot(self):
        return {
            "ok": True,
            "paper": True,
            "account_id": _ALPACA_ACCOUNT_ID,
        }

    def get_position_quantity(self, product_id):
        assert product_id == "ACTU"
        return 10.0

    def get_order_by_client_order_id_truth(self, cid):
        if cid in self.orders:
            return {"readable": True, "found": True, "order": self.orders[cid]}
        return {"readable": True, "found": False, "order": None}

    def _place(self, order_type: str, kwargs: dict):
        request = deepcopy(kwargs)
        cid = str(request["client_order_id"])
        self.place_requests.append(request)
        self.place_methods.append(order_type)
        self.events.append(f"place:{cid}")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        result = dict(outcome)
        result.setdefault("client_order_id", cid)
        if result.get("ok") and result.get("order_id"):
            self.orders[cid] = SimpleNamespace(
                order_id=result["order_id"],
                client_order_id=cid,
                product_id=request["product_id"],
                side=request["side"],
                status=result.get("status") or "new",
                order_type=order_type,
                filled_size=0.0,
                raw={
                    "qty": request["base_size"],
                    "limit_price": request.get("limit_price"),
                    "time_in_force": request["time_in_force"],
                    "extended_hours": request.get("extended_hours", False),
                    "position_intent": request["position_intent"],
                },
            )
        return result

    def place_limit_order_gtc(self, **kwargs):
        return self._place("limit", kwargs)

    def place_market_order(self, **kwargs):
        return self._place("market", kwargs)


class _ExitDb:
    def __init__(self, events: list[str]):
        self.events = events

    def flush(self):
        self.events.append("outer_flush")

    def commit(self):
        self.events.append("outer_commit")

    def rollback(self):
        self.events.append("outer_rollback")


def _install_durable_exit_seams(
    monkeypatch: pytest.MonkeyPatch,
    outbox: _DurableOwnerOutbox,
):
    freshness = SimpleNamespace(age_seconds=lambda **_kwargs: 0.0)
    final_tick = SimpleNamespace(
        product_id="ACTU",
        bid=2.48,
        ask=2.50,
        mid=2.49,
        freshness=freshness,
    )
    monkeypatch.setattr(
        live_runner,
        "_final_entry_bbo",
        lambda *_args, **_kwargs: (final_tick, {"reason": "ok"}),
    )
    monkeypatch.setattr(
        live_runner,
        "_cancel_scale_limit_and_clamp",
        lambda _db, _sess, _adapter, *, le, requested_qty, reason: requested_qty,
    )
    monkeypatch.setattr(
        live_runner,
        "_record_live_exit_intent_safe",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(live_runner, "_emit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(live_runner, "read_action_claim_committed", outbox.read_claim)
    monkeypatch.setattr(live_runner, "lease_owner_transport_committed", outbox.lease)
    monkeypatch.setattr(live_runner, "advance_owner_transport_committed", outbox.advance)
    monkeypatch.setattr(
        live_runner,
        "resolve_owner_transport_terminal_committed",
        outbox.resolve,
    )
    from app.services.trading.momentum_neural import market_profile

    monkeypatch.setattr(
        market_profile,
        "market_session_now",
        lambda _symbol, *, now=None: "regular",
    )


def test_alpaca_exit_transport_identity_is_durable_before_each_post(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        live_runner.settings,
        "chili_alpaca_paper",
        True,
        raising=False,
    )
    events: list[str] = []
    outbox = _DurableOwnerOutbox(events)
    _install_durable_exit_seams(monkeypatch, outbox)
    adapter = _DurableExitAdapter(
        events,
        [
            {
                "ok": False,
                "error": "deterministic_reject",
                "submit_outcome": "broker_rejected",
            },
            {
                "ok": False,
                "error": "deterministic_reject",
                "submit_outcome": "broker_rejected",
            },
        ],
    )
    sess, le = _retry_cap_session()
    le["exit_submit_attempts"] = 0
    le.pop("exit_submit_transport_identities", None)
    db = _ExitDb(events)

    first = live_runner._submit_live_market_exit(
        db,
        sess,
        adapter,
        le=le,
        product_id="ACTU",
        quantity=10.0,
        client_order_id="exit-cid-1",
        reason="stop",
        bid=2.48,
        ask=2.50,
        mid=2.49,
    )
    assert first["ok"] is False
    assert "outbox_commit:exit-cid-1" in events, (first, events, le)
    assert events.index("outbox_commit:exit-cid-1") < events.index("place:exit-cid-1")
    first_identity = le["exit_submit_transport_identities"][0]
    assert first_identity["attempt_no"] == 1
    assert first_identity["alpaca_account_id"] == _ALPACA_ACCOUNT_ID
    assert first_identity["product_id"] == "ACTU"
    assert first_identity["side"] == "sell"
    assert first_identity["base_size"] == "10"
    assert first_identity["client_order_id"] == "exit-cid-1"
    assert first_identity["position_intent"] == "sell_to_close"
    assert first_identity["order_type"] == "limit"
    assert first_identity["proven_no_transport"] is True
    assert first_identity["recorded_at_utc"]

    le.pop("exit_next_retry_at_utc", None)
    second = live_runner._submit_live_market_exit(
        db,
        sess,
        adapter,
        le=le,
        product_id="ACTU",
        quantity=10.0,
        client_order_id="exit-cid-2",
        reason="stop",
        bid=2.48,
        ask=2.50,
        mid=2.49,
    )
    assert second["ok"] is False
    assert events.index("outbox_commit:exit-cid-2") < events.index("place:exit-cid-2")
    assert [
        row["client_order_id"] for row in le["exit_submit_transport_identities"]
    ] == ["exit-cid-1", "exit-cid-2"]
    assert all(
        row["proven_no_transport"] is True
        for row in le["exit_submit_transport_identities"]
    )


def test_alpaca_exit_outbox_survives_outer_rollback_and_replays_same_cid(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        live_runner.settings,
        "chili_alpaca_paper",
        True,
        raising=False,
    )
    events: list[str] = []
    outbox = _DurableOwnerOutbox(events)
    _install_durable_exit_seams(monkeypatch, outbox)
    adapter = _DurableExitAdapter(
        events,
        [
            TimeoutError("ack lost"),
            {"ok": True, "order_id": "replayed-oid", "status": "new"},
        ],
    )
    sess, le = _retry_cap_session()
    le["exit_submit_attempts"] = 0
    le.pop("exit_submit_transport_identities", None)
    baseline = deepcopy(sess.risk_snapshot_json)
    db = _ExitDb(events)

    with pytest.raises(TimeoutError, match="ack lost"):
        live_runner._submit_live_market_exit(
            db,
            sess,
            adapter,
            le=le,
            product_id="ACTU",
            quantity=10.0,
            client_order_id="exit-cid-1",
            reason="stop",
            bid=2.48,
            ask=2.50,
            mid=2.49,
        )
    assert events.index("outbox_commit:exit-cid-1") < events.index("place:exit-cid-1")
    assert outbox.current is not None
    assert outbox.current["phase"] == "submit_indeterminate"

    # Model the outer session transaction rolling back while the independent
    # owner outbox remains committed. Once its lease expires, recovery may POST
    # only the same immutable CID/request.
    db.rollback()
    sess.risk_snapshot_json = deepcopy(baseline)
    replay_le = sess.risk_snapshot_json[live_runner.KEY_LIVE_EXEC]
    outbox.current["lease_expires_at_utc"] = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    ).isoformat()
    replayed = live_runner._submit_live_market_exit(
        db,
        sess,
        adapter,
        le=replay_le,
        product_id="ACTU",
        quantity=10.0,
        client_order_id="must-not-post-cid-2",
        reason="stop",
        bid=2.48,
        ask=2.50,
        mid=2.49,
    )
    assert replayed["ok"] is True
    assert replayed["client_order_id"] == "exit-cid-1"
    assert [request["client_order_id"] for request in adapter.place_requests] == [
        "exit-cid-1",
        "exit-cid-1",
    ]
    assert adapter.place_requests[0] == adapter.place_requests[1]
    replay_commit_positions = [
        idx
        for idx, event in enumerate(events)
        if event == "outbox_commit:exit-cid-1"
    ]
    place_positions = [
        idx for idx, event in enumerate(events) if event == "place:exit-cid-1"
    ]
    assert len(replay_commit_positions) == len(place_positions) == 2
    assert all(
        commit_idx < place_idx
        for commit_idx, place_idx in zip(replay_commit_positions, place_positions)
    )

    # A second local rollback must adopt the now-visible same order without a
    # third POST or a newly generated CID.
    db.rollback()
    sess.risk_snapshot_json = deepcopy(baseline)
    adopt_le = sess.risk_snapshot_json[live_runner.KEY_LIVE_EXEC]
    adopted = live_runner._submit_live_market_exit(
        db,
        sess,
        adapter,
        le=adopt_le,
        product_id="ACTU",
        quantity=10.0,
        client_order_id="must-not-post-cid-3",
        reason="stop",
        bid=2.48,
        ask=2.50,
        mid=2.49,
    )
    assert adopted["ok"] is True
    assert adopted["recovered_before_duplicate_exit"] is True
    assert adopted["order_id"] == "replayed-oid"
    assert len(adapter.place_requests) == 2


@pytest.mark.parametrize(
    ("first_reason", "replay_reason", "frozen_order_type"),
    [
        ("stop", "operator_flatten", "limit"),
        ("operator_flatten", "stop", "market"),
    ],
)
def test_alpaca_exit_outbox_replay_dispatches_from_frozen_order_type(
    monkeypatch: pytest.MonkeyPatch,
    first_reason: str,
    replay_reason: str,
    frozen_order_type: str,
):
    monkeypatch.setattr(
        live_runner.settings,
        "chili_alpaca_paper",
        True,
        raising=False,
    )
    events: list[str] = []
    outbox = _DurableOwnerOutbox(events)
    _install_durable_exit_seams(monkeypatch, outbox)
    adapter = _DurableExitAdapter(
        events,
        [
            TimeoutError("ack lost"),
            {"ok": True, "order_id": "same-type-replay-oid", "status": "new"},
        ],
    )
    sess, le = _retry_cap_session()
    le["exit_submit_attempts"] = 0
    le.pop("exit_submit_transport_identities", None)
    baseline = deepcopy(sess.risk_snapshot_json)
    db = _ExitDb(events)

    with pytest.raises(TimeoutError, match="ack lost"):
        live_runner._submit_live_market_exit(
            db,
            sess,
            adapter,
            le=le,
            product_id="ACTU",
            quantity=10.0,
            client_order_id="frozen-cid",
            reason=first_reason,
            bid=2.48,
            ask=2.50,
            mid=2.49,
        )
    assert outbox.current is not None
    assert outbox.current["order_request"]["order_type"] == frozen_order_type

    # Roll back every local ladder/counter choice, then deliberately choose the
    # opposite dynamic branch.  The durable request must still select the same
    # literal adapter method as the first POST.
    db.rollback()
    sess.risk_snapshot_json = deepcopy(baseline)
    replay_le = sess.risk_snapshot_json[live_runner.KEY_LIVE_EXEC]
    outbox.current["lease_expires_at_utc"] = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    ).isoformat()
    replayed = live_runner._submit_live_market_exit(
        db,
        sess,
        adapter,
        le=replay_le,
        product_id="ACTU",
        quantity=10.0,
        client_order_id="ephemeral-opposite-cid",
        reason=replay_reason,
        bid=2.48,
        ask=2.50,
        mid=2.49,
    )

    assert replayed["ok"] is True
    assert replayed["client_order_id"] == "frozen-cid"
    assert adapter.place_methods == [frozen_order_type, frozen_order_type]
    assert adapter.place_requests[0] == adapter.place_requests[1]


@pytest.mark.parametrize(
    ("symbol", "asset_location"),
    [
        ("BTC/USD", None),
        ("ACTU", "snapshot"),
        ("ACTU", "live_exec"),
        ("ACTU", "position"),
    ],
)
def test_live_runner_broker_boundary_quarantines_all_explicit_crypto_shapes(
    monkeypatch: pytest.MonkeyPatch,
    symbol: str,
    asset_location: str | None,
):
    monkeypatch.setattr(
        live_runner.settings,
        "chili_alpaca_paper",
        True,
        raising=False,
    )
    sess, le = _retry_cap_session(symbol=symbol)
    le["position"]["product_id"] = symbol
    if asset_location == "snapshot":
        sess.risk_snapshot_json["asset_class"] = "crypto"
    elif asset_location == "live_exec":
        le["asset_type"] = "cryptocurrency"
    elif asset_location == "position":
        le["position"]["asset_kind"] = "digital-asset"

    assert (
        live_runner._alpaca_execution_quarantine_reason(sess)
        == "alpaca_crypto_execution_not_certified"
    )
