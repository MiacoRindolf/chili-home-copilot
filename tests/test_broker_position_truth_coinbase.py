from datetime import datetime
from types import SimpleNamespace


def test_coinbase_open_trade_truth_filter_fails_open_without_position_identity() -> None:
    from app.services.trading.broker_position_truth import broker_stale_open_trade_snapshot

    trade = SimpleNamespace(
        status="open",
        broker_source="coinbase",
        ticker="KMNO-USD",
        position_id=None,
        user_id=1,
        direction="long",
    )

    assert broker_stale_open_trade_snapshot(None, trade) is None


def test_robinhood_option_truth_filter_fails_open_without_spot_identity() -> None:
    from app.services.trading.broker_position_truth import broker_stale_open_trade_snapshot

    trade = SimpleNamespace(
        status="open",
        broker_source="robinhood",
        ticker="SPY",
        position_id=None,
        user_id=1,
        direction="long",
        entry_date=datetime(2026, 1, 1),
        indicator_snapshot={
            "breakout_alert": {
                "asset_type": "options",
                "option_meta": {
                    "underlying": "SPY",
                    "expiration": "2026-06-19",
                    "strike": 729.0,
                    "option_type": "call",
                },
            }
        },
    )

    assert (
        broker_stale_open_trade_snapshot(
            None,
            trade,
            grace_seconds=0,
            now=datetime(2026, 1, 2),
        )
        is None
    )
