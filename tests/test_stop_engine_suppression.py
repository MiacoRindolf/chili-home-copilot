from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from app.services.trading.stop_engine import (
    BrainContext,
    MarketContext,
    StopDecisionResult,
    StopState,
    _fetch_market_context,
    _result_has_trade_state_change,
    _should_suppress_alert,
    evaluate_trade,
)
from app.services.trading import market_data


ROOT = Path(__file__).resolve().parents[1]


def test_stop_hit_suppression_window_detects_recent_decision():
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    recent = {(2064, "STOP_HIT"): now_utc - timedelta(minutes=2)}

    assert _should_suppress_alert(2064, "STOP_HIT", recent)


def test_stop_hit_suppression_accepts_aware_recent_decision():
    now_utc = datetime.now(timezone.utc)
    recent = {(2064, "STOP_HIT"): now_utc - timedelta(minutes=2)}

    assert _should_suppress_alert(2064, "STOP_HIT", recent)


def test_fetch_market_context_normalizes_aware_quote_ts(monkeypatch):
    quote_time = datetime.now(timezone.utc)

    monkeypatch.setattr(
        market_data,
        "fetch_quote",
        lambda _ticker: {"price": 2.04, "bid": 2.03, "ask": 2.05, "quote_ts": quote_time},
    )
    monkeypatch.setattr(
        market_data,
        "get_indicator_snapshot",
        lambda _ticker, interval="1d": {"atr": {"value": 0.08}},
    )

    context = _fetch_market_context("DIEM-USD")

    assert context.is_stale is False
    assert context.quote_ts is not None
    assert context.quote_ts.tzinfo is None


def test_fetch_market_context_prefers_trade_broker_quote(monkeypatch):
    quote_time = datetime.now(timezone.utc)
    adapter = SimpleNamespace(
        is_enabled=lambda: True,
        get_ticker=lambda _ticker: (
            SimpleNamespace(
                bid=101.0,
                ask=102.0,
                mid=101.5,
                last_price=101.25,
                spread_bps=98.52,
                raw={},
            ),
            SimpleNamespace(
                retrieved_at_utc=quote_time,
                max_age_seconds=15.0,
                age_seconds=lambda: 0.1,
            ),
        ),
    )

    monkeypatch.setattr(
        "app.services.trading.venue.factory.get_adapter",
        lambda broker_source: adapter if broker_source == "coinbase" else None,
    )
    monkeypatch.setattr(
        market_data,
        "fetch_quote",
        lambda _ticker: (_ for _ in ()).throw(
            AssertionError("market_data fallback should not be used")
        ),
    )
    monkeypatch.setattr(
        market_data,
        "get_indicator_snapshot",
        lambda _ticker, interval="1d": {"atr": {"value": 3.0}},
    )

    context = _fetch_market_context(
        "BTC-USD",
        broker_source="coinbase",
        direction="long",
    )

    assert context.price == 101.0
    assert context.spread_bps == 98.52
    assert context.atr == 3.0


def test_evaluate_trade_handles_aware_entry_date_for_time_exit():
    exceeded_hold_hours = 5
    trade = SimpleNamespace(
        id=2101,
        ticker="DIEM-USD",
        direction="long",
        entry_price=2.0,
        entry_date=datetime.now(timezone.utc) - timedelta(hours=exceeded_hold_hours),
        stop_loss=1.8,
        take_profit=2.4,
        stop_model="atr_crypto_breakout",
        trade_type="scalp",
    )

    result = evaluate_trade(
        trade,
        MarketContext(price=2.05, atr=0.08, is_stale=False),
        brain=BrainContext(),
    )

    assert result.alert_event == "TIME_EXIT"


def test_evaluate_trade_catches_recent_broker_bar_target_touch():
    trade = SimpleNamespace(
        id=2065,
        ticker="ACMR",
        direction="long",
        entry_price=67.40,
        entry_date=datetime.now(timezone.utc),
        stop_loss=67.76,
        take_profit=81.52,
        stop_model="snapshot",
        trade_type=None,
        high_watermark=None,
        trail_stop=None,
    )

    result = evaluate_trade(
        trade,
        MarketContext(
            price=80.02,
            atr=4.71,
            recent_high=84.50,
            recent_high_ts=datetime(2026, 5, 26, 0, 10),
            range_source="robinhood_legend_blue_ocean",
            is_stale=False,
        ),
        brain=BrainContext(pattern_name="Falling Wedge Breakout"),
    )

    assert result.alert_event == "TARGET_HIT"
    assert result.inputs["trigger_basis"] == "recent_high"
    assert result.inputs["trigger_price"] == 84.50
    assert "current=$80.0200" in result.reason


def test_repeated_stop_hit_has_no_trade_state_change():
    trade = SimpleNamespace(
        ticker="ABTC",
        stop_loss=1.4148,
        trail_stop=None,
        high_watermark=None,
        take_profit=1.4433,
        related_alert_id=19001,
    )
    result = StopDecisionResult(
        trade_id=2064,
        state=StopState.TRIGGERED,
        old_stop=1.4148,
        new_stop=None,
        alert_event="STOP_HIT",
    )

    assert _result_has_trade_state_change(trade, result) is False


def test_suppression_is_checked_before_stop_decision_insert():
    text = (ROOT / "app/services/trading/stop_engine.py").read_text()
    body = text[text.index("def evaluate_all("):]

    assert body.index("result_suppressed = _should_suppress_alert") < body.index(
        "_record_stop_decision(db, trade.id, result)"
    )
