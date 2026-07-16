"""Replay v3 seeded-position exit validation through the unchanged live FSM.

The replay-only seed bypasses entry discovery and broker entry placement so a
recorded quote path can isolate production winner/loser management.  Every exit
order below still goes through ``tick_live_session`` and the deterministic mock
broker; no production broker or market-data transport is available to the tests.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.config import settings
from app.models.trading import TradingAutomationSession
from app.services.trading.momentum_neural import live_runner as lr
from app.services.trading.momentum_neural import market_profile as market_profile
from app.services.trading.momentum_neural import replay_v3 as rv3
from app.services.trading.momentum_neural.live_fsm import (
    STATE_LIVE_ENTERED,
    STATE_LIVE_EXITED,
    STATE_LIVE_TRAILING,
)
from app.services.trading.momentum_neural.replay_mock_broker import MockBrokerAdapter


_BASE = datetime(2026, 7, 13, 16, 0, 0)


@pytest.fixture
def _exit_runtime(monkeypatch):
    """Enable the real runner while making every external read fail loudly."""

    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_adaptive_hold_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_halt_print_recency_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_overnight_trading_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_scale_grid_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_scale_out_fraction", 0.5)
    monkeypatch.setattr(lr, "_venue_broker_connected", lambda family: True)
    monkeypatch.setattr(lr, "is_kill_switch_active", lambda: False)
    monkeypatch.setattr(lr, "_entry_pricebook_snapshot", lambda symbol: None)
    monkeypatch.setattr(lr, "_refetch_bbo_secondary", lambda symbol: None)
    monkeypatch.setattr(market_profile, "is_tradeable_now", lambda symbol, **kwargs: True)
    monkeypatch.setattr(market_profile, "is_data_session_now", lambda symbol: False)
    monkeypatch.setattr(market_profile, "market_session_now", lambda symbol: "regular")

    # The non-Alpaca broker-quantity fallback is an external Robinhood read.  A
    # replay seed has no real venue position, so return unknown and let the exact
    # seeded local quantity flow to the mock order (the production fail-open path).
    import app.services.broker_service as broker_service

    monkeypatch.setattr(broker_service, "get_open_position_quantity", lambda symbol: None)

    import app.services.trading.market_data as market_data

    def _network_forbidden(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("real market-data transport called during seeded exit replay")

    monkeypatch.setattr(market_data, "fetch_ohlcv_df", _network_forbidden)

    import app.services.massive_client as massive_client

    monkeypatch.setattr(massive_client, "get_full_market_snapshot", _network_forbidden)


def _seed_position(
    db,
    *,
    symbol: str,
    opened_at: datetime,
    entry: float = 10.0,
    stop: float = 8.0,
    target: float = 20.0,
    hwm: float = 10.0,
    quantity: float = 100.0,
    state: str = STATE_LIVE_TRAILING,
) -> rv3.ReplaySeed:
    eligibility_anchor = opened_at
    if eligibility_anchor.tzinfo is not None:
        eligibility_anchor = eligibility_anchor.astimezone(timezone.utc).replace(tzinfo=None)
    arm = rv3.RecordedArm(
        symbol=symbol,
        live_eligible_at_utc=(eligibility_anchor - timedelta(seconds=30)).isoformat()
        + "+00:00",
        viability_score=0.9,
        atr_pct=0.02,
    )
    seed = rv3.seed_replay_session(db, arm, execution_family="robinhood_spot")
    return rv3.seed_replay_position(
        db,
        seed,
        entry_price=entry,
        stop_price=stop,
        target_price=target,
        high_water_mark=hwm,
        quantity=quantity,
        opened_at=opened_at,
        state=state,
    )


def _run_quotes(
    db,
    seed: rv3.ReplaySeed,
    quotes: list[rv3.RecordedNbboTick],
) -> tuple[rv3.ReplayResult, MockBrokerAdapter, TradingAutomationSession]:
    mock = MockBrokerAdapter(
        slippage_bps=0.0,
        venue_rt_bps=0.0,
        freshness_mode="wall",
    )
    driver = rv3.ReplayV3Driver(
        db,
        seed,
        mock=mock,
        ohlcv_provider=rv3.RecordedOhlcvProvider({}),
        grid=quotes,
        risk_gate_allows=True,
    )
    result = driver.run()
    sess = db.get(TradingAutomationSession, seed.session_id)
    assert sess is not None
    return result, mock, sess


def test_seed_replay_position_preserves_exact_economics_without_broker_order(db):
    opened_at = datetime(2026, 7, 13, 11, 30, tzinfo=timezone(timedelta(hours=-4)))
    seed = _seed_position(
        db,
        symbol="SEEDX",
        opened_at=opened_at,
        entry=10.25,
        stop=9.75,
        target=11.25,
        hwm=10.80,
        quantity=137.0,
        state=STATE_LIVE_ENTERED,
    )
    sess = db.get(TradingAutomationSession, seed.session_id)
    assert sess is not None
    position = sess.risk_snapshot_json[lr.KEY_LIVE_EXEC]["position"]

    assert sess.state == STATE_LIVE_ENTERED
    assert position == {
        "product_id": "SEEDX",
        "side": "long",
        "quantity": 137.0,
        "original_quantity": 137.0,
        "avg_entry_price": 10.25,
        "notional_usd": 1404.25,
        "opened_at_utc": "2026-07-13T15:30:00",
        "high_water_mark": 10.80,
        "stop_price": 9.75,
        "target_price": 11.25,
    }
    # No adapter is accepted by the helper and no broker identity was created.
    mock = MockBrokerAdapter()
    assert mock.get_fills(limit=10)[0] == []
    assert mock.list_open_orders(limit=10)[0] == []

    with pytest.raises(ValueError, match="already owns a position"):
        rv3.seed_replay_position(
            db,
            seed,
            entry_price=20.0,
            stop_price=19.0,
            target_price=22.0,
            high_water_mark=21.0,
            quantity=10.0,
            opened_at=opened_at,
            state=STATE_LIVE_ENTERED,
        )
    # The refused overwrite leaves the original exact replay economics intact.
    assert sess.risk_snapshot_json[lr.KEY_LIVE_EXEC]["position"] == position


@pytest.mark.parametrize(
    ("bid", "expect_exit"),
    [
        (11.0, False),  # green at the hold limit: defer to structure
        (9.95, True),  # non-green at the hold limit: reap the stale position
    ],
    ids=["green_deferred", "non_green_exited"],
)
def test_seeded_exit_fsm_adaptive_max_hold_green_vs_non_green(
    db, monkeypatch, _exit_runtime, bid: float, expect_exit: bool
):
    # The seeded replay policy cap is 14,400 seconds.  Step one second beyond
    # it so only the adaptive green/non-green branch distinguishes the cases.
    opened_at = _BASE - timedelta(seconds=14_401)
    seed = _seed_position(
        db,
        symbol="HOLDG" if not expect_exit else "HOLDN",
        opened_at=opened_at,
        entry=10.0,
        stop=8.0,
        target=20.0,
        hwm=max(10.0, bid),
        quantity=100.0,
        state=STATE_LIVE_TRAILING,
    )
    result, mock, sess = _run_quotes(
        db,
        seed,
        [rv3.RecordedNbboTick(ts=_BASE, bid=bid, ask=bid + 0.02, last=bid + 0.01)],
    )
    fills, _ = mock.get_fills(limit=10)
    le = sess.risk_snapshot_json[lr.KEY_LIVE_EXEC]

    if expect_exit:
        assert sess.state == STATE_LIVE_EXITED
        assert len(fills) == 1 and fills[0].side == "sell"
        assert fills[0].price == pytest.approx(bid)
        assert le["position"] is None
        assert le["last_exit_reason"] == "max_hold"
        assert "max_hold_deferred_green" not in result.events
        assert "live_exit_filled" in result.events
    else:
        assert sess.state == STATE_LIVE_TRAILING
        assert fills == []
        assert le["position"]["quantity"] == pytest.approx(100.0)
        assert "max_hold_deferred_green" in result.events
        assert "live_exit_filled" not in result.events


def test_seeded_exit_fsm_target_scales_to_breakeven_runner(
    db, monkeypatch, _exit_runtime
):
    seed = _seed_position(
        db,
        symbol="RUNR",
        opened_at=_BASE - timedelta(seconds=60),
        entry=10.0,
        stop=9.0,
        target=12.0,
        hwm=11.5,
        quantity=100.0,
        state=STATE_LIVE_TRAILING,
    )
    result, mock, sess = _run_quotes(
        db,
        seed,
        [
            rv3.RecordedNbboTick(ts=_BASE, bid=12.0, ask=12.02, last=12.01),
            rv3.RecordedNbboTick(
                ts=_BASE + timedelta(seconds=1), bid=12.0, ask=12.02, last=12.01
            ),
        ],
    )
    fills, _ = mock.get_fills(limit=10)
    le = sess.risk_snapshot_json[lr.KEY_LIVE_EXEC]
    position = le["position"]

    assert sess.state == STATE_LIVE_TRAILING
    assert len(fills) == 1 and fills[0].side == "sell"
    assert fills[0].size == pytest.approx(50.0)
    assert fills[0].price == pytest.approx(12.0)
    assert position["original_quantity"] == pytest.approx(100.0)
    assert position["quantity"] == pytest.approx(50.0)
    assert position["partial_taken"] is True
    # Breakeven is a floor, not permission to loosen an already-tighter
    # chandelier stop.  The first target tick ratcheted this runner above entry;
    # scale-out must preserve that stronger protection.
    assert position["stop_price"] >= 10.0
    assert position["stop_price"] <= position["high_water_mark"]
    assert position["high_water_mark"] == pytest.approx(12.0)
    assert "live_partial_exit" in result.events
    assert "live_partial_exit_filled" in result.events
    assert "live_scaled_out_to_runner" in result.events
