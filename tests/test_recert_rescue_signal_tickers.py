from __future__ import annotations

from types import SimpleNamespace


def test_recert_rescue_priority_tickers_prefer_blocked_signal():
    from app.services.trading.brain_work.handlers import profitability

    tickers = profitability._recert_rescue_priority_tickers(
        payload={
            "signal_ticker": "AAVE-USD",
            "priority_tickers": ["ETH-USD", "AAVE-USD"],
        },
        reliability={
            "primary_symbol": "BTC-USD",
            "tickers": {"SOL-USD": 3, "ETH-USD": 2},
        },
    )

    assert tickers == ["AAVE-USD", "ETH-USD", "BTC-USD", "SOL-USD"]


def test_backtest_request_priority_tickers_are_bounded_signal_first():
    from app.services.trading.backtest_queue_worker import (
        request_priority_tickers_from_payload,
    )

    tickers = request_priority_tickers_from_payload(
        {
            "signal_ticker": "AAVE-USD",
            "primary_symbol": "BTC-USD",
            "priority_tickers": ["ETH-USD", "SOL-USD"],
        },
        max_tickers=2,
    )

    assert tickers == ["AAVE-USD", "BTC-USD"]


def test_autotrader_recert_fastlane_emits_signal_ticker_rescue_work(monkeypatch):
    from app.services.trading import auto_trader
    from app.services.trading import recert_queue_service
    from app.services.trading import edge_reliability

    queued_scheduler: list[dict] = []
    queued_work: list[dict] = []

    monkeypatch.setattr(
        recert_queue_service,
        "queue_scheduler",
        lambda _db, **kwargs: queued_scheduler.append(kwargs)
        or SimpleNamespace(
            log_id=11,
            recert_id="recert-11",
            status="dispatched",
            mode="compare",
        ),
    )
    monkeypatch.setattr(
        edge_reliability,
        "emit_targeted_profitability_work",
        lambda _db, **kwargs: queued_work.append(kwargs) or 44,
    )

    alert = SimpleNamespace(
        id=7,
        scan_pattern_id=1260,
        ticker="AAVE-USD",
        asset_type="crypto",
    )
    pattern = SimpleNamespace(
        name="AAVE recert",
        recert_reason="missing_oos_recert",
        lifecycle_stage="pilot_promoted",
        promotion_status="pilot_collecting_ev",
    )

    result = auto_trader._queue_recert_for_blocked_signal(
        object(),
        alert=alert,
        pattern=pattern,
        reason="pattern_recert_required",
    )

    assert result["queued"] is True
    assert result["profitability_work_queued"] is True
    assert result["profitability_work_event_id"] == 44
    assert queued_scheduler[0]["payload"]["ticker"] == "AAVE-USD"
    assert queued_scheduler[0]["payload"]["asset_class"] == "crypto"
    work = queued_work[0]
    assert work["event_type"] == edge_reliability.RECERT_RESCUE_REFRESH
    assert work["asset_class"] == "crypto"
    assert work["payload"]["signal_ticker"] == "AAVE-USD"
    assert work["payload"]["priority_tickers"] == ["AAVE-USD"]


def test_recert_rescue_updates_open_backtest_request_with_signal_priority(db):
    from app.models.trading import BrainWorkEvent
    from app.services.trading.brain_work.handlers.profitability import (
        _recert_rescue_backtest_refresh,
    )
    from app.services.trading.brain_work.ledger import enqueue_work_event

    event_id = enqueue_work_event(
        db,
        event_type="backtest_requested",
        dedupe_key="test:open-recert-backtest-priority",
        payload={
            "scan_pattern_id": 1260,
            "source": "recert_rescue_refresh",
            "asset_class": "crypto",
            "priority_tickers": ["BTC-USD"],
        },
        lease_scope="backtest",
    )
    db.commit()

    result = _recert_rescue_backtest_refresh(
        db,
        scan_pattern_id=1260,
        reliability={
            "edge_eval_count": 6,
            "calibrated_ev_pct": 2.0,
            "slice_asset_class": "crypto",
            "evidence_fingerprint": "aave-fp",
            "primary_symbol": "AAVE-USD",
            "tickers": {"AAVE-USD": 9},
        },
        request_payload={"signal_ticker": "AAVE-USD"},
        hard_reasons=[],
        soft_reasons=["missing_oos_recert"],
        parent_event_id=0,
    )
    db.commit()

    row = db.get(BrainWorkEvent, event_id)
    assert result["requested"] is False
    assert result["event_id"] == event_id
    assert result["updated_open_request_priority_tickers"] is True
    assert row.payload["signal_ticker"] == "AAVE-USD"
    assert row.payload["priority_tickers"] == ["AAVE-USD", "BTC-USD"]
