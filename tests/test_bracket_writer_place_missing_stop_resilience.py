"""f-phase-e-revert-and-bracket-writer-crash-fix (2026-05-08).

Pin two defences in `place_missing_stop` against the active 2026-05-09
01:57 UTC crash loop on ADA/SOL:

  1. **Crypto-path refusal**: ALL crypto tickers (-USD suffix) are
     SKIPPED with reason='venue_unsupported_crypto_path' BEFORE any
     broker call. The equity rh.orders.order primitive crashes on
     `get_instruments_by_symbols('ADA')[0]` for crypto bases (no
     equity instrument record); the prefilter must catch this for
     listed-and-unlisted crypto alike.

  2. **Exception cooldown**: any exception raised inside the
     try/except around adapter.place_stop_loss_sell_order arms a
     5-min cooldown. Subsequent calls within the cooldown window
     SKIP early with reason='in_exception_cooldown' instead of
     re-firing every 60s sweep.

Helper-level. We mock the adapter and the broker pre-flight calls so
no DB / no broker.
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from app.services.trading import bracket_writer_g2 as bw
from app.services.trading.bracket_reconciler import ReconciliationDecision
from app.services.trading.venue.protocol import NormalizedOrder


# ── Helpers ──────────────────────────────────────────────────────────


def _decision_missing_stop():
    return ReconciliationDecision(
        kind="missing_stop", severity="warn", delta_payload={},
    )


def _enable_writer():
    """Patch the top-level + place_missing_stop flags so the function
    body actually runs (otherwise it short-circuits at the disabled
    early-return)."""
    from app.config import settings
    return [
        ("chili_bracket_writer_g2_enabled", True),
        ("chili_bracket_writer_g2_place_missing_stop", True),
    ]


@pytest.fixture()
def reset_cooldowns():
    """Wipe in-process cooldown dicts so tests don't bleed."""
    bw._intent_reject_cooldown.clear()
    bw._intent_post_place_cooldown.clear()
    bw._intent_exception_cooldown.clear()
    yield
    bw._intent_reject_cooldown.clear()
    bw._intent_post_place_cooldown.clear()
    bw._intent_exception_cooldown.clear()


# ── Crypto-path refusal ─────────────────────────────────────────────


def test_ada_usd_crypto_ticker_skipped_without_broker_call(
    reset_cooldowns, monkeypatch,
):
    """ADA-USD must skip with the new reason — NEVER reaching the
    adapter (which would crash inside rh.orders for the equity
    primitive)."""
    from app.config import settings
    monkeypatch.setattr(settings, "chili_bracket_writer_g2_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_bracket_writer_g2_place_missing_stop", True, raising=False)

    adapter = MagicMock()
    adapter.place_stop_loss_sell_order.side_effect = AssertionError(
        "adapter MUST NOT be called for crypto tickers"
    )

    res = bw.place_missing_stop(
        db=MagicMock(),
        trade_id=1808,
        bracket_intent_id=9001,
        ticker="ADA-USD",
        broker_source="robinhood",
        decision=_decision_missing_stop(),
        local_quantity=3621.0,
        stop_price=0.25663137,
        adapter_factory=lambda src: adapter,
    )
    assert res.ok is False
    assert res.reason == "venue_unsupported_crypto_path"
    adapter.place_stop_loss_sell_order.assert_not_called()


def test_sol_usd_crypto_ticker_skipped_same_path(reset_cooldowns, monkeypatch):
    """SOL-USD: the second crash-loop ticker; same skip path."""
    from app.config import settings
    monkeypatch.setattr(settings, "chili_bracket_writer_g2_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_bracket_writer_g2_place_missing_stop", True, raising=False)

    adapter = MagicMock()
    res = bw.place_missing_stop(
        db=MagicMock(),
        trade_id=1809,
        bracket_intent_id=9002,
        ticker="SOL-USD",
        broker_source="robinhood",
        decision=_decision_missing_stop(),
        local_quantity=10.0,
        stop_price=150.0,
        adapter_factory=lambda src: adapter,
    )
    assert res.reason == "venue_unsupported_crypto_path"
    adapter.place_stop_loss_sell_order.assert_not_called()


def test_zec_usd_unlisted_crypto_also_skipped(reset_cooldowns, monkeypatch):
    """ZEC-USD (originally an unlisted-crypto case from the May 4
    audit) must still be skipped — the new prefilter is broader, not
    narrower."""
    from app.config import settings
    monkeypatch.setattr(settings, "chili_bracket_writer_g2_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_bracket_writer_g2_place_missing_stop", True, raising=False)

    adapter = MagicMock()
    res = bw.place_missing_stop(
        db=MagicMock(),
        trade_id=999,
        bracket_intent_id=9003,
        ticker="ZEC-USD",
        broker_source="robinhood",
        decision=_decision_missing_stop(),
        local_quantity=1.0,
        stop_price=10.0,
        adapter_factory=lambda src: adapter,
    )
    assert res.reason == "venue_unsupported_crypto_path"
    adapter.place_stop_loss_sell_order.assert_not_called()


def test_equity_ticker_does_not_hit_crypto_skip(reset_cooldowns, monkeypatch):
    """An equity ticker (AAPL) must NOT be caught by the crypto
    refusal — the guard is suffix-scoped, not blanket."""
    from app.config import settings
    monkeypatch.setattr(settings, "chili_bracket_writer_g2_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_bracket_writer_g2_place_missing_stop", True, raising=False)

    adapter = MagicMock()
    # Stub the get_products call so the broker_qty / held_for_sells
    # branches are well-defined.
    adapter.get_products.return_value = ([], True)
    # Stub a plausible accept response so the call gets through to
    # adapter.place_stop_loss_sell_order.
    adapter.place_stop_loss_sell_order.return_value = {
        "ok": True, "order_id": "RH-1",
    }

    monkeypatch.setattr(
        "app.services.broker_service.get_position_held_for_sells",
        lambda *a, **kw: None,
    )
    # verify_order_landed downstream — short-circuit it.
    monkeypatch.setattr(
        "app.services.trading.bracket_writer_g2._g2_event",
        lambda *a, **kw: None,
    )

    res = bw.place_missing_stop(
        db=MagicMock(),
        trade_id=42,
        bracket_intent_id=42,
        ticker="AAPL",
        broker_source="robinhood",
        decision=_decision_missing_stop(),
        local_quantity=1.0,
        stop_price=100.0,
        adapter_factory=lambda src: adapter,
    )
    # We don't care about the outcome (verify_order_landed will mock-
    # short-circuit somewhere). We DO care that:
    assert res.reason != "venue_unsupported_crypto_path"


# ── Exception cooldown ──────────────────────────────────────────────


def test_exception_arms_cooldown_and_returns_clean_reason(
    reset_cooldowns, monkeypatch,
):
    """When the broker adapter raises (simulating the IndexError
    from rh.orders), the writer must:
      1. NOT propagate the exception.
      2. Arm the new exception cooldown.
      3. Return a clean WriterAction with reason='place_failed'.
    """
    from app.config import settings
    monkeypatch.setattr(settings, "chili_bracket_writer_g2_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_bracket_writer_g2_place_missing_stop", True, raising=False)

    intent_id = 7777
    adapter = MagicMock()
    adapter.get_products.return_value = ([], True)
    adapter.place_stop_loss_sell_order.side_effect = IndexError(
        "list index out of range"
    )

    monkeypatch.setattr(
        "app.services.broker_service.get_position_held_for_sells",
        lambda *a, **kw: None,
    )
    monkeypatch.setattr(
        "app.services.trading.bracket_writer_g2._g2_event",
        lambda *a, **kw: None,
    )

    assert bw._is_in_exception_cooldown(intent_id) is False
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
    assert res.ok is False
    assert res.reason == "place_failed"
    # Cooldown is now armed.
    assert bw._is_in_exception_cooldown(intent_id) is True


def test_subsequent_call_during_cooldown_skips_without_broker_call(
    reset_cooldowns, monkeypatch,
):
    """Second sweep within the cooldown window must SKIP with
    reason='in_exception_cooldown' and NEVER touch the adapter."""
    from app.config import settings
    monkeypatch.setattr(settings, "chili_bracket_writer_g2_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_bracket_writer_g2_place_missing_stop", True, raising=False)

    intent_id = 8888
    bw._arm_exception_cooldown(intent_id)
    assert bw._is_in_exception_cooldown(intent_id) is True

    adapter = MagicMock()
    adapter.place_stop_loss_sell_order.side_effect = AssertionError(
        "adapter MUST NOT be called during exception cooldown"
    )

    res = bw.place_missing_stop(
        db=MagicMock(),
        trade_id=2,
        bracket_intent_id=intent_id,
        ticker="AAPL",
        broker_source="robinhood",
        decision=_decision_missing_stop(),
        local_quantity=1.0,
        stop_price=100.0,
        adapter_factory=lambda src: adapter,
    )
    assert res.ok is False
    assert res.reason == "in_exception_cooldown"
    adapter.place_stop_loss_sell_order.assert_not_called()


def test_exception_cooldown_expires_cleanly(reset_cooldowns, monkeypatch):
    """Once `time.time() >= until`, the cooldown is dropped from the
    dict and `_is_in_exception_cooldown` returns False."""
    intent_id = 9999
    # Force expiry by setting `until` in the past.
    bw._intent_exception_cooldown[intent_id] = time.time() - 1.0

    assert bw._is_in_exception_cooldown(intent_id) is False
    assert intent_id not in bw._intent_exception_cooldown


def test_exception_cooldown_secs_reads_settings(monkeypatch):
    """The cooldown duration is settings-tunable (env override path)."""
    from app.config import settings
    monkeypatch.setattr(
        settings, "chili_bracket_writer_exception_cooldown_secs", 42,
        raising=False,
    )
    assert bw._exception_cooldown_secs() == 42


def _coinbase_stop(order_id: str, *, product_id: str, base_size: str, stop_price: str) -> NormalizedOrder:
    return NormalizedOrder(
        order_id=order_id,
        client_order_id=None,
        product_id=product_id,
        side="sell",
        status="open",
        order_type="STOP_LIMIT",
        filled_size=0.0,
        average_filled_price=None,
        raw={
            "order_configuration": {
                "stop_limit_stop_limit_gtc": {
                    "base_size": base_size,
                    "stop_price": stop_price,
                }
            }
        },
    )


def test_coinbase_tiny_uncovered_split_stop_gap_is_adopted(
    reset_cooldowns, monkeypatch,
):
    """Do not submit an unplaceable dust remainder when split Coinbase
    stops already cover the actionable intent size.
    """
    from app.config import settings

    monkeypatch.setattr(settings, "chili_bracket_writer_g2_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_bracket_writer_g2_place_missing_stop", True, raising=False)
    monkeypatch.setattr(
        "app.services.trading.bracket_writer_g2._g2_event",
        lambda *a, **kw: None,
    )

    adapter = MagicMock()
    adapter.get_products.return_value = ([], True)
    adapter.list_open_orders.return_value = ([
        _coinbase_stop("cb-1", product_id="ALCX-USD", base_size="0.705", stop_price="3.62"),
        _coinbase_stop("cb-2", product_id="ALCX-USD", base_size="0.3022", stop_price="3.62"),
        _coinbase_stop("cb-3", product_id="ALCX-USD", base_size="0.2529", stop_price="3.62"),
    ], True)

    res = bw.place_missing_stop(
        db=MagicMock(),
        trade_id=2108,
        bracket_intent_id=502,
        ticker="ALCX-USD",
        broker_source="coinbase",
        decision=_decision_missing_stop(),
        local_quantity=1.2601961995249407,
        stop_price=3.602922,
        adapter_factory=lambda src: adapter,
    )

    assert res.ok is True
    assert res.reason == "existing_coinbase_stop_coverage"
    assert res.new_stop_order_id == "cb-3"
    assert res.new_stop_qty == pytest.approx(1.2601)
    adapter.place_stop_limit_order_gtc.assert_not_called()


def test_phase_e_source_removed():
    """Acceptance criterion: `grep -r run_crypto_stale_trade_close
    app/` returns zero source matches. Pin it as a test so a
    re-introduction surfaces immediately."""
    import importlib
    with pytest.raises(ImportError):
        importlib.import_module(
            "app.services.trading.crypto_reconcile"
        )
    # Also assert the symbol is NOT exposed from
    # bracket_reconciliation_service.
    from app.services.trading import bracket_reconciliation_service as brs
    assert not hasattr(brs, "run_crypto_stale_trade_close")
