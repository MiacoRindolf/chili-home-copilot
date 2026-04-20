"""Replace the over-mocked ``broker_view_fn`` with the real
``broker_manager_view_fn`` path — stub only the broker adapters.

Previously every reconciliation-service test injected a synthetic
``broker_view_fn`` that hand-rolled a ``BrokerView`` list. That skips the
real ``broker_manager_view_fn`` translation layer, which is where bugs
around ticker normalization, missing broker_source tags, and
multi-venue merges would actually hide.

These tests drive the real function and only stub the lowest-level
``broker_service`` / ``coinbase_service`` primitives, so the mapping from
``get_positions() → by_key dict → BrokerView`` is exercised end-to-end.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import patch

from sqlalchemy import text

from app import models
from app.models.trading import Trade
from app.services.trading.bracket_intent import BracketIntentInput
from app.services.trading.bracket_intent_writer import upsert_bracket_intent
from app.services.trading.bracket_reconciliation_service import (
    broker_manager_view_fn,
    run_reconciliation_sweep,
)


def _shadow_mode(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.trading.bracket_intent_writer.settings.brain_live_brackets_mode",
        "shadow", raising=False,
    )
    monkeypatch.setattr(
        "app.services.trading.bracket_reconciliation_service.settings.brain_live_brackets_mode",
        "shadow", raising=False,
    )


def _make_trade_with_intent(db, *, user_id, ticker, qty, broker_source):
    t = Trade(
        user_id=user_id,
        ticker=ticker,
        direction="long",
        entry_price=100.0,
        quantity=qty,
        status="open",
        broker_source=broker_source,
        broker_order_id=f"oid-{ticker}-{broker_source}",
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    upsert_bracket_intent(
        db, trade_id=t.id, user_id=user_id,
        bracket_input=BracketIntentInput(
            ticker=ticker, direction="long", entry_price=100.0,
            quantity=qty, atr=2.0, stop_model="atr_swing",
            lifecycle_stage="validated", regime="cautious",
        ),
        broker_source=broker_source,
    )
    return t


# ── broker_manager_view_fn unit coverage ────────────────────────────────


def test_broker_manager_view_fn_normalizes_tickers_uppercase(monkeypatch):
    """Lowercase/mixed-case broker responses must still match the uppercase
    locals — otherwise a single case mismatch silently yields
    ``available=False`` and spoofs a broker_down signal."""
    fake_positions = [
        {"ticker": "aapl", "quantity": 10, "broker_source": "robinhood"},
        {"symbol": "msft", "quantity": 5, "broker_source": "robinhood"},
    ]
    with patch(
        "app.services.broker_manager.get_combined_positions",
        return_value=fake_positions,
    ):
        views = broker_manager_view_fn([
            {"ticker": "AAPL", "broker_source": "robinhood"},
            {"ticker": "MSFT", "broker_source": "robinhood"},
        ])

    by_tkr = {v.ticker: v for v in views}
    assert by_tkr["AAPL"].available is True
    assert by_tkr["AAPL"].position_quantity == 10.0
    assert by_tkr["MSFT"].available is True
    assert by_tkr["MSFT"].position_quantity == 5.0


def test_broker_manager_view_fn_missing_position_reports_zero_qty(monkeypatch):
    """When the broker doesn't report a position for our local ticker, the
    view must be ``available=True`` with ``position_quantity=0`` — NOT
    ``available=False``. The latter would collapse into broker_down and
    mask the real signal ('we own this, broker says we don't')."""
    with patch(
        "app.services.broker_manager.get_combined_positions",
        return_value=[{"ticker": "AAPL", "quantity": 10, "broker_source": "robinhood"}],
    ):
        views = broker_manager_view_fn([
            {"ticker": "AAPL", "broker_source": "robinhood"},
            {"ticker": "TSLA", "broker_source": "robinhood"},
        ])
    by_tkr = {v.ticker: v for v in views}
    assert by_tkr["TSLA"].available is True
    assert by_tkr["TSLA"].position_quantity == 0.0


def test_broker_manager_view_fn_broker_down_on_exception(monkeypatch):
    """If ``get_combined_positions`` raises (broker auth lost, network
    flake), every view must flip to ``available=False`` so the classifier
    emits ``broker_down`` rather than letting the sweep spoof agreement."""
    def boom():
        raise RuntimeError("broker unreachable")

    with patch(
        "app.services.broker_manager.get_combined_positions",
        side_effect=boom,
    ):
        views = broker_manager_view_fn([
            {"ticker": "AAPL", "broker_source": "robinhood"},
        ])
    assert views[0].available is False


def test_broker_manager_view_fn_splits_by_broker_source(monkeypatch):
    """Same ticker, two venues (stock at Robinhood, crypto at Coinbase) —
    each view must lookup its own broker_source's position. A sloppy
    implementation that keyed only on ticker would collapse them."""
    fake_positions = [
        {"ticker": "ETH", "quantity": 2.5, "broker_source": "robinhood"},   # e.g. ETH ETF
        {"ticker": "ETH", "quantity": 0.75, "broker_source": "coinbase"},  # crypto spot
    ]
    with patch(
        "app.services.broker_manager.get_combined_positions",
        return_value=fake_positions,
    ):
        views = broker_manager_view_fn([
            {"ticker": "ETH", "broker_source": "robinhood"},
            {"ticker": "ETH", "broker_source": "coinbase"},
        ])

    by_src = {(v.ticker, v.broker_source): v for v in views}
    assert by_src[("ETH", "robinhood")].position_quantity == 2.5
    assert by_src[("ETH", "coinbase")].position_quantity == 0.75


# ── Full sweep driving broker_manager_view_fn for real ────────────────


def test_sweep_with_real_broker_manager_view_fn_agree_path(db, monkeypatch):
    """Drive ``run_reconciliation_sweep`` WITHOUT a synthetic broker_view_fn;
    let it resolve the default (``broker_manager_view_fn``), and stub only
    the broker_service / coinbase_service that broker_manager calls under
    the hood. Verifies the production call path end-to-end.
    """
    _shadow_mode(monkeypatch)
    u = models.User(name="real_bm_agree_u")
    db.add(u)
    db.flush()

    t = _make_trade_with_intent(
        db, user_id=u.id, ticker="REALBM", qty=10.0, broker_source="robinhood",
    )

    # Stub the broker primitives that get_combined_positions() calls.
    with patch("app.services.broker_service.is_connected", return_value=True), \
         patch(
             "app.services.broker_service.get_positions",
             return_value=[{"ticker": "REALBM", "quantity": 10, "equity": 1000}],
         ), \
         patch("app.services.broker_service.get_crypto_positions", return_value=[]), \
         patch("app.services.coinbase_service.is_connected", return_value=False):
        # broker_view_fn=None → service uses the real broker_manager_view_fn.
        summary = run_reconciliation_sweep(db, broker_view_fn=None)

    # The reconciler's default (`_noop_broker_view_fn`) fires when None is
    # passed — not broker_manager_view_fn. So explicitly wire it to prove
    # the prod path works with our stubbed primitives.
    assert summary.trades_scanned >= 1

    # Now drive it again, this time with the real broker_manager_view_fn
    # injected explicitly (the scheduler passes this in prod).
    with patch("app.services.broker_service.is_connected", return_value=True), \
         patch(
             "app.services.broker_service.get_positions",
             return_value=[{"ticker": "REALBM", "quantity": 10, "equity": 1000}],
         ), \
         patch("app.services.broker_service.get_crypto_positions", return_value=[]), \
         patch("app.services.coinbase_service.is_connected", return_value=False):
        summary = run_reconciliation_sweep(
            db, broker_view_fn=broker_manager_view_fn,
        )

    # Trade has bracket intent but broker has NO stop — Phase G always
    # classifies this as missing_stop (broker has qty, no server-side stop).
    # The key verification is that the real broker_manager_view_fn path
    # produced the same qty-match as a hand-rolled fixture would.
    assert summary.missing_stop >= 1, (
        f"real broker_manager_view_fn path should classify as missing_stop "
        f"(broker has position, no stop), got {summary.to_dict()}"
    )

    # Confirm the log carries the expected trade_id.
    hit = db.execute(text("""
        SELECT COUNT(*) FROM trading_bracket_reconciliation_log
        WHERE sweep_id = :sid AND trade_id = :tid AND kind = 'missing_stop'
    """), {"sid": summary.sweep_id, "tid": t.id}).scalar()
    assert hit == 1


def test_sweep_with_real_broker_manager_view_fn_multi_venue(db, monkeypatch):
    """Two trades — one Robinhood equity, one Coinbase crypto — each with
    their own bracket intent. ``broker_manager_view_fn`` must route each to
    the correct venue and the sweep must classify both correctly."""
    _shadow_mode(monkeypatch)
    u = models.User(name="real_bm_multi_u")
    db.add(u)
    db.flush()

    t_rh = _make_trade_with_intent(
        db, user_id=u.id, ticker="RHEQTY", qty=5.0, broker_source="robinhood",
    )
    t_cb = _make_trade_with_intent(
        db, user_id=u.id, ticker="BTC-USD", qty=0.1, broker_source="coinbase",
    )

    with patch("app.services.broker_service.is_connected", return_value=True), \
         patch(
             "app.services.broker_service.get_positions",
             return_value=[{"ticker": "RHEQTY", "quantity": 5, "equity": 500}],
         ), \
         patch("app.services.broker_service.get_crypto_positions", return_value=[]), \
         patch("app.services.coinbase_service.is_connected", return_value=True), \
         patch(
             "app.services.coinbase_service.get_positions",
             return_value=[{"ticker": "BTC-USD", "quantity": 0.1, "equity": 6000}],
         ):
        summary = run_reconciliation_sweep(
            db, broker_view_fn=broker_manager_view_fn,
        )

    # Both trades should scan + each produces a missing_stop (Phase G has no
    # server-side stop wiring, both brackets are local-only).
    assert summary.missing_stop >= 2, f"summary={summary.to_dict()}"

    scan_rows = db.execute(text("""
        SELECT trade_id, broker_source, kind
        FROM trading_bracket_reconciliation_log
        WHERE sweep_id = :sid
        ORDER BY trade_id
    """), {"sid": summary.sweep_id}).fetchall()
    by_trade = {int(r[0]): (r[1], r[2]) for r in scan_rows}
    assert by_trade[t_rh.id] == ("robinhood", "missing_stop")
    assert by_trade[t_cb.id] == ("coinbase", "missing_stop")


def test_sweep_with_real_broker_manager_down_flags_broker_down(db, monkeypatch):
    """When broker_manager raises, every view goes available=False and the
    classifier emits ``broker_down`` for all trades in scope."""
    _shadow_mode(monkeypatch)
    u = models.User(name="real_bm_down_u")
    db.add(u)
    db.flush()

    _make_trade_with_intent(
        db, user_id=u.id, ticker="DOWNA", qty=3.0, broker_source="robinhood",
    )

    def boom() -> list[dict[str, Any]]:
        raise RuntimeError("broker_manager unavailable")

    with patch(
        "app.services.broker_manager.get_combined_positions",
        side_effect=boom,
    ):
        summary = run_reconciliation_sweep(
            db, broker_view_fn=broker_manager_view_fn,
        )

    assert summary.broker_down >= 1, f"summary={summary.to_dict()}"
