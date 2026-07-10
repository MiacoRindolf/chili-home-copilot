from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import text

from app.models.trading import (
    MomentumAutomationOutcome,
    MomentumStrategyVariant,
    Trade,
    TradingAutomationEvent,
    TradingAutomationRuntimeSnapshot,
    TradingAutomationSession,
)


def _variant(db):
    row = MomentumStrategyVariant(
        family="ross_momentum",
        variant_key="live-observer-test",
        label="Live observer test",
        params_json={},
        is_active=True,
        execution_family="robinhood_spot",
    )
    db.add(row)
    db.flush()
    return row


def test_live_monitor_requires_paired_account(client):
    response = client.get("/api/trading/momentum/replay/live")
    assert response.status_code == 403


def test_live_monitor_returns_runtime_pnl_events_and_bounded_candles(paired_client, db):
    from app.services.trading.momentum_neural.live_monitor import clear_live_monitor_caches

    client, user = paired_client
    variant = _variant(db)
    now = datetime.utcnow().replace(second=0, microsecond=0)
    active = TradingAutomationSession(
        user_id=user.id,
        venue="robinhood",
        execution_family="robinhood_spot",
        mode="live",
        symbol="OBS",
        variant_id=variant.id,
        state="live_entered",
        risk_snapshot_json={
            "momentum_live_execution": {
                "position": {
                    "quantity": 10,
                    "avg_entry_price": 2.0,
                    "stop_price": 1.8,
                    "target_price": 2.6,
                },
                "last_mid": 2.2,
                "last_tick_utc": now.isoformat(),
                "realized_pnl_usd": 5.0,
            }
        },
        started_at=now - timedelta(minutes=15),
        updated_at=now,
    )
    completed = TradingAutomationSession(
        user_id=user.id,
        venue="robinhood",
        execution_family="robinhood_spot",
        mode="live",
        symbol="OBS",
        variant_id=variant.id,
        state="live_finished",
        risk_snapshot_json={},
        started_at=now - timedelta(minutes=40),
        ended_at=now - timedelta(minutes=10),
        updated_at=now - timedelta(minutes=10),
    )
    db.add_all([active, completed])
    db.flush()
    db.add(
        TradingAutomationRuntimeSnapshot(
            session_id=active.id,
            user_id=user.id,
            symbol="OBS",
            mode="live",
            lane="live",
            state="live_entered",
            strategy_family="ross_momentum",
            strategy_label="Ross momentum",
            current_position_state="live-long",
            last_action="live_entry_filled",
            last_price=2.2,
            latest_levels_json={"entry": 2.0, "stop": 1.8, "target": 2.6},
            updated_at=now,
        )
    )
    db.add(
        TradingAutomationEvent(
            session_id=active.id,
            ts=now - timedelta(minutes=2),
            event_type="live_entry_filled",
            payload_json={"reason": "breakout_confirmed"},
        )
    )
    db.add(
        MomentumAutomationOutcome(
            session_id=completed.id,
            user_id=user.id,
            variant_id=variant.id,
            symbol="OBS",
            mode="live",
            execution_family="robinhood_spot",
            terminal_state="live_finished",
            terminal_at=now - timedelta(minutes=10),
            outcome_class="target_hit",
            realized_pnl_usd=7.0,
        )
    )
    db.add(
        Trade(
            user_id=user.id,
            ticker="OBS",
            direction="long",
            entry_price=2.0,
            quantity=10,
            status="open",
            broker_source="robinhood",
        )
    )
    db.flush()
    for offset, bid, ask, volume in (
        (3, 1.99, 2.01, 1000),
        (2, 2.04, 2.06, 1120),
        (1, 2.14, 2.16, 1300),
        (0, 2.19, 2.21, 1450),
    ):
        db.execute(
            text(
                "INSERT INTO momentum_nbbo_spread_tape "
                "(symbol, observed_at, bid, ask, mid, day_volume, source) "
                "VALUES ('OBS', :ts, :bid, :ask, (:bid + :ask) / 2.0, :volume, 'test')"
            ),
            {"ts": now - timedelta(minutes=offset), "bid": bid, "ask": ask, "volume": volume},
        )
    db.commit()
    clear_live_monitor_caches()

    response = client.get("/api/trading/momentum/replay/live")
    assert response.status_code == 200
    assert response.headers["x-chili-live-observer"] == "read-only"
    body = response.json()
    assert body["read_only"] is True
    assert body["observer"]["broker_calls"] == 0
    assert body["observer"]["provider_calls"] == 0
    assert body["observer"]["writes"] == 0
    assert body["observer"]["quote_row_cap_per_symbol"] == 480
    assert body["totals"] == {
        "realized_usd": 12.0,
        "unrealized_usd": 2.0,
        "trades": 1,
        "total_usd": 14.0,
    }
    symbol = next(row for row in body["symbols"] if row["symbol"] == "OBS")
    assert symbol["state"] == "live_entered"
    assert symbol["armed"] is False
    assert symbol["positions"][0]["quantity"] == 10.0
    assert symbol["pnl"]["realized_usd"] == 12.0
    assert symbol["pnl"]["unrealized_usd"] == 2.0
    assert symbol["events"][0]["stage"] == "live_entry_filled"
    assert body["series"]["OBS"][-1][4] == 2.2


def test_live_monitor_snapshot_is_single_flight_cached(monkeypatch):
    from app.services.trading.momentum_neural import live_monitor as monitor

    monitor.clear_live_monitor_caches()
    calls = {"state": 0, "chart": 0}

    def fake_state(_db, *, user_id, now_utc):
        calls["state"] += 1
        return {
            "ok": True,
            "read_only": True,
            "symbols": [{"symbol": "CACHE"}],
            "observer": {},
        }

    def fake_chart(_db, *, user_id, symbols, now_utc, now_mono):
        calls["chart"] += 1
        return {"CACHE": []}, "2026-07-10T00:00:00Z"

    monkeypatch.setattr(monitor, "_build_state_snapshot", fake_state)
    monkeypatch.setattr(monitor, "_cached_chart_series", fake_chart)
    first = monitor.live_monitor_snapshot(object(), user_id=71)
    second = monitor.live_monitor_snapshot(object(), user_id=71)
    assert first is second
    assert calls == {"state": 1, "chart": 1}


def test_minute_bar_series_builds_ohlc_without_external_market_data():
    from app.services.trading.momentum_neural.live_monitor import _minute_bar_series

    at = datetime(2026, 7, 10, 14, 31, 5)
    rows = [
        ("OBS", at, 9.9, 10.1, 10.0, 100.0),
        ("OBS", at + timedelta(seconds=20), 10.4, 10.6, 10.5, 140.0),
        ("OBS", at + timedelta(seconds=40), 9.7, 9.9, 9.8, 170.0),
    ]
    bars = _minute_bar_series(rows)
    assert bars["OBS"] == [["14:31", 10.0, 10.5, 9.8, 9.8, 70.0]]
