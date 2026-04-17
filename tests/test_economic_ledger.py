"""Phase A: economic-truth ledger unit tests.

Scope (frozen by .cursor/plans/phase_a_economic_truth_ledger.plan.md):
- Ops log shape parity with [chili_prediction_ops] / [exit_engine_ops] / [net_edge_ops].
- Mode gating: off short-circuits every public API.
- Cash/realized math: long/short entry + exit, fee attribution.
- Idempotency: duplicate record_entry_fill / record_exit_fill on same trade ref.
- reconcile_trade: agrees on tolerance, flags on breach.

Uses the real Postgres ``db`` fixture. Tests are self-contained:
they do not depend on any scan_pattern or trade rows existing.
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.models.trading import EconomicLedgerEvent, LedgerParityLog
from app.services.trading import economic_ledger as el
from app.trading_brain.infrastructure.ledger_ops_log import (
    CHILI_LEDGER_OPS_PREFIX,
    EVENT_ENTRY_FILL,
    EVENT_EXIT_FILL,
    EVENT_RECONCILE,
    MODE_OFF,
    MODE_SHADOW,
    format_ledger_ops_line,
)


@pytest.fixture
def shadow_mode(monkeypatch):
    monkeypatch.setattr(settings, "brain_economic_ledger_mode", MODE_SHADOW)
    monkeypatch.setattr(settings, "brain_economic_ledger_ops_log_enabled", True)
    monkeypatch.setattr(settings, "brain_economic_ledger_parity_tolerance_usd", 0.01)
    yield


@pytest.fixture
def off_mode(monkeypatch):
    monkeypatch.setattr(settings, "brain_economic_ledger_mode", MODE_OFF)
    yield


# ---------------------------------------------------------------------------
# Ops log shape
# ---------------------------------------------------------------------------


def test_ops_log_line_shape_is_frozen():
    line = format_ledger_ops_line(
        mode=MODE_SHADOW,
        source="paper",
        event_type=EVENT_ENTRY_FILL,
        trade_ref="paper:42",
        ticker="AAPL",
        quantity=100.0,
        price=150.25,
        cash_delta=-15025.0,
        realized_pnl_delta=0.0,
    )
    assert line.startswith(CHILI_LEDGER_OPS_PREFIX)
    assert " mode=shadow " in line
    assert " source=paper " in line
    assert " event_type=entry_fill " in line
    assert " trade_ref=paper:42 " in line
    assert " ticker=AAPL " in line
    assert " qty=100.000000 " in line
    assert " price=150.250000 " in line
    assert " cash_delta=-15025.0000 " in line
    assert " agree=none" in line


def test_ops_log_line_handles_none_price_quantity():
    line = format_ledger_ops_line(
        mode=MODE_SHADOW,
        source="paper",
        event_type=EVENT_RECONCILE,
        trade_ref="paper:1",
        ticker="BTC-USD",
        quantity=None,
        price=None,
        cash_delta=None,
        realized_pnl_delta=5.25,
        agree=True,
    )
    assert " qty=none " in line
    assert " price=none " in line
    assert " cash_delta=none " in line
    assert " agree=true" in line


# ---------------------------------------------------------------------------
# Mode gating
# ---------------------------------------------------------------------------


def test_off_mode_short_circuits_entry(db, off_mode):
    row = el.record_entry_fill(
        db,
        source="paper",
        paper_trade_id=1,
        ticker="AAPL",
        direction="long",
        quantity=100.0,
        fill_price=150.0,
    )
    assert row is None
    assert db.query(EconomicLedgerEvent).count() == 0


def test_off_mode_short_circuits_exit_and_reconcile(db, off_mode):
    assert (
        el.record_exit_fill(
            db,
            source="paper",
            paper_trade_id=1,
            ticker="AAPL",
            direction="long",
            quantity=100.0,
            fill_price=160.0,
            entry_price=150.0,
        )
        is None
    )
    assert (
        el.reconcile_trade(
            db,
            source="paper",
            paper_trade_id=1,
            ticker="AAPL",
            legacy_pnl=1000.0,
        )
        is None
    )


def test_mode_is_active_tracks_setting(monkeypatch):
    monkeypatch.setattr(settings, "brain_economic_ledger_mode", MODE_OFF)
    assert el.mode_is_active() is False
    monkeypatch.setattr(settings, "brain_economic_ledger_mode", MODE_SHADOW)
    assert el.mode_is_active() is True
    monkeypatch.setattr(settings, "brain_economic_ledger_mode", "compare")
    assert el.mode_is_active() is True
    monkeypatch.setattr(settings, "brain_economic_ledger_mode", "bogus")
    assert el.mode_is_active() is False  # invalid mode treated as off


# ---------------------------------------------------------------------------
# Cash / realized math — long
# ---------------------------------------------------------------------------


def test_long_entry_cash_delta_is_notional_plus_fee_negative(db, shadow_mode):
    row = el.record_entry_fill(
        db,
        source="paper",
        paper_trade_id=101,
        ticker="AAPL",
        direction="long",
        quantity=100.0,
        fill_price=150.0,
        fee=1.50,
    )
    assert row is not None
    assert row.event_type == EVENT_ENTRY_FILL
    # -(100 * 150) - 1.50 = -15001.50
    assert row.cash_delta == pytest.approx(-15001.50, abs=1e-6)
    assert row.realized_pnl_delta == 0.0
    assert row.position_qty_after == 100.0
    assert row.position_cost_basis_after == 150.0


def test_long_exit_realized_math(db, shadow_mode):
    el.record_entry_fill(
        db,
        source="paper",
        paper_trade_id=102,
        ticker="AAPL",
        direction="long",
        quantity=100.0,
        fill_price=150.0,
        fee=0.0,
    )
    row = el.record_exit_fill(
        db,
        source="paper",
        paper_trade_id=102,
        ticker="AAPL",
        direction="long",
        quantity=100.0,
        fill_price=160.0,
        entry_price=150.0,
        fee=1.0,
    )
    assert row is not None
    assert row.event_type == EVENT_EXIT_FILL
    # cash_delta = +100 * 160 - 1 = 15999
    assert row.cash_delta == pytest.approx(15999.0, abs=1e-6)
    # realized = 100 * (160 - 150) - 1 = 999
    assert row.realized_pnl_delta == pytest.approx(999.0, abs=1e-6)


def test_long_loss_realized_negative(db, shadow_mode):
    row = el.record_exit_fill(
        db,
        source="paper",
        paper_trade_id=103,
        ticker="AAPL",
        direction="long",
        quantity=50.0,
        fill_price=145.0,
        entry_price=150.0,
        fee=0.5,
    )
    assert row is not None
    # realized = 50 * (145 - 150) - 0.5 = -250.5
    assert row.realized_pnl_delta == pytest.approx(-250.5, abs=1e-6)


# ---------------------------------------------------------------------------
# Cash / realized math — short (mirror)
# ---------------------------------------------------------------------------


def test_short_entry_cash_delta_is_proceeds_minus_fee_positive(db, shadow_mode):
    row = el.record_entry_fill(
        db,
        source="paper",
        paper_trade_id=201,
        ticker="TSLA",
        direction="short",
        quantity=10.0,
        fill_price=200.0,
        fee=0.5,
    )
    assert row is not None
    # +10 * 200 - 0.5 = 1999.5
    assert row.cash_delta == pytest.approx(1999.5, abs=1e-6)
    assert row.realized_pnl_delta == 0.0
    assert row.position_qty_after == -10.0


def test_short_exit_profit(db, shadow_mode):
    row = el.record_exit_fill(
        db,
        source="paper",
        paper_trade_id=202,
        ticker="TSLA",
        direction="short",
        quantity=10.0,
        fill_price=190.0,
        entry_price=200.0,
        fee=0.5,
    )
    assert row is not None
    # cash_delta = -10 * 190 - 0.5 = -1900.5  (buy back to cover)
    assert row.cash_delta == pytest.approx(-1900.5, abs=1e-6)
    # realized = 10 * (200 - 190) - 0.5 = 99.5
    assert row.realized_pnl_delta == pytest.approx(99.5, abs=1e-6)


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_entry_fill_is_idempotent_per_paper_trade(db, shadow_mode):
    first = el.record_entry_fill(
        db,
        source="paper",
        paper_trade_id=300,
        ticker="AAPL",
        direction="long",
        quantity=100.0,
        fill_price=150.0,
    )
    assert first is not None
    second = el.record_entry_fill(
        db,
        source="paper",
        paper_trade_id=300,
        ticker="AAPL",
        direction="long",
        quantity=999.0,
        fill_price=999.0,
    )
    assert second is not None
    assert second.id == first.id
    assert db.query(EconomicLedgerEvent).filter_by(paper_trade_id=300).count() == 1


def test_exit_fill_is_idempotent_per_trade(db, shadow_mode):
    first = el.record_exit_fill(
        db,
        source="live",
        trade_id=500,
        ticker="AAPL",
        direction="long",
        quantity=10.0,
        fill_price=160.0,
        entry_price=150.0,
    )
    second = el.record_exit_fill(
        db,
        source="live",
        trade_id=500,
        ticker="AAPL",
        direction="long",
        quantity=999.0,
        fill_price=999.0,
        entry_price=150.0,
    )
    assert second.id == first.id
    assert db.query(EconomicLedgerEvent).filter_by(trade_id=500).count() == 1


# ---------------------------------------------------------------------------
# reconcile_trade
# ---------------------------------------------------------------------------


def test_reconcile_agrees_within_tolerance(db, shadow_mode):
    el.record_exit_fill(
        db,
        source="paper",
        paper_trade_id=401,
        ticker="AAPL",
        direction="long",
        quantity=100.0,
        fill_price=160.0,
        entry_price=150.0,
        fee=0.0,
    )
    parity = el.reconcile_trade(
        db,
        source="paper",
        paper_trade_id=401,
        ticker="AAPL",
        legacy_pnl=1000.0,  # identical to ledger
    )
    assert parity is not None
    assert parity.agree_bool is True
    assert parity.legacy_pnl == pytest.approx(1000.0, abs=1e-6)
    assert parity.ledger_pnl == pytest.approx(1000.0, abs=1e-6)
    assert parity.delta_abs == pytest.approx(0.0, abs=1e-6)


def test_reconcile_flags_disagreement_outside_tolerance(db, shadow_mode):
    el.record_exit_fill(
        db,
        source="paper",
        paper_trade_id=402,
        ticker="AAPL",
        direction="long",
        quantity=100.0,
        fill_price=160.0,
        entry_price=150.0,
        fee=0.0,
    )
    parity = el.reconcile_trade(
        db,
        source="paper",
        paper_trade_id=402,
        ticker="AAPL",
        legacy_pnl=950.0,  # 50 off -> disagree
    )
    assert parity is not None
    assert parity.agree_bool is False
    assert parity.delta_pnl == pytest.approx(50.0, abs=1e-6)
    assert parity.delta_abs == pytest.approx(50.0, abs=1e-6)


def test_reconcile_handles_missing_legacy_pnl(db, shadow_mode):
    parity = el.reconcile_trade(
        db,
        source="paper",
        paper_trade_id=403,
        ticker="AAPL",
        legacy_pnl=None,
    )
    assert parity is not None
    assert parity.legacy_pnl is None
    assert parity.agree_bool is False
    assert parity.ledger_pnl == pytest.approx(0.0, abs=1e-6)


# ---------------------------------------------------------------------------
# Crypto semantics
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ticker", ["BTC-USD", "BTCUSD", "zk-usd", "ZKUSD"])
def test_crypto_tickers_stored_uppercase_and_accepted(db, shadow_mode, ticker):
    row = el.record_entry_fill(
        db,
        source="paper",
        paper_trade_id=hash(ticker) & 0xFFFFFF,
        ticker=ticker,
        direction="long",
        quantity=0.5,
        fill_price=70000.0,
    )
    assert row is not None
    assert row.ticker == ticker.upper()


# ---------------------------------------------------------------------------
# ledger_summary diagnostics
# ---------------------------------------------------------------------------


def test_ledger_summary_aggregates_events_and_parity(db, shadow_mode):
    el.record_entry_fill(
        db,
        source="paper",
        paper_trade_id=901,
        ticker="AAPL",
        direction="long",
        quantity=100.0,
        fill_price=150.0,
    )
    el.record_exit_fill(
        db,
        source="paper",
        paper_trade_id=901,
        ticker="AAPL",
        direction="long",
        quantity=100.0,
        fill_price=160.0,
        entry_price=150.0,
    )
    el.reconcile_trade(
        db,
        source="paper",
        paper_trade_id=901,
        ticker="AAPL",
        legacy_pnl=1000.0,
    )

    summary = el.ledger_summary(db, lookback_hours=1)
    assert summary["mode"] == MODE_SHADOW
    assert summary["events_total"] >= 2
    assert summary["events_by_type"].get(EVENT_ENTRY_FILL, 0) >= 1
    assert summary["events_by_type"].get(EVENT_EXIT_FILL, 0) >= 1
    assert summary["parity_total"] >= 1
    assert summary["parity_agree"] >= 1
    assert summary["parity_rate"] is not None


def test_ledger_summary_empty_db(db, shadow_mode):
    summary = el.ledger_summary(db, lookback_hours=1)
    assert summary["events_total"] == 0
    assert summary["parity_total"] == 0
    assert summary["parity_rate"] is None


# ---------------------------------------------------------------------------
# Invalid input guards
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "qty,price",
    [(0, 150.0), (-1, 150.0), (100, 0), (100, -1), (None, 150.0), (100, None)],
)
def test_entry_rejects_invalid_qty_or_price(db, shadow_mode, qty, price):
    row = el.record_entry_fill(
        db,
        source="paper",
        paper_trade_id=800,
        ticker="AAPL",
        direction="long",
        quantity=qty,
        fill_price=price,
    )
    assert row is None


def test_unknown_source_is_rejected(db, shadow_mode):
    assert (
        el.record_entry_fill(
            db,
            source="bogus",
            paper_trade_id=900,
            ticker="AAPL",
            direction="long",
            quantity=100.0,
            fill_price=150.0,
        )
        is None
    )
