"""Phase B of f-evidence-fidelity-architecture (2026-05-14).

Tests the venue-truth wiring in ``execution_hooks.py``:

* Closing a live Trade writes a row to ``trading_venue_truth_log`` with
  expected_* pulled from the rolling estimate and realized_* pulled
  from TCA + the latest TradingExecutionEvent.
* Closing a PaperTrade writes a paper-flagged row.
* When ``record_fill_observation`` raises, the legacy emitter still
  fires — the load-bearing safety invariant.
* When no rolling estimate exists, expected_* land as NULL; realized_*
  still populate.
* The close hook lazy-refreshes the rolling estimate for that
  ``(ticker, side, window=30)``.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import text

from app.models.trading import (
    PaperTrade,
    Trade,
    TradingExecutionEvent,
)


# ── helpers ──────────────────────────────────────────────────────────


def _make_closed_trade(
    db,
    *,
    ticker: str = "AAPL",
    direction: str = "long",
    entry_price: float = 100.0,
    exit_price: float = 102.0,
    quantity: float = 10.0,
    tca_entry_slippage_bps: float | None = 6.5,
    broker_source: str = "robinhood",
) -> Trade:
    now = datetime.utcnow()
    t = Trade(
        ticker=ticker,
        direction=direction,
        entry_price=entry_price,
        exit_price=exit_price,
        quantity=quantity,
        entry_date=now - timedelta(hours=2),
        exit_date=now,
        filled_at=now - timedelta(hours=2, minutes=-1),
        status="closed",
        pnl=(exit_price - entry_price) * quantity,
        tca_entry_slippage_bps=tca_entry_slippage_bps,
        broker_source=broker_source,
    )
    db.add(t)
    db.flush()
    return t


def _make_closed_paper(
    db,
    *,
    ticker: str = "MSFT",
    direction: str = "long",
    entry_price: float = 200.0,
    exit_price: float = 198.0,
    quantity: float = 1.0,
) -> PaperTrade:
    now = datetime.utcnow()
    pt = PaperTrade(
        ticker=ticker,
        direction=direction,
        entry_price=entry_price,
        exit_price=exit_price,
        quantity=quantity,
        entry_date=now - timedelta(hours=3),
        exit_date=now,
        status="closed",
        pnl=(exit_price - entry_price) * quantity,
        exit_reason="stop",
    )
    db.add(pt)
    db.flush()
    return pt


def _seed_execution_event(
    db, *, trade: Trade, spread_bps: float = 4.2,
) -> TradingExecutionEvent:
    """One TradingExecutionEvent populated with a spread_bps measurement."""
    ev = TradingExecutionEvent(
        trade_id=int(trade.id),
        ticker=trade.ticker,
        venue="robinhood",
        execution_family="robinhood_equity",
        broker_source="robinhood",
        order_id=f"ord-{trade.id}",
        event_type="fill",
        status="filled",
        spread_bps=spread_bps,
        recorded_at=datetime.utcnow(),
        payload_json={},
    )
    db.add(ev)
    db.flush()
    return ev


def _seed_rolling_estimate(
    db,
    *,
    ticker: str,
    side: str = "long",
    window_days: int = 30,
    median_spread_bps: float = 3.0,
    p90_spread_bps: float = 7.0,
    median_slippage_bps: float = 2.5,
    p90_slippage_bps: float = 5.0,
    avg_daily_volume_usd: float = 1_000_000.0,
    sample_trades: int = 25,
) -> None:
    db.execute(
        text(
            """
            INSERT INTO trading_execution_cost_estimates (
                ticker, side, window_days,
                median_spread_bps, p90_spread_bps,
                median_slippage_bps, p90_slippage_bps,
                avg_daily_volume_usd, sample_trades, last_updated_at
            ) VALUES (
                :tkr, :side, :win,
                :ms, :p9s, :msl, :p9sl,
                :adv, :smp, NOW()
            )
            ON CONFLICT (ticker, side, window_days) DO UPDATE SET
                median_spread_bps = EXCLUDED.median_spread_bps,
                p90_spread_bps = EXCLUDED.p90_spread_bps,
                median_slippage_bps = EXCLUDED.median_slippage_bps,
                p90_slippage_bps = EXCLUDED.p90_slippage_bps,
                avg_daily_volume_usd = EXCLUDED.avg_daily_volume_usd,
                sample_trades = EXCLUDED.sample_trades,
                last_updated_at = NOW()
            """
        ),
        {
            "tkr": ticker, "side": side, "win": int(window_days),
            "ms": median_spread_bps, "p9s": p90_spread_bps,
            "msl": median_slippage_bps, "p9sl": p90_slippage_bps,
            "adv": avg_daily_volume_usd, "smp": int(sample_trades),
        },
    )
    db.flush()


def _venue_truth_rows(db, trade_id: int) -> list[dict]:
    rows = db.execute(
        text(
            """
            SELECT trade_id, ticker, side, notional_usd,
                   expected_spread_bps, realized_spread_bps,
                   expected_slippage_bps, realized_slippage_bps,
                   expected_cost_fraction, realized_cost_fraction,
                   paper_bool, mode
              FROM trading_venue_truth_log
             WHERE trade_id = :tid
            """
        ),
        {"tid": int(trade_id)},
    ).fetchall()
    keys = [
        "trade_id", "ticker", "side", "notional_usd",
        "expected_spread_bps", "realized_spread_bps",
        "expected_slippage_bps", "realized_slippage_bps",
        "expected_cost_fraction", "realized_cost_fraction",
        "paper_bool", "mode",
    ]
    return [dict(zip(keys, r)) for r in rows]


# ── tests ────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _shadow_venue_truth_mode(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "brain_venue_truth_mode", "shadow")


def test_live_close_writes_venue_truth_row(db):
    """Live trade close writes a row with expected+realized populated."""
    trade = _make_closed_trade(db)
    _seed_execution_event(db, trade=trade, spread_bps=4.2)
    _seed_rolling_estimate(db, ticker=trade.ticker, side="long")

    from app.services.trading.brain_work.execution_hooks import on_live_trade_closed
    on_live_trade_closed(db, trade, source="test")
    db.commit()

    rows = _venue_truth_rows(db, trade.id)
    assert len(rows) == 1, f"expected 1 venue_truth_log row, got {len(rows)}"
    row = rows[0]
    assert row["ticker"] == trade.ticker
    assert row["side"] == "long"
    assert row["paper_bool"] is False
    assert row["mode"] == "shadow"
    assert row["notional_usd"] == 100.0 * 10.0
    # realized fields populated
    assert row["realized_slippage_bps"] == 6.5
    assert row["realized_spread_bps"] == 4.2
    assert row["realized_cost_fraction"] is not None and row["realized_cost_fraction"] > 0
    # expected fields populated from rolling estimate (p90 path)
    assert row["expected_spread_bps"] == 7.0
    assert row["expected_slippage_bps"] == 5.0
    assert row["expected_cost_fraction"] is not None and row["expected_cost_fraction"] > 0


def test_paper_close_writes_venue_truth_row(db):
    """Paper trade close writes a paper-flagged row."""
    pt = _make_closed_paper(db, ticker="ZBRA", direction="long",
                            entry_price=50.0, exit_price=48.0, quantity=2.0)
    _seed_rolling_estimate(db, ticker=pt.ticker, side="long",
                           p90_spread_bps=11.0, p90_slippage_bps=4.0)

    from app.services.trading.brain_work.execution_hooks import on_paper_trade_closed
    on_paper_trade_closed(db, pt)
    db.commit()

    row = db.execute(
        text(
            """
            SELECT ticker, side, paper_bool, mode, notional_usd,
                   expected_spread_bps, realized_spread_bps,
                   realized_slippage_bps
              FROM trading_venue_truth_log
             WHERE ticker = :tkr
             ORDER BY id DESC LIMIT 1
            """
        ),
        {"tkr": pt.ticker},
    ).fetchone()
    assert row is not None, "expected at least one venue_truth_log row for paper close"
    assert row[0] == pt.ticker
    assert row[1] == "long"
    assert row[2] is True
    assert row[3] == "shadow"
    assert row[4] == 50.0 * 2.0
    # PaperTrade has no tca_entry_slippage_bps -> realized slippage is NULL.
    # PaperTrade also has no execution events -> realized spread is NULL.
    assert row[5] == 11.0  # expected_spread_bps from rolling estimate
    assert row[6] is None
    assert row[7] is None


def test_record_fill_observation_failure_does_not_block_hook(db):
    """A raise inside record_fill_observation must NOT bubble out of the hook,
    and the legacy emitter chain (emit_live_trade_closed_outcome) MUST still fire.
    """
    trade = _make_closed_trade(db, ticker="BLOCK", quantity=5.0)

    with patch(
        "app.services.trading.venue_truth.record_fill_observation",
        side_effect=RuntimeError("simulated venue_truth write failure"),
    ), patch(
        "app.services.trading.brain_work.execution_hooks.emit_live_trade_closed_outcome",
    ) as mock_emit:
        from app.services.trading.brain_work.execution_hooks import on_live_trade_closed
        # Must not raise.
        on_live_trade_closed(db, trade, source="test")
        db.commit()

    # Legacy emitter MUST have fired even though venue_truth raised.
    assert mock_emit.called, "emit_live_trade_closed_outcome must fire even when venue_truth fails"

    rows = _venue_truth_rows(db, trade.id)
    assert rows == [], "expected no venue_truth_log row when record_fill_observation raises"


def test_close_with_no_rolling_estimate_records_null_expected(db):
    """When the rolling estimate is absent, expected_* are NULL but realized_* populate."""
    trade = _make_closed_trade(db, ticker="NOPST", tca_entry_slippage_bps=8.0)
    _seed_execution_event(db, trade=trade, spread_bps=3.3)
    # Intentionally no _seed_rolling_estimate.

    from app.services.trading.brain_work.execution_hooks import on_live_trade_closed
    on_live_trade_closed(db, trade, source="test")
    db.commit()

    rows = _venue_truth_rows(db, trade.id)
    assert len(rows) == 1
    row = rows[0]
    assert row["expected_spread_bps"] is None
    assert row["expected_slippage_bps"] is None
    assert row["expected_cost_fraction"] is None
    assert row["realized_slippage_bps"] == 8.0
    assert row["realized_spread_bps"] == 3.3
    assert row["realized_cost_fraction"] is not None


def test_close_refreshes_rolling_estimate(db):
    """The close hook lazily refreshes trading_execution_cost_estimates."""
    # Seed three prior closed trades for the same (ticker, side) so the
    # rolling builder has a non-empty population.
    for _ in range(3):
        _make_closed_trade(
            db, ticker="RBLD", direction="long",
            entry_price=10.0, exit_price=10.2,
            quantity=4.0, tca_entry_slippage_bps=5.0,
        )

    # Fourth trade that triggers the hook.
    trade = _make_closed_trade(
        db, ticker="RBLD", direction="long",
        entry_price=10.0, exit_price=10.5,
        quantity=4.0, tca_entry_slippage_bps=4.5,
    )

    # Pre-condition: no rolling estimate exists yet.
    pre = db.execute(
        text(
            "SELECT COUNT(*) FROM trading_execution_cost_estimates "
            "WHERE ticker = :tkr AND side = :side AND window_days = 30"
        ),
        {"tkr": "RBLD", "side": "long"},
    ).scalar()
    assert pre == 0

    from app.services.trading.brain_work.execution_hooks import on_live_trade_closed
    on_live_trade_closed(db, trade, source="test")
    db.commit()

    row = db.execute(
        text(
            """
            SELECT sample_trades, p90_slippage_bps, last_updated_at
              FROM trading_execution_cost_estimates
             WHERE ticker = 'RBLD' AND side = 'long' AND window_days = 30
            """
        )
    ).fetchone()
    assert row is not None, "expected rolling estimate row after close hook"
    sample_trades, p90_slip, last_updated = row
    assert sample_trades >= 3
    assert p90_slip is not None and p90_slip > 0
    assert last_updated is not None
