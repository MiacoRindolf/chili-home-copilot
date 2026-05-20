from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from app.services.trading.stop_engine import (
    StopDecisionResult,
    StopState,
    _result_has_trade_state_change,
    _should_suppress_alert,
)


ROOT = Path(__file__).resolve().parents[1]


def test_stop_hit_suppression_window_detects_recent_decision():
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    recent = {(2064, "STOP_HIT"): now_utc - timedelta(minutes=2)}

    assert _should_suppress_alert(2064, "STOP_HIT", recent)


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
