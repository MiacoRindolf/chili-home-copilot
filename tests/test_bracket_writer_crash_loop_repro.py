"""f-prefilter-bypass-and-cooldown-investigation (2026-05-08).

Pin the two new defences against the ADA/SOL bypass:

  1. **Broker-layer backstop** in
     ``broker_service.place_sell_stop_loss_order``: any Robinhood
     crypto base (ADA, SOL, BTC, etc.) returns
     ``{"ok": False, "error": "crypto_ticker_unsupported_via_equity_primitive"}``
     BEFORE the try/except that previously caught the
     ``get_instruments_by_symbols('ADA')[0]`` IndexError.

  2. **Bracket-writer cooldown on code-bug class broker errors**.
     The 2026-05-09 ADA/SOL crash loop bypassed the existing
     exception cooldown because the broker layer caught the
     IndexError and packaged it as ``ok=False`` instead of letting
     it escape. The new ``_is_code_bug_error`` detection arms the
     same cooldown when the error string matches a Python exception
     class signature, breaking the per-sweep retry loop.

Helper-level (no DB).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.services.trading import bracket_writer_g2 as bw
from app.services.trading.bracket_reconciler import ReconciliationDecision


def _decision_missing_stop():
    return ReconciliationDecision(
        kind="missing_stop", severity="warn", delta_payload={},
    )


@pytest.fixture()
def reset_cooldowns():
    bw._intent_reject_cooldown.clear()
    bw._intent_post_place_cooldown.clear()
    bw._intent_exception_cooldown.clear()
    yield
    bw._intent_reject_cooldown.clear()
    bw._intent_post_place_cooldown.clear()
    bw._intent_exception_cooldown.clear()


# ── _is_code_bug_error matrix ────────────────────────────────────────


@pytest.mark.parametrize("err", [
    "list index out of range",
    "IndexError: list index out of range",
    "TypeError: 'NoneType' object is not subscriptable",
    "AttributeError: 'dict' object has no attribute 'foo'",
    "KeyError: 'missing_key'",
    "crypto_ticker_unsupported_via_equity_primitive",
])
def test_code_bug_patterns_match(err):
    assert bw._is_code_bug_error(err) is True


@pytest.mark.parametrize("err", [
    "Not enough shares to sell",
    "Insufficient shares",
    "Order rejected by venue",
    "",
    None,
])
def test_genuine_broker_rejects_do_not_match(err):
    assert bw._is_code_bug_error(err) is False


def test_robinhood_error_extractor_preserves_non_field_errors():
    from app.services import broker_service as bs

    err = bs._extract_robinhood_error_message(
        {"non_field_errors": ["Invalid time in force for fractional order."]},
        "Robinhood returned no order_id",
    )

    assert err == "non_field_errors: Invalid time in force for fractional order."


def test_fractional_time_in_force_reject_is_terminal():
    assert bw._is_terminal_reject(
        "non_field_errors: Invalid time in force for fractional order."
    )


def test_coinbase_insufficient_balance_reject_is_terminal():
    assert bw._is_terminal_reject("Insufficient balance in source account")


# ── Broker layer: ADA refused before any SDK call ────────────────────


@pytest.mark.parametrize("err", [
    "product info fetch failed for ALCX-USD - refusing to place stop",
    "product_info_unavailable",
])
def test_transient_data_unavailable_patterns_match(err):
    assert bw._is_transient_data_unavailable_error(err) is True


@pytest.mark.parametrize("err", [
    "Not enough shares to sell",
    "Insufficient shares",
    "list index out of range",
    "",
    None,
])
def test_transient_data_unavailable_does_not_match_terminal_or_code_bug(err):
    assert bw._is_transient_data_unavailable_error(err) is False


def test_broker_layer_refuses_ada_crypto_base(monkeypatch):
    """ADA hitting place_sell_stop_loss_order directly must NOT reach
    the rh.orders.order primitive."""
    from app.services import broker_service as bs

    # Simulate a connected broker so we get past the early _rh_available
    # / is_connected gates.
    monkeypatch.setattr(bs, "_rh_available", True, raising=False)
    monkeypatch.setattr(bs, "is_connected", lambda: True)

    # Sentinel: if the SDK is reached, surface a loud failure.
    fake_rh = MagicMock()
    fake_rh.orders.order.side_effect = AssertionError(
        "rh.orders.order MUST NOT be called for crypto bases"
    )
    monkeypatch.setitem(__import__("sys").modules, "robin_stocks.robinhood", fake_rh)

    res = bs.place_sell_stop_loss_order(
        "ADA", 1.0, trigger_price=0.25,
    )
    assert res["ok"] is False
    assert res["error"] == "crypto_ticker_unsupported_via_equity_primitive"


def test_broker_layer_refuses_sol_crypto_base(monkeypatch):
    from app.services import broker_service as bs
    monkeypatch.setattr(bs, "_rh_available", True, raising=False)
    monkeypatch.setattr(bs, "is_connected", lambda: True)

    res = bs.place_sell_stop_loss_order(
        "SOL", 10.0, trigger_price=150.0,
    )
    assert res["ok"] is False
    assert res["error"] == "crypto_ticker_unsupported_via_equity_primitive"


def test_broker_layer_does_not_refuse_equity(monkeypatch):
    """AAPL must NOT trigger the crypto backstop -- the guard is
    suffix/whitelist scoped, not blanket."""
    from app.services import broker_service as bs
    monkeypatch.setattr(bs, "_rh_available", True, raising=False)
    monkeypatch.setattr(bs, "is_connected", lambda: True)

    # We don't want to actually reach robin_stocks. Make _retry_api_call
    # return a fake successful response.
    monkeypatch.setattr(
        bs, "_retry_api_call",
        lambda fn, label: {"id": "RH-1", "state": "queued"},
    )
    monkeypatch.setattr(bs, "_rh_order_session_kwargs", lambda **_: {
        "extendedHours": False, "market_hours": "regular",
    })

    res = bs.place_sell_stop_loss_order(
        "AAPL", 1.0, trigger_price=100.0,
    )
    # Should NOT be the crypto-refusal error.
    assert res.get("error") != "crypto_ticker_unsupported_via_equity_primitive"


# ── Bracket-writer cooldown on swallowed IndexError ──────────────────


def test_swallowed_index_error_arms_exception_cooldown(
    reset_cooldowns, monkeypatch,
):
    """Simulate the historical bypass: broker_service catches an
    IndexError internally and returns it as `ok=False, error="list
    index out of range"`. The bracket_writer must detect the
    code-bug class string and arm the exception cooldown."""
    from app.config import settings
    monkeypatch.setattr(settings, "chili_bracket_writer_g2_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_bracket_writer_g2_place_missing_stop", True, raising=False)

    intent_id = 5001

    # Stub the broker pre-flights so we get past held_for_sells / qty
    # branches into the actual placement.
    monkeypatch.setattr(
        "app.services.broker_service.get_position_held_for_sells",
        lambda *a, **kw: None,
    )
    monkeypatch.setattr(
        "app.services.trading.bracket_writer_g2._g2_event",
        lambda *a, **kw: None,
    )

    adapter = MagicMock()
    adapter.get_products.return_value = ([], True)
    # The bypass: broker returns ok=False with the IndexError text
    # rather than letting the exception escape.
    adapter.place_stop_loss_sell_order.return_value = {
        "ok": False, "error": "list index out of range",
    }

    assert bw._is_in_exception_cooldown(intent_id) is False
    res = bw.place_missing_stop(
        db=MagicMock(),
        trade_id=1,
        bracket_intent_id=intent_id,
        ticker="AAPL",  # bypass the crypto prefilter for this test
        broker_source="robinhood",
        decision=_decision_missing_stop(),
        local_quantity=1.0,
        stop_price=100.0,
        adapter_factory=lambda src: adapter,
    )
    assert res.ok is False
    assert res.reason == "place_failed"
    # KEY ASSERTION: cooldown engaged on the swallowed IndexError.
    assert bw._is_in_exception_cooldown(intent_id) is True


def test_product_info_unavailable_arms_exception_cooldown(
    reset_cooldowns, monkeypatch,
):
    """Coinbase metadata failures must not loosen quantization.

    They are transient data-dependency failures, so one reject arms the
    short exception cooldown instead of retrying every sweep.
    """
    from app.config import settings
    monkeypatch.setattr(settings, "chili_bracket_writer_g2_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_bracket_writer_g2_place_missing_stop", True, raising=False)

    intent_id = 5004
    monkeypatch.setattr(
        "app.services.broker_service.get_position_held_for_sells",
        lambda *a, **kw: None,
    )
    monkeypatch.setattr(
        "app.services.trading.bracket_writer_g2._g2_event",
        lambda *a, **kw: None,
    )

    adapter = MagicMock()
    adapter.get_products.return_value = ([], True)
    adapter.place_stop_limit_order_gtc.return_value = {
        "ok": False,
        "error": (
            "product info fetch failed for ALCX-USD - refusing to place "
            "stop with unquantized price (no magic-fallback policy)"
        ),
    }

    assert bw._is_in_exception_cooldown(intent_id) is False
    res = bw.place_missing_stop(
        db=MagicMock(),
        trade_id=1,
        bracket_intent_id=intent_id,
        ticker="ALCX-USD",
        broker_source="coinbase",
        decision=_decision_missing_stop(),
        local_quantity=4.3931,
        stop_price=3.560422,
        adapter_factory=lambda src: adapter,
    )
    assert res.ok is False
    assert res.reason == "place_failed"
    assert bw._is_in_exception_cooldown(intent_id) is True


def test_subsequent_sweep_after_swallowed_index_error_skips(
    reset_cooldowns, monkeypatch,
):
    """Second sweep after the swallowed-IndexError bypass must SKIP
    via the exception cooldown -- not call the adapter again."""
    from app.config import settings
    monkeypatch.setattr(settings, "chili_bracket_writer_g2_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_bracket_writer_g2_place_missing_stop", True, raising=False)

    intent_id = 5002
    bw._arm_exception_cooldown(intent_id)

    adapter = MagicMock()
    adapter.place_stop_loss_sell_order.side_effect = AssertionError(
        "adapter MUST NOT be called during exception cooldown"
    )

    res = bw.place_missing_stop(
        db=MagicMock(),
        trade_id=1,
        bracket_intent_id=intent_id,
        ticker="AAPL",
        broker_source="robinhood",
        decision=_decision_missing_stop(),
        local_quantity=1.0,
        stop_price=100.0,
        adapter_factory=lambda src: adapter,
    )
    assert res.reason == "in_exception_cooldown"
    adapter.place_stop_loss_sell_order.assert_not_called()


def test_genuine_broker_reject_does_not_arm_exception_cooldown(
    reset_cooldowns, monkeypatch,
):
    """A real broker reject ('Not enough shares') must arm the
    terminal-reject cooldown, NOT the exception cooldown -- they're
    distinct concerns and cooldown durations differ."""
    from app.config import settings
    monkeypatch.setattr(settings, "chili_bracket_writer_g2_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_bracket_writer_g2_place_missing_stop", True, raising=False)

    intent_id = 5003
    monkeypatch.setattr(
        "app.services.broker_service.get_position_held_for_sells",
        lambda *a, **kw: None,
    )
    monkeypatch.setattr(
        "app.services.trading.bracket_writer_g2._g2_event",
        lambda *a, **kw: None,
    )
    monkeypatch.setattr(
        "app.services.trading.bracket_writer_g2.mark_terminal_reject"
        if False else
        "app.services.trading.bracket_intent_writer.mark_terminal_reject",
        lambda *a, **kw: None,
    )

    adapter = MagicMock()
    adapter.get_products.return_value = ([], True)
    adapter.place_stop_loss_sell_order.return_value = {
        "ok": False, "error": "Not enough shares to sell",
    }

    res = bw.place_missing_stop(
        db=MagicMock(),
        trade_id=1,
        bracket_intent_id=intent_id,
        ticker="AAPL",
        broker_source="robinhood",
        decision=_decision_missing_stop(),
        local_quantity=1.0,
        stop_price=100.0,
        adapter_factory=lambda src: adapter,
    )
    assert res.reason == "terminal_reject"
    # Terminal-reject cooldown engaged; exception cooldown did NOT.
    assert bw._is_in_reject_cooldown(intent_id) is True
    assert bw._is_in_exception_cooldown(intent_id) is False


def test_fractional_stop_time_in_force_reject_is_skipped_before_broker(
    reset_cooldowns, monkeypatch,
):
    """Robinhood fractional equity stops are software-managed before broker IO."""
    from app.config import settings

    monkeypatch.setattr(settings, "chili_bracket_writer_g2_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_bracket_writer_g2_place_missing_stop", True, raising=False)
    monkeypatch.setattr(
        "app.services.broker_service.get_position_held_for_sells",
        lambda *a, **kw: None,
    )
    monkeypatch.setattr(
        "app.services.trading.bracket_writer_g2._g2_event",
        lambda *a, **kw: None,
    )
    monkeypatch.setattr(
        "app.services.trading.bracket_intent_writer.mark_terminal_reject",
        lambda *a, **kw: True,
    )

    intent_id = 5005
    adapter = MagicMock()
    adapter.get_products.return_value = ([], True)
    adapter.place_stop_loss_sell_order.return_value = {
        "ok": False,
        "error": "non_field_errors: Invalid time in force for fractional order.",
    }

    res = bw.place_missing_stop(
        db=MagicMock(),
        trade_id=1,
        bracket_intent_id=intent_id,
        ticker="AAOX",
        broker_source="robinhood",
        decision=_decision_missing_stop(),
        local_quantity=0.758481,
        stop_price=7.58,
        adapter_factory=MagicMock(return_value=adapter),
    )

    assert res.reason == "software_stop_managed_robinhood_fractional_equity"
    assert adapter.place_stop_loss_sell_order.call_count == 0
    assert bw._is_in_reject_cooldown(intent_id) is False
    assert bw._is_in_exception_cooldown(intent_id) is False


# ── Full-chain integration: prefilter + backstop + cooldown ──────────


def test_full_chain_ada_prefilter_path(reset_cooldowns, monkeypatch):
    """Full chain through bracket_writer for ADA-USD: the bracket-
    writer prefilter catches it; broker is never called."""
    from app.config import settings
    monkeypatch.setattr(settings, "chili_bracket_writer_g2_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_bracket_writer_g2_place_missing_stop", True, raising=False)

    adapter = MagicMock()
    adapter.place_stop_loss_sell_order.side_effect = AssertionError(
        "adapter must not be called -- prefilter should catch ADA-USD"
    )

    res = bw.place_missing_stop(
        db=MagicMock(),
        trade_id=1,
        bracket_intent_id=6001,
        ticker="ADA-USD",
        broker_source="robinhood",
        decision=_decision_missing_stop(),
        local_quantity=3621.0,
        stop_price=0.25663137,
        adapter_factory=lambda src: adapter,
    )
    assert res.reason == "venue_unsupported_crypto_path"


def test_full_chain_broker_backstop_when_ticker_already_stripped(
    reset_cooldowns, monkeypatch,
):
    """If a hypothetical bypass passes ticker='ADA' (stripped) directly
    to broker_service.place_sell_stop_loss_order, the backstop catches
    it. This is the defence-in-depth path tonight's brief explicitly
    asks for."""
    from app.services import broker_service as bs
    monkeypatch.setattr(bs, "_rh_available", True, raising=False)
    monkeypatch.setattr(bs, "is_connected", lambda: True)

    res = bs.place_sell_stop_loss_order(
        "ADA", 3621.0, trigger_price=0.25663137,
    )
    assert res["ok"] is False
    assert res["error"] == "crypto_ticker_unsupported_via_equity_primitive"


def test_full_chain_backstop_then_cooldown_engagement(
    reset_cooldowns, monkeypatch,
):
    """End-to-end: bracket_writer prefilter is hypothetically bypassed
    (we pass ticker='AAPL' so the equity path runs), the broker
    backstop returns the new error string, the bracket_writer code-bug
    detector recognizes it and arms the cooldown.

    This proves: even if every other defence fails, the cooldown
    breaks the per-sweep loop.
    """
    from app.config import settings
    monkeypatch.setattr(settings, "chili_bracket_writer_g2_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_bracket_writer_g2_place_missing_stop", True, raising=False)

    intent_id = 6002
    monkeypatch.setattr(
        "app.services.broker_service.get_position_held_for_sells",
        lambda *a, **kw: None,
    )
    monkeypatch.setattr(
        "app.services.trading.bracket_writer_g2._g2_event",
        lambda *a, **kw: None,
    )

    # Adapter returns the exact error string the new backstop emits.
    adapter = MagicMock()
    adapter.get_products.return_value = ([], True)
    adapter.place_stop_loss_sell_order.return_value = {
        "ok": False,
        "error": "crypto_ticker_unsupported_via_equity_primitive",
    }

    res = bw.place_missing_stop(
        db=MagicMock(),
        trade_id=1,
        bracket_intent_id=intent_id,
        ticker="AAPL",
        broker_source="robinhood",
        decision=_decision_missing_stop(),
        local_quantity=1.0,
        stop_price=100.0,
        adapter_factory=lambda src: adapter,
    )
    assert res.reason == "place_failed"
    # The cooldown is the third-and-last line of defence.
    assert bw._is_in_exception_cooldown(intent_id) is True
