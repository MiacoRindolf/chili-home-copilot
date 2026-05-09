"""f-coinbase-autotrader-enablement-phase-4-bracket-writer-path (2026-05-09).

Pin venue routing in `bracket_writer_g2.place_missing_stop`:

  * **RH stop path BYTE-IDENTICAL** parity test — captures the exact
    `place_stop_loss_sell_order` call args (product_id, base_size,
    trigger_price, client_order_id) and asserts they match the
    pre-Phase-4 contract.
  * Coinbase routes to the new `place_stop_limit_order_gtc`
    primitive with limit_price = stop_price * (1 - buffer_pct).
  * Coinbase crypto ticker (e.g. ADA-USD) reaches placement; the
    ALL-crypto refusal narrowed to RH-only.
  * RH crypto still SKIPPED with `venue_unsupported_crypto_path`
    (the 2026-05-08 prefilter unchanged for RH).

Helper-level (no DB; mocked adapter via the existing
`adapter_factory` injection seam in `place_missing_stop`).
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


def _common_monkeypatches(monkeypatch):
    """Stub heavy plumbing so the placement path is the only thing
    under test."""
    from app.config import settings
    monkeypatch.setattr(
        settings, "chili_bracket_writer_g2_enabled", True, raising=False,
    )
    monkeypatch.setattr(
        settings, "chili_bracket_writer_g2_place_missing_stop", True,
        raising=False,
    )
    monkeypatch.setattr(
        "app.services.broker_service.get_position_held_for_sells",
        lambda *a, **kw: None,
    )
    monkeypatch.setattr(
        "app.services.trading.bracket_writer_g2._g2_event",
        lambda *a, **kw: None,
    )


# ── RH stop path BYTE-IDENTICAL parity ──────────────────────────────


def test_rh_equity_stop_call_args_byte_identical(
    reset_cooldowns, monkeypatch,
):
    """The call args to ad.place_stop_loss_sell_order MUST match the
    pre-Phase-4 contract: product_id=ticker, base_size=str(qty),
    trigger_price=str(stop_price), client_order_id=<built coid>.

    Any change here breaks the byte-identical guarantee that gates
    Phase 4.
    """
    _common_monkeypatches(monkeypatch)

    adapter = MagicMock()
    adapter.get_products.return_value = ([], True)
    adapter.place_stop_loss_sell_order.return_value = {
        "ok": True, "order_id": "RH-1",
    }

    res = bw.place_missing_stop(
        db=MagicMock(),
        trade_id=42,
        bracket_intent_id=42,
        ticker="AAPL",
        broker_source="robinhood",
        decision=_decision_missing_stop(),
        local_quantity=10.0,
        stop_price=145.50,
        adapter_factory=lambda src: adapter,
    )

    adapter.place_stop_loss_sell_order.assert_called_once()
    kwargs = adapter.place_stop_loss_sell_order.call_args.kwargs
    assert kwargs["product_id"] == "AAPL"
    assert kwargs["base_size"] == "10.0"
    assert kwargs["trigger_price"] == "145.5"
    assert kwargs["client_order_id"].startswith("g2-miss-42-")
    # Coinbase primitive MUST NOT be called for RH path.
    assert not adapter.place_stop_limit_order_gtc.called


# ── Coinbase crypto routes to new primitive ─────────────────────────


def test_coinbase_crypto_routes_to_stop_limit_primitive(
    reset_cooldowns, monkeypatch,
):
    """ADA-USD with broker_source='coinbase' calls
    place_stop_limit_order_gtc with limit_price below stop_price."""
    _common_monkeypatches(monkeypatch)
    from app.config import settings
    monkeypatch.setattr(
        settings, "chili_coinbase_stop_limit_buffer_pct", 0.005,
        raising=False,
    )

    adapter = MagicMock()
    adapter.get_products.return_value = ([], True)
    adapter.place_stop_limit_order_gtc.return_value = {
        "ok": True, "order_id": "CB-1", "client_order_id": "x",
    }

    res = bw.place_missing_stop(
        db=MagicMock(),
        trade_id=99,
        bracket_intent_id=99,
        ticker="ADA-USD",
        broker_source="coinbase",
        decision=_decision_missing_stop(),
        local_quantity=100.0,
        stop_price=0.4500,
        adapter_factory=lambda src: adapter,
    )

    adapter.place_stop_limit_order_gtc.assert_called_once()
    kwargs = adapter.place_stop_limit_order_gtc.call_args.kwargs
    assert kwargs["product_id"] == "ADA-USD"
    assert kwargs["side"] == "sell"
    assert kwargs["base_size"] == "100.0"
    assert kwargs["stop_price"] == "0.45"
    # limit_price = 0.45 * (1 - 0.005) = 0.44775
    assert float(kwargs["limit_price"]) == pytest.approx(0.44775, rel=1e-6)
    assert kwargs["client_order_id"].startswith("g2-miss-99-")
    # RH primitive MUST NOT be called for Coinbase path.
    assert not adapter.place_stop_loss_sell_order.called


# ── RH crypto still refused (the existing 2026-05-08 prefilter) ─────


def test_rh_crypto_still_refused_via_prefilter(
    reset_cooldowns, monkeypatch,
):
    """ADA-USD with broker_source='robinhood' MUST hit the
    venue_unsupported_crypto_path skip — the 2026-05-08 prefilter
    is still in place for RH."""
    _common_monkeypatches(monkeypatch)

    adapter = MagicMock()
    adapter.place_stop_loss_sell_order.side_effect = AssertionError(
        "RH adapter must NOT be reached for crypto ticker"
    )
    adapter.place_stop_limit_order_gtc.side_effect = AssertionError(
        "Coinbase primitive must NOT be reached for RH-routed crypto"
    )

    res = bw.place_missing_stop(
        db=MagicMock(),
        trade_id=101,
        bracket_intent_id=101,
        ticker="ADA-USD",
        broker_source="robinhood",
        decision=_decision_missing_stop(),
        local_quantity=100.0,
        stop_price=0.45,
        adapter_factory=lambda src: adapter,
    )

    assert res.ok is False
    assert res.reason == "venue_unsupported_crypto_path"


# ── Coinbase rejection arms exception cooldown via code-bug detector ─


def test_coinbase_stop_rejection_arms_exception_cooldown_on_code_bug(
    reset_cooldowns, monkeypatch,
):
    """If Coinbase returns a code-bug-class ok=False (e.g. SDK
    crashed and packaged as error), the existing
    _is_code_bug_error detector arms the exception cooldown — same
    safety net as the RH path."""
    _common_monkeypatches(monkeypatch)

    intent_id = 12345
    adapter = MagicMock()
    adapter.get_products.return_value = ([], True)
    adapter.place_stop_limit_order_gtc.return_value = {
        "ok": False, "error": "list index out of range",
    }

    assert bw._is_in_exception_cooldown(intent_id) is False

    res = bw.place_missing_stop(
        db=MagicMock(),
        trade_id=intent_id,
        bracket_intent_id=intent_id,
        ticker="ADA-USD",
        broker_source="coinbase",
        decision=_decision_missing_stop(),
        local_quantity=100.0,
        stop_price=0.45,
        adapter_factory=lambda src: adapter,
    )
    assert res.reason == "place_failed"
    # Exception cooldown engaged so the next sweep skips for 5min.
    assert bw._is_in_exception_cooldown(intent_id) is True


# ── Coinbase venue eligibility ──────────────────────────────────────


def test_supported_venues_includes_coinbase():
    assert "coinbase" in bw._SUPPORTED_VENUES
    assert "robinhood" in bw._SUPPORTED_VENUES


def test_unsupported_venue_rejected_pre_routing(
    reset_cooldowns, monkeypatch,
):
    """Unknown broker_source returns reason='unsupported_venue'
    BEFORE any adapter call."""
    _common_monkeypatches(monkeypatch)

    adapter = MagicMock()
    adapter.place_stop_loss_sell_order.side_effect = AssertionError(
        "adapter must NOT be reached for unsupported venue"
    )
    adapter.place_stop_limit_order_gtc.side_effect = AssertionError(
        "Coinbase primitive must NOT be reached for unsupported venue"
    )

    res = bw.place_missing_stop(
        db=MagicMock(),
        trade_id=200,
        bracket_intent_id=200,
        ticker="AAPL",
        broker_source="kraken",  # unknown
        decision=_decision_missing_stop(),
        local_quantity=10.0,
        stop_price=100.0,
        adapter_factory=lambda src: adapter,
    )
    assert res.ok is False
    assert res.reason == "unsupported_venue"


# ── Buffer setting respected ────────────────────────────────────────


def test_coinbase_buffer_pct_setting_applied(reset_cooldowns, monkeypatch):
    """Custom buffer (1%) → limit_price = stop_price * 0.99."""
    _common_monkeypatches(monkeypatch)
    from app.config import settings
    monkeypatch.setattr(
        settings, "chili_coinbase_stop_limit_buffer_pct", 0.01, raising=False,
    )

    adapter = MagicMock()
    adapter.get_products.return_value = ([], True)
    adapter.place_stop_limit_order_gtc.return_value = {
        "ok": True, "order_id": "CB-2",
    }

    bw.place_missing_stop(
        db=MagicMock(),
        trade_id=300,
        bracket_intent_id=300,
        ticker="ARB-USD",
        broker_source="coinbase",
        decision=_decision_missing_stop(),
        local_quantity=50.0,
        stop_price=2.00,
        adapter_factory=lambda src: adapter,
    )
    kwargs = adapter.place_stop_limit_order_gtc.call_args.kwargs
    # 2.00 * (1 - 0.01) = 1.98
    assert float(kwargs["limit_price"]) == pytest.approx(1.98, rel=1e-6)
