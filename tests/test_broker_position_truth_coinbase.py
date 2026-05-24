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
