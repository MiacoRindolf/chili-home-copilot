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

import uuid
from datetime import datetime

import pytest

from app.config import settings
from app.models.trading import (
    EconomicLedgerEvent,
    LedgerParityLog,
    MomentumAutomationOutcome,
    MomentumStrategyVariant,
    TradingAutomationSession,
)
from app.services.trading import economic_ledger as el
from app.trading_brain.infrastructure.ledger_ops_log import (
    CHILI_LEDGER_OPS_PREFIX,
    EVENT_ENTRY_FILL,
    EVENT_EXIT_FILL,
    EVENT_PARTIAL_FILL,
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


def test_default_economic_ledger_mode_is_shadow_active():
    assert settings.brain_economic_ledger_mode == MODE_SHADOW
    assert settings.brain_economic_ledger_require_parity_for_evolution is True


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


def test_automation_trade_id_is_negative_and_stable():
    assert el.automation_trade_id(77) == -77
    assert el.automation_trade_id(-77) == -77


def test_automation_session_ledger_uses_negative_session_trade_id(db, shadow_mode):
    entry = el.record_automation_session_entry_fill(
        db,
        session_id=77,
        user_id=1,
        ticker="BTC-USD",
        quantity=0.1,
        fill_price=100_000.0,
        mode="paper",
        decision_packet_id=123,
    )
    assert entry is not None
    assert entry.source == "automation"
    assert entry.trade_id == -77
    assert entry.provenance_json["decision_packet_id"] == 123

    exit_row = el.record_automation_session_exit_fill(
        db,
        session_id=77,
        user_id=1,
        ticker="BTC-USD",
        quantity=0.1,
        fill_price=100_200.0,
        entry_price=100_000.0,
        realized_pnl_usd=19.0,
        mode="paper",
        decision_packet_id=123,
    )
    assert exit_row is not None
    assert exit_row.event_type == EVENT_EXIT_FILL
    assert exit_row.realized_pnl_delta == pytest.approx(19.0, abs=1e-6)

    parity = el.reconcile_automation_session(
        db,
        session_id=77,
        user_id=1,
        ticker="BTC-USD",
        legacy_pnl=19.0,
        mode="paper",
    )
    assert parity is not None
    assert parity.source == "automation"
    assert parity.trade_id == -77
    assert parity.agree_bool is True


def test_automation_partial_and_terminal_exit_reconcile_cumulative_pnl(db, shadow_mode):
    el.record_automation_session_entry_fill(
        db,
        session_id=78,
        ticker="ETH-USD",
        quantity=3.0,
        fill_price=100.0,
        mode="paper",
    )
    part = el.record_automation_session_partial_exit_fill(
        db,
        session_id=78,
        ticker="ETH-USD",
        quantity=1.0,
        fill_price=106.0,
        entry_price=100.0,
        realized_pnl_usd=5.5,
        mode="paper",
    )
    assert part is not None
    assert part.event_type == EVENT_PARTIAL_FILL
    final = el.record_automation_session_exit_fill(
        db,
        session_id=78,
        ticker="ETH-USD",
        quantity=2.0,
        fill_price=104.0,
        entry_price=100.0,
        realized_pnl_usd=7.5,
        mode="paper",
    )
    assert final is not None
    parity = el.reconcile_automation_session(
        db,
        session_id=78,
        ticker="ETH-USD",
        legacy_pnl=13.0,
        mode="paper",
    )
    assert parity is not None
    assert parity.ledger_pnl == pytest.approx(13.0, abs=1e-6)
    assert parity.agree_bool is True


def test_reconcile_missing_automation_outcome_parity_dry_run_then_apply(db, shadow_mode):
    variant = MomentumStrategyVariant(
        family="ledger_repair",
        variant_key=f"parity_{uuid.uuid4().hex[:12]}",
        label="Ledger repair parity variant",
        params_json={},
    )
    db.add(variant)
    db.flush()

    now = datetime.utcnow()
    matched_session = TradingAutomationSession(
        mode="paper",
        symbol="BTC-USD",
        variant_id=variant.id,
        state="finished",
        ended_at=now,
    )
    entry_only_session = TradingAutomationSession(
        mode="paper",
        symbol="ETH-USD",
        variant_id=variant.id,
        state="finished",
        ended_at=now,
    )
    db.add_all([matched_session, entry_only_session])
    db.flush()

    db.add_all(
        [
            MomentumAutomationOutcome(
                session_id=matched_session.id,
                variant_id=variant.id,
                symbol=matched_session.symbol,
                mode="paper",
                execution_family="coinbase_spot",
                terminal_state="finished",
                terminal_at=now,
                outcome_class="small_win",
                realized_pnl_usd=19.0,
                return_bps=9.5,
                regime_snapshot_json={},
                entry_regime_snapshot_json={},
                exit_regime_snapshot_json={},
                readiness_snapshot_json={},
                admission_snapshot_json={},
                governance_context_json={},
                extracted_summary_json={},
                evidence_weight=1.0,
                contributes_to_evolution=False,
            ),
            MomentumAutomationOutcome(
                session_id=entry_only_session.id,
                variant_id=variant.id,
                symbol=entry_only_session.symbol,
                mode="paper",
                execution_family="coinbase_spot",
                terminal_state="finished",
                terminal_at=now,
                outcome_class="small_win",
                realized_pnl_usd=5.0,
                return_bps=4.0,
                regime_snapshot_json={},
                entry_regime_snapshot_json={},
                exit_regime_snapshot_json={},
                readiness_snapshot_json={},
                admission_snapshot_json={},
                governance_context_json={},
                extracted_summary_json={},
                evidence_weight=1.0,
                contributes_to_evolution=False,
            ),
        ]
    )
    db.flush()

    el.record_automation_session_entry_fill(
        db,
        session_id=matched_session.id,
        ticker=matched_session.symbol,
        quantity=0.1,
        fill_price=100_000.0,
        mode="paper",
        decision_packet_id=123,
    )
    el.record_automation_session_exit_fill(
        db,
        session_id=matched_session.id,
        ticker=matched_session.symbol,
        quantity=0.1,
        fill_price=100_200.0,
        entry_price=100_000.0,
        realized_pnl_usd=19.0,
        mode="paper",
        decision_packet_id=123,
    )
    el.record_automation_session_entry_fill(
        db,
        session_id=entry_only_session.id,
        ticker=entry_only_session.symbol,
        quantity=1.0,
        fill_price=100.0,
        mode="paper",
    )
    db.commit()

    dry = el.reconcile_missing_automation_outcome_parity(db, days=30, dry_run=True)

    assert dry["dry_run"] is True
    assert dry["processed"] == 2
    assert dry["candidate_count"] == 1
    assert dry["applied_count"] == 0
    assert dry["skipped_without_realized_ledger_events"] == 1
    assert dry["candidates"][0]["session_id"] == matched_session.id
    assert dry["candidates"][0]["ledger_pnl"] == pytest.approx(19.0, abs=1e-6)
    assert (
        db.query(LedgerParityLog)
        .filter(
            LedgerParityLog.source == "automation",
            LedgerParityLog.trade_id == el.automation_trade_id(matched_session.id),
        )
        .count()
        == 0
    )

    applied = el.reconcile_missing_automation_outcome_parity(db, days=30, dry_run=False)

    assert applied["dry_run"] is False
    assert applied["candidate_count"] == 1
    assert applied["applied_count"] == 1
    assert applied["applied"][0]["agree_bool"] is True
    assert applied["applied"][0]["delta_abs"] == pytest.approx(0.0, abs=1e-6)
    parity = (
        db.query(LedgerParityLog)
        .filter(
            LedgerParityLog.source == "automation",
            LedgerParityLog.trade_id == el.automation_trade_id(matched_session.id),
        )
        .one()
    )
    assert parity.agree_bool is True
    assert parity.provenance_json["repair"] == "automation_outcome_parity_reconcile"

    again = el.reconcile_missing_automation_outcome_parity(db, days=30, dry_run=True)
    assert again["candidate_count"] == 0
    assert again["skipped_existing_agree"] == 1


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
    el.record_entry_fill(
        db,
        source="paper",
        paper_trade_id=902,
        ticker="MSFT",
        direction="long",
        quantity=10.0,
        fill_price=200.0,
        provenance={"decision_packet_id": 123},
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
    assert summary["fill_events_total"] >= 3
    assert summary["fill_events_with_decision_packet"] >= 1
    assert summary["fill_event_packet_lineage_rate"] is not None
    assert summary["parity_total"] >= 1
    assert summary["parity_agree"] >= 1
    assert summary["parity_rate"] is not None


def test_ledger_summary_empty_db(db, shadow_mode):
    summary = el.ledger_summary(db, lookback_hours=1)
    assert summary["events_total"] == 0
    assert summary["fill_events_total"] == 0
    assert summary["fill_events_with_decision_packet"] == 0
    assert summary["fill_event_packet_lineage_rate"] is None
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
